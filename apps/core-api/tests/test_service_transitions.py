"""MVP 서비스 연결·전환 — AWS 대역과 PostgreSQL로 facade·AWS 모듈의 보장 경계를 굳힌다.

실 AWS·실 HTTP 대상은 없다. 대역은 둘이다.
  - World: ec2·elbv2·asg·sts 상태형 대역. 표에 없는 작업은 AttributeError로 끊고, 모든 호출에서
    hook(DB 트랜잭션이 열려 있지 않은가)을 부른다. after_call로 "명령 도중의 외부 변화"를 흉내 낸다.
  - FakeHttp: 탐침 출발지(IMDS)와 GET. ALB DNS면 그 포트 리스너의 TG에서 healthy 대상 하나가,
    사설 IP면 그 인스턴스가 답한다. 본문은 스모크 _USER_DATA처럼 인스턴스 Name 그대로다.

AWS-only 테스트는 DB 없이 돈다. facade 테스트는 PostgreSQL이 없으면 skip된다(conftest pg_engine).
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from schemas.autoscaling import (
    SCALING_POLICY_NAME,
    AutoscalingExecutionPlan,
    autoscaling_image_name,
)
from schemas.service_transition import (
    TRANSITION_TAG_KEY,
    CleanupItem,
    OriginalRoute,
    ProbeClient,
    RouteMember,
    ServiceTransitionPlan,
    transition_target_group_name,
)
from tests.audit_cleanup import purge_audit_events

REGION, ACCOUNT = "ap-northeast-2", "123456789012"
VPC, SUBNET = "vpc-0aaaaaaaaaaaaaaa1", "subnet-0aaaaaaa1"
SOURCE = "i-0000000000000000a"  # idle-dev — 복제 원본, 원래 TG 대상이 아니다
WEB1, WEB2, PROBE, MEMBER = (
    "i-0000000000000000b",
    "i-0000000000000000c",
    "i-0000000000000000d",
    "i-0000000000000000e",
)
SG_ALB, SG_WEB, SG_PROBE, SG_ISO = (
    "sg-0000000000000000a",
    "sg-0000000000000000b",
    "sg-0000000000000000c",
    "sg-0000000000000000d",
)
LB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/smoke-alb/0123456789abcdef"
LISTENER_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/smoke-alb/0123456789abcdef/1111111111111111"
ORIGINAL_TG = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/vigilantis-smoke-tg/2222222222222222"
ALB_DNS = "internal-smoke-alb-1.ap-northeast-2.elb.amazonaws.com"
ALB_IPS = ("10.42.1.100", "10.42.2.100")
IPS = {
    SOURCE: "10.42.1.10",
    WEB1: "10.42.1.11",
    WEB2: "10.42.2.12",
    PROBE: "10.42.1.13",
    MEMBER: "10.42.1.14",
}
ALB_ZONES = ("ap-northeast-2a", "ap-northeast-2c")
ZONES = {WEB2: "ap-northeast-2c"}  # 나머지는 ap-northeast-2a
TYPES = {
    SOURCE: "t3.medium",
    MEMBER: "t3.medium",
    WEB1: "t3.micro",
    WEB2: "t3.micro",
    PROBE: "t3.micro",
}
# 스모크 _USER_DATA: echo "{name}" > /srv/www/index.html — 복제본은 원본(idle-dev) 디스크를 그대로 갖는다
BODY = {
    SOURCE: "vigilantis-smoke-idle-dev\n",
    MEMBER: "vigilantis-smoke-idle-dev\n",
    WEB1: "vigilantis-smoke-web-1\n",
    WEB2: "vigilantis-smoke-web-2\n",
}
LT_ID, AMI, SNAP, VOL = (
    "lt-0aaaaaaaaaaaaaaa1",
    "ami-0aaaaaaaaaaaaaaa1",
    "snap-0aaaaaaaaaaaaaaa1",
    "vol-0aaaaaaaaaaaaaaa1",
)
ORPHAN = "snap-0aaaaaaaaaaaaaaa2"
MUTATIONS = {
    "create_target_group",
    "create_listener",
    "modify_listener",
    "delete_listener",
    "delete_target_group",
    "attach_load_balancer_target_groups",
    "detach_load_balancer_target_groups",
    "delete_auto_scaling_group",
    "delete_volume",
    "delete_launch_template",
    "deregister_image",
    "delete_snapshot",
}
DELETE_RETENTION = {"retention_decision": "delete_execution_resources"}
SERVICE_ONLY = {"retention_decision": "retain_execution_infrastructure"}


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status, "RequestId": "r"},
        },
        "Op",
    )


def autoscaling_plan(execution_id: str) -> AutoscalingExecutionPlan:
    return AutoscalingExecutionPlan.model_validate(
        {
            "target_arn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{SOURCE}",
            "execution_id": execution_id,
            "min_size": 1,
            "max_size": 2,
            "desired_capacity": 1,
            "image_name": autoscaling_image_name(SOURCE, execution_id),
            "launch_template_name": f"vigilantis-lt-{SOURCE}",
            "launch_template_version": 1,
            "asg_name": f"vigilantis-asg-{SOURCE}",
            "scaling_policy_name": SCALING_POLICY_NAME,
            "cpu_target_percent": 50,
            "default_instance_warmup_seconds": 300,
            "source": {
                "instance_id": SOURCE,
                "instance_type": "t3.medium",
                "subnet_id": SUBNET,
                "security_group_ids": [SG_WEB],
                "iam_instance_profile_arn": None,
                "ebs_optimized": False,
                "detailed_monitoring": False,
                "dedicated_tenancy": False,
                "associate_public_ip_address": False,
                "metadata_options": {
                    "http_tokens": "required",
                    "http_endpoint": "enabled",
                    "http_put_response_hop_limit": 2,
                    "http_protocol_ipv6": None,
                    "instance_metadata_tags": None,
                },
                "preserved_tags": [{"key": "vigilantis:smoke", "value": "true"}],
            },
        }
    )


def _tcp(port, *, cidr=None, groups=()):
    return {
        "IpProtocol": "tcp",
        "FromPort": port,
        "ToPort": port,
        "IpRanges": [{"CidrIp": cidr}] if cidr else [],
        "UserIdGroupPairs": [{"GroupId": g} for g in groups],
    }


class World:
    """스모크 VPC 한 장면: idle-dev(원본)·web-1/2(원래 TG)·탐침·#446 ASG 멤버 1대."""

    def __init__(self, execution_id: str) -> None:
        self.as_plan = autoscaling_plan(execution_id)
        self.execution_id = execution_id
        tags = [{"Key": t.key, "Value": t.value} for t in self.as_plan.resource_tags()]
        self.exec_tags = tags

        def instance(iid, groups, extra_tags=()):
            return {
                "InstanceId": iid,
                "State": {"Name": "running"},
                "VpcId": VPC,
                "SubnetId": SUBNET,
                "InstanceType": TYPES[iid],
                "PrivateIpAddress": IPS[iid],
                "ImageId": "ami-0bbbbbbbbbbbbbbb1",
                "Placement": {"AvailabilityZone": ZONES.get(iid, ALB_ZONES[0])},
                "Tags": list(extra_tags),
                "NetworkInterfaces": [
                    {
                        "NetworkInterfaceId": f"eni-{iid[2:]}",
                        "Attachment": {"DeviceIndex": 0},
                        "Groups": [{"GroupId": g} for g in groups],
                    }
                ],
                "SecurityGroups": [{"GroupId": g} for g in groups],
            }

        self.instances = {
            SOURCE: instance(SOURCE, [SG_WEB]),
            WEB1: instance(WEB1, [SG_WEB]),
            WEB2: instance(WEB2, [SG_WEB]),
            PROBE: instance(PROBE, [SG_PROBE]),
            MEMBER: instance(MEMBER, [SG_WEB], tags),
        }
        self.instances[MEMBER]["ImageId"] = AMI
        self.sgs = {
            SG_ALB: {
                "GroupId": SG_ALB,
                "Tags": [],
                "IpPermissions": [
                    _tcp(80, cidr="10.42.0.0/16"),
                    _tcp(8080, groups=[SG_PROBE]),
                ],
            },
            SG_WEB: {
                "GroupId": SG_WEB,
                "Tags": [],
                "IpPermissions": [_tcp(80, groups=[SG_ALB, SG_PROBE])],
            },
            SG_PROBE: {"GroupId": SG_PROBE, "Tags": [], "IpPermissions": []},
            SG_ISO: {
                "GroupId": SG_ISO,
                "Tags": [{"Key": "vigilantis:role", "Value": "isolation"}],
                "IpPermissions": [],
            },
        }
        self.load_balancer = {
            "LoadBalancerArn": LB_ARN,
            "DNSName": ALB_DNS,
            "Scheme": "internal",
            "Type": "application",
            "VpcId": VPC,
            "State": {"Code": "active"},
            "SecurityGroups": [SG_ALB],
            "AvailabilityZones": [{"ZoneName": z} for z in ALB_ZONES],
        }
        self.target_groups = {ORIGINAL_TG: self._tg(ORIGINAL_TG, "vigilantis-smoke-tg")}
        self.targets = {ORIGINAL_TG: {WEB1: "healthy", WEB2: "healthy"}}
        self.elb_tags: dict[str, list] = {}
        self.listeners = {
            LISTENER_ARN: {
                "ListenerArn": LISTENER_ARN,
                "LoadBalancerArn": LB_ARN,
                "Port": 80,
                "Protocol": "HTTP",
                "DefaultActions": [{"Type": "forward", "TargetGroupArn": ORIGINAL_TG}],
            }
        }
        self.rules: dict[str, list] = {}
        self.templates = {
            LT_ID: {
                "LaunchTemplateId": LT_ID,
                "LaunchTemplateName": self.as_plan.launch_template_name,
                "Tags": tags,
            }
        }
        self.template_versions: dict[tuple[str, str], dict] = {}
        self.launch_configurations: dict[str, dict] = {}
        self.group = {
            "AutoScalingGroupName": self.as_plan.asg_name,
            "MinSize": 1,
            "MaxSize": 2,
            "DesiredCapacity": 1,
            "VPCZoneIdentifier": SUBNET,
            "LaunchTemplate": {"LaunchTemplateId": LT_ID, "Version": "1"},
            "DefaultInstanceWarmup": 300,
            "Tags": [{**t, "PropagateAtLaunch": False} for t in tags],
            "Instances": [
                {
                    "InstanceId": MEMBER,
                    "LifecycleState": "InService",
                    "HealthStatus": "Healthy",
                    "ProtectedFromScaleIn": False,
                    "LaunchTemplate": {"LaunchTemplateId": LT_ID, "Version": "1"},
                }
            ],
            "TargetGroupARNs": [],
            "LoadBalancerNames": [],
        }
        self.other_groups: list[dict] = []
        self.images = {
            AMI: {
                "ImageId": AMI,
                "Name": self.as_plan.image_name,
                "OwnerId": ACCOUNT,
                "State": "available",
                "Tags": tags,
                "BlockDeviceMappings": [{"Ebs": {"SnapshotId": SNAP}}],
            }
        }
        self.snapshots = {SNAP: {"SnapshotId": SNAP, "Tags": tags}}
        self.volumes = {VOL: {"VolumeId": VOL, "State": "in-use", "Tags": tags}}
        self.snapshot_page_size = 50
        self.version_page_size = 50
        self.calls: list[tuple[str, dict]] = []
        self.failures: dict[str, BaseException] = {}
        self.apply_before_failure: set[str] = set()
        self.after_call: dict[str, object] = {}
        self.after_alb_request = None
        self.hook = lambda: None
        self.serial = 0
        self._refresh_load_balancer_refs()

    @staticmethod
    def _tg(arn, name):
        return {
            "TargetGroupArn": arn,
            "TargetGroupName": name,
            "VpcId": VPC,
            "Protocol": "HTTP",
            "Port": 80,
            "TargetType": "instance",
            "HealthCheckPath": "/",
            "Matcher": {"HttpCode": "200"},
            "LoadBalancerArns": [],
        }

    def _refresh_load_balancer_refs(self):
        used = {
            a["TargetGroupArn"]
            for l in self.listeners.values()
            for a in l["DefaultActions"]
        }
        for arn, group in self.target_groups.items():
            group["LoadBalancerArns"] = [LB_ARN] if arn in used else []

    def clients(self) -> dict:
        return {
            name: FakeClient(self, name) for name in ("ec2", "elbv2", "asg", "sts")
        } | {"http": FakeHttp(self)}

    def mutations(self, name=None):
        return [
            (op, kw)
            for op, kw in self.calls
            if op in MUTATIONS and (name is None or op == name)
        ]

    def finish_draining(self):
        for tg in self.targets:
            self.targets[tg] = {
                i: s for i, s in self.targets[tg].items() if s != "draining"
            }

    def finish_termination(self):
        self.group = None
        for item in self.instances.values():
            if item["State"]["Name"] == "shutting-down":
                item["State"]["Name"] = "terminated"
        self.volumes.clear()

    def isolate(self, instance_id):
        """현재 ENI를 격리 SG로 — EC2_ISOLATE가 남기는 AWS 현물."""
        self.instances[instance_id]["NetworkInterfaces"][0]["Groups"] = [
            {"GroupId": SG_ISO}
        ]
        self.instances[instance_id]["SecurityGroups"] = [{"GroupId": SG_ISO}]

    def smoke_down(self):
        """scripts/provision_smoke_aws.py down이 남기는 현물. 앱 ASG(부착 TG에서 멤버를 빼고 종료)·
        공유 ALB(리스너 포함)·원래 TG·VPC 인스턴스·vigilantis-lt-* 시작 템플릿을 지운다. 이름이 다른
        전환 TG와 AMI·스냅숏은 남는다(_DOWN_IMAGE_WARNING). SG·서브넷은 이 경로가 읽지 않아 둔다."""
        self.group = None
        self.load_balancer = None
        self.listeners.clear()
        self.rules.clear()
        self.target_groups.pop(ORIGINAL_TG)
        self.targets = {arn: {} for arn in self.targets if arn != ORIGINAL_TG}
        for item in self.instances.values():
            item["State"]["Name"] = "terminated"
        self.templates.clear()
        self.volumes.clear()
        self._refresh_load_balancer_refs()

    # ---------------- sts
    def sts_get_caller_identity(self):
        return {"Account": ACCOUNT}

    # ---------------- ec2
    def ec2_describe_instances(self, InstanceIds=None, Filters=None):
        if InstanceIds is not None:
            if any(i not in self.instances for i in InstanceIds):
                raise client_error("InvalidInstanceID.NotFound")
            found = [self.instances[i] for i in InstanceIds]
        else:
            found = list(self.instances.values())
        for item in Filters or ():
            name, values = item["Name"], item["Values"]
            if name.startswith("tag:"):
                found = [
                    i
                    for i in found
                    if any(
                        t["Key"] == name[4:] and t["Value"] in values for t in i["Tags"]
                    )
                ]
            elif name == "image-id":
                found = [i for i in found if i["ImageId"] in values]
            elif name == "instance-state-name":
                found = [i for i in found if i["State"]["Name"] in values]
        return {"Reservations": [{"Instances": found}]}

    def ec2_describe_security_groups(self, GroupIds):
        return {"SecurityGroups": [self.sgs[g] for g in GroupIds]}

    def ec2_describe_network_interfaces(self, Filters):
        description = "ELB app/smoke-alb/0123456789abcdef"
        if Filters[0]["Values"] != [description]:
            return {"NetworkInterfaces": []}
        return {
            "NetworkInterfaces": [
                {
                    "Description": description,
                    "PrivateIpAddresses": [{"PrivateIpAddress": ip}],
                }
                for ip in ALB_IPS
            ]
        }

    def ec2_describe_launch_templates(self, Filters=None, LaunchTemplateIds=None):
        if LaunchTemplateIds is not None:
            if not all(i in self.templates for i in LaunchTemplateIds):
                raise client_error("InvalidLaunchTemplateId.NotFound")
            return {"LaunchTemplates": [self.templates[i] for i in LaunchTemplateIds]}
        if Filters is None:
            return {"LaunchTemplates": list(self.templates.values())}
        name = Filters[0]["Values"][0]
        return {
            "LaunchTemplates": [
                t for t in self.templates.values() if t["LaunchTemplateName"] == name
            ]
        }

    def ec2_describe_launch_template_versions(
        self,
        Versions=None,
        LaunchTemplateId=None,
        LaunchTemplateName=None,
        NextToken=None,
    ):
        owner = LaunchTemplateId or LaunchTemplateName
        if Versions is None:
            if LaunchTemplateId is not None and LaunchTemplateId not in self.templates:
                raise client_error("InvalidLaunchTemplateId.NotFound")
            numbered = sorted(
                (int(v), data)
                for (o, v), data in self.template_versions.items()
                if o == owner and v.isdigit()
            )
            start = int(NextToken or 0)
            end = start + self.version_page_size
            return {
                "LaunchTemplateVersions": [
                    {
                        "LaunchTemplateId": owner,
                        "VersionNumber": n,
                        "LaunchTemplateData": data,
                    }
                    for n, data in numbered[start:end]
                ],
                **({"NextToken": str(end)} if end < len(numbered) else {}),
            }
        key = (owner, Versions[0])
        if key not in self.template_versions:
            raise client_error("InvalidLaunchTemplateId.VersionNotFound")
        return {
            "LaunchTemplateVersions": [
                {"LaunchTemplateData": self.template_versions[key]}
            ]
        }

    def ec2_describe_images(
        self, Owners=None, IncludeDisabled=None, Filters=None, ImageIds=None
    ):
        """실제 EC2처럼 — 없는 ImageIds는 InvalidAMIID.NotFound, 비활성(disabled) AMI는
        IncludeDisabled 없이는 응답에서 빠진다."""
        images = list(self.images.values())
        if ImageIds is not None:
            if any(i not in self.images for i in ImageIds):
                raise client_error("InvalidAMIID.NotFound")
            images = [self.images[i] for i in ImageIds]
        if not IncludeDisabled:
            images = [i for i in images if i.get("State") != "disabled"]
        for item in Filters or ():
            if item["Name"] == "name":
                images = [i for i in images if i["Name"] in item["Values"]]
            elif item["Name"] == "block-device-mapping.snapshot-id":
                images = [
                    i
                    for i in images
                    if {m["Ebs"]["SnapshotId"] for m in i["BlockDeviceMappings"]}
                    & set(item["Values"])
                ]
        return {"Images": images}

    def ec2_describe_snapshots(
        self, OwnerIds=None, Filters=None, SnapshotIds=None, NextToken=None
    ):
        snapshots = list(self.snapshots.values())
        if SnapshotIds is not None:
            if not all(i in self.snapshots for i in SnapshotIds):
                raise client_error("InvalidSnapshot.NotFound")
            snapshots = [self.snapshots[i] for i in SnapshotIds]
        for item in Filters or ():
            key = item["Name"][4:]
            snapshots = [
                s
                for s in snapshots
                if any(
                    t["Key"] == key and t["Value"] in item["Values"] for t in s["Tags"]
                )
            ]
        start = int(NextToken or 0)
        page = snapshots[start : start + self.snapshot_page_size]
        more = start + self.snapshot_page_size < len(snapshots)
        return {
            "Snapshots": page,
            **({"NextToken": str(start + self.snapshot_page_size)} if more else {}),
        }

    def ec2_describe_volumes(self, Filters=None, VolumeIds=None):
        if VolumeIds is not None:
            if not all(v in self.volumes for v in VolumeIds):
                raise client_error("InvalidVolume.NotFound")
            return {"Volumes": [self.volumes[v] for v in VolumeIds]}
        key = Filters[0]["Name"][4:]
        return {
            "Volumes": [
                v
                for v in self.volumes.values()
                if any(
                    t["Key"] == key and t["Value"] in Filters[0]["Values"]
                    for t in v["Tags"]
                )
            ]
        }

    def ec2_modify_network_interface_attribute(
        self, NetworkInterfaceId, Groups, DryRun=False
    ):
        if not DryRun:
            raise AssertionError("이 대역은 UNISOLATE precheck의 DryRun만 받습니다")
        raise client_error("DryRunOperation", 412)

    def ec2_delete_volume(self, VolumeId):
        self.volumes.pop(VolumeId)

    def ec2_delete_launch_template(self, LaunchTemplateId):
        self.templates.pop(LaunchTemplateId)

    def ec2_deregister_image(self, ImageId):
        self.images.pop(ImageId)

    def ec2_delete_snapshot(self, SnapshotId):
        self.snapshots.pop(SnapshotId)

    # ---------------- elbv2
    def _require_load_balancer(self, *arns):
        """없는 ALB를 지목하면 실제 ELBv2처럼 LoadBalancerNotFound다(빈 목록이 아니다)."""
        if self.load_balancer is None or any(a != LB_ARN for a in arns):
            raise client_error("LoadBalancerNotFound")

    def elbv2_describe_load_balancers(self, LoadBalancerArns):
        self._require_load_balancer(*LoadBalancerArns)
        return {"LoadBalancers": [self.load_balancer]}

    def elbv2_describe_listeners(self, ListenerArns=None, LoadBalancerArn=None):
        if ListenerArns is not None:
            if not all(a in self.listeners for a in ListenerArns):
                raise client_error("ListenerNotFound")
            return {"Listeners": [self.listeners[a] for a in ListenerArns]}
        self._require_load_balancer(LoadBalancerArn)
        return {
            "Listeners": [
                l
                for l in self.listeners.values()
                if l["LoadBalancerArn"] == LoadBalancerArn
            ]
        }

    def elbv2_describe_rules(self, ListenerArn):
        return {"Rules": [{"IsDefault": True}, *self.rules.get(ListenerArn, [])]}

    def elbv2_describe_target_groups(self, Names=None, TargetGroupArns=None):
        if Names is not None:
            found = [
                g for g in self.target_groups.values() if g["TargetGroupName"] in Names
            ]
        else:
            found = [
                self.target_groups[a]
                for a in TargetGroupArns
                if a in self.target_groups
            ]
        if not found:
            raise client_error("TargetGroupNotFound")
        return {"TargetGroups": found}

    def elbv2_describe_target_health(self, TargetGroupArn):
        """실제 ALB처럼 — 없는 TG는 TargetGroupNotFound, 리스너가 쓰지 않는 TG나 ALB에 꺼진 AZ의
        대상은 unused(Target.NotInUse), running이 아닌 인스턴스는 unused(Target.InvalidState)다.
        draining은 그대로 둔다."""
        if TargetGroupArn not in self.target_groups:
            raise client_error("TargetGroupNotFound")
        in_use = bool(self.target_groups[TargetGroupArn]["LoadBalancerArns"])
        zones = {
            z["ZoneName"]
            for z in (self.load_balancer or {}).get("AvailabilityZones", ())
        }
        descriptions = []
        for instance_id, state in self.targets.get(TargetGroupArn, {}).items():
            health = {"State": state}
            instance = self.instances.get(instance_id)
            if state != "draining" and instance is not None:
                if instance["State"]["Name"] != "running":
                    health = {"State": "unused", "Reason": "Target.InvalidState"}
                elif (
                    not in_use or instance["Placement"]["AvailabilityZone"] not in zones
                ):
                    health = {"State": "unused", "Reason": "Target.NotInUse"}
            descriptions.append(
                {"Target": {"Id": instance_id, "Port": 80}, "TargetHealth": health}
            )
        return {"TargetHealthDescriptions": descriptions}

    def elbv2_describe_tags(self, ResourceArns):
        return {
            "TagDescriptions": [
                {"ResourceArn": a, "Tags": self.elb_tags.get(a, [])}
                for a in ResourceArns
            ]
        }

    def elbv2_create_target_group(self, Name, Tags, **_):
        self.serial += 1
        arn = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{Name}/{self.serial:016x}"
        self.target_groups[arn] = self._tg(arn, Name)
        self.targets[arn] = {}
        self.elb_tags[arn] = Tags
        return {"TargetGroups": [self.target_groups[arn]]}

    def elbv2_create_listener(
        self, LoadBalancerArn, Protocol, Port, DefaultActions, Tags
    ):
        self.serial += 1
        arn = f"{LISTENER_ARN.rsplit('/', 1)[0]}/{self.serial:016x}"
        self.listeners[arn] = {
            "ListenerArn": arn,
            "LoadBalancerArn": LoadBalancerArn,
            "Port": Port,
            "Protocol": Protocol,
            "DefaultActions": DefaultActions,
        }
        self.elb_tags[arn] = Tags
        self._refresh_load_balancer_refs()
        return {"Listeners": [self.listeners[arn]]}

    def elbv2_modify_listener(self, ListenerArn, DefaultActions):
        self.listeners[ListenerArn]["DefaultActions"] = DefaultActions
        self._refresh_load_balancer_refs()
        return {"Listeners": [self.listeners[ListenerArn]]}

    def elbv2_delete_listener(self, ListenerArn):
        self.listeners.pop(ListenerArn)
        self._refresh_load_balancer_refs()

    def elbv2_delete_target_group(self, TargetGroupArn):
        self.target_groups.pop(TargetGroupArn)

    # ---------------- autoscaling
    def asg_describe_auto_scaling_groups(self, AutoScalingGroupNames=None):
        groups = ([self.group] if self.group else []) + self.other_groups
        if AutoScalingGroupNames is not None:
            groups = [
                g for g in groups if g["AutoScalingGroupName"] in AutoScalingGroupNames
            ]
        return {"AutoScalingGroups": groups}

    def asg_describe_launch_configurations(self, LaunchConfigurationNames):
        return {
            "LaunchConfigurations": [
                self.launch_configurations[n]
                for n in LaunchConfigurationNames
                if n in self.launch_configurations
            ]
        }

    def asg_attach_load_balancer_target_groups(
        self, AutoScalingGroupName, TargetGroupARNs
    ):
        self.group["TargetGroupARNs"] += TargetGroupARNs
        for arn in TargetGroupARNs:
            self.targets[arn] = {
                m["InstanceId"]: "healthy" for m in self.group["Instances"]
            }

    def asg_detach_load_balancer_target_groups(
        self, AutoScalingGroupName, TargetGroupARNs
    ):
        self.group["TargetGroupARNs"] = [
            a for a in self.group["TargetGroupARNs"] if a not in TargetGroupARNs
        ]
        for arn in TargetGroupARNs:
            self.targets[arn] = {i: "draining" for i in self.targets[arn]}

    def asg_delete_auto_scaling_group(self, AutoScalingGroupName, ForceDelete):
        self.group["Status"] = "Delete in progress"
        for member in self.group["Instances"]:
            self.instances[member["InstanceId"]]["State"]["Name"] = "shutting-down"


class FakeClient:
    def __init__(self, world: World, service: str) -> None:
        self._world, self._service = world, service
        self.meta = SimpleNamespace(region_name=REGION)

    def __getattr__(self, operation):
        handler = getattr(self._world, f"{self._service}_{operation}")

        def call(**kwargs):
            self._world.hook()
            self._world.calls.append((operation, kwargs))
            failure = self._world.failures.pop(operation, None)
            if failure is not None:
                if operation in self._world.apply_before_failure:
                    handler(**kwargs)
                raise failure
            result = handler(**kwargs) or {}
            after = self._world.after_call.pop(operation, None)
            if after:
                after()
            return result

        return call


class FakeHttp:
    def __init__(self, world: World) -> None:
        self.world, self.origin, self.bodies, self.calls = world, PROBE, dict(BODY), []
        self.alb_status: int | None = (
            None  # 운영 ALB가 아직 대상 준비 전(예: 503)인 상황
        )

    def origin_instance_id(self):
        self.world.hook()
        return self.origin

    def get(self, *, host, port, path, headers, timeout, max_body_bytes):
        self.world.hook()
        self.calls.append((host, port, path, dict(headers)))
        if host == ALB_DNS:
            if self.alb_status is not None:
                return {
                    "status": self.alb_status,
                    "headers": {},
                    "body": b"",
                    "peer_ip": ALB_IPS[0],
                }
            listener = next(
                l for l in self.world.listeners.values() if l["Port"] == port
            )
            group = listener["DefaultActions"][0]["TargetGroupArn"]
            health = self.world.elbv2_describe_target_health(TargetGroupArn=group)[
                "TargetHealthDescriptions"
            ]
            target = next(
                d["Target"]["Id"]
                for d in health
                if d["TargetHealth"]["State"] == "healthy"
            )
            if self.world.after_alb_request:
                self.world.after_alb_request()
            return {
                "status": 200,
                "headers": {},
                "body": self.bodies[target].encode(),
                "peer_ip": ALB_IPS[0],
            }
        target = next(
            i
            for i, item in self.world.instances.items()
            if item["PrivateIpAddress"] == host
        )
        return {
            "status": 200,
            "headers": {},
            "body": self.bodies[target].encode(),
            "peer_ip": host,
        }


def plan_options(**overrides) -> dict:
    data = {
        "account_id": ACCOUNT,
        "region": REGION,
        "vpc_id": VPC,
        "load_balancer_arn": LB_ARN,
        "listener_arn": LISTENER_ARN,
        "validation_listener_port": 8080,
        "probe_instance_id": PROBE,
        "probe_security_group_id": SG_PROBE,
        "clone_expected_body": BODY[SOURCE],
        "original_target_expected_bodies": {WEB1: BODY[WEB1], WEB2: BODY[WEB2]},
        "runtime_facts": {
            "captured_instance_type_hourly_usd": "0.05",
            "simultaneous_cost_cap_usd_per_hour": "0.10",
            "vcpu_quota": 8,
            "rto_minutes": 30,
        },
    }
    data.update(overrides)
    return data


def direct_plan(world: World) -> ServiceTransitionPlan:
    """DB 없이 AWS 모듈만 볼 때 쓰는 계획 — facade plan과 같은 모양이다."""
    transition_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    return ServiceTransitionPlan(
        transition_id=transition_id,
        execution_id=world.execution_id,
        planned_at=now,
        account_id=ACCOUNT,
        region=REGION,
        vpc_id=VPC,
        service_unit="smoke-http.service",
        service_port=80,
        service_path="/",
        expected_status=200,
        clone_expected_body=BODY[SOURCE],
        load_balancer_arn=LB_ARN,
        original_route=OriginalRoute(
            listener_arn=LISTENER_ARN,
            listener_port=80,
            listener_protocol="HTTP",
            target_group_arn=ORIGINAL_TG,
            members=tuple(
                RouteMember(
                    instance_id=i,
                    port=80,
                    health_state="healthy",
                    quarantined=False,
                    expected_body=BODY[i],
                )
                for i in (WEB1, WEB2)
            ),
            captured_at=now,
        ),
        validation_listener_port=8080,
        target_group_name=transition_target_group_name(transition_id),
        probe=ProbeClient(
            instance_id=PROBE, security_group_id=SG_PROBE, private_ip=IPS[PROBE]
        ),
        runtime_facts={},
    )


def prepared(world: World):
    """AWS 모듈의 변경 함수로 TG·검증 리스너·부착을 세운 장면."""
    from services.aws import service_transition as aws

    c = aws.clients_from(world.clients())
    plan = direct_plan(world)
    tg = aws.perform(aws.create_target_group(c, plan, world.as_plan)).response[
        "target_group_arn"
    ]
    aws.perform(aws.create_validation_listener(c, plan, world.as_plan, tg))
    aws.perform(aws.attach_target_group(c, world.as_plan, tg))
    return c, plan, tg


# ============================================================ AWS 모듈 (DB 불필요)
@pytest.mark.parametrize(
    ("actions", "ok"),
    [
        ([{"Type": "forward", "TargetGroupArn": ORIGINAL_TG}], True),
        (
            [
                {
                    "Type": "forward",
                    "ForwardConfig": {
                        "TargetGroups": [{"TargetGroupArn": ORIGINAL_TG, "Weight": 1}]
                    },
                }
            ],
            True,
        ),
        (
            [
                {
                    "Type": "forward",
                    "ForwardConfig": {
                        "TargetGroups": [
                            {"TargetGroupArn": ORIGINAL_TG},
                            {"TargetGroupArn": "x"},
                        ]
                    },
                }
            ],
            False,
        ),
        (
            [
                {
                    "Type": "forward",
                    "ForwardConfig": {
                        "TargetGroups": [{"TargetGroupArn": ORIGINAL_TG}],
                        "TargetGroupStickinessConfig": {"Enabled": True},
                    },
                }
            ],
            False,
        ),
        ([{"Type": "redirect", "RedirectConfig": {}}], False),
        (
            [
                {"Type": "forward", "TargetGroupArn": ORIGINAL_TG},
                {"Type": "forward", "TargetGroupArn": "y"},
            ],
            False,
        ),
    ],
)
def test_only_single_forward_default_actions_are_supported(actions, ok):
    from services.aws import service_transition as aws

    target, problem = aws.forward_target(actions)
    assert (
        (target == ORIGINAL_TG and problem is None)
        if ok
        else (target is None and problem)
    )


@pytest.mark.parametrize(
    ("permissions", "ok"),
    [
        ([_tcp(8080, groups=[SG_PROBE])], True),
        ([], False),
        ([_tcp(8080, cidr="10.42.0.0/16")], False),
        ([_tcp(8080, groups=[SG_PROBE, SG_WEB])], False),
        (
            [
                _tcp(8080, groups=[SG_PROBE]),
                {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": SG_WEB}]},
            ],
            False,
        ),
    ],
)
def test_validation_port_must_be_reachable_only_from_the_probe_group(permissions, ok):
    from services.aws import service_transition as aws

    assert (
        aws.validation_port_restriction(
            [{"IpPermissions": permissions}], 8080, SG_PROBE
        ).passed
        is ok
    )


@pytest.mark.parametrize(
    ("failure", "state"),
    [
        (client_error("ValidationError", 400), "NOT_APPLIED"),
        (client_error("Throttling", 400), "NOT_APPLIED"),
        (client_error("InternalFailure", 500), "UNCERTAIN"),
        (
            EndpointConnectionError(endpoint_url="https://elasticloadbalancing"),
            "UNCERTAIN",
        ),
    ],
)
def test_perform_classifies_once_and_never_resends(failure, state):
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    world.failures["create_target_group"] = failure
    outcome = aws.perform(aws.create_target_group(c, direct_plan(world), world.as_plan))
    assert outcome.state == state and outcome.error_code
    assert len(world.mutations("create_target_group")) == 1


def test_created_resources_carry_execution_and_transition_ownership():
    world = World(str(uuid.uuid4()))
    _, plan, tg = prepared(world)
    tags = {t["Key"]: t["Value"] for t in world.elb_tags[tg]}
    assert (
        tags[TRANSITION_TAG_KEY] == plan.transition_id
        and tags["vigilantis:execution-id"] == world.execution_id
    )
    assert tags["vigilantis:smoke"] == "true"
    ((_, kwargs),) = world.mutations("create_target_group")
    assert (kwargs["Port"], kwargs["HealthCheckPath"], kwargs["Matcher"]) == (
        80,
        "/",
        {"HttpCode": "200"},
    )


def test_service_evidence_separates_alb_route_from_member_responses():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, tg = prepared(world)
    inspection = aws.inspect_service(c, plan, world.as_plan)
    assert all(item.passed for item in inspection.checks), [
        i for i in inspection.checks if not i.passed
    ]
    assert [h.path_kind for h in inspection.http] == [
        "alb_validation_listener",
        "member_direct",
    ]
    alb, member = inspection.http
    assert (
        alb.passed and alb.peer_ip in ALB_IPS and alb.instance_id is None
    )  # 대상 귀속을 주장하지 않는다
    assert (
        member.passed and member.instance_id == MEMBER and member.peer_ip == IPS[MEMBER]
    )
    assert {r.target_group_arn for r in inspection.routes} == {tg}
    assert (
        world.listeners[LISTENER_ARN]["DefaultActions"][0]["TargetGroupArn"]
        == ORIGINAL_TG
    )
    assert SOURCE not in world.targets[ORIGINAL_TG] and SOURCE not in world.targets[tg]


def test_route_change_during_the_request_fails_the_evidence():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, tg = prepared(world)
    world.after_alb_request = lambda: world.targets[tg].update({WEB1: "healthy"})
    inspection = aws.inspect_service(c, plan, world.as_plan)
    assert not next(
        i for i in inspection.checks if i.name == "route_stable_during_request"
    ).passed


@pytest.mark.parametrize(
    "spoil",
    [
        lambda w, h: setattr(h, "origin", WEB1),  # 지정 탐침이 아닌 곳에서 돈다
        lambda w, h: w.sgs[SG_ALB]["IpPermissions"].append(
            _tcp(8080, cidr="0.0.0.0/0")
        ),
        lambda w, h: w.instances[MEMBER]["NetworkInterfaces"][0]["Groups"].append(
            {"GroupId": SG_ISO}
        ),
        lambda w, h: w.targets[next(a for a in w.targets if a != ORIGINAL_TG)].update(
            {SOURCE: "healthy"}
        ),
    ],
)
def test_structural_failures_produce_no_http_evidence(spoil):
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, _ = prepared(world)
    spoil(world, c.http)
    inspection = aws.inspect_service(c, plan, world.as_plan)
    assert not all(item.passed for item in inspection.checks)
    assert inspection.http == [] and c.http.calls == []


def test_wrong_member_content_fails_even_with_a_healthy_target_group():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, _ = prepared(world)
    c.http.bodies[MEMBER] = "something else\n"
    inspection = aws.inspect_service(c, plan, world.as_plan)
    assert all(item.passed for item in inspection.checks)
    assert not any(h.passed for h in inspection.http)


def test_redirect_and_unexpected_peer_are_not_success():
    from services.aws import service_transition as aws

    class Redirecting:
        def get(self, **_):
            return {
                "status": 302,
                "headers": {"Location": "http://elsewhere/"},
                "body": b"",
                "peer_ip": "10.0.0.1",
            }

    evidence = aws.http_probe(
        Redirecting(),
        path_kind="member_direct",
        host="10.0.0.1",
        port=80,
        peers=("10.0.0.1",),
        expected_bodies=("x",),
    )
    assert not evidence.passed and evidence.location == "http://elsewhere/"

    class Proxy:
        def get(self, **_):
            return {"status": 200, "headers": {}, "body": b"x", "peer_ip": "192.0.2.1"}

    assert not aws.http_probe(
        Proxy(),
        path_kind="member_direct",
        host="10.0.0.1",
        port=80,
        peers=("10.0.0.1",),
        expected_bodies=("x",),
    ).passed

    class Broken:
        def get(self, **_):
            raise TimeoutError("timed out")

    broken = aws.http_probe(
        Broken(),
        path_kind="member_direct",
        host="10.0.0.1",
        port=80,
        peers=("10.0.0.1",),
        expected_bodies=("x",),
    )
    assert not broken.passed and "TimeoutError" in broken.error


def test_direct_probe_does_not_follow_redirects_and_reports_the_peer():
    from services.aws import service_transition as aws

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.2/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        evidence = aws.http_probe(
            aws.DirectHttpProbe(),
            path_kind="member_direct",
            host="127.0.0.1",
            port=server.server_address[1],
            peers=("127.0.0.1",),
            expected_bodies=("x",),
        )
    finally:
        server.shutdown()
    assert (
        evidence.status == 302
        and evidence.peer_ip == "127.0.0.1"
        and not evidence.passed
    )


def test_inventory_reads_every_snapshot_page_and_orphans_after_deregistration():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, _ = prepared(world)
    world.snapshot_page_size = 1
    world.snapshots[ORPHAN] = {"SnapshotId": ORPHAN, "Tags": world.exec_tags}
    world.images.clear()  # AMI는 이미 등록 해제됐다 — 매핑으로는 스냅숏을 찾을 수 없다
    inventory = aws.cleanup_inventory(
        c, world.as_plan, [plan], retention_decision="delete_execution_resources"
    )
    assert [
        i.resource_id for i in inventory.manifest.items if i.kind == "snapshot"
    ] == [SNAP, ORPHAN]
    service = {i.kind: i.transition_id for i in inventory.manifest.items}
    assert (
        service["validation_listener"] == service["target_group"] == plan.transition_id
    )
    assert service["snapshot"] is None and service["auto_scaling_group"] is None
    retained = aws.cleanup_inventory(
        c, world.as_plan, [plan], retention_decision="retain_launch_image"
    )
    assert not [
        i
        for i in retained.manifest.items
        if i.kind in ("snapshot", "launch_template", "image")
    ]
    assert {r["id"] for r in retained.retained} >= {SNAP, ORPHAN, LT_ID}
    infra = aws.cleanup_inventory(
        c, world.as_plan, [plan], retention_decision="retain_execution_infrastructure"
    )
    assert {i.kind for i in infra.manifest.items} == {
        "validation_listener",
        "target_group_attachment",
        "target_group",
    }
    assert not infra.destroys_instances
    assert {r["id"] for r in infra.retained} >= {
        world.as_plan.asg_name,
        MEMBER,
        VOL,
        LT_ID,
        SNAP,
    }
    assert all(
        "계속 과금" in r["reason"] for r in infra.retained if r["id"] in (MEMBER, SNAP)
    )
    undecided = aws.cleanup_inventory(c, world.as_plan, [plan], retention_decision=None)
    assert any("retention_decision" in b for b in undecided.blockers)


def test_inventory_blocks_foreign_refs_and_active_route():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, tg = prepared(world)
    world.listeners[LISTENER_ARN]["DefaultActions"] = [
        {"Type": "forward", "TargetGroupArn": tg}
    ]
    world.group["TargetGroupARNs"].append(ORIGINAL_TG)
    inventory = aws.cleanup_inventory(
        c, world.as_plan, [plan], retention_decision="delete_execution_resources"
    )
    assert any("서비스 중" in b for b in inventory.blockers)
    assert any("부착" in b for b in inventory.blockers)
    ids = {i.resource_id for i in inventory.manifest.items}
    assert not ids & {LB_ARN, LISTENER_ARN, ORIGINAL_TG, SOURCE, WEB1, WEB2}


@pytest.mark.parametrize("transition_tg_left", [True, False])
def test_inventory_after_smoke_down_reads_deleted_shared_resources_as_absent(
    transition_tg_left,
):
    """down이 공유 ALB(리스너 포함)·원래 TG를 지운 뒤에도 재고가 돈다.

    지워진 공유 자원은 '없음'으로 읽어 보존 목록에만 남고, 정리 대상은 남은 실행 자원뿐이다.
    """
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, tg = prepared(world)
    world.smoke_down()
    if not transition_tg_left:
        world.target_groups.pop(tg)
        world.targets.pop(tg)
    inventory = aws.cleanup_inventory(
        c, world.as_plan, [plan], retention_decision="delete_execution_resources"
    )
    remaining = [("target_group", tg)] if transition_tg_left else []
    assert [(i.kind, i.resource_id) for i in inventory.manifest.items] == [
        *remaining,
        ("image", AMI),
        ("snapshot", SNAP),
    ]
    assert inventory.blockers == [] and inventory.unknown == []
    assert inventory.counts["original_route_members"] == 0
    shared = {LB_ARN, LISTENER_ARN, ORIGINAL_TG}
    assert shared <= {r["id"] for r in inventory.retained}
    assert not shared & set(inventory.manifest.resource_ids())
    assert (
        aws.observe_resource(
            c, plan, "validation_listener", f"{LB_ARN}:8080", {"port": 8080}
        )
        is False
    )


def _fail_nth(world, handler_name, nth, error, when=lambda kwargs: True):
    """대역 작업에서 when에 맞는 n번째 호출만 error로 끊는다 — 앞선 호출은 그대로 답한다."""
    real, seen = getattr(world, handler_name), []

    def handler(**kwargs):
        if when(kwargs):
            seen.append(kwargs)
            if len(seen) == nth:
                raise error
        return real(**kwargs)

    setattr(world, handler_name, handler)


def _load_balancer_scoped(kwargs):
    return kwargs.get("LoadBalancerArn") is not None


ABSENCE_READS = [
    pytest.param(
        "elbv2_describe_listeners",
        1,
        _load_balancer_scoped,
        "LoadBalancerNotFound",
        lambda aws, c, plan, world: aws._listener_on_port(
            c.elbv2, LB_ARN, plan.validation_listener_port
        ),
        lambda listener: listener is None,
        id="validation_listener_on_port",
    ),
    pytest.param(
        "elbv2_describe_listeners",
        2,  # 재고의 두 번째 ALB 리스너 나열 = 전환 TG를 참조하는 다른 리스너 검사
        _load_balancer_scoped,
        "LoadBalancerNotFound",
        lambda aws, c, plan, world: aws.cleanup_inventory(
            c, world.as_plan, [plan], retention_decision="delete_execution_resources"
        ),
        lambda inventory: inventory.blockers == [],
        id="inventory_listener_scan",
    ),
    pytest.param(
        "elbv2_describe_target_health",
        1,
        lambda kwargs: True,
        "TargetGroupNotFound",
        lambda aws, c, plan, world: aws._target_states(c.elbv2, ORIGINAL_TG),
        lambda targets: targets == (),
        id="original_target_health",
    ),
    pytest.param(
        "ec2_describe_images",
        1,
        lambda kwargs: True,
        "InvalidAMIID.NotFound",
        lambda aws, c, plan, world: aws.observe_resource(
            c, plan, "image", AMI, {"image_id": AMI}
        ),
        lambda present: present is False,
        id="image_reconciliation",
    ),
]


@pytest.mark.parametrize(
    ("code", "status"),
    [(None, 400), ("AccessDenied", 403), ("Throttling", 400), ("InternalFailure", 500)],
)
@pytest.mark.parametrize(
    ("handler", "nth", "when", "not_found", "read", "absent"), ABSENCE_READS
)
def test_only_a_confirmed_not_found_reads_as_absent(
    handler, nth, when, not_found, read, absent, code, status
):
    """확정된 NotFound만 '없음'이다. 권한 거부·스로틀링·5xx는 부재가 아니라 판정 불가로 올라간다."""
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, _ = prepared(world)
    _fail_nth(world, handler, nth, client_error(code or not_found, status), when)
    if code is None:
        assert absent(read(aws, c, plan, world))
        return
    with pytest.raises(ClientError) as raised:
        read(aws, c, plan, world)
    assert raised.value.response["Error"]["Code"] == code


UNTAGGED_SNAP = "snap-0aaaaaaaaaaaaaaa3"


@pytest.mark.parametrize(
    ("state", "registered"),
    [("available", True), ("disabled", True), (None, True), ("deregistered", False)],
)
def test_only_a_confirmed_deregistered_image_leaves_inventory_and_snapshot_references(
    state, registered
):
    """State=deregistered로 확정된 AMI만 정리 대상·스냅숏 사용자에서 빠진다(다시 해제하지 않고 스냅숏
    삭제를 막지 않는다). 비활성·사용 가능·상태 없음은 등록된 AMI로 남아 보호한다. 해제된 소유 AMI의
    매핑도 스냅숏 출처로 계속 읽는다 — 태그 없는 매핑 스냅숏이 조용히 사라지지 않는다."""
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, plan, _ = prepared(world)
    world.snapshots[UNTAGGED_SNAP] = {"SnapshotId": UNTAGGED_SNAP, "Tags": []}
    world.images[AMI]["BlockDeviceMappings"].append(
        {"Ebs": {"SnapshotId": UNTAGGED_SNAP}}
    )
    world.images[FOREIGN_AMI] = {
        "ImageId": FOREIGN_AMI,
        "Name": "someone-else",
        "OwnerId": ACCOUNT,
        "Tags": [],
        "BlockDeviceMappings": [{"Ebs": {"SnapshotId": SNAP}}],
    }
    for image_id in (AMI, FOREIGN_AMI):
        world.images[image_id]["State"] = state

    inventory = aws.cleanup_inventory(
        c, world.as_plan, [plan], retention_decision="delete_execution_resources"
    )
    items = {(i.kind, i.resource_id) for i in inventory.manifest.items}
    assert (("image", AMI) in items) is registered
    assert ("snapshot", SNAP) in items
    assert {
        "kind": "snapshot",
        "id": UNTAGGED_SNAP,
        "reason": "소유 태그 불일치(AMI 매핑)",
    } in inventory.unknown
    assert any(FOREIGN_AMI in b for b in inventory.blockers) is registered
    refs = aws.shared_resource_refs(c, _item("delete_snapshot", SNAP), world.as_plan)
    users = {i for i in (AMI, FOREIGN_AMI) if any(i in r for r in refs)}
    assert users == ({AMI, FOREIGN_AMI} if registered else set()), refs


OTHER_LT = "lt-0bbbbbbbbbbbbbbb1"


@pytest.mark.parametrize(
    ("other", "setup", "fragment"),
    [
        (
            {"LaunchTemplate": {"LaunchTemplateId": OTHER_LT, "Version": "$Default"}},
            lambda w: w.template_versions.update(
                {(OTHER_LT, "$Default"): {"ImageId": AMI}}
            ),
            "다른 시작 템플릿",
        ),
        (
            {
                "MixedInstancesPolicy": {
                    "LaunchTemplate": {
                        "LaunchTemplateSpecification": {
                            "LaunchTemplateId": OTHER_LT,
                            "Version": "$Latest",
                        },
                        "Overrides": [
                            {
                                "LaunchTemplateSpecification": {
                                    "LaunchTemplateName": "other-override",
                                    "Version": "3",
                                }
                            }
                        ],
                    }
                }
            },
            lambda w: w.template_versions.update(
                {
                    (OTHER_LT, "$Latest"): {"ImageId": "ami-0ccccccccccccccc1"},
                    ("other-override", "3"): {"ImageId": AMI},
                }
            ),
            "다른 시작 템플릿",
        ),
        (
            {"LaunchConfigurationName": "legacy"},
            lambda w: w.launch_configurations.update({"legacy": {"ImageId": AMI}}),
            "시작 구성",
        ),
        (
            {"LaunchTemplate": {"LaunchTemplateId": OTHER_LT, "Version": "$Default"}},
            lambda w: None,
            "확정할 수 없습니다",
        ),
        (
            {"LaunchTemplate": {"LaunchTemplateId": OTHER_LT, "Version": "$Default"}},
            lambda w: w.template_versions.update(
                {(OTHER_LT, "$Default"): {"ImageId": "resolve:ssm:/x"}}
            ),
            "확정할 수 없습니다",
        ),
        (
            {"LaunchTemplate": {"LaunchTemplateId": LT_ID, "Version": "1"}},
            lambda w: None,
            "시작 템플릿을 씁니다",
        ),
    ],
)
def test_shared_image_through_another_asg_blocks_image_deletion(other, setup, fragment):
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    world.other_groups.append(
        {"AutoScalingGroupName": "someone-else", "Instances": [], **other}
    )
    setup(world)
    inventory = aws.cleanup_inventory(
        c, world.as_plan, [], retention_decision="delete_execution_resources"
    )
    assert any(fragment in b for b in inventory.blockers), inventory.blockers
    kept = aws.cleanup_inventory(
        c, world.as_plan, [], retention_decision="retain_launch_image"
    )
    assert not any(
        fragment in b for b in kept.blockers
    )  # 보존하면 AMI를 지우지 않으니 막을 이유가 없다


LT_NAME = f"vigilantis-lt-{SOURCE}"
OTHER_AMI = "ami-0ccccccccccccccc1"


@pytest.mark.parametrize(
    "other",
    [
        {"LaunchTemplate": {"LaunchTemplateName": LT_NAME, "Version": "$Latest"}},
        {
            "MixedInstancesPolicy": {
                "LaunchTemplate": {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": OTHER_LT,
                        "Version": "1",
                    },
                    "Overrides": [
                        {
                            "LaunchTemplateSpecification": {
                                "LaunchTemplateName": LT_NAME,
                                "Version": "$Latest",
                            }
                        }
                    ],
                }
            }
        },
    ],
)
def test_owned_launch_template_shared_by_name_blocks_deletion_without_the_image(other):
    """소유 AMI는 이미 없고, 남의 ASG가 이 실행의 시작 템플릿을 **이름**으로 쓴다($Latest는 다른 유효 AMI)."""
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    world.images.clear()
    world.template_versions.update(
        {
            (LT_NAME, "$Latest"): {"ImageId": OTHER_AMI},
            (OTHER_LT, "1"): {"ImageId": OTHER_AMI},
        }
    )
    world.other_groups.append(
        {"AutoScalingGroupName": "someone-else", "Instances": [], **other}
    )
    inventory = aws.cleanup_inventory(
        c, world.as_plan, [], retention_decision="delete_execution_resources"
    )
    assert LT_ID in inventory.manifest.resource_ids()
    assert any("이 실행의 시작 템플릿을 씁니다" in b for b in inventory.blockers), (
        inventory.blockers
    )


def test_cleanup_gate_keeps_launch_image_until_asg_termination_is_observed():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c, _, _ = prepared(world)
    item = CleanupItem(
        order=8, kind="image", resource_id=AMI, action="deregister_image"
    )
    assert "종료 확인 전" in aws.cleanup_gate(c, item, world.as_plan)
    world.instances[MEMBER]["State"]["Name"] = "terminated"
    assert "삭제 확인 전" in aws.cleanup_gate(c, item, world.as_plan)
    world.group = None
    assert aws.cleanup_gate(c, item, world.as_plan) is None


@pytest.mark.parametrize(
    "hold",
    [
        lambda w: w.group["Instances"][0].update({"ProtectedFromScaleIn": True}),
        lambda w: w.group["Instances"][0].pop("ProtectedFromScaleIn"),  # 모름은 보류다
        lambda w: w.isolate(MEMBER),
    ],
)
def test_security_holds_cover_members_and_unknown_protection(hold):
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    assert aws.security_holds(c, world.as_plan) == ([MEMBER], {})
    hold(world)
    ids, holds = aws.security_holds(c, world.as_plan)
    assert ids == [MEMBER] and holds[MEMBER]


# ============================================================ facade (PostgreSQL)
def test_schema_needs_no_db_or_aws():
    import service_transitions as facade

    result = facade.run_service_command(None, command="schema", clients=None)
    json.dumps(result)
    assert result["success"] is True
    assert set(result["commands"]) == set(facade.COMMANDS)
    plan_schema = result["commands"]["plan"]["options_schema"]
    assert {
        "clone_expected_body",
        "original_target_expected_bodies",
        "replaces_transition_id",
    } <= set(plan_schema["properties"])
    assert (
        result["commands"]["cleanup"]["mutating"]
        and not result["commands"]["verify"]["mutating"]
    )
    assert result["clients"]["required"] == ["ec2", "elbv2", "asg", "sts"]


def test_inputs_are_validated_before_db_or_aws():
    import service_transitions as facade

    invalid = facade.run_service_command(
        object(), command="plan", clients={}, options={"apply": True}
    )
    assert invalid["success"] is False and invalid["error"] == "invalid_options"
    missing = facade.run_service_command(
        object(), command="status", clients={"ec2": object()}
    )
    assert missing["error"] == "missing_clients" and "elbv2" in missing["detail"]
    unknown = facade.run_service_command(object(), command="down", clients={})
    assert unknown["error"] == "unknown_command"


@pytest.fixture()
def scene(db, make_incident):
    """#446 실행 1건 + World. AWS 대역 호출마다 DB 트랜잭션이 닫혀 있는지 본다."""
    from db import models
    from schemas.api.actions import ExecutionStatus
    from schemas.runbooks import RunbookId, TriggerSource

    def build(*, status=ExecutionStatus.SUCCESS):
        incident = make_incident(
            db, subject_arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{SOURCE}"
        )
        execution_id = str(uuid.uuid4())
        world = World(execution_id)
        db.add(
            models.ActionExecution(
                execution_id=execution_id,
                incident_id=incident.incident_id,
                runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
                target_arn=world.as_plan.target_arn,
                trigger_source=TriggerSource.USER_APPROVAL,
                status=status,
                autoscaling_plan=world.as_plan.model_dump(mode="json"),
            )
        )
        db.commit()

        def no_open_transaction():
            assert not db.in_transaction(), (
                "AWS·HTTP 호출 중 DB 트랜잭션이 열려 있습니다"
            )

        world.hook = no_open_transaction
        # 감시가 헛돌지 않는지 — 읽기 트랜잭션이 열린 상태에서는 실제로 걸린다
        db.get(models.ActionExecution, execution_id)
        with pytest.raises(AssertionError):
            world.hook()
        db.commit()
        return world

    return build


def run(db, world, command, clients=None, **kwargs):
    import service_transitions as facade

    result = facade.run_service_command(
        db, command=command, clients=clients or world.clients(), **kwargs
    )
    json.dumps(result)
    return result


def _planned(db, world, **overrides):
    result = run(
        db,
        world,
        "plan",
        execution_id=world.execution_id,
        options=plan_options(**overrides),
    )
    assert result["success"], result
    return result["transition_id"]


def _cleanup_round(db, world, options, **ids):
    inventory = run(db, world, "cleanup-plan", options=options, **ids)
    assert inventory["success"] and inventory["cleanup_allowed"], inventory
    return inventory, run(
        db,
        world,
        "cleanup",
        options={
            **options,
            "manifest_sha256": inventory["manifest_sha256"],
            "resource_ids": inventory["resource_ids"],
        },
        **ids,
    )


def _seed_isolation(db, make_incident, instance_id):
    from db import models
    from schemas.api.actions import ExecutionStatus
    from schemas.runbooks import RunbookId, TriggerSource

    incident = make_incident(db)
    db.add(
        models.ActionExecution(
            incident_id=incident.incident_id,
            runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
            target_arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance_id}",
            trigger_source=TriggerSource.USER_APPROVAL,
            status=ExecutionStatus.SUCCESS,
            scale_in_protection_result={
                "phase": "PROTECT",
                "outcome": "UNKNOWN",
                "reason": "WRITE_UNCERTAIN",
                "asg_name": f"vigilantis-asg-{SOURCE}",
                "instance_id": instance_id,
                "write_may_have_begun": True,
                "observed_protected": None,
                "residual": True,
                "observed_at": "2026-10-13T00:00:00+00:00",
            },
        )
    )
    db.commit()


def test_full_transition_return_and_cleanup_keep_shared_resources(db, scene):
    from db import models

    world = scene()
    tid = _planned(db, world)
    assert world.mutations() == []  # plan은 읽기만 한다

    prepare = run(db, world, "prepare", transition_id=tid)
    assert prepare["success"], prepare
    assert [s["state"] for s in prepare["steps"]] == ["APPLIED"] * 3
    assert prepare["runtime"]["simultaneous_compute_usd_per_hour"] == "0.10"
    assert (
        world.listeners[LISTENER_ARN]["DefaultActions"][0]["TargetGroupArn"]
        == ORIGINAL_TG
    )

    verify = run(db, world, "verify", transition_id=tid)
    assert verify["success"], verify["observation"]["checks"]

    cutover = run(db, world, "cutover", transition_id=tid)
    assert cutover["success"], cutover
    tg = prepare["target_group_arn"]
    assert world.listeners[LISTENER_ARN]["DefaultActions"] == [
        {"Type": "forward", "TargetGroupArn": tg}
    ]
    active = run(db, world, "cleanup-plan", transition_id=tid, options=DELETE_RETENTION)
    assert active["cleanup_allowed"] is False and any(
        "서비스 중" in b for b in active["blockers"]
    )

    back = run(db, world, "return", transition_id=tid)
    assert (
        back["success"] and back["route_restored"] and back["service_response_verified"]
    ), back
    assert world.listeners[LISTENER_ARN]["DefaultActions"] == [
        {"Type": "forward", "TargetGroupArn": ORIGINAL_TG}
    ]
    again = run(db, world, "return", transition_id=tid)
    assert (
        again["success"]
        and again["noop"]
        and again["step"] is None
        and again["service_response_verified"]
    )

    rounds = []
    for finish in (world.finish_draining, world.finish_termination, None):
        _, result = _cleanup_round(db, world, DELETE_RETENTION, transition_id=tid)
        assert result["success"], result
        rounds.append(result)
        if finish:
            assert (
                not result["complete"]
                and result["waiting"]
                and result["residual"]["pending"]
            )
            finish()
    assert rounds[-1]["complete"] and rounds[-1]["residual"]["pending"] == []
    deleted = [
        op for op, _ in world.mutations() if op.startswith(("delete", "deregister"))
    ]
    assert deleted == [
        "delete_listener",
        "delete_target_group",
        "delete_auto_scaling_group",
        "delete_launch_template",
        "deregister_image",
        "delete_snapshot",
    ]
    assert ORIGINAL_TG in world.target_groups and LISTENER_ARN in world.listeners
    assert (
        world.instances[WEB1]["State"]["Name"] == "running"
        and world.instances[SOURCE]["State"]["Name"] == "running"
    )
    assert len(world.mutations("modify_listener")) == 2  # cutover·return뿐
    # 기록은 지우지 않는다. 시도의 서비스 자원 변경은 그 시도로, #446 인프라 정리는 시도 없이 남는다
    operations = (
        db.query(models.ServiceTransitionOperation)
        .filter_by(execution_id=world.execution_id)
        .all()
    )
    assert len(operations) == len(world.mutations()) and all(
        op.state == "APPLIED" for op in operations
    )
    attribution = {op.aws_operation: op.transition_id for op in operations}
    assert (
        attribution["elbv2.delete_listener"]
        == attribution["elbv2.delete_target_group"]
        == tid
    )
    assert attribution["autoscaling.detach_load_balancer_target_groups"] == tid
    assert (
        attribution["autoscaling.delete_auto_scaling_group"] is None
        and attribution["ec2.deregister_image"] is None
    )
    assert db.get(models.ServiceTransition, tid) is not None


def test_uncertain_create_stays_blocked_on_absence_and_resolves_only_on_observed_effect(
    db, scene
):
    from services.aws import service_transition as aws

    world = scene()
    tid = _planned(db, world)
    world.failures["create_target_group"] = EndpointConnectionError(
        endpoint_url="https://elb"
    )
    first = run(db, world, "prepare", transition_id=tid)
    assert first["success"] is False and first["steps"][0]["state"] == "UNCERTAIN"

    absent = run(db, world, "status", transition_id=tid)
    (entry,) = absent["reconciliation"]
    assert (
        entry["reconciled_state"] == "EFFECT_NOT_OBSERVED"
        and not entry["gate_released"]
    )
    assert absent["unresolved_operations"] == 1
    blocked = run(db, world, "prepare", transition_id=tid)
    assert blocked["error"] == "unresolved_operation"
    assert (
        len(world.mutations("create_target_group")) == 1
    )  # 부재는 재전송 근거가 아니다
    assert (
        run(db, world, "cleanup-plan", transition_id=tid, options=DELETE_RETENTION)[
            "cleanup_allowed"
        ]
        is False
    )

    # 응답을 잃은 요청이 뒤늦게 반영됐다 — 소유 태그가 맞는 TG가 보인다
    plan = ServiceTransitionPlan.model_validate(absent["intent"]["plan"])
    world.elbv2_create_target_group(
        Name=plan.target_group_name, Tags=aws._transition_tags(plan, world.as_plan)
    )
    observed = run(db, world, "status", transition_id=tid)
    assert observed["reconciliation"][0]["reconciled_state"] == "EFFECT_OBSERVED"
    journal = {op["aws_operation"]: op for op in observed["journal"]}
    assert (
        journal["elbv2.create_target_group"]["state"] == "UNCERTAIN"
    )  # 원래 결과는 그대로다

    resumed = run(db, world, "prepare", transition_id=tid)
    assert resumed["success"], resumed
    assert len(world.mutations("create_target_group")) == 1
    assert [s["aws_operation"] for s in resumed["steps"]] == [
        "elbv2.create_listener",
        "autoscaling.attach_load_balancer_target_groups",
    ]


def test_cutover_requires_fresh_evidence_and_detects_drift(db, scene):
    world = scene()
    tid = _planned(db, world)
    assert run(db, world, "cutover", transition_id=tid)["error"] == "prepare_incomplete"
    assert run(db, world, "prepare", transition_id=tid)["success"]

    spoiled = world.clients()  # 같은 World를 보는 새 클라이언트 — 복제본 본문만 바꾼다
    spoiled["http"].bodies[MEMBER] = "other\n"
    assert (
        run(db, world, "cutover", clients=spoiled, transition_id=tid)["error"]
        == "fresh_verification_failed"
    )
    assert world.mutations("modify_listener") == []

    other = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/someone-else/3333333333333333"
    world.target_groups[other] = World._tg(other, "someone-else")
    world.targets[other] = {WEB1: "healthy"}
    world.listeners[LISTENER_ARN]["DefaultActions"] = [
        {"Type": "forward", "TargetGroupArn": other}
    ]
    drift = run(db, world, "cutover", transition_id=tid)
    assert drift["error"] == "route_drift" and world.mutations("modify_listener") == []
    # 이 시도의 cutover가 아니면 return도 덮어쓰지 않는다
    assert run(db, world, "return", transition_id=tid)["error"] == "route_drift"


def _cut_over(db, world):
    tid = _planned(db, world)
    assert run(db, world, "prepare", transition_id=tid)["success"]
    assert run(db, world, "cutover", transition_id=tid)["success"]
    return tid


@pytest.mark.parametrize("hold", ["aws_isolation_sg", "db_isolation"])
def test_return_refuses_while_a_routable_original_member_is_quarantined(
    db, scene, make_incident, hold
):
    world = scene()
    tid = _cut_over(db, world)
    if hold == "aws_isolation_sg":
        world.isolate(
            WEB1
        )  # 격리됐는데 아직 등록돼 있다(cutover 뒤라 unused로 보인다) — WEB2는 멀쩡하다
    else:
        _seed_isolation(db, make_incident, WEB1)
    refused = run(db, world, "return", transition_id=tid)
    assert refused["error"] == "original_route_unusable", refused
    assert WEB1 in refused["observation"]["facts"]["quarantined"]
    assert (
        len(world.mutations("modify_listener")) == 1
    )  # cutover뿐 — 복귀는 일어나지 않았다
    assert not [op for op, _ in world.calls if "register" in op or "unisolate" in op]


def _direct(result) -> list[str]:
    return sorted(
        h["instance_id"]
        for h in result["observation"]["http"]
        if h["path_kind"] == "member_direct"
    )


def test_return_restores_after_cutover_leaves_original_targets_unused(db, scene):
    """cutover 뒤 원래 TG는 리스너가 없어 대상이 unused(Target.NotInUse)다 — 직접 응답으로 확인하고 돌린다."""
    world = scene()
    tid = _cut_over(db, world)
    health = world.elbv2_describe_target_health(TargetGroupArn=ORIGINAL_TG)[
        "TargetHealthDescriptions"
    ]
    assert {
        (d["TargetHealth"]["State"], d["TargetHealth"]["Reason"]) for d in health
    } == {("unused", "Target.NotInUse")}
    back = run(db, world, "return", transition_id=tid)
    assert (
        back["success"] and back["route_restored"] and back["service_response_verified"]
    ), back
    assert back["noop"] is False and _direct(back) == [WEB1, WEB2]
    assert world.listeners[LISTENER_ARN]["DefaultActions"] == [
        {"Type": "forward", "TargetGroupArn": ORIGINAL_TG}
    ]


@pytest.mark.parametrize(
    ("spoil", "restores"),
    [
        (
            lambda w: w.instances[WEB1]["State"].update({"Name": "stopped"}),
            True,
        ),  # unused(Target.InvalidState)
        (
            lambda w: w.instances[WEB1]["Placement"].update(
                {"AvailabilityZone": "ap-northeast-2d"}
            ),
            True,
        ),  # 꺼진 AZ
        (
            lambda w: (
                w.instances[WEB1]["State"].update({"Name": "stopped"}),
                w.instances[WEB2]["Placement"].update(
                    {"AvailabilityZone": "ap-northeast-2d"}
                ),
            ),
            False,
        ),
    ],
)
def test_return_never_counts_invalid_state_or_disabled_az_targets_as_verified(
    db, scene, spoil, restores
):
    world = scene()
    tid = _cut_over(db, world)
    spoil(world)
    result = run(db, world, "return", transition_id=tid)
    if restores:
        assert result["success"], result
        assert _direct(result) == [WEB2] and set(
            result["observation"]["facts"]["not_routable"]
        ) == {WEB1}
        assert len(world.mutations("modify_listener")) == 2
    else:
        assert result["error"] == "original_route_unusable"
        assert _direct(result) == [] and set(
            result["observation"]["facts"]["not_routable"]
        ) == {WEB1, WEB2}
        assert len(world.mutations("modify_listener")) == 1  # cutover뿐


def test_already_original_return_succeeds_only_on_a_real_production_response(db, scene):
    """첫 return이 경로는 바꿨지만 ALB가 503 — 다음 return은 이미 원래 경로라도 실제 응답 전에는 실패다."""
    world = scene()
    tid = _cut_over(db, world)
    clients = world.clients()
    clients["http"].alb_status = 503
    first = run(db, world, "return", clients=clients, transition_id=tid)
    assert (
        first["success"] is False
        and first["route_restored"]
        and not first["service_response_verified"]
    )
    assert first["noop"] is False and first["step"]["state"] == "APPLIED"
    writes = len(world.mutations())
    again = run(db, world, "return", clients=clients, transition_id=tid)
    assert again["success"] is False and again["noop"] and again["step"] is None
    assert again["route_restored"] and not again["service_response_verified"]
    assert _direct(again) == [
        WEB1,
        WEB2,
    ]  # 이미 원래 경로여도 같은 안전·직접 응답 확인을 거친다
    clients["http"].alb_status = None
    ready = run(db, world, "return", clients=clients, transition_id=tid)
    assert ready["success"] and ready["noop"] and ready["service_response_verified"], (
        ready
    )
    assert (
        len(world.mutations()) == writes
        and len(world.mutations("modify_listener")) == 2
    )


def test_return_refuses_without_verified_original_responses(db, scene):
    world = scene()
    tid = _cut_over(db, world)
    clients = world.clients()
    clients["http"].bodies.update(
        {WEB1: "clone body\n", WEB2: BODY[SOURCE]}
    )  # 원래 경로가 복제본 본문을 내도 통과가 아니다
    refused = run(db, world, "return", clients=clients, transition_id=tid)
    assert refused["error"] == "original_route_unusable"
    assert len(world.mutations("modify_listener")) == 1


def test_return_requires_the_designated_probe_host_before_any_http(db, scene):
    world = scene()
    tid = _cut_over(db, world)
    clients = world.clients()
    clients["http"].origin = WEB2
    refused = run(db, world, "return", clients=clients, transition_id=tid)
    assert refused["error"] == "return_prerequisite_failed"
    assert clients["http"].calls == [] and len(world.mutations("modify_listener")) == 1


def test_isolated_member_blocks_and_status_exposes_protection_residual(
    db, scene, make_incident
):
    world = scene()
    tid = _planned(db, world)
    assert run(db, world, "prepare", transition_id=tid)["success"]
    _seed_isolation(db, make_incident, MEMBER)
    verify = run(db, world, "verify", transition_id=tid)
    assert verify["success"] is False
    assert any(
        c["name"] == "db_isolation_clear" and not c["passed"]
        for c in verify["observation"]["checks"]
    )
    status = run(db, world, "status", transition_id=tid)
    (fact,) = status["isolation"]
    assert fact["active"] and fact["protection_residual"]
    assert fact["scale_in_protection_result"]["reason"] == "WRITE_UNCERTAIN"


@pytest.mark.parametrize("hold", ["protected", "isolation_sg", "db_isolation"])
def test_cleanup_never_destroys_security_held_instances(db, scene, make_incident, hold):
    world = scene()
    if hold == "protected":
        world.group["Instances"][0]["ProtectedFromScaleIn"] = True
    elif hold == "isolation_sg":
        world.isolate(MEMBER)
    else:
        _seed_isolation(db, make_incident, MEMBER)
    inventory = run(
        db,
        world,
        "cleanup-plan",
        execution_id=world.execution_id,
        options=DELETE_RETENTION,
    )
    assert inventory["success"] and inventory["cleanup_allowed"] is False
    assert MEMBER in inventory["security_holds"] and inventory["security_hold_note"]
    refused = run(
        db,
        world,
        "cleanup",
        execution_id=world.execution_id,
        options={
            **DELETE_RETENTION,
            "manifest_sha256": inventory["manifest_sha256"],
            "resource_ids": inventory["resource_ids"],
        },
    )
    assert refused["error"] == "cleanup_blocked" and world.mutations() == []
    # 서비스 자원만 걷는 결정은 인스턴스를 없애지 않으므로 막히지 않고, 보류는 그대로 보고한다
    service_only = run(
        db, world, "cleanup-plan", execution_id=world.execution_id, options=SERVICE_ONLY
    )
    assert MEMBER in service_only["security_holds"]
    assert not [
        b for b in service_only["blockers"] if "보안 보류" in b or "DB의 열린 격리" in b
    ]


def test_cleanup_rechecks_security_holds_right_before_asg_deletion(db, scene):
    world = scene()
    tid = _planned(db, world)
    assert run(db, world, "prepare", transition_id=tid)["success"]
    _, first = _cleanup_round(db, world, DELETE_RETENTION, transition_id=tid)
    assert first["waiting"]
    world.finish_draining()
    # 계획을 승인한 뒤, TG 삭제와 ASG 삭제 사이에 멤버가 보호된다(동시 격리)
    world.after_call["delete_target_group"] = lambda: world.group["Instances"][
        0
    ].update({"ProtectedFromScaleIn": True})
    _, second = _cleanup_round(db, world, DELETE_RETENTION, transition_id=tid)
    assert second["success"] is False and second["security_hold"]["holds"][MEMBER] == [
        "ProtectedFromScaleIn"
    ]
    assert world.mutations("delete_auto_scaling_group") == [] and not second["complete"]


def test_failed_446_partial_resources_clean_up_without_a_service_attempt(db, scene):
    from db import models
    from schemas.api.actions import ExecutionStatus

    world = scene(status=ExecutionStatus.FAILED)
    # #446이 AMI·스냅숏까지 만들고 실패했다 — ASG·시작 템플릿·인스턴스는 없고, 태그만 남은 고아 스냅숏이 있다
    world.group, world.templates, world.volumes = None, {}, {}
    del world.instances[MEMBER]
    world.snapshots[ORPHAN] = {"SnapshotId": ORPHAN, "Tags": world.exec_tags}
    inventory, result = _cleanup_round(
        db, world, DELETE_RETENTION, execution_id=world.execution_id
    )
    assert inventory["transition_id"] is None and inventory["attempts"] == []
    assert {i["resource_id"] for i in inventory["pending"]} == {AMI, SNAP, ORPHAN}
    assert result["success"] and result["complete"], result
    assert [op for op, _ in world.mutations()] == [
        "deregister_image",
        "delete_snapshot",
        "delete_snapshot",
    ]
    operations = (
        db.query(models.ServiceTransitionOperation)
        .filter_by(execution_id=world.execution_id)
        .all()
    )
    assert len(operations) == 3 and all(op.transition_id is None for op in operations)
    assert world.listeners[LISTENER_ARN] and ORIGINAL_TG in world.target_groups


def _seed_restore_reference(db, make_incident, instance_id, target_group_arn):
    """EC2_ISOLATE 성공 + 불변 SG/TG 백업(복원 TG = target_group_arn) — 격리 workflow가 남기는 DB 사실."""
    from db import models
    from schemas.api.actions import ExecutionStatus
    from schemas.backups import BackupType, SgAndTgMappingBackup
    from schemas.runbooks import RunbookId, TriggerSource

    target_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance_id}"
    incident = make_incident(db)
    isolation = models.ActionExecution(
        incident_id=incident.incident_id,
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=target_arn,
        trigger_source=TriggerSource.USER_APPROVAL,
        status=ExecutionStatus.SUCCESS,
    )
    db.add(isolation)
    db.flush()
    payload = SgAndTgMappingBackup(
        security_group_ids=[SG_WEB],
        target_group_arn=target_group_arn,
        target_port=80,
        network_interfaces=[
            {
                "network_interface_id": f"eni-{instance_id[2:]}",
                "security_group_ids": [SG_WEB],
            }
        ],
    ).model_dump(mode="json")
    backup = models.BackupRecord(
        execution_id=isolation.execution_id,
        target_arn=target_arn,
        backup_type=BackupType.SAVE_CURRENT_SG_AND_TG_MAPPING.value,
        payload=payload,
    )
    db.add(backup)
    db.flush()
    isolation.backup_record_id = backup.backup_record_id
    db.commit()
    return isolation.execution_id, backup.backup_record_id, payload


def test_service_only_cleanup_keeps_the_target_group_an_active_isolation_restores_to(
    db, scene, make_incident, monkeypatch
):
    from db import models
    from services.aws import executor
    from workflows import _DbBackupRecordLoader

    world = scene()
    tid = _planned(db, world)
    tg = run(db, world, "prepare", transition_id=tid)["target_group_arn"]
    # ASG 멤버를 격리했다 — ENI는 격리 SG, TG에서는 빠졌고, 백업의 복원 TG는 이 시도의 TG다
    world.isolate(MEMBER)
    world.targets[tg].pop(MEMBER)
    isolation_id, backup_id, payload = _seed_restore_reference(
        db, make_incident, MEMBER, tg
    )

    def unisolate_precheck():
        monkeypatch.setattr(
            executor,
            "aws_client",
            lambda service, region=None, **_: FakeClient(world, service),
        )
        hook, world.hook = (
            world.hook,
            (lambda: None),
        )  # precheck는 로더 조회 트랜잭션을 연 채 AWS를 부른다(남의 경계)
        try:
            return executor.precheck(
                "RUNBOOK_EC2_UNISOLATE",
                f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{MEMBER}",
                {
                    "instance_id": MEMBER,
                    "backup_record_id": backup_id,
                    "evidence_id": str(uuid.uuid4()),
                },
                backup_loader=_DbBackupRecordLoader(db),
            )
        finally:
            world.hook = hook
            db.commit()

    assert unisolate_precheck().passed
    inventory = run(
        db, world, "cleanup-plan", execution_id=world.execution_id, options=SERVICE_ONLY
    )
    attachment = f"{world.as_plan.asg_name}|{tg}"
    assert (
        inventory["recovery_holds"] == {tg: [isolation_id]}
        and inventory["recovery_hold_note"]
    )
    assert inventory["cleanup_allowed"] and [
        i["kind"] for i in inventory["pending"]
    ] == ["validation_listener"]
    assert {r["id"] for r in inventory["retained"]} >= {tg, attachment}
    destructive = run(
        db,
        world,
        "cleanup-plan",
        execution_id=world.execution_id,
        options=DELETE_RETENTION,
    )
    assert not destructive["cleanup_allowed"] and any(
        "복원" in b for b in destructive["blockers"]
    )

    _, result = _cleanup_round(db, world, SERVICE_ONLY, execution_id=world.execution_id)
    assert result["success"] and not result["complete"], result
    assert result["residual"]["recovery_holds"] == {tg: [isolation_id]}
    assert [
        op for op, _ in world.mutations() if op.startswith(("delete", "detach"))
    ] == ["delete_listener"]
    assert tg in world.target_groups and tg in world.group["TargetGroupARNs"]
    assert unisolate_precheck().passed  # 기존 UNISOLATE 경로는 그대로 쓸 수 있다
    assert db.get(models.BackupRecord, backup_id).payload == payload  # 백업은 그대로다
    assert (
        db.get(models.ActionExecution, isolation_id).status.value == "SUCCESS"
    )  # 격리를 풀지 않았다
    # 대조군 — TG가 없어지면 UNISOLATE precheck가 실패한다. 이 보류가 지키는 것이다
    world.target_groups.pop(tg)
    assert not unisolate_precheck().passed


def test_cleanup_rechecks_restore_references_right_before_target_group_removal(
    db, scene, make_incident
):
    world = scene()
    tid = _planned(db, world)
    tg = run(db, world, "prepare", transition_id=tid)["target_group_arn"]
    inventory = run(
        db, world, "cleanup-plan", execution_id=world.execution_id, options=SERVICE_ONLY
    )
    assert inventory["recovery_holds"] == {}
    # 승인 뒤, 검증 리스너 삭제와 TG 분리 사이에 격리가 생긴다
    world.after_call["delete_listener"] = lambda: _seed_restore_reference(
        db, make_incident, MEMBER, tg
    )
    result = run(
        db,
        world,
        "cleanup",
        execution_id=world.execution_id,
        options={
            **SERVICE_ONLY,
            "manifest_sha256": inventory["manifest_sha256"],
            "resource_ids": inventory["resource_ids"],
        },
    )
    assert (
        result["success"] is False
        and result["recovery_hold"]["item"]["kind"] == "target_group_attachment"
    )
    assert [op for op, _ in world.mutations()] == [
        "create_target_group",
        "create_listener",
        "attach_load_balancer_target_groups",
        "delete_listener",
    ]
    assert not result["complete"] and tg in world.target_groups


def test_cleanup_waits_for_a_running_446_execution(db, scene):
    from schemas.api.actions import ExecutionStatus

    world = scene(status=ExecutionStatus.IN_PROGRESS)
    assert (
        run(
            db,
            world,
            "cleanup-plan",
            execution_id=world.execution_id,
            options=DELETE_RETENTION,
        )["error"]
        == "execution_running"
    )
    refused = run(
        db,
        world,
        "cleanup",
        execution_id=world.execution_id,
        options={
            **DELETE_RETENTION,
            "manifest_sha256": "0" * 64,
            "resource_ids": [AMI],
        },
    )
    assert refused["error"] == "execution_running" and world.mutations() == []


def test_replacement_attempt_reuses_infrastructure_and_keeps_old_plan_readable(
    db, scene
):
    world = scene()
    first = _planned(db, world)
    assert run(db, world, "prepare", transition_id=first)["success"]

    assert (
        run(db, world, "plan", execution_id=world.execution_id, options=plan_options())[
            "error"
        ]
        == "replacement_required"
    )
    open_footprint = run(
        db,
        world,
        "plan",
        execution_id=world.execution_id,
        options=plan_options(replaces_transition_id=first),
    )
    assert (
        open_footprint["error"] == "predecessor_footprint_open"
        and open_footprint["footprint"]
    )

    # 서비스 자원만 걷는다 — ASG·인스턴스·시작 템플릿·AMI는 남는다
    _, removed = _cleanup_round(
        db, world, SERVICE_ONLY, execution_id=world.execution_id
    )
    assert removed["waiting"]
    world.finish_draining()
    _, done = _cleanup_round(db, world, SERVICE_ONLY, execution_id=world.execution_id)
    assert done["complete"] and done["residual"]["billing_note"]
    assert world.group and AMI in world.images and LT_ID in world.templates

    second = _planned(db, world, replaces_transition_id=first)
    status = run(db, world, "status", execution_id=world.execution_id)
    assert status["transition_id"] == second
    assert [
        (a["transition_id"], a["supersedes_transition_id"], a["leaf"])
        for a in status["attempts"]
    ] == [(first, None, False), (second, first, True)]
    old = run(db, world, "status", transition_id=first)
    assert (
        old["success"]
        and old["superseded"]
        and old["intent"]["plan"]["transition_id"] == first
    )
    for command in ("prepare", "verify", "cutover", "return"):
        assert run(db, world, command, transition_id=first)["error"] == "superseded"
    assert (
        run(
            db,
            world,
            "plan",
            execution_id=world.execution_id,
            options=plan_options(replaces_transition_id=first),
        )["error"]
        == "replacement_required"
    )

    before = len(world.calls)
    again = run(db, world, "prepare", transition_id=second)
    assert again["success"], again
    created = [op for op, _ in world.calls[before:] if op in MUTATIONS]
    assert created == [
        "create_target_group",
        "create_listener",
        "attach_load_balancer_target_groups",
    ]
    assert again["target_group_arn"].split("/")[-2] == transition_target_group_name(
        second
    )
    assert run(db, world, "verify", transition_id=second)["success"]


def test_shared_coordinates_exclude_concurrent_mutation_and_second_binding(db, scene):
    from db.repositories import service_transitions as repo

    world = scene()
    tid = _planned(db, world)
    with repo.mutation_claim(db, [f"listener:{LISTENER_ARN}"]) as held:
        assert held
        busy = run(db, world, "prepare", transition_id=tid)
    assert busy["error"] == "mutation_in_progress" and world.mutations() == []
    assert run(db, world, "prepare", transition_id=tid)["success"]

    second = scene()
    bound = run(
        db, second, "plan", execution_id=second.execution_id, options=plan_options()
    )
    assert bound["error"] == "listener_bound"


@pytest.mark.parametrize("command", ["prepare", "cutover", "return", "cleanup"])
def test_a_replacement_committed_between_load_and_claim_blocks_the_stale_mutation(
    pg_engine, make_incident, monkeypatch, command
):
    """독립 PostgreSQL 세션 둘. A가 시도를 읽고 잠금 직전에 멈춘 사이 B가 대체 시도를 commit한다.

    A는 잠금 아래에서 시도 목록을 다시 읽어 멈춰야 한다 — 시도 명령은 superseded, 실행 단위 정리는
    attempts_changed — 그리고 AWS 변경도 일지도 0건이다.
    """
    import service_transitions as facade
    from db import models
    from db.repositories import service_transitions as repo
    from schemas.api.actions import ExecutionStatus
    from schemas.runbooks import RunbookId, TriggerSource
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    def session() -> Session:
        return Session(bind=pg_engine, autoflush=False, expire_on_commit=False)

    seed = session()
    incident = make_incident(
        seed, subject_arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{SOURCE}"
    )
    execution_id = str(uuid.uuid4())
    world = World(execution_id)
    seed.add(
        models.ActionExecution(
            execution_id=execution_id,
            incident_id=incident.incident_id,
            runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
            target_arn=world.as_plan.target_arn,
            trigger_source=TriggerSource.USER_APPROVAL,
            status=ExecutionStatus.SUCCESS,
            autoscaling_plan=world.as_plan.model_dump(mode="json"),
        )
    )
    seed.commit()
    try:
        first = facade.run_service_command(
            seed,
            command="plan",
            clients=world.clients(),
            execution_id=execution_id,
            options=plan_options(),
        )
        assert first["success"], first
        stale = first["transition_id"]
        seed.commit()

        loaded, replaced = threading.Event(), threading.Event()
        real_claim = repo.mutation_claim

        @contextmanager
        def pausing_claim(db, identities):
            in_attempt_a = threading.current_thread().name.startswith("attempt-a")
            if in_attempt_a and not loaded.is_set():
                loaded.set()  # A는 _load를 마쳤고 아직 잠금을 잡지 않았다
                assert replaced.wait(30)
            with real_claim(db, identities) as held:
                yield held

        monkeypatch.setattr(repo, "mutation_claim", pausing_claim)
        if command == "cleanup":
            ids = {"execution_id": execution_id}
            options = {
                **DELETE_RETENTION,
                "manifest_sha256": "0" * 64,
                "resource_ids": [AMI],
            }
        else:
            ids, options = {"transition_id": stale}, None

        def attempt_a():
            a = session()
            try:
                return facade.run_service_command(
                    a, command=command, clients=world.clients(), options=options, **ids
                )
            finally:
                a.close()

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attempt-a")
        try:
            future = pool.submit(attempt_a)
            assert loaded.wait(30)
            b = session()
            try:
                second = facade.run_service_command(
                    b,
                    command="plan",
                    clients=world.clients(),
                    execution_id=execution_id,
                    options=plan_options(replaces_transition_id=stale),
                )
            finally:
                b.close()
                replaced.set()
            result = future.result(timeout=30)
        finally:
            replaced.set()
            pool.shutdown(wait=True)
        assert second["success"], second
        assert result["success"] is False
        assert result["error"] == (
            "attempts_changed" if command == "cleanup" else "superseded"
        ), result
        assert world.mutations() == []
        check = session()
        try:
            assert (
                check.query(models.ServiceTransitionOperation)
                .filter_by(execution_id=execution_id)
                .count()
                == 0
            )
        finally:
            check.close()
    finally:
        seed.rollback()
        purge_audit_events(seed, execution_ids=[execution_id])
        for table in (
            models.ServiceTransitionObservation,
            models.ServiceTransitionOperation,
            models.ServiceTransition,
        ):
            seed.execute(delete(table).where(table.execution_id == execution_id))
        seed.execute(
            delete(models.ActionExecution).where(
                models.ActionExecution.execution_id == execution_id
            )
        )
        seed.execute(
            delete(models.Incident).where(
                models.Incident.incident_id == incident.incident_id
            )
        )
        seed.commit()
        seed.close()


def test_plan_rejects_original_bodies_that_do_not_match_the_route(db, scene):
    from db import models

    world = scene()
    result = run(
        db,
        world,
        "plan",
        execution_id=world.execution_id,
        options=plan_options(original_target_expected_bodies={WEB1: BODY[WEB1]}),
    )
    assert result["error"] == "plan_rejected"
    assert any(
        c["name"] == "original_expected_bodies" and not c["passed"]
        for c in result["checks"]
    )
    assert (
        db.query(models.ServiceTransition)
        .filter_by(execution_id=world.execution_id)
        .count()
        == 0
    )


@pytest.mark.parametrize(
    ("facts", "fragment"),
    [
        ({}, "captured_instance_type_hourly_usd"),
        # 원본(idle-dev 0.05) + ASG 1대(0.05) = 0.10 — 원래 TG 대상 web-1·web-2는 세지 않는다
        (
            {
                "captured_instance_type_hourly_usd": "0.05",
                "simultaneous_cost_cap_usd_per_hour": "0.09",
                "vcpu_quota": 8,
                "rto_minutes": 30,
            },
            "0.10",
        ),
    ],
)
def test_prepare_requires_the_operator_simultaneous_cost_bound(
    db, scene, facts, fragment
):
    world = scene()
    tid = _planned(db, world, runtime_facts=facts)
    refused = run(db, world, "prepare", transition_id=tid)
    assert refused["error"] == "prepare_rejected" and world.mutations() == []
    gate = next(c for c in refused["checks"] if c["name"] == "runtime_cost_gate")
    assert fragment in gate["detail"] and not gate["passed"]
    assert refused["runtime"]["source_instance"]["instance_id"] == SOURCE


def test_query_failure_is_not_success_or_absence(db, scene):
    world = scene()
    tid = _planned(db, world)
    world.failures["describe_listeners"] = client_error("AccessDenied", 403)
    result = run(db, world, "status", transition_id=tid)
    assert result["success"] is False and result["error"] == "aws_query_failed"
    assert result["reason_code"] == "PRECHECK_UNAUTHORIZED"


FOREIGN_AMI = "ami-0ddddddddddddddd1"
FOREIGN_INSTANCE = "i-0000000000000000f"
STANDALONE_LT = "lt-0eeeeeeeeeeeeeee1"
FULL_FACTS = plan_options()["runtime_facts"]
MISSING_FACT = {
    "vcpu_quota": "운영자 공급값 없음: runtime_facts.vcpu_quota",
    "rto_minutes": "운영자 공급값 없음: runtime_facts.rto_minutes",
}
KINDS = {
    "delete_launch_template": "launch_template",
    "deregister_image": "image",
    "delete_snapshot": "snapshot",
    "delete_listener": "validation_listener",
}


def _item(action: str, resource_id: str) -> CleanupItem:
    return CleanupItem(
        order=1, kind=KINDS[action], resource_id=resource_id, action=action
    )


def foreign_group(**launch) -> dict:
    return {"AutoScalingGroupName": "someone-else", "Instances": [], **launch}


def standalone_template(world: World, versions: dict[str, str]) -> None:
    world.templates[STANDALONE_LT] = {
        "LaunchTemplateId": STANDALONE_LT,
        "LaunchTemplateName": "standalone",
        "Tags": [],
    }
    for number, image in versions.items():
        world.template_versions[(STANDALONE_LT, number)] = {"ImageId": image}


def foreign_instance(world: World, state: str = "running") -> None:
    world.instances[FOREIGN_INSTANCE] = {
        "InstanceId": FOREIGN_INSTANCE,
        "State": {"Name": state},
        "ImageId": AMI,
        "Tags": [],
    }


def test_shared_resource_refs_dispatch_per_action():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())

    def refs(action, rid):
        return aws.shared_resource_refs(c, _item(action, rid), world.as_plan)

    assert refs("delete_launch_template", LT_ID) == []
    assert refs("deregister_image", AMI) == []
    assert any(AMI in r for r in refs("delete_snapshot", SNAP))
    world.images.clear()
    assert refs("delete_snapshot", SNAP) == []
    world.other_groups.append(
        foreign_group(LaunchTemplate={"LaunchTemplateName": LT_NAME, "Version": "1"})
    )
    assert refs("delete_launch_template", LT_ID)
    assert refs("delete_listener", LISTENER_ARN) == []


@pytest.mark.parametrize(
    "launch",
    [
        {"LaunchTemplate": {"LaunchTemplateId": LT_ID, "Version": "1"}},
        {"LaunchTemplate": {"LaunchTemplateName": LT_NAME, "Version": "$Latest"}},
    ],
)
def test_template_refs_cover_other_asgs_by_id_and_name(launch):
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    world.other_groups.append(foreign_group(**launch))
    refs = aws.shared_resource_refs(c, _item("delete_launch_template", LT_ID), world.as_plan)
    assert any("시작 템플릿을 씁니다" in r for r in refs), refs


@pytest.mark.parametrize(
    ("setup", "fragment"),
    [
        (
            lambda w: (
                w.template_versions.update({(OTHER_LT, "$Default"): {"ImageId": AMI}}),
                w.other_groups.append(
                    foreign_group(
                        LaunchTemplate={
                            "LaunchTemplateId": OTHER_LT,
                            "Version": "$Default",
                        }
                    )
                ),
            ),
            "다른 시작 템플릿",
        ),
        (lambda w: foreign_instance(w), "떠 있습니다"),
    ],
)
def test_image_refs_cover_foreign_asg_and_live_instances(setup, fragment):
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    setup(world)
    refs = aws.shared_resource_refs(c, _item("deregister_image", AMI), world.as_plan)
    assert any(fragment in r for r in refs), refs


def test_terminated_foreign_instance_is_not_a_reference():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    foreign_instance(world, "terminated")
    assert aws.shared_resource_refs(c, _item("deregister_image", AMI), world.as_plan) == []


def test_standalone_template_versions_are_read_in_full_and_fail_closed():
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    world.version_page_size = 1
    item = _item("deregister_image", AMI)
    world.template_versions[(LT_ID, "1")] = {"ImageId": AMI}
    standalone_template(world, {"1": OTHER_AMI, "2": OTHER_AMI, "3": OTHER_AMI})
    assert aws.shared_resource_refs(c, item, world.as_plan) == []
    # 번호 버전 2(기본·최신 아님)만 이 AMI — 페이지를 끝까지 읽어야 보인다.
    standalone_template(world, {"1": OTHER_AMI, "2": AMI, "3": OTHER_AMI})
    refs = aws.shared_resource_refs(c, item, world.as_plan)
    assert len(refs) == 1 and STANDALONE_LT in refs[0] and "v2" in refs[0]
    inventory = aws.cleanup_inventory(
        c, world.as_plan, [], retention_decision="delete_execution_resources"
    )
    assert any(STANDALONE_LT in b for b in inventory.blockers)
    kept = aws.cleanup_inventory(
        c, world.as_plan, [], retention_decision="retain_launch_image"
    )
    assert not any(STANDALONE_LT in b for b in kept.blockers)
    standalone_template(world, {"1": "resolve:ssm:/golden/ami"})
    world.template_versions.pop((STANDALONE_LT, "2"))
    world.template_versions.pop((STANDALONE_LT, "3"))
    refs = aws.shared_resource_refs(c, item, world.as_plan)
    assert any("확정할 수 없습니다" in r for r in refs), refs


@pytest.mark.parametrize(
    "operation", ["describe_launch_templates", "describe_launch_template_versions"]
)
def test_reference_read_failure_raises_instead_of_reporting_absence(operation):
    from services.aws import service_transition as aws

    world = World(str(uuid.uuid4()))
    c = aws.clients_from(world.clients())
    standalone_template(world, {"1": OTHER_AMI})
    world.failures[operation] = client_error("AccessDenied", 403)
    with pytest.raises(ClientError):
        aws.shared_resource_refs(c, _item("deregister_image", AMI), world.as_plan)


def _returned(db, world, **overrides):
    tid = _planned(db, world, **overrides)
    for command in ("prepare", "verify", "cutover", "return"):
        assert run(db, world, command, transition_id=tid)["success"], command
    return tid


def _cleanup_once(db, world, tid):
    inventory = run(db, world, "cleanup-plan", transition_id=tid, options=DELETE_RETENTION)
    assert inventory["cleanup_allowed"], inventory
    return run(
        db,
        world,
        "cleanup",
        transition_id=tid,
        options={
            **DELETE_RETENTION,
            "manifest_sha256": inventory["manifest_sha256"],
            "resource_ids": inventory["resource_ids"],
        },
    )


def _until_launch_resources(db, world, tid):
    """리스너·TG·ASG 삭제와 종료 확인까지 — 다음 라운드가 LT·AMI·스냅숏에 닿는다."""
    for finish in (world.finish_draining, world.finish_termination):
        assert _cleanup_once(db, world, tid)["success"]
        finish()


def _assert_reference_hold(result, resource_id, fragment):
    assert result["success"] is False and result["complete"] is False, result
    hold = result["reference_hold"]
    assert hold["item"]["resource_id"] == resource_id
    assert any(fragment in r for r in hold["references"]), hold


def test_foreign_asg_using_the_template_by_name_stops_template_deletion(db, scene):
    world = scene()
    tid = _returned(db, world)
    assert _cleanup_once(db, world, tid)["success"]
    world.finish_draining()

    def foreign_asg_appears():
        world.finish_termination()
        world.other_groups.append(
            foreign_group(
                LaunchTemplate={"LaunchTemplateName": LT_NAME, "Version": "$Latest"}
            )
        )

    world.after_call["delete_auto_scaling_group"] = foreign_asg_appears
    result = _cleanup_once(db, world, tid)
    _assert_reference_hold(result, LT_ID, "시작 템플릿을 씁니다")
    assert LT_ID in world.templates
    assert world.mutations("delete_launch_template") == []
    assert world.mutations("deregister_image") == []
    assert world.mutations("delete_snapshot") == []


@pytest.mark.parametrize(
    ("appear", "fragment"),
    [
        (
            lambda w: (
                w.template_versions.update({(OTHER_LT, "$Default"): {"ImageId": AMI}}),
                w.other_groups.append(
                    foreign_group(
                        LaunchTemplate={
                            "LaunchTemplateId": OTHER_LT,
                            "Version": "$Default",
                        }
                    )
                ),
            ),
            "다른 시작 템플릿으로 AMI",
        ),
        (lambda w: foreign_instance(w), "떠 있습니다"),
        (
            lambda w: standalone_template(w, {"1": OTHER_AMI, "2": AMI}),
            "시작 템플릿",
        ),
    ],
)
def test_foreign_ami_reference_stops_image_deregistration(db, scene, appear, fragment):
    world = scene()
    tid = _returned(db, world)
    _until_launch_resources(db, world, tid)
    world.after_call["delete_launch_template"] = lambda: appear(world)
    result = _cleanup_once(db, world, tid)
    _assert_reference_hold(result, AMI, fragment)
    assert AMI in world.images and SNAP in world.snapshots
    assert world.mutations("deregister_image") == []
    assert world.mutations("delete_snapshot") == []


def test_new_foreign_ami_mapping_stops_snapshot_deletion(db, scene):
    world = scene()
    tid = _returned(db, world)
    _until_launch_resources(db, world, tid)

    def foreign_image_appears():
        world.images[FOREIGN_AMI] = {
            "ImageId": FOREIGN_AMI,
            "Name": "someone-else",
            "OwnerId": ACCOUNT,
            "State": "available",
            "Tags": [],
            "BlockDeviceMappings": [{"Ebs": {"SnapshotId": SNAP}}],
        }

    world.after_call["deregister_image"] = foreign_image_appears
    result = _cleanup_once(db, world, tid)
    _assert_reference_hold(result, SNAP, FOREIGN_AMI)
    assert SNAP in world.snapshots
    assert world.mutations("delete_snapshot") == []


def test_reference_read_failure_before_image_deregistration_deletes_nothing(db, scene):
    world = scene()
    tid = _returned(db, world)
    _until_launch_resources(db, world, tid)
    world.after_call["delete_launch_template"] = lambda: world.failures.__setitem__(
        "describe_launch_templates", client_error("AccessDenied", 403)
    )
    result = _cleanup_once(db, world, tid)
    assert result["success"] is False and result["error"] == "aws_query_failed"
    assert result["reason_code"] == "PRECHECK_UNAUTHORIZED"
    assert AMI in world.images and SNAP in world.snapshots
    assert world.mutations("deregister_image") == []
    assert world.mutations("delete_snapshot") == []


def _drop_runtime_facts(db, tid, *names):
    """누락 입력을 허용하던 이전 버전에서 저장된 계획을 테스트 DB에 구성한다."""
    from db import models

    row = db.get(models.ServiceTransition, tid)
    row.plan = {
        **row.plan,
        "runtime_facts": {**row.plan["runtime_facts"], **{n: None for n in names}},
    }
    db.commit()


MISSING_CASES = [("vcpu_quota",), ("rto_minutes",), ("vcpu_quota", "rto_minutes")]


@pytest.mark.parametrize("missing", MISSING_CASES)
def test_prepare_refuses_without_operator_quota_or_rto(db, scene, missing):
    world = scene()
    facts = {k: v for k, v in FULL_FACTS.items() if k not in missing}
    tid = _planned(db, world, runtime_facts=facts)
    refused = run(db, world, "prepare", transition_id=tid)
    assert refused["error"] == "prepare_rejected" and world.mutations() == []
    gate = next(c for c in refused["checks"] if c["name"] == "runtime_cost_gate")
    assert not gate["passed"]
    for name, text in MISSING_FACT.items():
        assert (text in gate["detail"]) == (name in missing)


@pytest.mark.parametrize("missing", MISSING_CASES)
def test_cutover_refuses_without_operator_quota_or_rto(db, scene, missing):
    world = scene()
    tid = _planned(db, world)
    assert run(db, world, "prepare", transition_id=tid)["success"]
    assert run(db, world, "verify", transition_id=tid)["success"]
    _drop_runtime_facts(db, tid, *missing)
    refused = run(db, world, "cutover", transition_id=tid)
    assert refused["error"] == "runtime_cost_gate"
    assert refused["detail"] == [MISSING_FACT[n] for n in MISSING_FACT if n in missing]
    assert world.mutations("modify_listener") == []
    assert (
        world.listeners[LISTENER_ARN]["DefaultActions"][0]["TargetGroupArn"]
        == ORIGINAL_TG
    )


def test_status_return_and_cleanup_are_not_gated_by_missing_quota_or_rto(db, scene):
    world = scene()
    tid = _planned(db, world)
    for command in ("prepare", "verify", "cutover"):
        assert run(db, world, command, transition_id=tid)["success"], command
    _drop_runtime_facts(db, tid, "vcpu_quota", "rto_minutes")
    assert run(db, world, "status", transition_id=tid)["success"]
    back = run(db, world, "return", transition_id=tid)
    assert back["success"] and back["route_restored"], back
    _, result = _cleanup_round(db, world, DELETE_RETENTION, transition_id=tid)
    assert result["success"], result


# ============================================================ down 뒤 정리·불확실 AMI·세션·결속 경합
@pytest.mark.parametrize("transition_tg_left", [True, False])
def test_status_and_cleanup_after_smoke_down_reach_the_remaining_image_and_snapshot(
    db, scene, transition_tg_left
):
    """down이 공유 ALB·원래 TG를 지운 뒤 — _DOWN_IMAGE_WARNING이 안내하는 status·cleanup이 돌고
    남은 전환 TG·AMI·스냅숏만 걷는다. 지워진 공유 자원은 '없음'으로 읽고 건드리지 않는다."""
    world = scene()
    tid = _returned(db, world)
    tg = next(arn for arn in world.target_groups if arn != ORIGINAL_TG)
    world.smoke_down()
    if not transition_tg_left:
        world.target_groups.pop(tg)
        world.targets.pop(tg)

    status = run(db, world, "status", transition_id=tid)
    assert status["success"], status
    live = status["live"]
    assert live["route_phase"] == "drifted"
    assert live["production_route"]["unsupported"] == "리스너 없음"
    assert live["validation_listener"]["arn"] is None
    assert live["original_route_targets"] == []
    assert live["target_group"]["arn"] == (tg if transition_tg_left else None)
    assert status["unresolved_operations"] == 0

    before = len(world.mutations())
    inventory, result = _cleanup_round(db, world, DELETE_RETENTION, transition_id=tid)
    remaining = [("target_group", tg)] if transition_tg_left else []
    assert [(i["kind"], i["resource_id"]) for i in inventory["pending"]] == [
        *remaining,
        ("image", AMI),
        ("snapshot", SNAP),
    ]
    assert result["success"] and result["complete"], result
    assert [op for op, _ in world.mutations()[before:]] == [
        *(["delete_target_group"] if transition_tg_left else []),
        "deregister_image",
        "delete_snapshot",
    ]
    assert AMI not in world.images and SNAP not in world.snapshots
    assert not world.target_groups


@pytest.mark.parametrize(
    ("aftermath", "reconciled"),
    [
        ("gone", "EFFECT_OBSERVED"),  # 끝내 InvalidAMIID.NotFound
        # 해제 직후 잠시 State=deregistered로 보인다
        ("deregistered_record", "EFFECT_OBSERVED"),
        ("still_available", "EFFECT_NOT_OBSERVED"),  # 요청이 반영되지 않았다
        # 반영되지 않았고 그새 비활성화됐다 — 기본 응답에서 빠지는 AMI
        ("disabled", "EFFECT_NOT_OBSERVED"),
        ("missing_state", None),  # 기록은 있으나 State가 없다 — 해제로 추정하지 않는다(모름)
    ],
)
def test_uncertain_image_deregistration_reconciles_only_from_what_ec2_returns(
    db, scene, aftermath, reconciled
):
    """응답을 잃은 deregister_image는 다시 보내지 않는다 — status가 읽은 현물로만 관문을 푼다."""
    observed = reconciled == "EFFECT_OBSERVED"
    world = scene()
    tid = _returned(db, world)
    _until_launch_resources(db, world, tid)
    record = dict(world.images[AMI])
    world.failures["deregister_image"] = EndpointConnectionError(
        endpoint_url="https://ec2"
    )
    if observed:
        world.apply_before_failure.add("deregister_image")
    uncertain = _cleanup_once(db, world, tid)
    assert uncertain["success"] is False
    step = uncertain["steps"][-1]
    assert (step["aws_operation"], step["state"]) == (
        "ec2.deregister_image",
        "UNCERTAIN",
    )
    if aftermath == "deregistered_record":
        world.images[AMI] = {**record, "State": "deregistered"}
    elif aftermath == "disabled":
        world.images[AMI]["State"] = "disabled"
    elif aftermath == "missing_state":
        del world.images[AMI]["State"]

    status = run(db, world, "status", transition_id=tid)
    assert status["success"], status
    (entry,) = status["reconciliation"]
    assert entry["reconciled_state"] == reconciled
    assert entry["gate_released"] is observed
    (journal,) = [
        op for op in status["journal"] if op["aws_operation"] == "ec2.deregister_image"
    ]
    assert journal["reconciled_state"] == reconciled
    assert status["unresolved_operations"] == (0 if observed else 1)
    assert SNAP in world.snapshots

    if observed:
        # 다음 승인 정리는 해제된 AMI를 다시 보내지 않고 남은 스냅숏만 지워 끝난다
        before = len(world.mutations())
        result = _cleanup_once(db, world, tid)
        assert result["success"] and result["complete"], result
        assert [op for op, _ in world.mutations()[before:]] == ["delete_snapshot"]
        assert SNAP not in world.snapshots
    else:
        blocked = run(
            db, world, "cleanup-plan", transition_id=tid, options=DELETE_RETENTION
        )
        assert blocked["cleanup_allowed"] is False and any(
            "결과를 모르는 변경" in b for b in blocked["blockers"]
        )
        assert SNAP in world.snapshots
    assert len(world.mutations("deregister_image")) == 1  # 재전송 없음


@pytest.mark.parametrize("spoil", ["wrong_account", "wrong_region", "unknown_account"])
def test_status_does_not_reconcile_under_a_mismatched_or_unknown_session(
    db, scene, spoil
):
    """다른 계정·리전(또는 확인하지 못한 세션)에서 읽은 존재·부재는 계획 자원의 사실이 아니다 —
    대조하지도 관문을 풀지도 않는다. 맞는 세션으로 다시 보면 그때 대조한다."""
    world = scene()
    tid = _planned(db, world)
    world.failures["create_target_group"] = EndpointConnectionError(
        endpoint_url="https://elb"
    )
    world.apply_before_failure.add("create_target_group")
    first = run(db, world, "prepare", transition_id=tid)
    assert first["steps"][0]["state"] == "UNCERTAIN"

    clients = world.clients()
    if spoil == "wrong_account":
        world.sts_get_caller_identity = lambda: {"Account": "210987654321"}
    elif spoil == "unknown_account":
        world.sts_get_caller_identity = dict
    else:
        clients["elbv2"].meta.region_name = "us-east-1"
    skipped = run(db, world, "status", clients=clients, transition_id=tid)
    assert skipped["success"], skipped
    assert not all(check["passed"] for check in skipped["live"]["session"])
    (entry,) = skipped["reconciliation"]
    assert "세션" in entry["skipped"]
    assert skipped["unresolved_operations"] == 1
    (op,) = skipped["journal"]
    assert op["reconciled_state"] is None and op["unresolved"]

    world.__dict__.pop("sts_get_caller_identity", None)
    blocked = run(db, world, "prepare", transition_id=tid)
    assert blocked["error"] == "unresolved_operation"
    assert len(world.mutations("create_target_group")) == 1
    observed = run(db, world, "status", transition_id=tid)
    assert observed["reconciliation"][0]["reconciled_state"] == "EFFECT_OBSERVED"


def test_listener_binding_opened_before_the_plan_lock_stores_no_losing_plan(
    db, scene, monkeypatch
):
    """plan이 읽은 뒤·잠금을 잡기 전에 다른 실행의 시도가 같은 운영 리스너를 연다 — 결속을 잠금
    아래에서 다시 읽으므로 진 쪽 계획·관측을 저장하지 않는다."""
    from db import models
    from db.repositories import service_transitions as repo

    first = scene()
    first_tid = _planned(db, first)
    second = scene()
    real_claim = repo.mutation_claim
    opened: list[bool] = []

    @contextmanager
    def claim_after_first_opens(session, identities):
        if not opened:
            opened.append(True)  # second의 plan이 읽기를 마치고 잠금을 잡기 직전
            assert run(db, first, "prepare", transition_id=first_tid)["success"]
        with real_claim(session, identities) as held:
            yield held

    monkeypatch.setattr(repo, "mutation_claim", claim_after_first_opens)
    lost = run(
        db, second, "plan", execution_id=second.execution_id, options=plan_options()
    )
    assert opened
    assert lost["error"] == "listener_bound" and first_tid in lost["detail"], lost
    assert second.mutations() == [] and len(first.mutations()) == 3
    for model in (models.ServiceTransition, models.ServiceTransitionObservation):
        assert db.query(model).filter_by(execution_id=second.execution_id).count() == 0

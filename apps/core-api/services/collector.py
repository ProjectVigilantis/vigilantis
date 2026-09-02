# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 자산/메트릭 수집기입니다. 단일 계정·1~2개 리전의 EC2/SG 인벤토리와 CloudWatch
# (CPU/Network) 지표를 수집·정형화합니다. (구 scan-worker 흡수)
#
# 구현 범위(현재): 수집 + packages/schemas.assets 로의 Pydantic 정형화까지.
#   - EC2/SG 인벤토리 수집 (describe_instances / describe_security_groups / ENI)
#   - CloudWatch CPU/Network 메트릭을 get_metric_data 로 배치 조회 후 ARN 키로 정형화
#   - 결과를 schemas.AssetInventory(Pydantic) 로 반환 → rule_engine 파이프라인 입력
#
# 아직 하지 않는 것(후속):
#   - DB 적재: 안성일의 db.models.Asset/SpecSnapshot 확정 후 연결 (TODO: persist)
#   - 판정: Idle/미사용/Skip 사유는 rule_engine 의 몫 (여기선 정형화까지만)
#
# 설정 주입: 리전·엔드포인트·자격증명 해석과 클라이언트 생성은 services/aws/client.py
#   가 단일 원천이다(ADR-0006 §3, Issue #128). 여기서는 수집 창 설정만 합친다.
# ==============================================================================

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from botocore.exceptions import BotoCoreError, ClientError

from config import get_collector_settings
from schemas.assets import (
    AlbTargetGroupAsset,
    AssetInventory,
    AutoScalingGroupAsset,
    EbsAsset,
    Ec2Asset,
    LaunchTemplateAsset,
    MetricName,
    MetricSeries,
    MetricSummary,
    NaclAsset,
    OpenPort,
    SecurityGroupAsset,
)

from .aws.client import account_id as _account_id
from .aws.client import aws_client, deployment_mode, regions

_log = logging.getLogger(__name__)

_METRIC_NAMES = (MetricName.CPU_UTILIZATION, MetricName.NETWORK_IN, MetricName.NETWORK_OUT)
# get_metric_data 는 호출당 최대 500 쿼리를 받지만 응답 안정성을 위해 보수적으로 끊는다.
_QUERY_BATCH = 100


def _failure_reason(exc: BaseException) -> str:
    """degrade 사유를 사람이 읽을 짧은 코드로. ClientError 는 AWS 오류 코드
    (InternalFailure·AccessDenied·Throttling 등), 그 외는 예외 클래스명."""
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") or "ClientError"
    return type(exc).__name__


def _failures_summary(failures: dict[str, str]) -> str:
    """collector_failures 를 error_summary(String(1024))에 실을 compact JSON 으로.
    상한 초과 시 문자열을 자르면 JSON 이 깨지므로, 항목을 버리고 표식만 남겨 유효 JSON 을
    유지한다(라벨은 4종뿐이라 실제 도달은 불가하나 계약(json.loads)을 안전하게 지킨다)."""
    text = json.dumps(failures, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text) <= 1024:
        return text
    return json.dumps({"_truncated": str(len(failures))}, ensure_ascii=False, separators=(",", ":"))


# 리전 단위 재시도 대상 — 일시성 오류만. AccessDenied·InternalFailure 등 비재시도성은 즉시 실패로.
_RETRYABLE_CLIENT_CODES = (
    "Throttling", "ThrottlingException", "RequestLimitExceeded",
    "ServiceUnavailable", "RequestTimeout", "InternalError",
)
_RETRYABLE_BOTOCORE = (
    "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
    "ConnectionError", "ConnectionClosedError",
)


def _is_retryable(exc: BaseException) -> bool:
    """리전 단위 재시도 대상인지. botocore adaptive(max 5)가 이미 도는 계층 위이므로
    대상을 일시성(스로틀·서비스 불가·연결 계열)으로 좁힌다 — 나머지는 재시도해도 결과가 같다."""
    if isinstance(exc, ClientError):
        return _failure_reason(exc) in _RETRYABLE_CLIENT_CODES
    return type(exc).__name__ in _RETRYABLE_BOTOCORE


def _safe_describe(fn, label: str, failures: dict[str, str]) -> list:
    """describe 호출 1건을 시도하고, AWS 오류면 빈 목록으로 degrade 하며 `failures[label]`에
    사유 코드를 남긴다(자산 단위 실패 사유, C4).

    목적은 부분 실패 시 나머지 수집을 살리는 것이다. autoscaling·elbv2 는 LocalStack
    Community 미포함(ADR-0006 §4)이라 로컬에서 `InternalFailure`(ClientError)가 나는데,
    그 실패가 EC2/SG/EBS/NACL 등 나머지 수집까지 무너뜨리면 안 되므로 여기서 흡수한다.
    환경(LocalStack 여부)을 보고 분기하지 않고 '호출은 시도하되 실패를 잡아 강등'하는
    방식이라 ADR-0006 §3(코드 분기 금지)에 저촉되지 않는다.

    실 AWS 의 AccessDenied·Throttling 도 같은 ClientError 라 함께 흡수되므로, '정상 0건'과
    구별하려고 실패 라벨·사유를 모은다 — persist 가 이를 error_summary(JSON)에 싣고 PARTIAL
    로 마감한다(라우터가 PARTIAL → collection_status=PARTIAL 로 표면화). 실 검증은 스모크(§4).
    """
    try:
        return fn()
    except (ClientError, BotoCoreError) as exc:
        reason = _failure_reason(exc)
        _log.warning("자산 수집 degrade — %s 조회 실패(%s): %s", label, reason, exc)
        failures[label] = reason
        return []


def _paginate(client, operation_name: str, key: str) -> list:
    """페이지네이션 describe 를 전 페이지 순회해 key 리스트를 평탄화한다.

    단일 호출은 첫 페이지만 받아 계정에 자산이 많으면 뒤 페이지가 조용히 누락된다.
    describe_instances(collect_region)와 동일하게 paginator 로 전량 수집한다.
    """
    return [
        item
        for page in client.get_paginator(operation_name).paginate()
        for item in page[key]
    ]


# ------------------------------------------------------------------ 설정
def _runtime_config() -> dict:
    """수집 1회에 필요한 설정. 리전은 클라이언트 팩토리가 해석한 목록을 그대로 쓴다."""
    settings = get_collector_settings()
    return {
        "regions": regions(),
        "lookback_days": settings.METRIC_LOOKBACK_DAYS,
        "period_seconds": settings.METRIC_PERIOD_SECONDS,
    }


def _arn(resource_type: str, resource_id: str, region: str, account_id: str) -> str:
    """가드레일 3단계(ARN Match)가 이 문자열을 그대로 비교하므로 포맷을 반드시 고정한다.
    예) arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc123"""
    return f"arn:aws:ec2:{region}:{account_id}:{resource_type}/{resource_id}"


# ------------------------------------------------------------------ 정형화 헬퍼
def _name_tag(tags: list[dict]) -> str | None:
    return next((t["Value"] for t in tags or [] if t["Key"] == "Name"), None)


def _open_to_world(sg: dict) -> list[OpenPort]:
    """0.0.0.0/0 또는 ::/0 로 열린 인그레스만 추린다(22/tcp, 3389/tcp 위협 탐지용)."""
    ports: list[OpenPort] = []
    for perm in sg.get("IpPermissions", []):
        world_v4 = any(r.get("CidrIp") == "0.0.0.0/0" for r in perm.get("IpRanges", []))
        world_v6 = any(r.get("CidrIpv6") == "::/0" for r in perm.get("Ipv6Ranges", []))
        if not (world_v4 or world_v6):
            continue
        proto = perm.get("IpProtocol")
        ports.append(
            OpenPort(
                protocol="all" if proto == "-1" else proto,
                from_port=perm.get("FromPort"),
                to_port=perm.get("ToPort"),
                ipv6=world_v6,
            )
        )
    return ports


def _used_sg_ids(instances: list[dict], enis: list[dict]) -> set[str]:
    """ENI 에 붙은 SG 를 '사용 중'으로 본다. LocalStack 은 ENI 를 비워둘 때가 있어
    그 경우 인스턴스에 직접 붙은 SG 로 대체 집계한다(check_env 3번과 동일한 fallback)."""
    used = {g["GroupId"] for eni in enis for g in eni.get("Groups", [])}
    if not used and instances:
        used = {g["GroupId"] for i in instances for g in i.get("SecurityGroups", [])}
    return used


def _fetch_metrics(cw, instance_ids: list[str], start: datetime, end: datetime, period: int) -> dict[str, dict[MetricName, MetricSeries]]:
    """인스턴스별 CPU/Network 시계열을 get_metric_data 로 배치 조회.
    실 계정 비용 = 호출 수이므로 단건 반복 대신 배치 조회를 유지한다."""
    queries, ref = [], {}
    for idx, iid in enumerate(instance_ids):
        for m in _METRIC_NAMES:
            qid = f"q{idx}_{m.name.lower()}"
            ref[qid] = (iid, m)
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/EC2",
                            "MetricName": m.value,
                            "Dimensions": [{"Name": "InstanceId", "Value": iid}],
                        },
                        "Period": period,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                }
            )

    out: dict[str, dict[MetricName, MetricSeries]] = {iid: {} for iid in instance_ids}
    for i in range(0, len(queries), _QUERY_BATCH):
        batch = queries[i : i + _QUERY_BATCH]
        token = None
        while True:
            kwargs = dict(MetricDataQueries=batch, StartTime=start, EndTime=end, ScanBy="TimestampAscending")
            if token:
                kwargs["NextToken"] = token
            res = cw.get_metric_data(**kwargs)
            for r in res["MetricDataResults"]:
                iid, m = ref[r["Id"]]
                series = out[iid].get(m)
                if series is None:
                    series = MetricSeries(metric_name=m)
                    out[iid][m] = series
                series.timestamps.extend(r.get("Timestamps", []))
                series.values.extend(r.get("Values", []))
            token = res.get("NextToken")
            if not token:
                break
    return out


def _summarize(series_by_metric: dict[MetricName, MetricSeries]) -> MetricSummary:
    def avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 2) if vals else None

    cpu = series_by_metric.get(MetricName.CPU_UTILIZATION)
    net_in = series_by_metric.get(MetricName.NETWORK_IN)
    net_out = series_by_metric.get(MetricName.NETWORK_OUT)
    cpu_vals = cpu.values if cpu else []
    return MetricSummary(
        cpu_datapoints=len(cpu_vals),
        cpu_avg=avg(cpu_vals),
        cpu_max=round(max(cpu_vals), 2) if cpu_vals else None,
        net_in_avg=avg(net_in.values if net_in else []),
        net_out_avg=avg(net_out.values if net_out else []),
    )


def _reusable_summaries(
    instance_ids: list[str],
    fresh: dict[str, MetricSummary] | None,
) -> dict[str, MetricSummary] | None:
    """이번 회차의 CloudWatch 조회를 건너뛸 수 있으면 재사용할 요약을, 아니면 None 을 준다(#255).

    **전부 아니면 전무다.** get_metric_data 는 인스턴스 전량을 한 번에 배치 조회하므로
    한 대라도 새로 받아야 하면 나머지를 아껴도 호출 수가 줄지 않는다. 부분 재사용은
    같은 회차 안에 창이 다른 요약을 섞어 metric_summaries 의 window 를 못 믿게 만들기만 한다.
    """
    if not instance_ids or not fresh:
        return None
    if any(iid not in fresh for iid in instance_ids):
        return None  # 신규 인스턴스가 있으면 전량 재조회
    return fresh


def _asg_launch_template(g: dict) -> tuple[str | None, str | None]:
    """ASG describe 응답에서 (launch_template_id, name) 을 뽑는다(ASG→LT USES 파생용).
    LaunchTemplate 직접 지정과 MixedInstancesPolicy 두 형태를 모두 본다.
    LaunchConfiguration 만 쓰는 구형 ASG 는 LT 가 없으므로 (None, None)."""
    lt = g.get("LaunchTemplate")
    if not lt:
        lt = (
            g.get("MixedInstancesPolicy", {})
            .get("LaunchTemplate", {})
            .get("LaunchTemplateSpecification")
        )
    if not lt:
        return None, None
    return lt.get("LaunchTemplateId"), lt.get("LaunchTemplateName")


def _is_alb_target_group(tg: dict) -> bool:
    """ALB Target Group 만 선별. describe_target_groups 는 NLB(TCP/UDP/TLS)·GWLB(GENEVE)
    TG 도 반환하는데, 공개 계약의 자산 유형은 ALB_TARGET_GROUP 이다. TG 프로토콜로 가른다
    (ALB=HTTP/HTTPS). lambda 대상 TG 등 프로토콜 없는 TG 도 여기서 제외된다."""
    return tg.get("Protocol") in ("HTTP", "HTTPS")


def _registered_instance_ids(target_health: list) -> list[str]:
    """target health 에서 등록된 EC2 instance id 목록(순서 유지·중복 제거).

    같은 인스턴스가 한 TG 에 여러 포트로 등록되면 describe_target_health 가 중복 반환한다.
    REGISTERED_IN 은 (source, relation, target_arn) 단위 unique 라 중복을 지우지 않으면
    동일 관계가 두 번 INSERT 되어 수집 전체가 롤백된다. instance 대상만(i-xxxx) 취한다."""
    ids = [
        d["Target"]["Id"]
        for d in target_health
        if str(d.get("Target", {}).get("Id", "")).startswith("i-")
    ]
    return list(dict.fromkeys(ids))


# ------------------------------------------------------------------ 공개 API
def collect_region(
    region: str,
    cfg: dict | None = None,
    fresh_metrics: dict[str, MetricSummary] | None = None,
    fresh_window_end: datetime | None = None,
) -> AssetInventory:
    """한 리전의 EC2/SG 인벤토리 + 메트릭을 수집해 AssetInventory 로 정형화한다.

    fresh_metrics 는 아직 유효한(= 메트릭 입자 안에서 수집된) 요약이다. 인스턴스 전량이
    덮이면 CloudWatch 를 건너뛰고 그 값을 그대로 쓴다(#255). DB 는 여기서 읽지 않는다 —
    조회는 호출자(_collect_store_region)가 하고 이 함수는 결과만 받는다.
    """
    cfg = cfg or _runtime_config()

    ec2 = aws_client("ec2", region)
    cw = aws_client("cloudwatch", region)
    account_id = _account_id(region)

    instances_raw = [
        i
        for page in ec2.get_paginator("describe_instances").paginate()
        for r in page["Reservations"]
        for i in r["Instances"]
    ]
    sgs_raw = ec2.describe_security_groups()["SecurityGroups"]
    enis_raw = ec2.describe_network_interfaces()["NetworkInterfaces"]
    nacls_raw = ec2.describe_network_acls()["NetworkAcls"]
    volumes_raw = _paginate(ec2, "describe_volumes", "Volumes")
    # Launch Template 은 ec2(Community 지원). ASG 는 autoscaling(Pro 전용)이라 로컬에선
    # _safe_describe 가 빈 목록으로 degrade 하고 failures 에 (서비스→사유)를 남긴다(ADR-0006 §4, C4).
    failures: dict[str, str] = {}
    lts_raw = _safe_describe(
        lambda: _paginate(ec2, "describe_launch_templates", "LaunchTemplates"),
        "launch_templates", failures,
    )
    asg = aws_client("autoscaling", region)
    asgs_raw = _safe_describe(
        lambda: _paginate(asg, "describe_auto_scaling_groups", "AutoScalingGroups"),
        "auto_scaling_groups", failures,
    )
    # ALB Target Group 도 elbv2(Pro 전용)라 로컬에선 degrade 된다(ADR-0006 §4).
    elbv2 = aws_client("elbv2", region)
    tgs_raw = _safe_describe(
        lambda: _paginate(elbv2, "describe_target_groups", "TargetGroups"),
        "alb_target_groups", failures,
    )
    used = _used_sg_ids(instances_raw, enis_raw)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=cfg["lookback_days"])
    ids = [i["InstanceId"] for i in instances_raw]
    reuse = _reusable_summaries(ids, fresh_metrics)
    # 재사용 시 시계열은 받지 않는다 — 원자료를 쓰는 곳은 _summarize 뿐이고 그 결과를
    # 그대로 물려받기 때문이다(Ec2Asset.metrics 를 읽는 소비자는 없다).
    metrics: dict[str, dict[MetricName, MetricSeries]] = {}
    if reuse is not None:
        _log.info(
            "리전 %s 메트릭 재사용 — CloudWatch 조회 생략(인스턴스 %d대, 창 끝 %s)",
            region, len(ids), fresh_window_end,
        )
    elif ids:
        metrics = _fetch_metrics(cw, ids, start, end, cfg["period_seconds"])

    ec2_assets: list[Ec2Asset] = []
    for i in instances_raw:
        iid = i["InstanceId"]
        series = metrics.get(iid, {})
        ec2_assets.append(
            Ec2Asset(
                arn=_arn("instance", iid, region, account_id),
                instance_id=iid,
                name=_name_tag(i.get("Tags", [])),
                instance_type=i.get("InstanceType"),
                state=i.get("State", {}).get("Name"),
                region=region,
                availability_zone=i.get("Placement", {}).get("AvailabilityZone"),
                vpc_id=i.get("VpcId"),
                subnet_id=i.get("SubnetId"),
                private_ip=i.get("PrivateIpAddress"),
                launch_time=i.get("LaunchTime"),
                security_group_ids=[g["GroupId"] for g in i.get("SecurityGroups", [])],
                tags={t["Key"]: t["Value"] for t in i.get("Tags", [])},
                metrics=series,
                metric_summary=reuse[iid] if reuse else _summarize(series),
            )
        )

    sg_assets = [
        SecurityGroupAsset(
            arn=_arn("security-group", sg["GroupId"], region, account_id),
            group_id=sg["GroupId"],
            name=sg.get("GroupName"),
            description=sg.get("Description"),
            region=region,
            vpc_id=sg.get("VpcId"),
            attached=sg["GroupId"] in used,
            open_to_world=_open_to_world(sg),
        )
        for sg in sgs_raw
    ]

    nacl_assets = [
        NaclAsset(
            arn=_arn("network-acl", n["NetworkAclId"], region, account_id),
            nacl_id=n["NetworkAclId"],
            region=region,
            vpc_id=n.get("VpcId"),
            is_default=bool(n.get("IsDefault", False)),
            associated_subnet_ids=[
                a["SubnetId"] for a in n.get("Associations", []) if a.get("SubnetId")
            ],
        )
        for n in nacls_raw
    ]

    ebs_assets = [
        EbsAsset(
            arn=_arn("volume", v["VolumeId"], region, account_id),
            volume_id=v["VolumeId"],
            region=region,
            volume_type=v.get("VolumeType"),
            size_gib=v.get("Size"),
            availability_zone=v.get("AvailabilityZone"),
            encrypted=v.get("Encrypted"),
            state=v.get("State"),
            attached_instance_ids=[
                att["InstanceId"] for att in v.get("Attachments", []) if att.get("InstanceId")
            ],
        )
        for v in volumes_raw
    ]

    lt_assets = [
        LaunchTemplateAsset(
            arn=_arn("launch-template", lt["LaunchTemplateId"], region, account_id),
            launch_template_id=lt["LaunchTemplateId"],
            name=lt.get("LaunchTemplateName"),
            region=region,
            latest_version=lt.get("LatestVersionNumber"),
            default_version=lt.get("DefaultVersionNumber"),
        )
        for lt in lts_raw
    ]

    asg_assets = []
    for g in asgs_raw:
        lt_id, lt_name = _asg_launch_template(g)
        asg_assets.append(
            AutoScalingGroupAsset(
                arn=g["AutoScalingGroupARN"],
                name=g["AutoScalingGroupName"],
                region=region,
                min_size=g["MinSize"],
                max_size=g["MaxSize"],
                desired_capacity=g["DesiredCapacity"],
                health_check_type=g.get("HealthCheckType"),
                instance_ids=[i["InstanceId"] for i in g.get("Instances", [])],
                launch_template_id=lt_id,
                launch_template_name=lt_name,
            )
        )

    tg_assets = []
    for tg in tgs_raw:
        if not _is_alb_target_group(tg):
            continue  # NLB/GWLB/lambda TG 는 ALB_TARGET_GROUP 이 아니다
        tg_arn = tg["TargetGroupArn"]
        # 등록 인스턴스는 describe_target_health(elbv2, Pro 전용)로만 얻는다. TG describe 가
        # 성공한 실 AWS 에서만 호출되며(로컬은 tgs_raw 가 비어 루프 자체가 안 돈다),
        # target_type=instance 의 Target.Id(i-xxxx)만 REGISTERED_IN 대상, 중복 제거.
        health = _safe_describe(
            lambda arn=tg_arn: elbv2.describe_target_health(TargetGroupArn=arn)["TargetHealthDescriptions"],
            "alb_target_health", failures,
        )
        target_instance_ids = _registered_instance_ids(health)
        tg_assets.append(
            AlbTargetGroupAsset(
                arn=tg_arn,
                name=tg["TargetGroupName"],
                region=region,
                protocol=tg.get("Protocol"),
                port=tg.get("Port"),
                target_type=tg.get("TargetType"),
                health_check_path=tg.get("HealthCheckPath"),
                target_instance_ids=target_instance_ids,
            )
        )

    return AssetInventory(
        account_id=account_id,
        region=region,
        mode=deployment_mode(),
        lookback_days=cfg["lookback_days"],
        period_seconds=cfg["period_seconds"],
        metrics_window_end=fresh_window_end if reuse else None,
        ec2_instances=ec2_assets,
        security_groups=sg_assets,
        nacls=nacl_assets,
        ebs_volumes=ebs_assets,
        launch_templates=lt_assets,
        auto_scaling_groups=asg_assets,
        alb_target_groups=tg_assets,
        collector_failures=failures,  # degraded_collectors 는 여기서 파생(computed_field)
    )


def collect() -> list[AssetInventory]:
    """설정된 모든 리전을 수집한다(정형화까지). DB 적재 없이 결과만 필요할 때 사용."""
    cfg = _runtime_config()
    return [collect_region(region, cfg) for region in cfg["regions"]]


# ------------------------------------------------------------------ DB 적재
def persist_inventory(inv: AssetInventory, db, collection_run_id: str | None = None) -> dict:
    """AssetInventory 를 DB(CollectionRun, Asset, MetricSummary, AssetRelationship)에 적재한다.
    Repository는 commit하지 않으므로 호출부에서 트랜잭션을 관리한다.
    """
    from datetime import timedelta

    from db.repositories import assets as assets_repo
    from schemas.api.assets import AssetType, RelationType
    from schemas.collections import CollectionRunStatus

    started_own_run = False
    if collection_run_id is None:
        run = assets_repo.start_collection_run(
            db,
            account_id=inv.account_id,
            region=inv.region,
            mode=inv.mode,
            lookback_days=inv.lookback_days,
            period_seconds=inv.period_seconds,
        )
        collection_run_id = run.collection_run_id
        started_own_run = True

    ec2_count = len(inv.ec2_instances)
    sg_count = len(inv.security_groups)
    nacl_count = len(inv.nacls)
    ebs_count = len(inv.ebs_volumes)
    lt_count = len(inv.launch_templates)
    asg_count = len(inv.auto_scaling_groups)
    tg_count = len(inv.alb_target_groups)
    total = ec2_count + sg_count + nacl_count + ebs_count + lt_count + asg_count + tg_count

    # subnet → NACL ARN (EC2→NACL PROTECTED_BY 파생용)
    subnet_to_nacl = {
        subnet_id: n.arn
        for n in inv.nacls
        for subnet_id in n.associated_subnet_ids
    }

    # instance_id → [Volume ARN] (EC2→EBS ATTACHED_TO 파생용)
    instance_to_volumes: dict[str, list[str]] = {}
    for v in inv.ebs_volumes:
        for iid in v.attached_instance_ids:
            instance_to_volumes.setdefault(iid, []).append(v.arn)

    # instance_id → ASG ARN (EC2→ASG MEMBER_OF 파생용). 인스턴스는 최대 1개 ASG 소속.
    instance_to_asg = {
        iid: g.arn for g in inv.auto_scaling_groups for iid in g.instance_ids
    }

    # instance_id → [TG ARN] (EC2→ALB TG REGISTERED_IN 파생용). 한 인스턴스가 여러 TG 등록 가능.
    instance_to_tgs: dict[str, list[str]] = {}
    for tg in inv.alb_target_groups:
        for iid in tg.target_instance_ids:
            instance_to_tgs.setdefault(iid, []).append(tg.arn)

    # 재사용 요약은 이번 회차가 아니라 원본 창을 적는다 — 안 받은 구간을 관측한 것처럼
    # 남기지 않으려는 것이다(#255). 직접 조회했으면 metrics_window_end 가 None 이다.
    window_end = inv.metrics_window_end or inv.collected_at
    window_start = window_end - timedelta(days=inv.lookback_days)

    # 1. EC2 적재
    for a in inv.ec2_instances:
        ec2_spec = {
            "instance_type": a.instance_type,
            "availability_zone": a.availability_zone,
            "vpc_id": a.vpc_id,
            "subnet_id": a.subnet_id,
            "private_ip": a.private_ip,
            "tags": a.tags or {},
        }
        asset = assets_repo.upsert_asset(
            db,
            arn=a.arn,
            asset_type=AssetType.EC2,
            resource_id=a.instance_id,
            account_id=inv.account_id,
            region=inv.region,
            spec=ec2_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=a.name,
            state=a.state,
        )
        assets_repo.add_metric_summary(
            db,
            asset_id=asset.asset_id,
            collection_run_id=collection_run_id,
            summary=a.metric_summary,
            window_start=window_start,
            window_end=window_end,
            collected_at=inv.collected_at,
        )
        # SG(SECURED_BY) + NACL(PROTECTED_BY) + EBS(ATTACHED_TO) 를 한 번에 교체(replace 는 덮어쓰기)
        rel_items = [
            (RelationType.SECURED_BY, f"arn:aws:ec2:{inv.region}:{inv.account_id}:security-group/{sg_id}")
            for sg_id in a.security_group_ids
        ]
        nacl_arn = subnet_to_nacl.get(a.subnet_id)
        if nacl_arn:
            rel_items.append((RelationType.PROTECTED_BY, nacl_arn))
        for vol_arn in instance_to_volumes.get(a.instance_id, []):
            rel_items.append((RelationType.ATTACHED_TO, vol_arn))
        asg_arn = instance_to_asg.get(a.instance_id)
        if asg_arn:
            rel_items.append((RelationType.MEMBER_OF, asg_arn))
        for tg_arn in instance_to_tgs.get(a.instance_id, []):
            rel_items.append((RelationType.REGISTERED_IN, tg_arn))
        # (relation, target_arn) 중복 제거 — 동일 관계 두 번 INSERT 시 unique constraint 위반 방어
        rel_items = list(dict.fromkeys(rel_items))
        if rel_items:
            assets_repo.replace_relationships(
                db,
                source_asset_id=asset.asset_id,
                items=rel_items,
                collection_run_id=collection_run_id,
            )

    # 2. SG 적재
    for g in inv.security_groups:
        sg_spec = {
            "description": g.description,
            "vpc_id": g.vpc_id,
            "attached": g.attached,
            "open_to_world": [p.model_dump(mode="json") for p in g.open_to_world],
        }
        assets_repo.upsert_asset(
            db,
            arn=g.arn,
            asset_type=AssetType.SG,
            resource_id=g.group_id,
            account_id=inv.account_id,
            region=inv.region,
            spec=sg_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=g.name,
            state=None,
        )

    # 3. NACL 적재 (판정 비대상 — role/status 파생은 조회단이 처리)
    for n in inv.nacls:
        nacl_spec = {
            "vpc_id": n.vpc_id,
            "is_default": n.is_default,
            "associated_subnet_ids": n.associated_subnet_ids,
        }
        assets_repo.upsert_asset(
            db,
            arn=n.arn,
            asset_type=AssetType.NACL,
            resource_id=n.nacl_id,
            account_id=inv.account_id,
            region=inv.region,
            spec=nacl_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=None,
            state=None,
        )

    # 4. EBS 적재 (판정 대상 — verdict/status 는 rule_engine 이 매긴다)
    for v in inv.ebs_volumes:
        ebs_spec = {
            "volume_type": v.volume_type,
            "size_gib": v.size_gib,
            "availability_zone": v.availability_zone,
            "encrypted": v.encrypted,
            "attached_instance_ids": v.attached_instance_ids,
        }
        assets_repo.upsert_asset(
            db,
            arn=v.arn,
            asset_type=AssetType.EBS,
            resource_id=v.volume_id,
            account_id=inv.account_id,
            region=inv.region,
            spec=ebs_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=None,
            state=v.state,
        )

    # 5. Launch Template 적재 (판정 비대상)
    for lt in inv.launch_templates:
        lt_spec = {
            "latest_version": lt.latest_version,
            "default_version": lt.default_version,
        }
        assets_repo.upsert_asset(
            db,
            arn=lt.arn,
            asset_type=AssetType.LAUNCH_TEMPLATE,
            resource_id=lt.launch_template_id,
            account_id=inv.account_id,
            region=inv.region,
            spec=lt_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=lt.name,
            state=None,
        )

    # 6. ASG 적재 (판정 비대상) + ASG→LT(USES) 관계. USES 는 source 가 ASG 라
    #    EC2 관계 루프가 아니라 여기서 산출한다.
    for g in inv.auto_scaling_groups:
        asg_spec = {
            "min_size": g.min_size,
            "max_size": g.max_size,
            "desired_capacity": g.desired_capacity,
            "health_check_type": g.health_check_type,
        }
        asset = assets_repo.upsert_asset(
            db,
            arn=g.arn,
            asset_type=AssetType.AUTO_SCALING_GROUP,
            resource_id=g.name,
            account_id=inv.account_id,
            region=inv.region,
            spec=asg_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=g.name,
            state=None,
        )
        # USES 는 스냅샷 의미론(source 관계 전량 교체)이라 조건 밖에서 호출한다.
        # LT 를 떼어낸 ASG 는 items=[] 로 이전 수집의 stale USES 엣지가 지워진다.
        lt_items = (
            [(RelationType.USES, _arn("launch-template", g.launch_template_id, inv.region, inv.account_id))]
            if g.launch_template_id
            else []
        )
        assets_repo.replace_relationships(
            db,
            source_asset_id=asset.asset_id,
            items=lt_items,
            collection_run_id=collection_run_id,
        )

    # 7. ALB Target Group 적재 (판정 비대상)
    for tg in inv.alb_target_groups:
        tg_spec = {
            "protocol": tg.protocol,
            "port": tg.port,
            "target_type": tg.target_type,
            "health_check_path": tg.health_check_path,
        }
        assets_repo.upsert_asset(
            db,
            arn=tg.arn,
            asset_type=AssetType.ALB_TARGET_GROUP,
            resource_id=tg.name,
            account_id=inv.account_id,
            region=inv.region,
            spec=tg_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=tg.name,
            state=None,
        )

    if started_own_run:
        # degrade(빈 목록으로 흡수된 수집 실패)가 한 번이라도 있으면 PARTIAL 로 마감하고,
        # 자산 단위 실패 사유를 error_summary(JSON)에 싣는다(C4). run 단위 1문장이 아니라
        # {서비스: 사유} 지도라 화면·조회에서 무엇이 왜 빠졌는지 판별된다.
        run_status = (
            CollectionRunStatus.PARTIAL if inv.collector_failures else CollectionRunStatus.SUCCESS
        )
        error_summary = (
            _failures_summary(inv.collector_failures) if inv.collector_failures else None
        )
        assets_repo.finish_collection_run(
            db,
            collection_run_id=collection_run_id,
            status=run_status,
            finished_at=inv.collected_at,
            error_summary=error_summary,
        )

    return {
        "region": inv.region,
        "collection_run_id": collection_run_id,
        "ec2_count": ec2_count,
        "sg_count": sg_count,
        "nacl_count": nacl_count,
        "ebs_count": ebs_count,
        "lt_count": lt_count,
        "asg_count": asg_count,
        "tg_count": tg_count,
        "total": total,
        "degraded_collectors": list(inv.degraded_collectors),
        "collector_failures": dict(inv.collector_failures),
    }


def collect_and_store() -> list[dict]:
    """수집 → 정형화 → DB 적재까지. scheduler 가 이 함수를 주기 호출한다.
    (판정은 이후 rule_engine 이 assets 테이블 및 metric_summaries 를 읽어 수행)

    C4: 리전을 독립 트랜잭션으로 처리한다 — 한 리전이 실패해도 다른 리전은 커밋된다
    (기존엔 한 리전 예외 = 전체 롤백). 실패 리전은 1회 재시도 후 FAILED 로 기록한다.
    """
    from db.session import get_session_factory

    cfg = _runtime_config()
    session_factory = get_session_factory()
    return [_collect_store_region(region, cfg, session_factory) for region in cfg["regions"]]


def _collect_store_region(region: str, cfg: dict, session_factory) -> dict:
    """한 리전을 독립 트랜잭션으로 수집·적재. core describe 가 일시 오류로 실패하면 1회
    재시도하고, 그래도 실패하면 그 리전만 FAILED 로 기록한 뒤 예외를 삼켜 다음 리전이 계속되게 한다."""
    from db.repositories import assets as assets_repo

    db = session_factory()
    try:
        # 스캔 주기가 메트릭 입자보다 짧으면 같은 입자를 반복 조회하게 된다(#255).
        # 입자 안에서 이미 받아 둔 요약이 있으면 그것으로 대신한다.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=cfg["period_seconds"])
        fresh_window_end, fresh_metrics = assets_repo.fresh_ec2_metric_summaries(
            db, region=region, not_older_than=cutoff
        )
        try:
            inv = collect_region(region, cfg, fresh_metrics, fresh_window_end)
        except (ClientError, BotoCoreError) as exc:
            if not _is_retryable(exc):
                raise  # 비재시도성(AccessDenied·InternalFailure 등)은 즉시 실패로
            _log.warning("리전 %s 수집 일시 실패 — 1회 재시도(%s)", region, _failure_reason(exc))
            inv = collect_region(region, cfg, fresh_metrics, fresh_window_end)
        summary = persist_inventory(inv, db)
        db.commit()
        return summary
    except Exception as exc:  # 리전 격리 — 이 리전만 실패로 마감하고 다른 리전은 계속
        db.rollback()
        # 코드 버그(KeyError 등)도 여기서 삼키므로 스택트레이스를 남긴다(_log.exception).
        _log.exception("리전 %s 수집 최종 실패 — FAILED 로 기록하고 계속(%s)", region, _failure_reason(exc))
        _record_failed_region(region, cfg, exc, session_factory)
        return {"region": region, "status": "FAILED", "error": _failure_reason(exc)}
    finally:
        db.close()


def _record_failed_region(region: str, cfg: dict, exc: BaseException, session_factory) -> None:
    """실패한 리전을 FAILED CollectionRun 으로 남긴다(다른 리전과 격리된 별도 트랜잭션).
    기록 자체가 실패해도 파이프라인을 막지 않는다."""
    from db.repositories import assets as assets_repo
    from schemas.collections import CollectionRunStatus

    db = session_factory()
    try:
        # 실패 경로를 짧게 — STS(GetCallerIdentity) 재호출 안 하고 account 는 unknown 으로 둔다
        # (계정은 CollectionRun.region 과 함께 이미 성공 리전 run 에서 확인 가능).
        run = assets_repo.start_collection_run(
            db,
            account_id="unknown",
            region=region,
            mode=deployment_mode(),
            lookback_days=cfg["lookback_days"],
            period_seconds=cfg["period_seconds"],
        )
        assets_repo.finish_collection_run(
            db,
            collection_run_id=run.collection_run_id,
            status=CollectionRunStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
            # error_summary 키 축을 PARTIAL(서비스 라벨)과 통일 — 실패 단계 라벨. 리전은 run.region 이 담는다.
            error_summary=_failures_summary({"collect_region": _failure_reason(exc)}),
        )
        db.commit()
    except Exception:
        db.rollback()
        _log.exception("리전 %s FAILED 기록 실패 — 흔적 없이 유실 방지 로그", region)
    finally:
        db.close()


"""executor.precheck() 단위 테스트 (Issue #129, ADR-0007).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 판정만 검증한다.
LocalStack 실제 대상 검증은 test_precheck_localstack.py가 맡는다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.precheck import PrecheckOutcome, PrecheckReasonCode  # noqa: E402
from schemas.runbooks import ALLOWED_RUNBOOK_IDS, ROLLBACK_RUNBOOK_IDS  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
GROUP = "sg-0abc123456789def0"
ACL = "acl-0abc123456789def0"
VOLUME = "vol-0abc123456789def0"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/vigilantis/abc"


def arn(resource_type: str, resource_id: str, account: str = ACCOUNT) -> str:
    return f"arn:aws:ec2:{REGION}:{account}:{resource_type}/{resource_id}"


# 런북별 (target_arn, 유효 파라미터) — ADR-0007 §5 필수 키 표와 1:1이다
VALID = {
    "RUNBOOK_EC2_ISOLATE": (
        arn("instance", INSTANCE),
        {
            "instance_id": INSTANCE,
            "target_group_arn": TG_ARN,
            "isolation_group_id": GROUP,
            "evidence_id": "ev-1",
        },
    ),
    "RUNBOOK_NACL_ADD_DENY": (
        arn("network-acl", ACL),
        {
            "network_acl_id": ACL,
            # 아래 describe_network_acls 기본 응답에 없는 번호여야 한다
            "rule_number": 200,
            "cidr_block": "203.0.113.5/32",
            "protocol": "-1",
            "evidence_id": "ev-1",
        },
    ),
    "RUNBOOK_NACL_RESTORE": (
        arn("network-acl", ACL),
        {
            "network_acl_id": ACL,
            "rule_number": 100,
            "egress": False,
            "evidence_id": "ev-1",
        },
    ),
    "RUNBOOK_SG_DELETE_ISOLATED": (
        arn("security-group", GROUP),
        {"group_id": GROUP, "evidence_id": "ev-1"},
    ),
    "RUNBOOK_EC2_RIGHTSIZING": (
        arn("instance", INSTANCE),
        {
            "instance_id": INSTANCE,
            "current_instance_type": "t3.xlarge",
            "target_instance_type": "t3.medium",
            "evidence_id": "ev-1",
        },
    ),
    "RUNBOOK_EC2_ENABLE_AUTOSCALING": (
        arn("instance", INSTANCE),
        {"instance_id": INSTANCE, "min_size": 1, "max_size": 4, "evidence_id": "ev-1"},
    ),
    "RUNBOOK_EBS_DELETE_UNATTACHED": (
        arn("volume", VOLUME),
        {"volume_id": VOLUME, "evidence_id": "ev-1"},
    ),
    "RUNBOOK_EC2_UNISOLATE": (
        arn("instance", INSTANCE),
        {"instance_id": INSTANCE, "backup_record_id": "bk-1", "evidence_id": "ev-1"},
    ),
    "RUNBOOK_SG_RECREATE": (
        arn("security-group", GROUP),
        {"backup_record_id": "bk-1", "evidence_id": "ev-1"},
    ),
    "RUNBOOK_EC2_REVERT_SIZE": (
        arn("instance", INSTANCE),
        {"instance_id": INSTANCE, "backup_record_id": "bk-1", "evidence_id": "ev-1"},
    ),
}

# 런북별 백업 레코드 payload — 스펙 JSON 백업 모듈이 만들어야 할 모양이다
BACKUP_PAYLOADS = {
    "RUNBOOK_NACL_RESTORE": (ex.BACKUP_NACL_RULE_INDEX, {"rule_number": 100, "egress": False}),
    "RUNBOOK_EC2_UNISOLATE": (
        ex.BACKUP_SG_AND_TG_MAPPING,
        {"security_group_ids": [GROUP], "target_group_arn": TG_ARN},
    ),
    "RUNBOOK_SG_RECREATE": (
        ex.BACKUP_SG_FULL_RULES,
        {
            "group_name": "restored",
            "description": "restored by vigilantis",
            "vpc_id": "vpc-0abc123456789def0",
            "ingress_permissions": [],
            "egress_permissions": [],
        },
    ),
    "RUNBOOK_EC2_REVERT_SIZE": (ex.BACKUP_INSTANCE_SPEC, {"instance_type": "t3.xlarge"}),
}


# ------------------------------------------------------------------ 가짜 AWS
def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "테스트"}}, "TestOperation")


DRY_RUN_OK = "__dry_run_ok__"

# 통과 경로에서 각 조회가 돌려줄 기본 응답
DEFAULT_RESPONSES = {
    "describe_instances": {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": INSTANCE,
                        "InstanceType": "t3.xlarge",
                        "State": {"Name": "running"},
                        "VpcId": "vpc-0abc123456789def0",
                        "NetworkInterfaces": [{"NetworkInterfaceId": "eni-0abc123456789def0"}],
                    }
                ]
            }
        ]
    },
    "describe_security_groups": {"SecurityGroups": [{"GroupId": GROUP}]},
    "describe_network_acls": {
        "NetworkAcls": [
            {
                "NetworkAclId": ACL,
                "Entries": [
                    {"RuleNumber": 100, "Egress": False, "RuleAction": "deny"},
                    {"RuleNumber": 32767, "Egress": False, "RuleAction": "allow"},
                ],
            }
        ]
    },
    "describe_target_health": {"TargetHealthDescriptions": [{"Target": {"Id": INSTANCE}}]},
    "describe_target_groups": {"TargetGroups": [{"VpcId": "vpc-0abc123456789def0"}]},
    "describe_auto_scaling_groups": {"AutoScalingGroups": []},
}


class FakeClient:
    """호출을 기록하고 지정된 응답·예외를 돌려주는 boto3 클라이언트 대역."""

    def __init__(self, overrides, calls):
        self._overrides = overrides
        self.calls = calls

    def __getattr__(self, operation):
        def call(**kwargs):
            self.calls.append((operation, kwargs))
            outcome = self._overrides.get(operation, DEFAULT_RESPONSES.get(operation, DRY_RUN_OK))
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is DRY_RUN_OK:
                # DryRun 성공은 예외로 온다(ADR-0007 §2)
                raise client_error("DryRunOperation")
            return outcome

        return call


@pytest.fixture
def aws(monkeypatch):
    """executor가 쓰는 클라이언트를 가짜로 갈아 끼운다. 기본은 전부 통과 경로."""
    state = {"overrides": {}, "calls": [], "clients": []}

    def factory(service, region=None, **_):
        state["clients"].append((service, region))
        return FakeClient(state["overrides"], state["calls"])

    monkeypatch.setattr(ex, "aws_client", factory)

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    configure.clients = state["clients"]
    return configure


class Loader:
    """BackupRecordLoader 대역. 레코드 하나만 들고 있다."""

    def __init__(self, record):
        self.record = record
        self.match_calls = []

    def get(self, backup_record_id):
        if self.record and self.record.backup_record_id == backup_record_id:
            return self.record
        return None

    def latest_for_target(self, target_arn, backup_type, payload_match=None):
        self.match_calls.append(payload_match)
        record = self.record
        if not (
            record
            and record.target_arn == target_arn
            and record.backup_type == backup_type
        ):
            return None
        if payload_match and any(
            record.payload.get(key) != value for key, value in payload_match.items()
        ):
            return None
        return record


def loader_for(runbook_id: str, *, payload=None, target_arn=None, backup_type=None):
    declared_type, declared_payload = BACKUP_PAYLOADS[runbook_id]
    return Loader(
        ex.BackupRecordView(
            backup_record_id="bk-1",
            target_arn=target_arn or VALID[runbook_id][0],
            backup_type=backup_type or declared_type,
            payload=payload if payload is not None else declared_payload,
        )
    )


def run(runbook_id: str, *, params=None, target_arn=None, loader=None, **loader_kwargs):
    """유효 입력을 기본값으로 두고 필요한 축만 바꿔 호출한다."""
    default_arn, default_params = VALID[runbook_id]
    if loader is None and runbook_id in BACKUP_PAYLOADS:
        loader = loader_for(runbook_id, **loader_kwargs)
    return ex.precheck(
        runbook_id,
        target_arn if target_arn is not None else default_arn,
        default_params if params is None else params,
        backup_loader=loader,
    )


# ------------------------------------------------------------------ 디스패치
def test_every_whitelisted_runbook_is_implemented():
    """확정 10종에 빈칸이 없어야 한다 — 하나라도 비면 그 런북은 실행 경로에 못 든다."""
    assert ex.IMPLEMENTED_RUNBOOK_IDS == ALLOWED_RUNBOOK_IDS
    assert set(VALID) == ALLOWED_RUNBOOK_IDS


def test_every_spec_has_a_handler():
    for spec in ex.RUNBOOK_SPECS.values():
        assert spec.handler in ex._HANDLERS


def test_unknown_runbook_is_not_implemented():
    outcome = ex.precheck("RUNBOOK_NOT_REAL", arn("instance", INSTANCE), {})
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_NOT_IMPLEMENTED)


def test_rollback_runbooks_are_dispatched_too():
    """롤백 3종은 AI 추천 대상이 아닐 뿐 실행 Whitelist에는 있다(ADR-0004 정책 ①)."""
    assert ROLLBACK_RUNBOOK_IDS <= ex.IMPLEMENTED_RUNBOOK_IDS


@pytest.mark.parametrize("runbook_id", sorted(VALID))
def test_valid_input_passes(runbook_id, aws):
    outcome = run(runbook_id)
    assert outcome.passed, f"{runbook_id}: {outcome.verification_summary}"
    assert outcome.verification_summary.startswith(
        ex.RUNBOOK_SPECS[runbook_id].method.value
    )


@pytest.mark.parametrize("runbook_id", sorted(VALID))
def test_summary_items_never_collide_with_the_separator(runbook_id, aws):
    """요약 항목은 "·"로 이어 붙는다 — 절 구분이 흐려지지 않는지 눈으로 셀 수 있어야 한다."""
    summary = run(runbook_id).verification_summary
    verified = summary.split(" | 확인: ")[1].split(" | 미확인: ")[0]
    assert not verified.startswith("·") and not verified.endswith("·")
    assert "··" not in summary


# ------------------------------------------------------------------ 파라미터 계약
@pytest.mark.parametrize("runbook_id", sorted(VALID))
def test_missing_required_key_is_rejected(runbook_id, aws):
    """ADR-0007 §5 필수 키 표 — 한 칸이라도 비면 거절이다."""
    _, params = VALID[runbook_id]
    for key in params:
        trimmed = {k: v for k, v in params.items() if k != key}
        outcome = run(runbook_id, params=trimmed)
        assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)
        assert key in outcome.verification_summary


@pytest.mark.parametrize("runbook_id", sorted(VALID))
def test_unknown_key_is_rejected_without_echoing_its_name(runbook_id, aws):
    """키 이름은 payload가 지은 문자열이다 — 거절 요약에 실어 FE까지 보내지 않는다."""
    _, params = VALID[runbook_id]
    outcome = run(runbook_id, params={**params, "sudo_flag": "rm -rf /"})
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)
    assert "sudo_flag" not in outcome.verification_summary
    assert "1개" in outcome.verification_summary


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_rollback_refuses_restore_values_in_parameters(runbook_id, aws):
    """원복 값의 유일한 원천은 백업 레코드다(ADR-0004 정책 ③, ADR-0007 §5 ①)."""
    _, params = VALID[runbook_id]
    outcome = run(runbook_id, params={**params, "instance_type": "t3.nano"})
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


@pytest.mark.parametrize(
    "runbook_id,key,bad",
    [
        ("RUNBOOK_EC2_RIGHTSIZING", "instance_id", "i-XYZ"),
        ("RUNBOOK_EC2_RIGHTSIZING", "instance_id", "i-0abc"),
        ("RUNBOOK_EC2_RIGHTSIZING", "target_instance_type", ""),
        ("RUNBOOK_SG_DELETE_ISOLATED", "group_id", "sg-"),
        ("RUNBOOK_EBS_DELETE_UNATTACHED", "volume_id", "vol-zzzz1234"),
        ("RUNBOOK_NACL_ADD_DENY", "rule_number", 0),
        ("RUNBOOK_NACL_ADD_DENY", "rule_number", 32767),
        ("RUNBOOK_NACL_ADD_DENY", "rule_number", True),
        ("RUNBOOK_NACL_ADD_DENY", "cidr_block", "203.0.113.5"),
        ("RUNBOOK_NACL_ADD_DENY", "cidr_block", "not-an-ip/32"),
        ("RUNBOOK_NACL_ADD_DENY", "protocol", "sctp"),
        ("RUNBOOK_NACL_RESTORE", "egress", "false"),
        ("RUNBOOK_EC2_ENABLE_AUTOSCALING", "max_size", 5),
        ("RUNBOOK_EC2_ENABLE_AUTOSCALING", "min_size", 0),
        ("RUNBOOK_EC2_ISOLATE", "target_group_arn", "arn:aws:ec2:x:y:instance/i-1"),
        ("RUNBOOK_EC2_ISOLATE", "isolation_group_id", INSTANCE),
    ],
)
def test_value_format_violations_are_rejected(runbook_id, key, bad, aws):
    _, params = VALID[runbook_id]
    outcome = run(runbook_id, params={**params, key: bad})
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


def test_min_size_may_not_exceed_max_size(aws):
    runbook_id = "RUNBOOK_EC2_ENABLE_AUTOSCALING"
    _, params = VALID[runbook_id]
    outcome = run(runbook_id, params={**params, "min_size": 4, "max_size": 2})
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


# ------------------------------------------------------------------ ARN 교차 확인
@pytest.mark.parametrize("bad_arn", ["", "not-an-arn", "arn:aws:ec2:r:a:instance", "arn:aws:ec2"])
def test_malformed_target_arn_is_rejected(bad_arn, aws):
    outcome = run("RUNBOOK_EC2_RIGHTSIZING", target_arn=bad_arn)
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


def test_primary_parameter_must_match_target_arn(aws):
    """③ ARN Match는 target_arn 하나만 본다 — 파라미터가 다른 자원을 가리키면 여기서 막는다."""
    outcome = run(
        "RUNBOOK_EC2_RIGHTSIZING", target_arn=arn("instance", "i-0000000000000dead")
    )
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


def test_target_arn_resource_type_must_match_the_runbook(aws):
    outcome = run("RUNBOOK_SG_DELETE_ISOLATED", target_arn=arn("instance", INSTANCE))
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


def test_arn_parameter_from_another_account_is_rejected(aws):
    """Scope Escalation 2차 방어 — 남의 계정 TG로 트래픽을 돌리는 요청."""
    _, params = VALID["RUNBOOK_EC2_ISOLATE"]
    foreign = TG_ARN.replace(ACCOUNT, "999999999999")
    outcome = run("RUNBOOK_EC2_ISOLATE", params={**params, "target_group_arn": foreign})
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


# ------------------------------------------------------------------ 백업 레코드
@pytest.mark.parametrize("runbook_id", sorted(BACKUP_PAYLOADS))
def test_missing_loader_is_a_wiring_error_not_a_verdict(runbook_id, aws):
    """FAIL로 남기면 멀쩡한 원복 요청에 거절 기록이 붙는다 — 배선 오류는 예외로 막는다."""
    target_arn, params = VALID[runbook_id]
    with pytest.raises(RuntimeError, match="backup_loader"):
        ex.precheck(runbook_id, target_arn, params)


@pytest.mark.parametrize("runbook_id", sorted(BACKUP_PAYLOADS))
def test_absent_backup_record_is_target_not_found(runbook_id, aws):
    outcome = run(runbook_id, loader=Loader(None))
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


# backup_record_id를 파라미터로 받는 런북(ID 조회)과 받지 않는 런북(대상으로 조회)은
# 잘못된 백업이 걸러지는 자리가 다르다. NACL_RESTORE만 후자다(런북 명세서 기준).
_BY_ID = sorted(set(BACKUP_PAYLOADS) - {"RUNBOOK_NACL_RESTORE"})


@pytest.mark.parametrize("runbook_id", _BY_ID)
def test_backup_record_of_another_resource_is_rejected(runbook_id, aws):
    """다른 자원의 백업으로 원복하려는 시도 — Scope Escalation."""
    foreign = arn("instance", "i-0000000000000dead", account="999999999999")
    outcome = run(runbook_id, loader=loader_for(runbook_id, target_arn=foreign))
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


@pytest.mark.parametrize("runbook_id", _BY_ID)
def test_backup_record_of_wrong_kind_is_rejected(runbook_id, aws):
    outcome = run(runbook_id, loader=loader_for(runbook_id, backup_type="SOMETHING_ELSE"))
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


@pytest.mark.parametrize("wrong", [{"target_arn": arn("network-acl", "acl-0999999999999999f")},
                                  {"backup_type": "SOMETHING_ELSE"}])
def test_nacl_restore_backup_lookup_is_scoped_to_the_target(wrong, aws):
    """대상으로 찾는 경로에서는 어긋난 백업이 애초에 조회되지 않는다."""
    outcome = run("RUNBOOK_NACL_RESTORE", loader=loader_for("RUNBOOK_NACL_RESTORE", **wrong))
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


@pytest.mark.parametrize(
    "runbook_id,payload",
    [
        ("RUNBOOK_EC2_REVERT_SIZE", {}),
        ("RUNBOOK_EC2_REVERT_SIZE", {"instance_type": ""}),
        ("RUNBOOK_SG_RECREATE", {"group_name": "g", "description": "d", "vpc_id": "v"}),
        ("RUNBOOK_EC2_UNISOLATE", {"security_group_ids": [], "target_group_arn": TG_ARN}),
        ("RUNBOOK_EC2_UNISOLATE", {"security_group_ids": [INSTANCE], "target_group_arn": TG_ARN}),
    ],
)
def test_unusable_backup_payload_is_param_invalid(runbook_id, payload, aws):
    outcome = run(runbook_id, loader=loader_for(runbook_id, payload=payload))
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)


def test_restore_value_comes_from_the_backup_record(aws):
    """원복 스펙은 파라미터가 아니라 백업 레코드에서 온다 — 실제 호출 인자로 확인한다."""
    run(
        "RUNBOOK_EC2_REVERT_SIZE",
        loader=loader_for("RUNBOOK_EC2_REVERT_SIZE", payload={"instance_type": "m5.4xlarge"}),
    )
    modify = [kwargs for op, kwargs in aws.calls if op == "modify_instance_attribute"]
    assert modify and modify[0]["InstanceType"] == {"Value": "m5.4xlarge"}


# ------------------------------------------------------------------ DryRun 판정 규약
@pytest.mark.parametrize(
    "aws_code,expected",
    [
        ("UnauthorizedOperation", R.PRECHECK_UNAUTHORIZED),
        ("InvalidInstanceID.NotFound", R.PRECHECK_TARGET_NOT_FOUND),
        ("IncorrectInstanceState", R.PRECHECK_INVALID_STATE),
        ("InternalFailure", R.PRECHECK_AWS_ERROR),
    ],
)
def test_dry_run_rejection_carries_the_aws_reason(aws_code, expected, aws):
    aws(modify_instance_attribute=client_error(aws_code))
    outcome = run("RUNBOOK_EC2_RIGHTSIZING")
    assert (outcome.passed, outcome.reason_code) == (False, expected)


def test_silent_dry_run_success_is_rejected(aws):
    """예외 없이 반환 = 플래그 미적용. LocalStack NACL 결함이 이 모양이었다."""
    aws(modify_instance_attribute={"Return": True})
    outcome = run("RUNBOOK_EC2_RIGHTSIZING")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_AWS_ERROR)


def test_dry_run_flag_is_always_set(aws):
    """조치를 확인하러 간 호출이 실제 조치가 되면 안 된다."""
    for runbook_id in sorted(VALID):
        run(runbook_id)
    mutating = {
        "modify_instance_attribute",
        "modify_network_interface_attribute",
        "create_security_group",
        "delete_security_group",
        "create_snapshot",
        "delete_volume",
        "create_launch_template",
        "authorize_security_group_ingress",
        "authorize_security_group_egress",
    }
    calls = [(op, kwargs) for op, kwargs in aws.calls if op in mutating]
    assert calls, "변경 계열 호출이 하나도 없었습니다 — 전제가 깨졌습니다"
    assert all(kwargs.get("DryRun") is True for _, kwargs in calls)


# ------------------------------------------------------------------ 런북별 실패 경로
def test_nacl_add_deny_refuses_a_rule_number_in_use(aws):
    _, params = VALID["RUNBOOK_NACL_ADD_DENY"]
    outcome = run("RUNBOOK_NACL_ADD_DENY", params={**params, "rule_number": 100})
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_INVALID_STATE)


def test_nacl_add_deny_ignores_outbound_rules_with_the_same_number(aws):
    """(rule_number, egress) 조합으로 본다 — 아웃바운드 100번은 인바운드와 다른 자리다."""
    aws(describe_network_acls={
        "NetworkAcls": [{"Entries": [{"RuleNumber": 200, "Egress": True, "RuleAction": "deny"}]}]
    })
    assert run("RUNBOOK_NACL_ADD_DENY").passed


def test_missing_nacl_is_target_not_found(aws):
    aws(describe_network_acls={"NetworkAcls": []})
    outcome = run("RUNBOOK_NACL_ADD_DENY")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


def test_nacl_restore_requires_the_rule_to_exist(aws):
    aws(describe_network_acls={"NetworkAcls": [{"Entries": []}]})
    outcome = run("RUNBOOK_NACL_RESTORE")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


def test_nacl_restore_refuses_to_delete_an_allow_rule(aws):
    """원복은 우리가 넣은 deny를 걷는 조치다 — 기존 allow 규칙을 지우면 안 된다."""
    aws(describe_network_acls={
        "NetworkAcls": [{"Entries": [{"RuleNumber": 100, "Egress": False, "RuleAction": "allow"}]}]
    })
    outcome = run("RUNBOOK_NACL_RESTORE")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_INVALID_STATE)


def test_nacl_restore_requires_the_backup_rule_index_to_match(aws):
    """다른 규칙의 백업으로는 복원하지 않는다 — 조회 자체가 rule index로 좁혀진다."""
    loader = loader_for(
        "RUNBOOK_NACL_RESTORE", payload={"rule_number": 900, "egress": False}
    )
    outcome = run("RUNBOOK_NACL_RESTORE", loader=loader)
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)
    assert loader.match_calls == [{"rule_number": 100, "egress": False}]


def test_isolate_requires_the_target_to_be_registered(aws):
    aws(describe_target_health={"TargetHealthDescriptions": []})
    outcome = run("RUNBOOK_EC2_ISOLATE")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


def test_isolate_requires_the_isolation_group_to_exist(aws):
    aws(describe_security_groups=client_error("InvalidGroup.NotFound"))
    outcome = run("RUNBOOK_EC2_ISOLATE")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


def test_unisolate_requires_every_backed_up_group_to_still_exist(aws):
    aws(describe_security_groups=client_error("InvalidGroup.NotFound"))
    outcome = run("RUNBOOK_EC2_UNISOLATE")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


def test_unisolate_refuses_a_target_group_in_another_vpc(aws):
    aws(describe_target_groups={"TargetGroups": [{"VpcId": "vpc-0999999999999999f"}]})
    outcome = run("RUNBOOK_EC2_UNISOLATE")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_INVALID_STATE)


def test_enable_autoscaling_requires_a_running_instance(aws):
    stopped = {"Reservations": [{"Instances": [{
        "InstanceId": INSTANCE, "InstanceType": "t3.xlarge", "State": {"Name": "stopped"},
        "VpcId": "vpc-0abc123456789def0",
        "NetworkInterfaces": [{"NetworkInterfaceId": "eni-0abc123456789def0"}],
    }]}]}
    aws(describe_instances=stopped)
    outcome = run("RUNBOOK_EC2_ENABLE_AUTOSCALING")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_INVALID_STATE)


def test_enable_autoscaling_refuses_an_existing_group_of_the_same_name(aws):
    aws(describe_auto_scaling_groups={"AutoScalingGroups": [{"AutoScalingGroupName": "x"}]})
    outcome = run("RUNBOOK_EC2_ENABLE_AUTOSCALING")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_INVALID_STATE)


def test_absent_instance_is_target_not_found(aws):
    aws(describe_instances={"Reservations": []})
    outcome = run("RUNBOOK_EC2_ISOLATE")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


# ------------------------------------------------------------------ 로컬 환경 제약
# elbv2·autoscaling은 LocalStack Community에 없다(ADR-0006 §4 4행). 아래 3종이
# 로컬에서 실패하는 것은 ADR-0006 §3을 지킨 결과의 정상 동작이다(ADR-0007 §Consequences).
LOCAL_UNSUPPORTED = {
    "RUNBOOK_EC2_ISOLATE": "describe_target_health",
    "RUNBOOK_EC2_UNISOLATE": "describe_target_groups",
    "RUNBOOK_EC2_ENABLE_AUTOSCALING": "describe_auto_scaling_groups",
}


@pytest.mark.parametrize("runbook_id,operation", sorted(LOCAL_UNSUPPORTED.items()))
def test_pro_only_service_failure_is_an_aws_error(runbook_id, operation, aws):
    aws(**{operation: client_error("InternalFailure")})
    outcome = run(runbook_id)
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_AWS_ERROR)


# ------------------------------------------------------------------ 예외 미유출
@pytest.mark.parametrize("runbook_id", sorted(VALID))
@pytest.mark.parametrize(
    "params",
    [{}, {"instance_id": None}, {"evidence_id": 1}, {"rule_number": []}, {"x": {"y": ["z"]}}],
)
def test_no_input_makes_precheck_raise(runbook_id, params, aws):
    """가드레일 쪽에 try/except를 요구하지 않는다는 계약(ADR-0007 §1)."""
    target_arn, _ = VALID[runbook_id]
    outcome = ex.precheck(runbook_id, target_arn, params, backup_loader=Loader(None))
    assert isinstance(outcome, PrecheckOutcome) and not outcome.passed


# ------------------------------------------------------------------ 리전
def test_every_client_is_built_for_the_target_region(aws):
    """멀티리전 — 기본 리전이 아니라 target_arn의 리전에서 검사해야 한다.

    SSOT의 1-2개 리전 범위에서 두 번째 리전 자산을 기본 리전에서 조회하면
    자원이 없다고 나오거나(오판정) 같은 ID의 다른 자원을 본다.
    """
    for runbook_id in sorted(VALID):
        run(runbook_id)
    assert aws.clients, "클라이언트를 하나도 만들지 않았습니다 — 전제가 깨졌습니다"
    assert {region for _, region in aws.clients} == {REGION}


def test_arn_parameter_in_another_region_is_rejected(aws):
    """ARN 파라미터도 같은 리전이어야 한다 — ③ ARN Match는 target_arn만 본다."""
    _, params = VALID["RUNBOOK_EC2_ISOLATE"]
    other = dict(
        params,
        target_group_arn=f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:targetgroup/x/y",
    )
    outcome = run("RUNBOOK_EC2_ISOLATE", params=other)
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_PARAM_INVALID)
    assert not aws.calls, "거절은 AWS를 부르기 전에 끝나야 합니다"


# ------------------------------------------------------------------ 등록 여부
def test_isolate_refuses_a_target_that_is_not_registered(aws):
    """AWS는 미등록 대상에도 설명을 돌려준다 — 목록이 비었는지만 보면 통과한다."""
    aws(describe_target_health={
        "TargetHealthDescriptions": [
            {
                "Target": {"Id": INSTANCE},
                "TargetHealth": {"State": "unused", "Reason": "Target.NotRegistered"},
            }
        ]
    })
    outcome = run("RUNBOOK_EC2_ISOLATE")
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


def test_isolate_accepts_a_registered_but_unhealthy_target(aws):
    """등록돼 있으면 헬스 상태와 무관하게 이탈 대상이다."""
    aws(describe_target_health={
        "TargetHealthDescriptions": [
            {
                "Target": {"Id": INSTANCE},
                "TargetHealth": {"State": "unhealthy", "Reason": "Target.FailedHealthChecks"},
            }
        ]
    })
    assert run("RUNBOOK_EC2_ISOLATE").passed


# ------------------------------------------------------------------ 백업 선택
def test_nacl_restore_can_reach_an_older_rule_backup(aws):
    """같은 NACL에 조치가 누적돼도 대상 규칙의 백업을 고를 수 있어야 한다."""

    class Multi:
        """규칙마다 백업이 따로 쌓인 로더. 최신은 rule 900이다."""

        def __init__(self):
            target_arn = VALID["RUNBOOK_NACL_RESTORE"][0]
            self.records = [
                ex.BackupRecordView("bk-900", target_arn, ex.BACKUP_NACL_RULE_INDEX,
                                    {"rule_number": 900, "egress": False}),
                ex.BackupRecordView("bk-100", target_arn, ex.BACKUP_NACL_RULE_INDEX,
                                    {"rule_number": 100, "egress": False}),
            ]

        def get(self, backup_record_id):
            return None

        def latest_for_target(self, target_arn, backup_type, payload_match=None):
            for record in self.records:
                if record.target_arn != target_arn or record.backup_type != backup_type:
                    continue
                if payload_match and any(
                    record.payload.get(key) != value for key, value in payload_match.items()
                ):
                    continue
                return record
            return None

    assert run("RUNBOOK_NACL_RESTORE", loader=Multi()).passed


# ------------------------------------------------------------------ 규칙 재주입 권한
SG_RULES = {
    "group_name": "restored",
    "description": "restored by vigilantis",
    "vpc_id": "vpc-0abc123456789def0",
    "ingress_permissions": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443}],
    "egress_permissions": [{"IpProtocol": "-1"}],
}


def test_sg_recreate_dry_runs_the_rule_reinjection(aws):
    """create만 확인하면 빈 SG를 만들고 규칙 복원에서 실패하는 경로가 통과한다."""
    assert run("RUNBOOK_SG_RECREATE", payload=SG_RULES).passed
    operations = [operation for operation, _ in aws.calls]
    assert operations == [
        "create_security_group",
        "authorize_security_group_ingress",
        "authorize_security_group_egress",
    ]
    assert all(kwargs.get("GroupId") == GROUP for op, kwargs in aws.calls if op.startswith("authorize"))


def test_sg_recreate_fails_when_rule_reinjection_is_unauthorized(aws):
    aws(authorize_security_group_ingress=client_error("UnauthorizedOperation"))
    outcome = run("RUNBOOK_SG_RECREATE", payload=SG_RULES)
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_UNAUTHORIZED)


def test_sg_recreate_skips_authorize_for_an_empty_direction(aws):
    """빈 목록으로 authorize를 부르면 DryRun 이전에 파라미터 오류가 난다."""
    rules = dict(SG_RULES, egress_permissions=[])
    assert run("RUNBOOK_SG_RECREATE", payload=rules).passed
    assert "authorize_security_group_egress" not in [operation for operation, _ in aws.calls]

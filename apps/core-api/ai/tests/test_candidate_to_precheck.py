"""후보 → precheck(parameters) 변환 경로의 왕복 테스트 (Issue #154).

본편 7종만 다룬다 — 롤백 3종은 AI 후보가 될 수 없어 이 경로를 타지 않는다
(ADR-0004 정책 ②, #126 복구 접수 경로).

"왕복"이 확인하는 것은 **AI가 낸 후보를 그대로 실행 파라미터로 바꿔 넣을 수 있는가**다.
AWS를 부르지 않고 executor의 파라미터 계약(_validate_params)과 대상 대조(_validate_scope)만
통과시킨다 — 그 뒤의 AWS 판정은 services/tests/test_precheck_dispatch.py가 본다.
"""

import pytest
from botocore.exceptions import ClientError
from schemas.agents import RunbookCandidateDraft
from schemas.precheck import PrecheckOutcome, PrecheckReasonCode
from schemas.runbook_parameters import RESOURCE_ID_PARAM, build_precheck_parameters
from schemas.runbooks import AI_RECOMMENDABLE_RUNBOOK_IDS, RunbookId
from services.aws import executor as ex

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
GROUP = "sg-0abc123456789def0"
ACL = "acl-0abc123456789def0"
VOLUME = "vol-0abc123456789def0"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/vigilantis/abc"


def arn(resource_type: str, resource_id: str) -> str:
    return f"arn:aws:ec2:{REGION}:{ACCOUNT}:{resource_type}/{resource_id}"


# (target_arn, AI가 정하는 값, 접수 시점에 조회로 채우는 값)
CANDIDATES = {
    "RUNBOOK_EC2_ISOLATE": (
        arn("instance", INSTANCE),
        {},
        {"target_group_arn": TG_ARN, "isolation_group_id": GROUP},
    ),
    "RUNBOOK_NACL_ADD_DENY": (
        arn("network-acl", ACL),
        {"rule_number": 100, "cidr_block": "203.0.113.5/32", "protocol": "-1"},
        {},
    ),
    "RUNBOOK_NACL_RESTORE": (
        arn("network-acl", ACL),
        {"rule_number": 100, "egress": False},
        {},
    ),
    "RUNBOOK_SG_DELETE_ISOLATED": (arn("security-group", GROUP), {}, {}),
    "RUNBOOK_EC2_RIGHTSIZING": (
        arn("instance", INSTANCE),
        {"target_instance_type": "t3.medium"},
        {"current_instance_type": "t3.xlarge"},
    ),
    "RUNBOOK_EC2_ENABLE_AUTOSCALING": (
        arn("instance", INSTANCE),
        {"min_size": 1, "max_size": 2},
        {},
    ),
    "RUNBOOK_EBS_DELETE_UNATTACHED": (arn("volume", VOLUME), {}, {}),
}

EVIDENCE_IDS = ["ev-1", "ev-2"]


def _draft(runbook_id: str) -> RunbookCandidateDraft:
    target_arn, parameters, _ = CANDIDATES[runbook_id]
    return RunbookCandidateDraft.model_validate({
        "runbook_id": runbook_id,
        "target_arn": target_arn,
        "parameters": parameters,
        "evidence_ids": EVIDENCE_IDS,
    })


def _convert(runbook_id: str):
    """실행 접수가 하는 일 — target_arn을 해석하고 조회값을 얹어 실행 파라미터를 만든다."""
    draft = _draft(runbook_id)
    _, _, lookups = CANDIDATES[runbook_id]
    target = ex.parse_arn(draft.target_arn)
    assert target is not None
    return build_precheck_parameters(
        draft.runbook_id,
        draft.parameters,
        resource_id=target.resource_id,
        evidence_ids=draft.evidence_ids,
        **lookups,
    )


def test_every_ai_recommendable_runbook_has_a_conversion_case():
    """본편 7종에 빈칸이 없어야 한다 — 하나라도 비면 그 후보는 실행으로 못 간다."""
    assert set(CANDIDATES) == AI_RECOMMENDABLE_RUNBOOK_IDS


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATES))
def test_converted_parameters_satisfy_the_executor_contract(runbook_id):
    params = _convert(runbook_id).model_dump(mode="json")
    spec = ex.RUNBOOK_SPECS[runbook_id]

    assert ex._validate_params(spec, params) is None


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATES))
def test_converted_parameters_point_at_the_candidates_own_target(runbook_id):
    """ADR-0007 §5 ② Scope Escalation 2차 방어 — 후보의 target_arn과 갈리면 안 된다."""
    target_arn, _, _ = CANDIDATES[runbook_id]
    params = _convert(runbook_id).model_dump(mode="json")
    spec = ex.RUNBOOK_SPECS[runbook_id]
    target = ex.parse_arn(target_arn)

    assert ex._validate_scope(spec, target, params) is None
    assert params[RESOURCE_ID_PARAM[RunbookId(runbook_id)]] == target.resource_id


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATES))
def test_values_the_ai_chose_reach_the_execution_parameters_unchanged(runbook_id):
    """관제자가 승인한 값과 실행되는 값이 같아야 한다 — 변환이 값을 바꾸면 안 된다."""
    draft = _draft(runbook_id)
    params = _convert(runbook_id).model_dump()

    for key, value in draft.parameters.model_dump().items():
        assert params[key] == value


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATES))
def test_evidence_id_is_the_first_of_the_candidates_evidence_ids(runbook_id):
    assert _convert(runbook_id).evidence_id == EVIDENCE_IDS[0]


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATES))
def test_display_parameters_never_leak_into_the_execution_parameters(runbook_id):
    """화면 표시본은 실행 경로가 읽지 않는다 — 계약이 그것을 아예 받지 않는다."""
    params = _convert(runbook_id).model_dump()
    spec = ex.RUNBOOK_SPECS[runbook_id]

    assert set(params) == set(spec.params_model.model_fields)


# ---------------------------------------------------------------- 공개 경계
# 위 테스트들은 executor 내부 판정 함수를 직접 불러 키·값을 세밀히 대조한다.
# 그것만으로는 공개 함수 precheck()가 변환 결과(Pydantic 모델)를 받지 못하는
# 타입 불일치를 놓친다 — 아래는 그 경계 자체를 지나가게 한다.


class _ErringClient:
    """어떤 작업을 불러도 ClientError를 내는 가짜 boto3 클라이언트.

    목적은 AWS 판정이 아니라 그 앞의 파라미터·대상 검증을 통과했다는 증명이다 —
    거절 사유가 PARAM_INVALID가 아니면 변환 결과가 경계를 지나간 것이다.
    """

    def __getattr__(self, operation: str):
        def call(**kwargs):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "stub"}}, operation)

        return call


class _NaclBackupLoader:
    """NACL_RESTORE의 백업 조회 — 대상·종류·rule index가 맞는 레코드를 돌려준다."""

    def get(self, backup_record_id):
        return None

    def latest_for_target(self, target_arn, backup_type, payload_match=None):
        return ex.BackupRecordView("bk-1", target_arn, backup_type, dict(payload_match or {}))


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATES))
def test_converted_model_is_accepted_by_the_public_precheck(runbook_id, monkeypatch):
    """변환 결과 모델을 model_dump 없이 precheck()에 그대로 넣을 수 있다."""
    monkeypatch.setattr(ex, "aws_client", lambda *args, **kwargs: _ErringClient())
    target_arn, _, _ = CANDIDATES[runbook_id]

    outcome = ex.precheck(
        RunbookId(runbook_id),
        target_arn,
        _convert(runbook_id),  # 모델 그대로 — dict로 바꾸지 않는다
        backup_loader=_NaclBackupLoader(),
    )

    assert isinstance(outcome, PrecheckOutcome)
    # 파라미터·대상 검증을 지나 AWS 단계에서 멈췄다는 뜻이다
    assert outcome.reason_code not in (
        PrecheckReasonCode.PRECHECK_PARAM_INVALID,
        PrecheckReasonCode.PRECHECK_NOT_IMPLEMENTED,
    )

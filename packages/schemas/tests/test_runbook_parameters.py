"""Runbook별 typed 파라미터 계약 테스트 (Issue #154).

두 가지를 고정한다.
  ① precheck 모델 10종의 키 집합이 ADR-0007 §5 표와 같은가 — 표를 아래에 그대로
     옮겨 적었다. 문서가 바뀌었는데 코드가 안 바뀌면(또는 반대면) 여기서 걸린다.
  ② 후보 모델 7종이 "AI가 정하는 값"만 담는가 — 나머지 키가 후보에 새어 들어오면
     AI가 지어낼 수 있는 자리가 늘어난다.
"""

import pytest
from pydantic import ValidationError

from schemas.runbook_parameters import (
    CANDIDATE_PARAMETER_MODELS,
    PRECHECK_PARAMETER_MODELS,
    RESOURCE_ID_PARAM,
    Ec2RightsizingCandidateParameters,
    NaclAddDenyCandidateParameters,
    build_display_parameters,
    build_precheck_parameters,
)
from schemas.runbooks import (
    AI_RECOMMENDABLE_RUNBOOK_IDS,
    ALLOWED_RUNBOOK_IDS,
    ROLLBACK_RUNBOOK_IDS,
    RunbookId,
)

INSTANCE = "i-0abc123456789def0"
GROUP = "sg-0abc123456789def0"
ACL = "acl-0abc123456789def0"
VOLUME = "vol-0abc123456789def0"
TG_ARN = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/vig/abc"

# ADR-0007 §5 파라미터 계약 표
SPEC_KEYS = {
    "RUNBOOK_EC2_ISOLATE": {
        "instance_id", "target_group_arn", "isolation_group_id", "evidence_id",
    },
    "RUNBOOK_NACL_ADD_DENY": {
        "network_acl_id", "rule_number", "cidr_block", "protocol", "evidence_id",
    },
    "RUNBOOK_NACL_RESTORE": {"network_acl_id", "rule_number", "egress", "evidence_id"},
    "RUNBOOK_SG_DELETE_ISOLATED": {"group_id", "evidence_id"},
    "RUNBOOK_EC2_RIGHTSIZING": {
        "instance_id", "current_instance_type", "target_instance_type", "evidence_id",
    },
    "RUNBOOK_EC2_ENABLE_AUTOSCALING": {
        "instance_id", "min_size", "max_size", "evidence_id",
    },
    "RUNBOOK_EBS_DELETE_UNATTACHED": {"volume_id", "evidence_id"},
    "RUNBOOK_EC2_UNISOLATE": {"instance_id", "backup_record_id", "evidence_id"},
    "RUNBOOK_SG_RECREATE": {"backup_record_id", "evidence_id"},
    "RUNBOOK_EC2_REVERT_SIZE": {"instance_id", "backup_record_id", "evidence_id"},
}

# AI가 정하는 값 (#154 결정 ①). 나머지는 target_arn 파생·조회·evidence_ids 첫 항목이다.
CANDIDATE_KEYS = {
    "RUNBOOK_EC2_ISOLATE": set(),
    "RUNBOOK_NACL_ADD_DENY": {"rule_number", "cidr_block", "protocol"},
    "RUNBOOK_NACL_RESTORE": {"rule_number", "egress"},
    "RUNBOOK_SG_DELETE_ISOLATED": set(),
    "RUNBOOK_EC2_RIGHTSIZING": {"target_instance_type"},
    "RUNBOOK_EC2_ENABLE_AUTOSCALING": {"min_size", "max_size"},
    "RUNBOOK_EBS_DELETE_UNATTACHED": set(),
}

# 조회로 채우는 값 (#154 결정 ①의 "시스템·DB" 행 중 본편 7종에 쓰이는 것)
LOOKUPS = {
    "RUNBOOK_EC2_ISOLATE": {"target_group_arn": TG_ARN, "isolation_group_id": GROUP},
    "RUNBOOK_EC2_RIGHTSIZING": {"current_instance_type": "t3.xlarge"},
}

CANDIDATE_VALUES = {
    "RUNBOOK_EC2_ISOLATE": {},
    "RUNBOOK_NACL_ADD_DENY": {
        "rule_number": 100, "cidr_block": "203.0.113.5/32", "protocol": "-1",
    },
    "RUNBOOK_NACL_RESTORE": {"rule_number": 100, "egress": False},
    "RUNBOOK_SG_DELETE_ISOLATED": {},
    "RUNBOOK_EC2_RIGHTSIZING": {"target_instance_type": "t3.medium"},
    "RUNBOOK_EC2_ENABLE_AUTOSCALING": {"min_size": 1, "max_size": 2},
    "RUNBOOK_EBS_DELETE_UNATTACHED": {},
}

RESOURCE_IDS = {
    "RUNBOOK_EC2_ISOLATE": INSTANCE,
    "RUNBOOK_NACL_ADD_DENY": ACL,
    "RUNBOOK_NACL_RESTORE": ACL,
    "RUNBOOK_SG_DELETE_ISOLATED": GROUP,
    "RUNBOOK_EC2_RIGHTSIZING": INSTANCE,
    "RUNBOOK_EC2_ENABLE_AUTOSCALING": INSTANCE,
    "RUNBOOK_EBS_DELETE_UNATTACHED": VOLUME,
}


def _build(runbook_id: str, **over):
    model = CANDIDATE_PARAMETER_MODELS[RunbookId(runbook_id)]
    kwargs = {
        "resource_id": RESOURCE_IDS[runbook_id],
        "evidence_ids": ["ev-1", "ev-2"],
        **LOOKUPS.get(runbook_id, {}),
    }
    kwargs.update(over)
    return build_precheck_parameters(
        RunbookId(runbook_id), model.model_validate(CANDIDATE_VALUES[runbook_id]), **kwargs
    )


# ---------------------------------------------------------------- 모델 등록 전수
def test_precheck_models_cover_every_confirmed_runbook():
    assert {r.value for r in PRECHECK_PARAMETER_MODELS} == ALLOWED_RUNBOOK_IDS


def test_candidate_models_cover_exactly_the_ai_recommendable_runbooks():
    """롤백 3종은 후보 모델이 없다 — 있으면 AI가 제안할 수 있는 모양이 생긴다."""
    assert {r.value for r in CANDIDATE_PARAMETER_MODELS} == AI_RECOMMENDABLE_RUNBOOK_IDS


def test_resource_id_param_covers_every_runbook_but_sg_recreate():
    """SG_RECREATE만 복원 대상을 백업 레코드가 가리킨다(§5 표에 자원 ID가 없다)."""
    assert {r.value for r in RESOURCE_ID_PARAM} == (
        ALLOWED_RUNBOOK_IDS - {RunbookId.RUNBOOK_SG_RECREATE.value}
    )


# ---------------------------------------------------------------- 키 집합 대조
@pytest.mark.parametrize("runbook_id", sorted(SPEC_KEYS))
def test_precheck_model_keys_match_the_adr_table(runbook_id):
    model = PRECHECK_PARAMETER_MODELS[RunbookId(runbook_id)]
    assert set(model.model_fields) == SPEC_KEYS[runbook_id]


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATE_KEYS))
def test_candidate_model_carries_only_ai_decided_values(runbook_id):
    model = CANDIDATE_PARAMETER_MODELS[RunbookId(runbook_id)]
    assert set(model.model_fields) == CANDIDATE_KEYS[runbook_id]


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATE_KEYS))
def test_every_precheck_key_has_exactly_one_source(runbook_id):
    """§5 키 하나하나가 AI·자원 ID·조회·evidence 중 정확히 한 곳에서 온다.

    빠진 키가 있으면 변환이 필수 키를 못 채우고, 겹치는 키가 있으면 어느 값이
    실행으로 가는지가 코드를 읽어야만 정해진다.
    """
    sources = [
        CANDIDATE_KEYS[runbook_id],
        {RESOURCE_ID_PARAM[RunbookId(runbook_id)]},
        set(LOOKUPS.get(runbook_id, {})),
        {"evidence_id"},
    ]
    union = set().union(*sources)
    assert union == SPEC_KEYS[runbook_id]
    assert sum(len(s) for s in sources) == len(union), "두 출처가 같은 키를 채운다"


# ---------------------------------------------------------------- 값 제약
@pytest.mark.parametrize("over", [
    {"rule_number": 0},
    {"rule_number": 32767},
    {"rule_number": True},        # bool은 int의 하위 타입이라 명시적으로 막는다
    {"rule_number": "100"},
    {"cidr_block": "203.0.113.5"},  # 접두 길이 필수
    {"cidr_block": "not-an-ip/32"},
    {"protocol": "sctp"},
])
def test_nacl_add_deny_value_violations(over):
    with pytest.raises(ValidationError):
        NaclAddDenyCandidateParameters.model_validate(
            {**CANDIDATE_VALUES["RUNBOOK_NACL_ADD_DENY"], **over}
        )


@pytest.mark.parametrize("value", ["", "   ", "v" * 257])
def test_free_text_bounds(value):
    with pytest.raises(ValidationError):
        Ec2RightsizingCandidateParameters(target_instance_type=value)


@pytest.mark.parametrize("bad_id", ["i-XYZ0123456", "i-0abc", "xi-0abc12345678", INSTANCE + "\n"])
def test_resource_id_patterns_are_full_matches(bad_id):
    with pytest.raises(ValidationError):
        _build("RUNBOOK_EC2_RIGHTSIZING", resource_id=bad_id)


def test_min_size_may_not_exceed_max_size():
    model = CANDIDATE_PARAMETER_MODELS[RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING]
    with pytest.raises(ValidationError):
        model(min_size=4, max_size=2)


# ---------------------------------------------------------------- 변환 경로
@pytest.mark.parametrize("runbook_id", sorted(CANDIDATE_KEYS))
def test_build_fills_every_required_key(runbook_id):
    built = _build(runbook_id)
    assert set(built.model_dump()) == SPEC_KEYS[runbook_id]
    assert built.model_dump()[RESOURCE_ID_PARAM[RunbookId(runbook_id)]] == RESOURCE_IDS[runbook_id]


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATE_KEYS))
def test_evidence_id_is_the_first_of_evidence_ids(runbook_id):
    """선택 규칙은 결정적이기만 하면 판정에 영향이 없다 — 첫 항목으로 고정한다(#154 결정 ②)."""
    assert _build(runbook_id, evidence_ids=["ev-9", "ev-1"]).evidence_id == "ev-9"


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATE_KEYS))
def test_empty_evidence_ids_is_rejected(runbook_id):
    with pytest.raises(ValueError):
        _build(runbook_id, evidence_ids=[])


@pytest.mark.parametrize("runbook_id", sorted(ROLLBACK_RUNBOOK_IDS))
def test_rollback_runbooks_have_no_conversion_path(runbook_id):
    """롤백 3종의 parameters는 #126 복구 접수 경로가 만든다 — 이 경로를 타지 않는다."""
    with pytest.raises(ValueError):
        build_precheck_parameters(
            RunbookId(runbook_id),
            NaclAddDenyCandidateParameters.model_validate(
                CANDIDATE_VALUES["RUNBOOK_NACL_ADD_DENY"]
            ),
            resource_id=INSTANCE,
            evidence_ids=["ev-1"],
        )


def test_lookup_value_for_a_runbook_that_does_not_take_it_is_rejected():
    """조용히 버리면 호출부의 조립 실수가 그대로 실행으로 간다."""
    with pytest.raises(ValueError):
        _build("RUNBOOK_NACL_ADD_DENY", current_instance_type="t3.small")


def test_candidate_model_of_another_runbook_is_rejected():
    with pytest.raises(ValueError):
        build_precheck_parameters(
            RunbookId.RUNBOOK_EC2_RIGHTSIZING,
            NaclAddDenyCandidateParameters.model_validate(
                CANDIDATE_VALUES["RUNBOOK_NACL_ADD_DENY"]
            ),
            resource_id=INSTANCE,
            evidence_ids=["ev-1"],
        )


def test_missing_lookup_value_is_rejected():
    with pytest.raises(ValidationError):
        _build("RUNBOOK_EC2_RIGHTSIZING", current_instance_type=None)


# ---------------------------------------------------------------- 화면 표시본
def test_display_parameters_render_scalars_as_json_style_text():
    model = CANDIDATE_PARAMETER_MODELS[RunbookId.RUNBOOK_NACL_RESTORE]
    display = build_display_parameters(model(rule_number=100, egress=False))
    assert display == {"rule_number": "100", "egress": "false"}


@pytest.mark.parametrize("runbook_id", sorted(CANDIDATE_KEYS))
def test_display_parameters_never_include_system_values(runbook_id):
    """대상 자원은 FE가 target_arn으로 따로 보여준다 — 여기 섞으면 출처가 흐려진다."""
    model = CANDIDATE_PARAMETER_MODELS[RunbookId(runbook_id)]
    display = build_display_parameters(model.model_validate(CANDIDATE_VALUES[runbook_id]))
    assert set(display) == CANDIDATE_KEYS[runbook_id]

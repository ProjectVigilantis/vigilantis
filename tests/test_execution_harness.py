# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 실행 계열 하네스(execution_harness.py + conftest.py)의 자체 회귀입니다. (Issue #136)
#
# 계층 구분 — 다른 사람 것과 겹치지 않게 나눈다.
#   apps/core-api/services/tests/test_precheck_dispatch.py (김세혁) = precheck 판정
#     자체. boto3 를 스텁으로 갈아 끼우고 런북별 분기·거절 사유를 전수로 본다.
#   apps/core-api/ai/tests/test_candidate_to_precheck.py (안성일) = 후보 → precheck
#     변환 함수(#154)의 계약.
#   이 파일 = **하네스가 만드는 입력이 그 계약들과 계속 맞는가.** 하네스 기본값이
#     낡으면 이것을 쓰는 실행 테스트가 전부 조용히 잘못된 전제로 돌기 때문에,
#     하네스 쪽에 먼저 이빨을 박아 둔다.
#
# precheck 를 실제로 부르지 않는다 — 그건 위 두 파일의 몫이고, 여기서 부르면
# LocalStack 유무에 따라 결과가 갈려 "하네스가 맞는가"라는 질문이 흐려진다.
# ==============================================================================

from __future__ import annotations

import pytest
from execution_harness import (
    BACKUP_REQUIRED_RUNBOOKS,
    CANDIDATE_PARAMS_BY_RUNBOOK,
    IDEMPOTENCY_KEY_MAX,
    P2_LOCAL_FAIL_CASES,
    P2_LOCAL_FAIL_RUNBOOKS,
    FakeBackupLoader,
    default_target_arn,
    expects_local_precheck_fail,
)
from schemas.guardrails import GuardrailValidationContext
from schemas.runbook_parameters import CANDIDATE_PARAMETER_MODELS
from schemas.runbooks import RunbookId
from services.aws.executor import RUNBOOK_SPECS

# precheck 파라미터를 조립할 때 "조회로 채우는" 값. 후보에는 없고 실행부가 넣는다.
_LOOKUPS = {
    RunbookId.RUNBOOK_EC2_ISOLATE: {
        "target_group_arn": (
            "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012"
            ":targetgroup/vigilantis-tg/0123456789abcdef"
        ),
        "isolation_group_id": "sg-0fedcba987654321",
    },
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: {"current_instance_type": "t3.xlarge"},
}

_CANDIDATE_RUNBOOKS = sorted(CANDIDATE_PARAMETER_MODELS, key=lambda r: r.value)

_ROLLBACK_RUNBOOKS = [
    RunbookId.RUNBOOK_EC2_UNISOLATE,
    RunbookId.RUNBOOK_SG_RECREATE,
    RunbookId.RUNBOOK_EC2_REVERT_SIZE,
]


# ------------------------------------------------------------------ B. 조립 헬퍼
def test_candidate_params_cover_every_ai_recommendable_runbook():
    """하네스 기본 파라미터에 빠진 런북이 없다 — 빠지면 KeyError 로 늦게 터진다."""
    assert set(CANDIDATE_PARAMS_BY_RUNBOOK) == set(CANDIDATE_PARAMETER_MODELS)


@pytest.mark.parametrize("runbook_id", _CANDIDATE_RUNBOOKS, ids=lambda r: r.value)
def test_candidate_factory_builds_every_ai_recommendable_runbook(runbook_id, candidate_factory):
    """본편 7종이 전부 조립된다 — 기본 파라미터가 typed 계약과 맞다는 뜻이다.

    #154 로 parameters 가 typed 로 바뀐 뒤, 런북과 무관한 값을 실으면 ① Schema Check
    이전에 스키마가 거절한다. 그 어긋남을 실행 테스트마다 발견하지 않게 여기서 잡는다.
    """
    candidate = candidate_factory(runbook_id)
    assert candidate.runbook_id is runbook_id
    assert isinstance(candidate.parameters, CANDIDATE_PARAMETER_MODELS[runbook_id])
    # display_parameters 는 서버 파생본이다 — 넘기지 않았는데도 키가 맞아야 한다
    assert set(candidate.display_parameters) == set(candidate.parameters.model_dump())


@pytest.mark.parametrize("runbook_id", _CANDIDATE_RUNBOOKS, ids=lambda r: r.value)
def test_precheck_parameters_point_at_the_same_resource_as_the_target_arn(
    runbook_id, candidate_factory, precheck_parameters_factory
):
    """후보 → precheck 변환이 7종 전부 성립하고, 자원 ID 가 target_arn 과 같다.

    이 일치가 깨진 입력을 하네스가 만들면 precheck 의 Scope Escalation 2차 방어
    (ADR-0007 §5 ②)가 늘 거절해, 실행 테스트가 이유 없이 빨간불이 된다.
    """
    candidate = candidate_factory(runbook_id)
    params = precheck_parameters_factory(candidate, **_LOOKUPS.get(runbook_id, {}))
    spec = RUNBOOK_SPECS[runbook_id.value]
    if spec.primary_param is not None:
        expected_resource_id = candidate.target_arn.rsplit("/", 1)[1]
        assert params.model_dump()[spec.primary_param] == expected_resource_id


@pytest.mark.parametrize("runbook_id", _ROLLBACK_RUNBOOKS, ids=lambda r: r.value)
def test_candidate_factory_refuses_rollback_runbooks(runbook_id, candidate_factory):
    """롤백 3종은 AI 후보가 될 수 없다(ADR-0004). 하네스가 그 경로를 열어 두면 안 된다."""
    with pytest.raises(ValueError, match="AI 후보가 될 수 없다"):
        candidate_factory(runbook_id)


# ------------------------------------------------------------------ idempotency_key
def test_idempotency_keys_are_unique_and_within_the_column_width(idempotency_key_factory):
    keys = {idempotency_key_factory() for _ in range(50)}
    assert len(keys) == 50
    assert all(len(key) <= IDEMPOTENCY_KEY_MAX for key in keys)


def test_idempotency_key_factory_fails_loudly_instead_of_truncating(idempotency_key_factory):
    """잘라 내면 서로 다른 두 요청이 같은 키가 된다 — 멱등 테스트가 가짜가 된다(#116)."""
    with pytest.raises(ValueError, match=str(IDEMPOTENCY_KEY_MAX)):
        idempotency_key_factory("x" * IDEMPOTENCY_KEY_MAX)


def test_execute_request_carries_only_the_ssot_three_fields(execute_request_factory):
    """target_arn·실행 파라미터는 요청에 싣지 않는다 — 서버가 저장된 제안에서 재구성한다."""
    request = execute_request_factory()
    assert set(request.model_dump()) == {"incident_id", "runbook_id", "idempotency_key"}


# ------------------------------------------------------------------ C. 원복 백업
def test_backup_required_runbooks_are_exactly_the_four_restore_runbooks():
    """원복 계열 4종.

    집합 자체는 RUNBOOK_SPECS 에서 파생하지만, "지금 4종이 무엇인가"는 못 박아 둔다.
    파생만 해 두면 5종이 됐을 때 하네스를 쓰는 쪽이 바뀌어야 하는데도 조용히 늘어난다.
    """
    assert BACKUP_REQUIRED_RUNBOOKS == {
        RunbookId.RUNBOOK_NACL_RESTORE.value,
        RunbookId.RUNBOOK_EC2_UNISOLATE.value,
        RunbookId.RUNBOOK_SG_RECREATE.value,
        RunbookId.RUNBOOK_EC2_REVERT_SIZE.value,
    }


@pytest.mark.parametrize("runbook_value", sorted(BACKUP_REQUIRED_RUNBOOKS))
def test_backup_loader_serves_the_record_by_id_and_by_target(
    runbook_value, backup_loader_factory
):
    runbook_id = RunbookId(runbook_value)
    target_arn = default_target_arn(runbook_id)
    loader, record = backup_loader_factory(runbook_id, target_arn)

    assert loader.get(record.backup_record_id) is record
    assert loader.get("bkp-does-not-exist") is None
    assert loader.latest_for_target(target_arn, record.backup_type) is record

    other_arn = default_target_arn(runbook_id, account_id="210987654321")
    assert loader.latest_for_target(other_arn, record.backup_type) is None


def test_backup_loader_honours_payload_match():
    """NACL 하나에 deny 규칙이 둘 이상 쌓이면 '최신 하나'로는 복원 대상을 못 고른다.

    실제 로더 계약이 payload_match 를 요구하는 이유이며(executor.py:110), 대역이
    그 필터를 빼면 테스트는 통과해도 실제로는 오래된 규칙을 복원하게 된다.
    """
    from services.aws.executor import BACKUP_NACL_RULE_INDEX, BackupRecordView

    target_arn = default_target_arn(RunbookId.RUNBOOK_NACL_RESTORE)
    older = BackupRecordView(
        "bkp-1", target_arn, BACKUP_NACL_RULE_INDEX, {"rule_number": 100, "egress": False}
    )
    newer = BackupRecordView(
        "bkp-2", target_arn, BACKUP_NACL_RULE_INDEX, {"rule_number": 200, "egress": False}
    )
    loader = FakeBackupLoader([older, newer])

    assert loader.latest_for_target(target_arn, BACKUP_NACL_RULE_INDEX) is newer
    assert (
        loader.latest_for_target(
            target_arn, BACKUP_NACL_RULE_INDEX, payload_match={"rule_number": 100}
        )
        is older
    )


def test_backup_record_factory_refuses_runbooks_that_do_not_use_backups(backup_record_factory):
    with pytest.raises(ValueError, match="백업 레코드를 쓰지 않는다"):
        backup_record_factory(
            RunbookId.RUNBOOK_EC2_RIGHTSIZING,
            default_target_arn(RunbookId.RUNBOOK_EC2_RIGHTSIZING),
        )


# ------------------------------------------------------------------ D. P2 로컬 FAIL 전제
def test_p2_premise_covers_all_three_runbooks():
    """ADR-0007 P2 3종이 빠짐없이 표에 있다."""
    assert P2_LOCAL_FAIL_RUNBOOKS == {
        RunbookId.RUNBOOK_EC2_ISOLATE,
        RunbookId.RUNBOOK_EC2_UNISOLATE,
        RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
    }


def test_p2_premise_is_expressed_per_guardrail_context():
    """문맥 3종이 전부 등장한다 — 후보 경로 하나로 잡으면 ROLLBACK_EXECUTION 이 빈다.

    이것이 #136 완료 조건 2번이 요구하는 것이다. 실패가 드러나는 자리가 런북마다
    다르므로(PR #121), 문맥 축이 빠지면 "P2 는 로컬에서 FAIL"이라는 전제가 UNISOLATE
    에는 적용되지 않는 것처럼 보인다.
    """
    assert {case.context for case in P2_LOCAL_FAIL_CASES} == set(GuardrailValidationContext)


def test_unisolate_premise_lives_only_in_the_rollback_context():
    """UNISOLATE 를 AI_CANDIDATE 로 적으면 틀린다 — 롤백은 후보가 될 수 없다(ADR-0004)."""
    contexts = {
        case.context
        for case in P2_LOCAL_FAIL_CASES
        if case.runbook_id is RunbookId.RUNBOOK_EC2_UNISOLATE
    }
    assert contexts == {GuardrailValidationContext.ROLLBACK_EXECUTION}


def test_p2_cases_name_the_service_that_localstack_community_lacks():
    """왜 FAIL 인지가 케이스에 적혀 있어야 나중에 버그로 오인하지 않는다."""
    assert {case.missing_service for case in P2_LOCAL_FAIL_CASES} == {"elbv2", "autoscaling"}
    assert all(case.observed for case in P2_LOCAL_FAIL_CASES)


def test_p2_case_ids_are_unique(p2_local_fail_case):
    """조합마다 고유 id — pytest 출력에서 어느 조합이 깨졌는지 보이게."""
    ids = [case.id for case in P2_LOCAL_FAIL_CASES]
    assert len(ids) == len(set(ids))
    assert p2_local_fail_case.id in ids


def test_local_fail_premise_is_gated_on_the_deployment_switch():
    """전제의 조건은 'P2 런북'이 아니라 'LocalStack 모드'다.

    ADR-0006 §3 은 AWS_ENDPOINT_URL 하나로만 전환한다. 실 AWS 스모크(§4)에서는 같은
    런북이 통과해야 하므로, 런북만 보고 FAIL 을 기대하면 그때 거짓 실패가 난다.
    """
    for runbook_id in P2_LOCAL_FAIL_RUNBOOKS:
        assert expects_local_precheck_fail(runbook_id, "localstack") is True
        assert expects_local_precheck_fail(runbook_id, "aws") is False
    assert expects_local_precheck_fail(RunbookId.RUNBOOK_EC2_RIGHTSIZING, "localstack") is False

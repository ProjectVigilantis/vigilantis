"""Action Whitelist 계약 테스트 — 확정 10종(본편 7 + 롤백 3)의 허용·차단·AI 추천 판정."""

import pytest
from ai.whitelist import (
    AI_RECOMMENDABLE_RUNBOOK_IDS,
    ALLOWED_RUNBOOK_IDS,
    ROLLBACK_RUNBOOK_IDS,
    RunbookId,
    is_ai_recommendable,
    is_allowed_runbook,
)

# ADR 원문 목록. 코드가 아니라 이 리터럴 집합이 기대값이다.
ADR_0002_MAIN_RUNBOOKS = {
    "RUNBOOK_EC2_ISOLATE",
    "RUNBOOK_NACL_ADD_DENY",
    "RUNBOOK_NACL_RESTORE",
    "RUNBOOK_SG_DELETE_ISOLATED",
    "RUNBOOK_EC2_RIGHTSIZING",
    "RUNBOOK_EC2_ENABLE_AUTOSCALING",
    "RUNBOOK_EBS_DELETE_UNATTACHED",
}
ADR_0004_ROLLBACK_RUNBOOKS = {
    "RUNBOOK_EC2_UNISOLATE",
    "RUNBOOK_SG_RECREATE",
    "RUNBOOK_EC2_REVERT_SIZE",
}
ALL_CONFIRMED_RUNBOOKS = ADR_0002_MAIN_RUNBOOKS | ADR_0004_ROLLBACK_RUNBOOKS


def test_whitelist_matches_adr_exactly():
    assert ALLOWED_RUNBOOK_IDS == ALL_CONFIRMED_RUNBOOKS
    assert {r.value for r in RunbookId} == ALL_CONFIRMED_RUNBOOKS
    assert ROLLBACK_RUNBOOK_IDS == ADR_0004_ROLLBACK_RUNBOOKS


def test_ai_recommendable_is_main_seven_only():
    # ADR-0004 정책 ②: 롤백 제외 — AI 추천 가능 = 본편 7종 그대로
    assert AI_RECOMMENDABLE_RUNBOOK_IDS == ADR_0002_MAIN_RUNBOOKS
    assert AI_RECOMMENDABLE_RUNBOOK_IDS.isdisjoint(ROLLBACK_RUNBOOK_IDS)
    assert AI_RECOMMENDABLE_RUNBOOK_IDS | ROLLBACK_RUNBOOK_IDS == ALLOWED_RUNBOOK_IDS


@pytest.mark.parametrize("runbook_id", sorted(ALL_CONFIRMED_RUNBOOKS))
def test_confirmed_runbooks_allowed(runbook_id):
    assert is_allowed_runbook(runbook_id) is True


@pytest.mark.parametrize("runbook_id", sorted(ADR_0002_MAIN_RUNBOOKS))
def test_main_runbooks_ai_recommendable(runbook_id):
    assert is_ai_recommendable(runbook_id) is True


@pytest.mark.parametrize("runbook_id", sorted(ADR_0004_ROLLBACK_RUNBOOKS))
def test_rollback_runbooks_not_ai_recommendable(runbook_id):
    assert is_ai_recommendable(runbook_id) is False


@pytest.mark.parametrize("runbook_id", [
    "RUNBOOK_EC2_DOWNSIZE",      # 폐기 2종
    "RUNBOOK_IP_BLOCK",
    "RUNBOOK_EBS_SNAPSHOT",      # 미등록(존재하지 않는 ID)
    "runbook_ec2_isolate",       # 대소문자 불일치
    "RUNBOOK_EC2_ISOLATE ",      # 공백 포함
    "",
])
def test_unlisted_runbooks_rejected(runbook_id):
    assert is_allowed_runbook(runbook_id) is False
    assert is_ai_recommendable(runbook_id) is False


def test_reexport_is_same_objects_as_schemas_source():
    # 원천은 packages/schemas/runbooks.py — ai.whitelist는 같은 객체의 재노출이어야 한다
    import ai.whitelist as reexport
    from schemas import runbooks as source

    assert reexport.RunbookId is source.RunbookId
    assert reexport.ALLOWED_RUNBOOK_IDS is source.ALLOWED_RUNBOOK_IDS
    assert reexport.ROLLBACK_RUNBOOK_IDS is source.ROLLBACK_RUNBOOK_IDS
    assert reexport.AI_RECOMMENDABLE_RUNBOOK_IDS is source.AI_RECOMMENDABLE_RUNBOOK_IDS
    assert reexport.is_allowed_runbook is source.is_allowed_runbook
    assert reexport.is_ai_recommendable is source.is_ai_recommendable

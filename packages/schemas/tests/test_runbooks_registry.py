"""Runbook ID 수준 분류(도메인·롤백 연결) 계약 테스트 (Issue #55)."""

from schemas.runbooks import (
    AI_RECOMMENDABLE_RUNBOOK_IDS,
    ALLOWED_RUNBOOK_IDS,
    ROLLBACK_RUNBOOK_BY_MAIN_ID,
    ROLLBACK_RUNBOOK_IDS,
    RUNBOOK_DOMAIN_BY_ID,
    ApprovalMode,
    RunbookDomain,
    TriggerSource,
    domain_of,
)


def test_domain_mapping_covers_whitelist_exactly():
    assert set(RUNBOOK_DOMAIN_BY_ID) == ALLOWED_RUNBOOK_IDS


def test_rollback_domain_matches_rollback_ids():
    rollback_by_domain = {
        rid for rid, d in RUNBOOK_DOMAIN_BY_ID.items() if d == RunbookDomain.ROLLBACK
    }
    assert rollback_by_domain == ROLLBACK_RUNBOOK_IDS
    # 본편 = FINOPS 3 + SECOPS 4
    finops = [rid for rid, d in RUNBOOK_DOMAIN_BY_ID.items() if d == RunbookDomain.FINOPS]
    secops = [rid for rid, d in RUNBOOK_DOMAIN_BY_ID.items() if d == RunbookDomain.SECOPS]
    assert len(finops) == 3 and len(secops) == 4
    assert set(finops) | set(secops) == AI_RECOMMENDABLE_RUNBOOK_IDS


def test_rollback_link_pairs_match_adr0002_references():
    # ADR-0002 명세의 rollback_runbook_id 참조 관계 3쌍 고정
    assert ROLLBACK_RUNBOOK_BY_MAIN_ID == {
        "RUNBOOK_EC2_ISOLATE": "RUNBOOK_EC2_UNISOLATE",
        "RUNBOOK_SG_DELETE_ISOLATED": "RUNBOOK_SG_RECREATE",
        "RUNBOOK_EC2_RIGHTSIZING": "RUNBOOK_EC2_REVERT_SIZE",
    }
    # 키는 본편, 값은 롤백 3종 전부
    assert set(ROLLBACK_RUNBOOK_BY_MAIN_ID) <= AI_RECOMMENDABLE_RUNBOOK_IDS
    assert set(ROLLBACK_RUNBOOK_BY_MAIN_ID.values()) == ROLLBACK_RUNBOOK_IDS
    # NACL 차단 해제는 주 조치(RUNBOOK_NACL_RESTORE) 경로 — 롤백 연결에 없어야 한다
    assert "RUNBOOK_NACL_ADD_DENY" not in ROLLBACK_RUNBOOK_BY_MAIN_ID


def test_domain_of_lookup():
    assert domain_of("RUNBOOK_EC2_RIGHTSIZING") is RunbookDomain.FINOPS
    assert domain_of("RUNBOOK_EC2_UNISOLATE") is RunbookDomain.ROLLBACK
    assert domain_of("RUNBOOK_NOT_REGISTERED") is None


def test_trigger_source_values():
    """실행 시작 사유 4종 — ADR-0004 1차 개정으로 확정된 어휘."""
    assert {t.value for t in TriggerSource} == {
        "USER_APPROVAL",
        "PRE_MITIGATION_0_5S",
        "TIMEOUT_ISOLATION_1M",
        "AUTO_ON_FAILURE",
    }


def test_approval_mode_values():
    """런북별 승인 정책 2종 — 구 어휘(AGENT_WAIT 등)를 재사용하지 않는다."""
    assert {a.value for a in ApprovalMode} == {"HUMAN_ONLY", "SYSTEM_OR_HUMAN"}


def test_two_axes_do_not_share_values():
    """두 축이 같은 문자열을 쓰면 코드화 시 다시 섞인다."""
    assert not {t.value for t in TriggerSource} & {a.value for a in ApprovalMode}

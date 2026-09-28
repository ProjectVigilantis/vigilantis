"""Runbook ID 수준 분류(도메인·롤백 연결) 계약 테스트 (Issue #55)."""

from schemas.runbooks import (
    AI_RECOMMENDABLE_RUNBOOK_IDS,
    ALLOWED_RUNBOOK_IDS,
    APPROVAL_MODE_BY_ROLLBACK_ID,
    AUTO_ROLLBACK_RUNBOOK_BY_MAIN_ID,
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


def test_rollback_link_pairs_are_fixed():
    # 주 조치 → 롤백 연결 3쌍 고정 (ADR-0004 등록 대상)
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


# --- 자동 발동 허용 여부 (Issue #368) -------------------------------------------


def test_approval_mode_covers_every_rollback_runbook():
    """ADR-0004 결정 표의 롤백 3행을 그대로 옮긴 표다 — 하나라도 빠지면
    AUTO_ROLLBACK_RUNBOOK_BY_MAIN_ID 파생이 KeyError로 끊긴다."""
    assert set(APPROVAL_MODE_BY_ROLLBACK_ID) == ROLLBACK_RUNBOOK_IDS


def test_only_revert_size_may_start_without_a_human():
    """`HUMAN_ONLY`는 사람이 누르기 전에는 시작되지 않는다는 뜻이다.

    셋 중 그 반대는 REVERT_SIZE 하나뿐이다(trigger_source에 AUTO_ON_FAILURE가 있다).
    """
    assert APPROVAL_MODE_BY_ROLLBACK_ID == {
        "RUNBOOK_EC2_UNISOLATE": ApprovalMode.HUMAN_ONLY,
        "RUNBOOK_SG_RECREATE": ApprovalMode.HUMAN_ONLY,
        "RUNBOOK_EC2_REVERT_SIZE": ApprovalMode.SYSTEM_OR_HUMAN,
    }


def test_auto_rollback_pairs_are_narrower_than_the_rollback_pairs():
    """**짝이 있다고 자동 발동이 열리지 않는다.**

    두 표를 가르는 것이 이 상수의 존재 이유다 — 저쪽은 "관제자 복구 버튼이 무엇을
    여는가", 이쪽은 "**사람 없이** 발동해도 되는가"를 답한다. 같은 표로 쓰면 SG 삭제가
    UNKNOWN으로 끝났을 때 시스템이 승인 없이 SG를 다시 만든다(Issue #368).
    """
    assert AUTO_ROLLBACK_RUNBOOK_BY_MAIN_ID == {
        "RUNBOOK_EC2_RIGHTSIZING": "RUNBOOK_EC2_REVERT_SIZE"
    }
    assert set(AUTO_ROLLBACK_RUNBOOK_BY_MAIN_ID) < set(ROLLBACK_RUNBOOK_BY_MAIN_ID)
    # 짝이 HUMAN_ONLY인 두 주 조치는 자동 발동 대상이 아니다
    assert "RUNBOOK_SG_DELETE_ISOLATED" not in AUTO_ROLLBACK_RUNBOOK_BY_MAIN_ID
    assert "RUNBOOK_EC2_ISOLATE" not in AUTO_ROLLBACK_RUNBOOK_BY_MAIN_ID

"""CollectionRunStatus 내부 계약 테스트 (Issue #48) — 값 4종 고정."""

from schemas.collections import CollectionRunStatus


def test_values_match_contract_exactly():
    assert {s.value for s in CollectionRunStatus} == {
        "IN_PROGRESS", "SUCCESS", "PARTIAL", "FAILED",
    }

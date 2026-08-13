"""Action Whitelist 계약 테스트 — ADR-0002 확정 7종의 허용·차단 판정."""

import sys
from pathlib import Path

import pytest

# apps/core-api 를 import 경로에 추가 (services/tests 와 동일 방식)
CORE_API = Path(__file__).resolve().parents[2]
if str(CORE_API) not in sys.path:
    sys.path.insert(0, str(CORE_API))

from ai.whitelist import ALLOWED_RUNBOOK_IDS, RunbookId, is_allowed_runbook  # noqa: E402

# ADR-0002 원문 7종. 코드가 아니라 이 리터럴 집합이 기대값이다.
ADR_0002_RUNBOOKS = {
    "RUNBOOK_EC2_ISOLATE",
    "RUNBOOK_NACL_ADD_DENY",
    "RUNBOOK_NACL_RESTORE",
    "RUNBOOK_SG_DELETE_ISOLATED",
    "RUNBOOK_EC2_RIGHTSIZING",
    "RUNBOOK_EC2_ENABLE_AUTOSCALING",
    "RUNBOOK_EBS_DELETE_UNATTACHED",
}


def test_whitelist_matches_adr_0002_exactly():
    assert ALLOWED_RUNBOOK_IDS == ADR_0002_RUNBOOKS
    assert {r.value for r in RunbookId} == ADR_0002_RUNBOOKS


@pytest.mark.parametrize("runbook_id", sorted(ADR_0002_RUNBOOKS))
def test_confirmed_runbooks_allowed(runbook_id):
    assert is_allowed_runbook(runbook_id) is True


@pytest.mark.parametrize("runbook_id", [
    "RUNBOOK_EC2_DOWNSIZE",      # 폐기 2종
    "RUNBOOK_IP_BLOCK",
    "RUNBOOK_EC2_UNISOLATE",     # 미등록(존재하지 않는 롤백 계열)
    "runbook_ec2_isolate",       # 대소문자 불일치
    "RUNBOOK_EC2_ISOLATE ",      # 공백 포함
    "",
])
def test_unlisted_runbooks_rejected(runbook_id):
    assert is_allowed_runbook(runbook_id) is False

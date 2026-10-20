"""기록 분류 규칙만 검증한다. 저장·commit·PostgreSQL의 증거는 통합 테스트 몫이다."""

import pytest
from notification_events import classify_execution_transition
from schemas.api.actions import ExecutionStatus as Status
from schemas.runbooks import RunbookId, TriggerSource


@pytest.mark.parametrize(
    "trigger,parent,previous,next_status,held,has_steps,settled,expected",
    [
        ("PRE_MITIGATION_0_5S", None, "IN_PROGRESS", "SUCCESS", False, True, False,
         ("AUTO_ISOLATION", "RESULT", "SUCCESS", False)),
        ("TIMEOUT_ISOLATION_1M", None, "IN_PROGRESS", "FAILED", False, True, False,
         ("AUTO_ISOLATION", "RESULT", "FAILED", False)),
        ("PRE_MITIGATION_0_5S", None, "IN_PROGRESS", "UNVERIFIED", True, True, False,
         ("AUTO_ISOLATION", "RESULT", "UNVERIFIED", False)),
        ("PRE_MITIGATION_0_5S", None, "IN_PROGRESS", "FAILED", True, False, False,
         ("AUTO_ISOLATION", "RESULT", "FAILED", False)),
        ("USER_APPROVAL", None, "IN_PROGRESS", "SUCCESS", False, True, False, None),
        ("USER_APPROVAL", None, "IN_PROGRESS", "ROLLBACK_INITIATED", False, True, False,
         ("AUTO_ROLLBACK", "REQUESTED", None, False)),
        ("USER_APPROVAL", None, "ROLLBACK_INITIATED", "ROLLBACK_FAILED", False, False, False,
         ("AUTO_ROLLBACK", "RESULT", "FAILED", False)),
        ("AUTO_ON_FAILURE", "origin", "IN_PROGRESS", "SUCCESS", False, True, True,
         ("AUTO_ROLLBACK", "RESULT", "SUCCESS", True)),
        ("AUTO_ON_FAILURE", "origin", "IN_PROGRESS", "FAILED", True, True, True,
         ("AUTO_ROLLBACK", "RESULT", "UNVERIFIED", True)),
        ("AUTO_ON_FAILURE", "origin", "IN_PROGRESS", "FAILED", True, False, True,
         ("AUTO_ROLLBACK", "RESULT", "FAILED", True)),
        ("AUTO_ON_FAILURE", "origin", "IN_PROGRESS", "FAILED", False, True, True,
         ("AUTO_ROLLBACK", "RESULT", "FAILED", True)),
        ("USER_APPROVAL", "origin", "IN_PROGRESS", "SUCCESS", False, True, True, None),
        ("AUTO_ON_FAILURE", "origin", "IN_PROGRESS", "SUCCESS", False, True, False, None),
        ("USER_APPROVAL", None, "ROLLBACK_INITIATED", "ROLLBACK_INITIATED", False, True, False, None),
    ],
)
def test_transition_classification(
    trigger, parent, previous, next_status, held, has_steps, settled, expected,
):
    fact = classify_execution_transition(
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        trigger_source=TriggerSource(trigger),
        parent_execution_id=parent,
        previous_status=Status(previous),
        next_status=Status(next_status),
        verification_held=held,
        has_execution_steps=has_steps,
        origin_settled=settled,
    )
    observed = None if fact is None else (
        fact.kind.value, fact.phase.value,
        fact.outcome.value if fact.outcome is not None else None, fact.use_origin,
    )
    assert observed == expected


@pytest.mark.parametrize(
    "rejected,has_steps,held,next_status,trigger,expected",
    [
        (True, False, False, "FAILED", "AUTO_ON_FAILURE", "REJECTED"),
        (False, False, False, "FAILED", "AUTO_ON_FAILURE", "FAILED"),
        (True, True, False, "FAILED", "AUTO_ON_FAILURE", "FAILED"),
        (True, True, True, "FAILED", "AUTO_ON_FAILURE", "UNVERIFIED"),
        (True, False, True, "FAILED", "AUTO_ON_FAILURE", "FAILED"),
        (True, False, False, "SUCCESS", "AUTO_ON_FAILURE", "SUCCESS"),
        (True, False, False, "FAILED", "USER_APPROVAL", None),
    ],
)
def test_rollback_rejection_requires_pre_execution_guardrail_failure(
    rejected, has_steps, held, next_status, trigger, expected,
):
    fact = classify_execution_transition(
        runbook_id=RunbookId.RUNBOOK_EC2_REVERT_SIZE,
        trigger_source=TriggerSource(trigger),
        parent_execution_id="origin",
        previous_status=Status.IN_PROGRESS,
        next_status=Status(next_status),
        verification_held=held,
        has_execution_steps=has_steps,
        origin_settled=True,
        guardrail_rejected=rejected,
    )
    assert (fact.outcome.value if fact is not None else None) == expected

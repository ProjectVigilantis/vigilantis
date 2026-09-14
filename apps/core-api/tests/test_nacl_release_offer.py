"""차단 뒤 해제 제안 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #329)

차단(NACL_ADD_DENY)이 SUCCESS로 닫힌 뒤 [해제] 버튼이 설 후보를 **누가, 무엇으로, 몇 번**
만드는가를 본다.

  - 차단이 닫힌 그 주기에 해제 후보가 가드레일 4단계를 거쳐 저장되고, 판정이 남는가
  - 후보 값(rule_number·egress·대상)이 AI 출력이 아니라 **차단의 백업 레코드**에서 오는가
  - 가드레일이 거절하면 인시던트가 AWAITING_CLOSURE에 머무는가
  - 두 번째 제안이 생기지 않는가 — 통과·거절·해제 완료 뒤 모두
  - 차단 → 해제 제안 → 관제자 [해제] 접수 → 해제 SUCCESS 가 dispatcher 위에서 도는가

가짜 EC2는 NACL 엔트리를 **상태로 들고 있다**(test_nacl_restore_workflow.py와 같은 방식) —
차단이 넣은 규칙을 가드레일 ④와 해제가 실제로 보게 하기 위해서다.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import guardrails as guardrails_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.api.incidents import (  # noqa: E402
    IncidentCategory,
    IncidentStatus,
    ResolutionJudgement,
)
from schemas.api.ws import WsEventType  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.guardrails import GuardrailDecision, GuardrailValidationContext  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402

ADD_DENY = RunbookId.RUNBOOK_NACL_ADD_DENY
RESTORE = RunbookId.RUNBOOK_NACL_RESTORE

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
ACL = "acl-0abc123456789def0"
ACL_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-acl/{ACL}"
RULE_NUMBER = 100
CIDR = "203.0.113.10/32"
BLOCK_PARAMS = {"rule_number": RULE_NUMBER, "cidr_block": CIDR, "protocol": "tcp"}
BLOCK_EVIDENCE = ("3f5b8c1e-0000-4000-8000-0000000000aa",)


class StatefulEc2:
    """NACL 1개의 엔트리를 들고 있는 가짜 EC2 — 넣은 규칙이 실제로 슬롯에 남는다."""

    def __init__(self, state):
        self._state = state

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append(operation)
            hook = self._state["hooks"].get(operation)
            if hook is not None:
                hook(kwargs)
            entries = self._state["entries"]
            if operation == "describe_network_acls":
                return {"NetworkAcls": [{"NetworkAclId": ACL, "Entries": list(entries)}]}
            if operation == "create_network_acl_entry":
                entries.append(
                    {
                        key: kwargs[key]
                        for key in ("RuleNumber", "Egress", "CidrBlock", "Protocol", "RuleAction")
                    }
                )
                return {"ResponseMetadata": {"RequestId": "req-create"}}
            if operation == "delete_network_acl_entry":
                entries[:] = [
                    e
                    for e in entries
                    if (e["RuleNumber"], e["Egress"]) != (kwargs["RuleNumber"], kwargs["Egress"])
                ]
                return {"ResponseMetadata": {"RequestId": "req-delete"}}
            return {}

        return call


@pytest.fixture
def aws(monkeypatch):
    state = {"entries": [], "calls": [], "hooks": {}}

    def factory(service, region=None, **_):
        return StatefulEc2(state)

    monkeypatch.setattr(bk, "aws_client", factory)
    monkeypatch.setattr(ex, "aws_client", factory)
    return state


def seed_nacl_asset(db) -> None:
    """③ ARN Match가 대조할 NACL 자산 행 — 없으면 해제 후보가 ③에서 떨어진다."""
    run_row = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region=REGION,
        mode="localstack",
        lookback_days=3,
        period_seconds=3600,
    )
    assets_repo.upsert_asset(
        db,
        arn=ACL_ARN,
        asset_type=AssetType.NACL,
        resource_id=ACL,
        account_id=ACCOUNT,
        region=REGION,
        spec={},
        collection_run_id=run_row.collection_run_id,
        collected_at=datetime.now(timezone.utc),
        state=None,
    )


@pytest.fixture()
def reserved_block(db, make_incident, make_candidate, make_execution, seed_summary_lines):
    """관제자가 [조치 실행]으로 차단을 접수한 상태 — CLAIMED 후보 + IN_PROGRESS 실행.

    managed=False면 NACL 자산 행을 넣지 않는다(③ 거절 경로).
    """

    def _make(*, managed=True):
        if managed:
            seed_nacl_asset(db)
        incident = make_incident(
            db,
            category=IncidentCategory.SECOPS,
            subject_arn=ACL_ARN,
            status=IncidentStatus.ACTION_IN_PROGRESS,
            summary_lines=seed_summary_lines,
        )
        candidate = make_candidate(
            db,
            incident,
            runbook_id=ADD_DENY,
            target_arn=ACL_ARN,
            parameters=BLOCK_PARAMS,
            evidence_ids=BLOCK_EVIDENCE,
            status=CandidateStatus.CLAIMED,
        )
        execution = make_execution(
            db, incident, runbook_id=ADD_DENY, target_arn=ACL_ARN, candidate=candidate
        )
        db.commit()
        return incident.incident_id, execution.execution_id

    return _make


def release_candidates(db, incident_id):
    return [
        row
        for row in incidents_repo.list_candidates(db, incident_id)
        if row.runbook_id is RESTORE
    ]


def incident_status(db, incident_id):
    return incidents_repo.get_incident(db, incident_id).status


def block_of(db, incident_id):
    [row] = [
        r for r in exec_repo.list_by_incident(db, incident_id) if r.runbook_id is ADD_DENY
    ]
    return row


# ------------------------------------------------------ 차단이 닫힌 주기에 [해제]가 선다


def test_the_block_cycle_also_offers_the_release(db, reserved_block, aws):
    """차단 SUCCESS와 해제 제안이 한 주기에 끝난다 — 인시던트가 승인 대기로 다시 오른다."""
    incident_id, execution_id = reserved_block()
    events = []

    report = dispatcher.dispatch_pending(db, events.append)

    assert (report.closed, report.release_offered, report.release_rejected) == (1, 1, 0)
    assert exec_repo.get_execution(db, execution_id).status is ExecutionStatus.SUCCESS
    assert incident_status(db, incident_id) is IncidentStatus.AWAITING_APPROVAL
    [offer] = release_candidates(db, incident_id)
    assert offer.status is CandidateStatus.EXECUTABLE
    # 차단 확정 발행 뒤에 제안 발행이 한 번 더 온다 — 화면이 [해제]를 보려면 재조회해야 한다
    assert [e.event_type for e in events] == [
        WsEventType.EXECUTION_UPDATED,
        WsEventType.INCIDENT_UPDATED,
        WsEventType.INCIDENT_UPDATED,
    ]


def test_the_offer_passes_all_four_guardrail_steps_and_leaves_a_record(
    db, reserved_block, aws
):
    """AI 후보와 같은 4단계를 지나고 판정이 남는다 — 서버가 만든 후보라고 건너뛰지 않는다."""
    incident_id, _ = reserved_block()

    dispatcher.dispatch_pending(db)

    [offer] = release_candidates(db, incident_id)
    evaluation = guardrails_repo.latest_for_candidate(db, offer.candidate_id)
    assert evaluation is not None
    assert evaluation.validation_context is GuardrailValidationContext.AI_CANDIDATE
    assert evaluation.result is GuardrailDecision.PASS
    assert len(evaluation.steps) == 4
    # ④는 실제로 슬롯을 봤다 — 차단의 백업 캡처 1회 + ④ 1회
    assert aws["calls"].count("describe_network_acls") == 2


def test_the_offer_takes_its_values_from_the_blocks_backup(db, reserved_block, aws):
    """후보 값은 차단이 결속한 백업에서 온다 — 차단 후보의 값이나 AI 출력이 아니다."""
    incident_id, _ = reserved_block()

    dispatcher.dispatch_pending(db)

    block = block_of(db, incident_id)
    record = exec_repo.get_backup_record(db, block.backup_record_id)
    [offer] = release_candidates(db, incident_id)
    assert offer.target_arn == record.target_arn == ACL_ARN
    assert offer.parameters == {
        "rule_number": record.payload["rule_number"],
        "egress": record.payload["egress"],
    }
    assert offer.evidence_ids == list(BLOCK_EVIDENCE)
    assert offer.display_parameters  # 계약이 파생시킨 화면 표시본


def test_values_follow_the_backup_even_when_the_candidate_says_otherwise(
    db, make_incident, make_candidate, make_execution, aws
):
    """차단 후보와 백업이 갈리면 백업을 따른다 — 실제로 넣은 규칙을 가리키는 것은 백업이다."""
    seed_nacl_asset(db)
    incident = make_incident(
        db,
        category=IncidentCategory.SECOPS,
        subject_arn=ACL_ARN,
        status=IncidentStatus.AWAITING_CLOSURE,
    )
    candidate = make_candidate(
        db,
        incident,
        runbook_id=ADD_DENY,
        target_arn=ACL_ARN,
        parameters=BLOCK_PARAMS,  # rule 100
        status=CandidateStatus.CLAIMED,
    )
    block = make_execution(
        db,
        incident,
        runbook_id=ADD_DENY,
        target_arn=ACL_ARN,
        candidate=candidate,
        status=ExecutionStatus.SUCCESS,
    )
    record = exec_repo.create_backup_record(
        db,
        execution_id=block.execution_id,
        target_arn=ACL_ARN,
        backup_type=ex.BACKUP_NACL_RULE_INDEX,
        payload={
            "rule_number": 120,
            "egress": False,
            "cidr_block": CIDR,
            "protocol": "6",
            "rule_action": "deny",
        },
    )
    assert exec_repo.bind_backup_record(db, block.execution_id, record.backup_record_id)
    aws["entries"].append(
        {"RuleNumber": 120, "Egress": False, "CidrBlock": CIDR, "Protocol": "6", "RuleAction": "deny"}
    )
    db.commit()

    offer = workflows.offer_nacl_release(db, block.execution_id)

    assert offer.candidate_status is CandidateStatus.EXECUTABLE
    [row] = release_candidates(db, incident.incident_id)
    assert row.parameters == {"rule_number": 120, "egress": False}


# ------------------------------------------------------------- 거절은 머문다


def test_a_rejected_offer_keeps_the_incident_awaiting_closure(db, reserved_block, aws):
    """해제할 규칙이 이미 없다 — ④가 거절하고 인시던트는 종료 대기에 머문다."""
    incident_id, _ = reserved_block()

    def rule_removed_by_someone(kwargs):
        # 차단은 이미 들어갔고, 가드레일 ④가 보기 직전에 누가 규칙을 지웠다. 차단 쪽
        # describe는 삽입 전(백업 캡처) 한 번뿐이라 삽입 뒤의 describe는 ④다
        if "create_network_acl_entry" in aws["calls"]:
            aws["entries"].clear()

    aws["hooks"]["describe_network_acls"] = rule_removed_by_someone
    events = []

    report = dispatcher.dispatch_pending(db, events.append)

    assert (report.release_offered, report.release_rejected) == (0, 1)
    assert incident_status(db, incident_id) is IncidentStatus.AWAITING_CLOSURE
    [offer] = release_candidates(db, incident_id)
    assert offer.status is CandidateStatus.REJECTED
    evaluation = guardrails_repo.latest_for_candidate(db, offer.candidate_id)
    assert evaluation.result is not GuardrailDecision.PASS
    # 거절은 제안 목록에 오르지 않아 다시 알릴 것이 없다 — 발행은 차단 확정 한 벌뿐이다
    assert [e.event_type for e in events] == [
        WsEventType.EXECUTION_UPDATED,
        WsEventType.INCIDENT_UPDATED,
    ]


def test_an_unmanaged_nacl_is_rejected_at_arn_match(db, reserved_block, aws):
    """수집되지 않은 NACL은 ③에서 떨어진다 — AI 후보와 같은 관문이다."""
    incident_id, _ = reserved_block(managed=False)

    report = dispatcher.dispatch_pending(db)

    assert report.release_rejected == 1
    assert incident_status(db, incident_id) is IncidentStatus.AWAITING_CLOSURE


# ------------------------------------------------------------- 제안은 한 번


def test_no_second_offer_after_a_pass(db, reserved_block, aws):
    """다음 주기는 이 차단을 스캔에서부터 뺀다 — 가드레일 ④(AWS 조회)도 다시 부르지 않는다."""
    incident_id, _ = reserved_block()
    dispatcher.dispatch_pending(db)
    calls = len(aws["calls"])

    again = dispatcher.dispatch_pending(db)

    assert (again.release_offered, again.release_rejected) == (0, 0)
    assert len(release_candidates(db, incident_id)) == 1
    assert len(aws["calls"]) == calls


def test_an_offer_that_lands_while_the_guardrail_ran_is_not_doubled(
    db, reserved_block, make_candidate, aws
):
    """스캔과 저장 사이에 다른 제안이 먼저 들어왔다 — 잠근 뒤 다시 보고 두 번째를 버린다."""
    incident_id, execution_id = reserved_block()
    workflows.run_nacl_add_deny_execution(db, execution_id)
    workflows.close_execution(db, execution_id, next_status=ExecutionStatus.SUCCESS)

    def another_offer_lands(kwargs):
        aws["hooks"].pop("describe_network_acls")
        make_candidate(
            db,
            incident_id,
            runbook_id=RESTORE,
            target_arn=ACL_ARN,
            parameters={"rule_number": RULE_NUMBER, "egress": False},
            status=CandidateStatus.REJECTED,
        )
        db.commit()

    aws["hooks"]["describe_network_acls"] = another_offer_lands

    offer = workflows.offer_nacl_release(db, execution_id)

    assert offer.candidate_status is None and "이미" in offer.skipped_reason
    assert len(release_candidates(db, incident_id)) == 1
    assert incident_status(db, incident_id) is IncidentStatus.AWAITING_CLOSURE


def test_no_second_offer_after_a_rejection(db, reserved_block, aws):
    """거절된 제안을 다시 만들지 않는다 — 매 주기 같은 판정을 반복하지 않는다."""
    incident_id, _ = reserved_block(managed=False)
    dispatcher.dispatch_pending(db)

    again = dispatcher.dispatch_pending(db)

    assert (again.release_offered, again.release_rejected) == (0, 0)
    assert len(release_candidates(db, incident_id)) == 1


def test_a_resolved_incident_gets_no_offer(db, reserved_block, aws):
    """관제자가 해제 없이 닫았다 — 그 판단을 제안으로 뒤집지 않는다."""
    incident_id, execution_id = reserved_block()
    # 제안 스캔을 거치지 않고 차단만 확정한다 — 확정 직후 관제자가 먼저 닫은 상황
    workflows.run_nacl_add_deny_execution(db, execution_id)
    workflows.close_execution(db, execution_id, next_status=ExecutionStatus.SUCCESS)
    assert workflows.resolve_incident(db, incident_id, ResolutionJudgement.JUSTIFIED)

    report = dispatcher.dispatch_pending(db)

    assert (report.release_offered, report.release_rejected) == (0, 0)
    assert release_candidates(db, incident_id) == []


def test_an_incident_closed_while_the_guardrail_ran_is_left_alone(
    db, reserved_block, aws
):
    """가드레일이 AWS를 부르는 사이 관제자가 닫았다 — 잠근 뒤 다시 보고 저장하지 않는다."""
    incident_id, execution_id = reserved_block()
    workflows.run_nacl_add_deny_execution(db, execution_id)
    workflows.close_execution(db, execution_id, next_status=ExecutionStatus.SUCCESS)
    assert incident_status(db, incident_id) is IncidentStatus.AWAITING_CLOSURE

    def operator_resolves(kwargs):
        aws["hooks"].pop("describe_network_acls")
        workflows.resolve_incident(db, incident_id, ResolutionJudgement.JUSTIFIED)

    aws["hooks"]["describe_network_acls"] = operator_resolves

    offer = workflows.offer_nacl_release(db, execution_id)

    assert offer.candidate_status is None and "RESOLVED" in offer.skipped_reason
    assert incident_status(db, incident_id) is IncidentStatus.RESOLVED
    assert release_candidates(db, incident_id) == []


def test_a_block_without_its_backup_is_not_offered(
    db, make_incident, make_candidate, make_execution, aws
):
    """되돌릴 근거가 없는 차단에는 해제를 제안하지 않는다 — 현물로 추정하지 않는다(ADR-0008 §1 ④)."""
    seed_nacl_asset(db)
    incident = make_incident(
        db,
        category=IncidentCategory.SECOPS,
        subject_arn=ACL_ARN,
        status=IncidentStatus.AWAITING_CLOSURE,
    )
    candidate = make_candidate(
        db,
        incident,
        runbook_id=ADD_DENY,
        target_arn=ACL_ARN,
        parameters=BLOCK_PARAMS,
        status=CandidateStatus.CLAIMED,
    )
    block = make_execution(
        db,
        incident,
        runbook_id=ADD_DENY,
        target_arn=ACL_ARN,
        candidate=candidate,
        status=ExecutionStatus.SUCCESS,
    )
    db.commit()

    offer = workflows.offer_nacl_release(db, block.execution_id)

    assert offer.candidate_status is None
    assert "백업" in offer.skipped_reason
    assert release_candidates(db, incident.incident_id) == []
    assert aws["calls"] == []  # 가드레일까지 가지 않았다
    # 후보 행이 없으니 주기마다 다시 걸린다 — 신호는 주기 요약 카운터가 나른다
    assert dispatcher.dispatch_pending(db).release_skipped == 1


def test_the_offer_refuses_another_runbook(db, make_incident, make_execution):
    """해제 제안을 낳는 조치는 차단 하나뿐이다 — 다른 런북이 오면 판정이 아니라 배선 오류다."""
    incident = make_incident(db, status=IncidentStatus.AWAITING_CLOSURE)
    other = make_execution(db, incident, status=ExecutionStatus.SUCCESS)

    with pytest.raises(ValueError):
        workflows.offer_nacl_release(db, other.execution_id)


def test_offerable_statuses_leave_out_closed_and_unanalyzed_incidents():
    """RESOLVED는 관제자 판단, FAILED·ANALYZING은 성공한 차단 뒤에 오는 자리가 아니다."""
    assert workflows.RELEASE_OFFERABLE_STATUSES.isdisjoint(
        {IncidentStatus.RESOLVED, IncidentStatus.FAILED, IncidentStatus.ANALYZING}
    )


# ------------------------------------------------------- 차단 → 해제 왕복 (T2 7·8번)


def test_block_offer_release_round_trip(db, reserved_block, aws):
    """차단 → [해제] 제안 → 관제자 접수 → 해제 SUCCESS. 해제 뒤에는 다시 제안하지 않는다."""
    incident_id, _ = reserved_block()
    dispatcher.dispatch_pending(db)
    assert len(aws["entries"]) == 1

    reservation = workflows.reserve_execution(
        db,
        ExecuteActionRequest(
            incident_id=incident_id,
            runbook_id=RESTORE,
            idempotency_key=str(uuid.uuid4()),
        ),
    )
    assert reservation.created

    report = dispatcher.dispatch_pending(db)

    assert report.closed == 1 and report.release_offered == 0
    release = exec_repo.get_execution(db, reservation.response.execution_id)
    assert release.status is ExecutionStatus.SUCCESS
    assert aws["entries"] == []  # 우리가 넣은 규칙이 사라졌다
    assert incident_status(db, incident_id) is IncidentStatus.AWAITING_CLOSURE
    # 해제가 쓴 근거는 차단이 남긴 그 백업이다
    assert release.backup_record_id == block_of(db, incident_id).backup_record_id

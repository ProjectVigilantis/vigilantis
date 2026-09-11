"""NACL_RESTORE 실행 워크플로 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #298)

AWS 분기는 services/tests/test_execute_nacl_restore.py가 맡고, 여기서는 **근거를 어떻게
고르고, 언제 결속하고, 끊긴 해제를 무엇으로 판정하는가**를 본다.

  - 대상 기준 백업 조회(latest_backup_for_target)가 0건·여러 건일 때 무엇을 돌려주는가
  - 찾은 백업이 **첫 AWS 변경 이전에** 자기 행에 결속·커밋되는가 (ADR-0008 §4)
  - 로더가 없을 때와 백업이 없을 때가 서로 다른 결과인가 (ADR-0007 §1)
  - 후보 가드레일 ④가 트랜잭션 밖에서 백업을 읽고 예외 대신 판정을 내는가
  - 차단 → 해제 왕복이 dispatcher 위에서 도는가

가짜 EC2는 NACL 엔트리를 **상태로 들고 있다** — 차단이 넣은 규칙을 해제가 지우는지를
호출 목록이 아니라 슬롯의 내용으로 확인하기 위해서다.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

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
from schemas.agents import AgentGraphOutput  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.incidents import AgentInvocationStatus  # noqa: E402
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect
ADD_DENY = RunbookId.RUNBOOK_NACL_ADD_DENY
RESTORE = RunbookId.RUNBOOK_NACL_RESTORE

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
ACL = "acl-0abc123456789def0"
ACL_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-acl/{ACL}"
OTHER_ACL_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-acl/acl-0fff123456789def0"
RULE_NUMBER = 100
CIDR = "203.0.113.10/32"
BLOCK_PARAMS = {"rule_number": RULE_NUMBER, "cidr_block": CIDR, "protocol": "tcp"}
RELEASE_PARAMS = {"rule_number": RULE_NUMBER, "egress": False}


def fingerprint(**overrides) -> dict:
    """차단이 남기는 백업 payload — schemas.backups.NaclRuleIndexBackup 모양이다."""
    values = {
        "rule_number": RULE_NUMBER,
        "egress": False,
        "cidr_block": CIDR,
        "protocol": "6",
        "rule_action": "deny",
    }
    values.update(overrides)
    return values


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Op",
    )


class StatefulEc2:
    """NACL 1개의 엔트리를 들고 있는 가짜 EC2 — 넣은 규칙이 실제로 슬롯에 남는다."""

    def __init__(self, state):
        self._state = state

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            hook = self._state["hooks"].get(operation)
            if hook is not None:
                hook(kwargs)
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            entries = self._state["entries"]
            if operation == "describe_network_acls":
                if self._state["acl_missing"]:
                    return {"NetworkAcls": []}
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
    state = {"entries": [], "calls": [], "overrides": {}, "hooks": {}, "acl_missing": False}

    def factory(service, region=None, **_):
        return StatefulEc2(state)

    monkeypatch.setattr(bk, "aws_client", factory)
    monkeypatch.setattr(ex, "aws_client", factory)

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.state = state
    configure.calls = state["calls"]
    return configure


def operations(aws):
    return [name for name, _ in aws.calls]


def our_rule(**overrides) -> dict:
    values = {
        "RuleNumber": RULE_NUMBER,
        "Egress": False,
        "CidrBlock": CIDR,
        "Protocol": "6",
        "RuleAction": "deny",
    }
    values.update(overrides)
    return values


@pytest.fixture()
def incident(db, make_incident):
    return make_incident(
        db,
        category=IncidentCategory.SECOPS,
        subject_arn=ACL_ARN,
        status=IncidentStatus.ACTION_IN_PROGRESS,
    )


@pytest.fixture()
def block(db, incident, make_candidate, make_execution, aws):
    """차단 1건을 **실제 경로로** 돌려 SUCCESS로 확정한다 — 백업 모양이 운영과 같다."""

    def _block(**params):
        candidate = make_candidate(
            db,
            incident,
            runbook_id=ADD_DENY,
            target_arn=ACL_ARN,
            parameters={**BLOCK_PARAMS, **params},
            status=CandidateStatus.CLAIMED,
        )
        execution = make_execution(
            db, incident, runbook_id=ADD_DENY, target_arn=ACL_ARN, candidate=candidate
        )
        outcome = workflows.run_nacl_add_deny_execution(db, execution.execution_id)
        assert outcome.succeeded, outcome.error_summary
        workflows.close_execution(
            db, execution.execution_id, next_status=ExecutionStatus.SUCCESS
        )
        return execution

    return _block


@pytest.fixture()
def release(db, incident, make_candidate, make_execution):
    """관제자가 [해제]를 눌러 접수된 상태 — CLAIMED 후보 + IN_PROGRESS 실행."""

    def _release(**params):
        candidate = make_candidate(
            db,
            incident,
            runbook_id=RESTORE,
            target_arn=ACL_ARN,
            parameters={**RELEASE_PARAMS, **params},
            status=CandidateStatus.CLAIMED,
        )
        return make_execution(
            db, incident, runbook_id=RESTORE, target_arn=ACL_ARN, candidate=candidate
        )

    return _release


def run(db, execution):
    return workflows.run_nacl_restore_execution(db, execution.execution_id)


def judge(db, execution):
    return workflows.judge_nacl_restore(db, execution.execution_id)


# ------------------------------------------------- 대상 기준 백업 조회 (ADR-0008 §1 ④)


@pytest.fixture()
def seeded_record(db, incident, make_execution):
    """조회 규칙만 따로 보려고 레코드를 직접 심는다 — 만든 실행의 상태·시각을 고른다."""
    base = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)

    def _seed(*, status, minutes=0, target_arn=ACL_ARN, payload=None):
        origin = make_execution(
            db, incident, runbook_id=ADD_DENY, target_arn=target_arn, status=status
        )
        record = exec_repo.create_backup_record(
            db,
            execution_id=origin.execution_id,
            target_arn=target_arn,
            backup_type=ex.BACKUP_NACL_RULE_INDEX,
            payload=payload or fingerprint(),
        )
        record.created_at = base + timedelta(minutes=minutes)
        db.flush()
        return record

    return _seed


def lookup(db, **match):
    return workflows._latest_backup_record(
        db, ACL_ARN, ex.BACKUP_NACL_RULE_INDEX, match or dict(RELEASE_PARAMS)
    )


def test_no_record_is_none_not_an_error(db):
    """0건 — 로더는 None을 돌려주고 precheck가 "백업 없음"으로 거절한다."""
    assert lookup(db) is None


def test_the_latest_of_several_is_chosen(db, seeded_record):
    """차단 → 해제 → 같은 슬롯에 다시 차단. 지금 슬롯에 있는 것은 나중 차단이다."""
    seeded_record(status=ExecutionStatus.SUCCESS, minutes=0, payload=fingerprint(cidr_block="198.51.100.0/24"))
    newer = seeded_record(status=ExecutionStatus.SUCCESS, minutes=5)

    assert lookup(db).backup_record_id == newer.backup_record_id


def test_a_failed_blocks_record_does_not_shadow_the_applied_one(db, seeded_record):
    """실패한 차단도 백업을 남긴다(백업이 변경보다 먼저 커밋되므로). 최신이라고 고르면
    들어가지도 않은 규칙의 fingerprint로 실제 규칙을 가린다."""
    applied = seeded_record(status=ExecutionStatus.SUCCESS, minutes=0)
    seeded_record(
        status=ExecutionStatus.FAILED, minutes=5, payload=fingerprint(cidr_block="198.51.100.0/24")
    )

    assert lookup(db).backup_record_id == applied.backup_record_id


def test_a_record_already_used_for_release_is_not_offered_again(
    db, seeded_record, incident, make_execution
):
    """해제는 쓴 레코드를 자기 행에 결속한다 — 그 결속이 "이미 되돌렸다"는 기록이다.
    다시 내주면 같은 값으로 새로 생긴 제3자 규칙을 우리 것으로 읽는다."""
    record = seeded_record(status=ExecutionStatus.SUCCESS)
    used = make_execution(
        db, incident, runbook_id=RESTORE, target_arn=ACL_ARN, status=ExecutionStatus.SUCCESS
    )
    exec_repo.bind_backup_record(db, used.execution_id, record.backup_record_id)
    db.flush()

    assert lookup(db) is None


def test_a_release_that_failed_does_not_consume_the_record(
    db, seeded_record, incident, make_execution
):
    record = seeded_record(status=ExecutionStatus.SUCCESS)
    failed = make_execution(
        db, incident, runbook_id=RESTORE, target_arn=ACL_ARN, status=ExecutionStatus.FAILED
    )
    exec_repo.bind_backup_record(db, failed.execution_id, record.backup_record_id)
    db.flush()

    assert lookup(db).backup_record_id == record.backup_record_id


@pytest.mark.parametrize(
    "seed",
    [
        {"target_arn": OTHER_ACL_ARN},
        {"payload": fingerprint(rule_number=200)},
        {"payload": fingerprint(egress=True)},
    ],
)
def test_other_targets_and_slots_are_not_matched(db, seeded_record, seed):
    seeded_record(status=ExecutionStatus.SUCCESS, **seed)

    assert lookup(db) is None


def test_the_db_loader_answers_instead_of_raising(db, seeded_record):
    """종전에는 NotImplementedError였다 — 미구현이 거절로 둔갑하지 않게 막아 둔 자리다."""
    record = seeded_record(status=ExecutionStatus.SUCCESS)
    loader = workflows._DbBackupRecordLoader(db)

    view = loader.latest_for_target(ACL_ARN, ex.BACKUP_NACL_RULE_INDEX, dict(RELEASE_PARAMS))

    assert view.backup_record_id == record.backup_record_id
    assert view.payload == fingerprint()


def test_precheck_through_the_db_loader_passes_on_our_rule(db, seeded_record, aws):
    seeded_record(status=ExecutionStatus.SUCCESS)
    aws.state["entries"].append(our_rule())

    outcome = ex.precheck(
        RESTORE,
        ACL_ARN,
        {**RELEASE_PARAMS, "network_acl_id": ACL, "evidence_id": "ev-1"},
        backup_loader=workflows._DbBackupRecordLoader(db),
    )

    assert outcome.passed, outcome.verification_summary


def test_missing_loader_and_missing_backup_are_different_results(db, aws):
    """ADR-0007 §1 — 로더 미배선은 배선 오류(예외), 백업 부재는 판정(거절)이다."""
    params = {**RELEASE_PARAMS, "network_acl_id": ACL, "evidence_id": "ev-1"}

    with pytest.raises(RuntimeError, match="backup_loader"):
        ex.precheck(RESTORE, ACL_ARN, params)

    outcome = ex.precheck(
        RESTORE, ACL_ARN, params, backup_loader=workflows._DbBackupRecordLoader(db)
    )
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_TARGET_NOT_FOUND)


# ------------------------------------------------------------------ 실행


def test_block_then_release_removes_our_rule(db, block, release, aws):
    block()
    assert aws.state["entries"] == [our_rule()]
    execution = release()

    outcome = run(db, execution)

    assert outcome.succeeded
    assert aws.state["entries"] == []
    steps = exec_repo.list_steps(db, execution.execution_id)
    assert [(s.step_type, s.status, s.effect) for s in steps] == [
        (ex.STEP_DELETE_NACL_ENTRY, S.SUCCESS, E.APPLIED)
    ]


def test_the_backup_is_bound_and_committed_before_the_delete(db, block, release, aws):
    """ADR-0008 §4 보강 — 어느 레코드로 지웠는지가 **삭제 이전에** 기록에 남아야 한다."""
    blocked = block()
    execution = release()
    seen = {}

    def before_delete(_kwargs):
        # 같은 세션이라도 커밋 여부를 보려면 트랜잭션이 닫혀 있어야 한다
        seen["in_transaction"] = db.in_transaction()
        seen["bound"] = exec_repo.get_execution(db, execution.execution_id).backup_record_id

    aws.state["hooks"]["delete_network_acl_entry"] = before_delete

    run(db, execution)

    assert seen["bound"] == blocked.backup_record_id
    # 삭제 직전 IN_PROGRESS 단계가 커밋된 뒤라 트랜잭션이 비어 있다 — 결속은 그보다 먼저다
    assert seen["in_transaction"] is False


def test_no_backup_means_no_release(db, release, aws):
    """ADR-0008 §1 ④ — 우리가 넣었다는 근거가 없는 규칙은 지울 권한도 없다."""
    aws.state["entries"].append(our_rule())
    execution = release()

    outcome = run(db, execution)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND
    assert "delete_network_acl_entry" not in operations(aws)
    assert aws.state["entries"] == [our_rule()]
    assert execution.backup_record_id is None


def test_a_third_party_rule_in_our_slot_survives(db, block, release, aws):
    """승인 대기 동안 우리 규칙이 지워지고 같은 번호에 남의 규칙이 들어왔다."""
    block()
    aws.state["entries"][:] = [our_rule(CidrBlock="10.0.0.0/8")]
    execution = release()

    outcome = run(db, execution)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert aws.state["entries"] == [our_rule(CidrBlock="10.0.0.0/8")]
    assert all(s.effect is E.NOT_APPLIED for s in outcome.steps)


def test_an_already_empty_slot_is_a_release_without_change(db, block, release, aws):
    block()
    aws.state["entries"].clear()
    execution = release()

    outcome = run(db, execution)

    assert outcome.succeeded
    assert "delete_network_acl_entry" not in operations(aws)
    assert [s.effect for s in outcome.steps] == [E.NOT_APPLIED]


def test_a_deferred_release_reuses_its_bound_backup(db, block, release, aws):
    """보류는 단계를 남기지 않아 다음 주기가 처음부터 다시 돈다 — 근거는 재사용한다(§7)."""
    blocked = block()
    execution = release()
    aws(describe_network_acls=client_error("RequestLimitExceeded", status=503))

    first = run(db, execution)

    assert first.deferred
    assert exec_repo.list_steps(db, execution.execution_id) == []
    assert execution.backup_record_id == blocked.backup_record_id

    aws.state["overrides"].clear()
    second = run(db, execution)

    assert second.succeeded
    assert aws.state["entries"] == []
    assert execution.backup_record_id == blocked.backup_record_id


def test_parameters_can_come_from_the_validated_command(
    db, block, incident, make_execution, aws
):
    block()
    execution = make_execution(
        db,
        incident,
        runbook_id=RESTORE,
        target_arn=ACL_ARN,
        validated_command={
            "runbook_id": RESTORE.value,
            "target_arn": ACL_ARN,
            "parameters": {**RELEASE_PARAMS, "network_acl_id": ACL, "evidence_id": "ev-1"},
            "evidence_ids": ["ev-1"],
        },
    )

    assert run(db, execution).succeeded
    assert aws.state["entries"] == []


def test_missing_parameters_stop_before_aws(db, incident, make_execution, aws):
    execution = make_execution(db, incident, runbook_id=RESTORE, target_arn=ACL_ARN)

    outcome = run(db, execution)

    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


def test_the_runner_refuses_another_runbook(db, incident, make_execution):
    execution = make_execution(db, incident, runbook_id=ADD_DENY, target_arn=ACL_ARN)

    with pytest.raises(ValueError):
        run(db, execution)


# ------------------------------------------- 끊긴 해제의 종료 판정 (ADR-0008 §6)


@pytest.fixture()
def interrupted(db, block, release, aws):
    """삭제 응답을 못 받은 해제 — 단계가 남고 실행은 IN_PROGRESS다(판정 경로의 입력)."""

    def _make(*, landed: bool):
        block()
        execution = release()
        if landed:
            # 삭제는 적용됐고 응답만 잃었다
            aws.state["hooks"]["delete_network_acl_entry"] = lambda _k: aws.state[
                "entries"
            ].clear()
        aws(delete_network_acl_entry=client_error("InternalError", status=500))
        outcome = run(db, execution)
        assert [s.effect for s in outcome.steps] == [E.UNKNOWN]
        aws.state["overrides"].clear()
        aws.state["hooks"].clear()
        return execution

    return _make


def test_judge_confirms_success_when_our_rule_is_gone(db, interrupted, aws):
    execution = interrupted(landed=True)

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.SUCCESS


def test_judge_fails_when_our_rule_is_still_there(db, interrupted, aws):
    """삭제가 적용되지 않았다 — 판정이 대신 지우지 않는다(재개 단위는 실행, §7)."""
    execution = interrupted(landed=False)
    before = len(aws.calls)

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert "수동 개입" in judgement.error_summary
    assert operations(aws)[before:] == ["describe_network_acls"]
    assert aws.state["entries"] == [our_rule()]


def test_judge_counts_a_reused_slot_as_released(db, interrupted, aws):
    """우리 규칙은 없고 다른 규칙이 그 슬롯을 쓴다 — 우리 차단은 효력이 없다."""
    execution = interrupted(landed=True)
    aws.state["entries"].append(our_rule(CidrBlock="10.0.0.0/8"))

    assert judge(db, execution).next_status is ExecutionStatus.SUCCESS


def test_judge_defers_when_aws_cannot_be_asked(db, interrupted, aws):
    execution = interrupted(landed=True)
    aws(describe_network_acls=client_error("RequestLimitExceeded", status=503))

    judgement = judge(db, execution)

    assert judgement.deferred
    assert judgement.error_summary is None


def test_judge_hands_a_vanished_nacl_to_a_person(db, interrupted, aws):
    execution = interrupted(landed=True)
    aws.state["acl_missing"] = True

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.FAILED


# ------------------------------------------------------------------ dispatcher 왕복


def test_dispatcher_runs_block_then_release_to_success(db, block, release, aws):
    """차단 → 해제가 dispatcher 위에서 돈다. 해제는 반환이 곧 성공의 경계라 한 주기에 닫힌다."""
    block()
    execution = release()
    incident_id = execution.incident_id
    db.commit()

    report = dispatcher.dispatch_pending(db)

    assert report.started == 1 and report.closed == 1 and report.unsupported == 0
    assert exec_repo.get_execution(db, execution.execution_id).status is ExecutionStatus.SUCCESS
    assert aws.state["entries"] == []
    assert incidents_repo.get_incident(db, incident_id).status is IncidentStatus.AWAITING_CLOSURE


def test_dispatcher_sends_an_unknown_delete_to_the_judge_next_cycle(db, block, release, aws):
    """해제는 자동 원복 짝이 없다 — 적용 여부 불명은 ROLLBACK_INITIATED가 아니라 현물 판정이다."""
    block()
    execution = release()
    db.commit()
    aws(delete_network_acl_entry=client_error("InternalError", status=500))

    first = dispatcher.dispatch_pending(db)

    assert first.awaiting_judgement == 1 and first.rollback_initiated == 0
    aws.state["overrides"].clear()

    second = dispatcher.dispatch_pending(db)

    # 삭제는 적용되지 않았으므로 우리 규칙이 남아 있다 — 사람에게 넘긴다
    assert second.judged == 1 and second.closed == 1
    assert exec_repo.get_execution(db, execution.execution_id).status is ExecutionStatus.FAILED


def test_runner_and_judge_are_registered_as_a_pair():
    """ADR-0008 §6 — runner만 있으면 끊긴 해제가 재실행도 종료도 되지 않는다."""
    assert dispatcher._RUNNERS[RESTORE] is workflows.run_nacl_restore_execution
    assert dispatcher._JUDGES[RESTORE] is workflows.judge_nacl_restore
    assert RESTORE not in dispatcher._AWAIT_JUDGEMENT_ON_SUCCESS


# ------------------------------------------- 후보 가드레일 ④의 백업 조회 배선


@pytest.fixture()
def analyzing(db, make_incident):
    """해제 후보를 받을 SECOPS Incident + ③이 대조할 NACL 자산 행."""
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
    incident = make_incident(
        db,
        category=IncidentCategory.SECOPS,
        subject_arn=ACL_ARN,
        status=IncidentStatus.ANALYZING,
    )
    assert incidents_repo.claim_agent_invocation(
        db, incident.incident_id, started_at=datetime.now(timezone.utc)
    )
    db.commit()
    return incident


def release_proposal() -> AgentGraphOutput:
    return AgentGraphOutput.model_validate(
        {
            "invocation_status": AgentInvocationStatus.SUCCEEDED.value,
            "summary_lines": ["요약 1", "요약 2", "요약 3"],
            "reviewed_risk_level": "HIGH",
            "candidates": [
                {
                    "runbook_id": RESTORE.value,
                    "target_arn": ACL_ARN,
                    "parameters": RELEASE_PARAMS,
                    "evidence_ids": ["3f5b8c1e-0000-4000-8000-000000000001"],
                }
            ],
        }
    )


def test_a_release_candidate_passes_the_guardrail_on_our_rule(
    db, analyzing, seeded_record, aws
):
    """종전에는 ④에 로더가 없어 RuntimeError였다 — 이제 트랜잭션 밖에서 판정이 난다."""
    seeded_record(status=ExecutionStatus.SUCCESS)
    aws.state["entries"].append(our_rule())
    db.commit()

    outcome = workflows.record_agent_analysis(db, analyzing.incident_id, release_proposal())

    assert (outcome.executable, outcome.rejected) == (1, 0)
    assert outcome.next_status is IncidentStatus.AWAITING_APPROVAL


def test_a_release_candidate_without_a_backup_is_rejected_not_raised(db, analyzing, aws):
    aws.state["entries"].append(our_rule())

    outcome = workflows.record_agent_analysis(db, analyzing.incident_id, release_proposal())

    assert (outcome.executable, outcome.rejected) == (0, 1)
    candidate = incidents_repo.list_candidates(db, analyzing.incident_id)[0]
    assert candidate.status is CandidateStatus.REJECTED
    # 거절이 로그가 아니라 판정 기록으로 남는다 — "왜 이 제안이 사라졌는가"의 답이다
    assert guardrails_repo.latest_for_candidate(db, candidate.candidate_id) is not None


def test_the_prefetched_loader_refuses_a_lookup_it_did_not_prepare():
    """미리 뽑지 않은 조회를 None으로 답하면 배선 누락이 "백업 없음" 거절로 둔갑한다."""
    loader = workflows._PrefetchedBackupLoader({})

    with pytest.raises(RuntimeError):
        loader.latest_for_target(ACL_ARN, ex.BACKUP_NACL_RULE_INDEX, dict(RELEASE_PARAMS))

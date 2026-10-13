"""#447 자동 격리 런타임 배선 테스트.

보장 수준(서로 섞어 읽지 않는다):
  - 실제: receive_threat의 정규화·위험 판정·Intake·commit(PostgreSQL), 디스패처의
    _DISPATCH_LOCK과 dispatch_now/dispatch_pending(실제 세션·실제 스레드), 감시 스케줄러 등록.
  - 대역(한계): workflows.reserve_auto_isolation, EC2_ISOLATE runner/judge 등록,
    incidents_repo.list_expired_agent_waits 일부. 이 파일의 통과는 오케스트레이션 배선의
    증거일 뿐이며 #443/#444 러너·백업·SG/TG 공급자로 실제 격리가 성공한다는 증거가 아니다.
    30회 계측은 선행 #452 병합 전까지 보류되며 이 파일의 범위가 아니다.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import dispatcher
import isolation_timing
import pytest
import threat_ingress
import workflows
from ai import guardrails
from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from isolation_timing import begin_trace
from mock_threat_source import parse_observation
from schemas.api.actions import ExecutionStatus
from schemas.api.assets import AssetType
from schemas.api.incidents import IncidentStatus, ResponseMode, RiskLevel
from schemas.api.ws import WsEventType
from schemas.evidence import EvidenceType
from schemas.guardrails import (
    GuardrailDecision,
    GuardrailStep,
    GuardrailStepStatus,
    GuardrailValidationContext,
    GuardrailValidationRequest,
)
from schemas.precheck import PrecheckOutcome, PrecheckReasonCode
from schemas.runbooks import RunbookId, TriggerSource
from security import soar
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "datasets/golden/secops"
ISOLATE = RunbookId.RUNBOOK_EC2_ISOLATE
SOURCE_PREFIX = "test447rt-"


def _raw(name: str) -> dict:
    raw = json.loads((GOLDEN / "input" / f"{name}.json").read_text(encoding="utf-8"))
    raw["event_id"] = f"{SOURCE_PREFIX}{uuid.uuid4().hex}"
    raw["occurred_at"] = datetime.now(UTC).isoformat()
    return raw


@pytest.fixture
def committed_sessions(pg_engine):
    sessions = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    yield sessions
    with sessions() as db:
        threats = list(db.scalars(select(models.ThreatEvent.threat_event_id).where(
            models.ThreatEvent.source_event_id.like(f"{SOURCE_PREFIX}%"),
        )))
        incident_ids = list(db.scalars(select(models.Incident.incident_id).where(
            models.Incident.threat_event_id.in_(threats),
        )))
        db.commit()
        db.execute(delete(models.Evidence).where(models.Evidence.incident_id.in_(incident_ids)))
        db.execute(delete(models.Incident).where(models.Incident.incident_id.in_(incident_ids)))
        db.execute(delete(models.ThreatEvent).where(models.ThreatEvent.threat_event_id.in_(threats)))
        db.commit()


# ---------------------------------------------------------------- trigger_isolation (대역: workflows/dispatch)

def _reservation(created=True, status=ExecutionStatus.IN_PROGRESS):
    """계약 모양 그대로의 AutoIsolationReservation — 실행할 접수는 IN_PROGRESS뿐이다."""
    return workflows.AutoIsolationReservation(
        incident_id="inc-1", execution_id="exec-1", execution_status=status, created=created,
        occurred_at=datetime.now(UTC) if created else None,
    )


@pytest.fixture
def supported(monkeypatch):
    """STUB: 선행 #444 등록을 대역으로 세운다. 실제 러너가 있다는 뜻이 아니다."""
    monkeypatch.setitem(dispatcher._RUNNERS, ISOLATE, lambda db, eid: None)
    monkeypatch.setitem(dispatcher._JUDGES, ISOLATE, lambda db, eid: None)
    assert dispatcher.supports_isolation()


@pytest.fixture
def blocked(monkeypatch):
    monkeypatch.delitem(dispatcher._RUNNERS, ISOLATE, raising=False)
    monkeypatch.delitem(dispatcher._JUDGES, ISOLATE, raising=False)
    assert not dispatcher.supports_isolation()


INSTANCE_ID = "i-0a1b2c3d4e5f60718"
TARGET_ARN = f"arn:aws:ec2:ap-northeast-2:123456789012:instance/{INSTANCE_ID}"
TG_ARN = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/tg-447/0123456789abcdef"
SG_ID = "sg-0123456789abcdef0"
PRECHECK_SUMMARY = "DESCRIBE | 확인: 대상 상태 | 미확인: 없음"


def _command_payload(instance_id=INSTANCE_ID, target_arn=TARGET_ARN, evidence_ids=("ev-1",)):
    return {
        "runbook_id": ISOLATE.value,
        "target_arn": target_arn,
        "parameters": {
            "instance_id": instance_id, "target_group_arn": TG_ARN,
            "isolation_group_id": SG_ID, "evidence_id": evidence_ids[0],
        },
        "evidence_ids": list(evidence_ids),
    }


def _typed_pass_preparation(execution_id="exec-1"):
    """STUB 입력: 실제 4단계 가드레일(순수 함수)을 통과시켜 만든 유효한 PASS 준비 결과.

    AWS Dry-Run(④)만 통과 대역이다. 반환 형태는 soar.prepare_isolation 계약과 같다.
    """
    outcome = guardrails.run_guardrail_validation(
        GuardrailValidationRequest(
            validation_context=GuardrailValidationContext.AUTO_ISOLATION,
            execution_id=execution_id, command_payload=_command_payload(),
        ),
        is_managed_arn=lambda arn: arn == TARGET_ARN,
        precheck=lambda command: PrecheckOutcome(passed=True, verification_summary=PRECHECK_SUMMARY),
    )
    assert outcome.result.result is GuardrailDecision.PASS
    return execution_id, outcome.command.model_dump(mode="json"), outcome.result


def _rejected_preparation(execution_id="exec-1"):
    """실제 가드레일(순수 함수)이 ③ ARN 단계에서 거절한 준비 결과 — (id, None, FAIL)."""
    outcome = guardrails.run_guardrail_validation(
        GuardrailValidationRequest(
            validation_context=GuardrailValidationContext.AUTO_ISOLATION,
            execution_id=execution_id, command_payload=_command_payload(),
        ),
        is_managed_arn=lambda arn: False,
        precheck=lambda command: pytest.fail("rejected command must not reach AWS Dry-Run"),
    )
    assert outcome.result.result is GuardrailDecision.FAIL and outcome.command is None
    assert outcome.result.failed_step is GuardrailStep.ARN_MATCH
    return execution_id, None, outcome.result


def _stub_flow(monkeypatch, *, final_status, reservation=None, prepared="default"):
    calls = {"dispatch_now": [], "reserve": [], "prepare": []}

    def prepare(db, incident_id, trigger_source):
        calls["prepare"].append((incident_id, trigger_source))
        return _typed_pass_preparation() if prepared == "default" else prepared

    # STUB: 준비(AWS·#452 공급 포함)와 workflows.reserve_auto_isolation은 대역이다.
    # reserve 대역은 확정된 시그니처를 그대로 강제한다 — 인자가 어긋나면 TypeError.
    def reserve(
        db, incident_id, *, trigger_source, now, execution_id, validated_command, guardrail_result,
    ):
        calls["reserve"].append({
            "incident_id": incident_id, "trigger_source": trigger_source, "now": now,
            "execution_id": execution_id, "validated_command": validated_command,
            "guardrail_result": guardrail_result,
        })
        return reservation or _reservation()

    monkeypatch.setattr(soar, "prepare_isolation", prepare)
    monkeypatch.setattr(workflows, "reserve_auto_isolation", reserve, raising=False)
    monkeypatch.setattr(executions_repo, "list_by_incident", lambda db, incident_id: [])
    monkeypatch.setattr(
        dispatcher, "dispatch_now",
        lambda db, eid, publish=None: calls["dispatch_now"].append(eid),
    )
    monkeypatch.setattr(
        executions_repo, "get_execution", lambda db, eid: SimpleNamespace(status=final_status),
    )
    return calls


@pytest.mark.parametrize(("status", "expected", "confirmed"), [
    (ExecutionStatus.SUCCESS, "success", True),
    (ExecutionStatus.FAILED, "failed", False),
    (ExecutionStatus.IN_PROGRESS, "unconfirmed", False),
])
def test_supported_trigger_reserves_dispatches_and_reports_actual_status(
    supported, monkeypatch, status, expected, confirmed,
):
    calls = _stub_flow(monkeypatch, final_status=status)
    events = []
    db = MagicMock()
    with begin_trace() as trace:
        result = soar.trigger_isolation(
            db, "inc-1", trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
            publish=events.append,
        )
    assert result == expected
    assert calls["dispatch_now"] == ["exec-1"]
    expected_id, expected_command, expected_result = _typed_pass_preparation()
    (reserved,) = calls["reserve"]
    assert reserved["incident_id"] == "inc-1"
    assert reserved["trigger_source"] is TriggerSource.PRE_MITIGATION_0_5S
    assert reserved["execution_id"] == expected_id
    assert reserved["validated_command"] == expected_command == _command_payload()
    assert reserved["guardrail_result"].result is GuardrailDecision.PASS
    assert reserved["guardrail_result"].steps == expected_result.steps
    assert reserved["now"].tzinfo is not None
    assert [e.event_type for e in events] == [WsEventType.INCIDENT_UPDATED]
    record = trace.finish("failed")  # 이미 종료된 trace는 첫 결과를 돌려준다
    assert record["outcome"] == (expected if expected != "unconfirmed" else "unconfirmed")
    assert record["confirmed"] is confirmed
    assert (record["end_to_end_ms"] is not None) is confirmed
    assert record["execution_id"] == "exec-1"
    assert record["trigger_source"] == "PRE_MITIGATION_0_5S"


def test_guardrail_rejection_is_reserved_durably_but_never_dispatched(supported, monkeypatch):
    calls = _stub_flow(
        monkeypatch, final_status=ExecutionStatus.FAILED, prepared=_rejected_preparation(),
        reservation=_reservation(status=ExecutionStatus.FAILED),  # 거절은 FAILED로 저장된다
    )
    with begin_trace() as trace:
        result = soar.trigger_isolation(
            MagicMock(), "inc-1", trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        )
    assert result == "failed"
    (reserved,) = calls["reserve"]  # FAIL 평가는 승자 실행과 함께 저장되도록 예약에 전달된다
    assert reserved["validated_command"] is None
    assert reserved["guardrail_result"].result is GuardrailDecision.FAIL
    assert reserved["guardrail_result"].failed_step is GuardrailStep.ARN_MATCH
    assert calls["dispatch_now"] == []  # 거절된 명령은 runner에 닿지 않는다
    record = trace.finish("success", confirmed=True)
    assert record["outcome"] == "failed" and record["confirmed"] is False


def test_none_preparation_never_reserves_or_dispatches(supported, monkeypatch):
    calls = _stub_flow(monkeypatch, final_status=ExecutionStatus.SUCCESS, prepared=None)
    with begin_trace() as trace:
        result = soar.trigger_isolation(
            MagicMock(), "inc-1", trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        )
    assert result == "skipped"
    assert calls["reserve"] == [] and calls["dispatch_now"] == []
    assert trace.finish("success", confirmed=True)["confirmed"] is False


def test_existing_execution_replays_dispatch_without_preparation(supported, monkeypatch):
    calls = _stub_flow(monkeypatch, final_status=ExecutionStatus.SUCCESS)
    monkeypatch.setattr(executions_repo, "list_by_incident", lambda db, iid: [
        SimpleNamespace(execution_id="old", trigger_source=TriggerSource.TIMEOUT_ISOLATION_1M,
                        runbook_id=ISOLATE),  # 다른 출처 — 무시
        SimpleNamespace(execution_id="exec-old", trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
                        runbook_id=ISOLATE),
    ])
    with begin_trace() as trace:
        result = soar.trigger_isolation(
            MagicMock(), "inc-1", trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        )
        assert trace.execution_id is None  # 옛 실행을 새 접수의 응답 시간으로 묶지 않는다
    assert result == "skipped"
    assert calls["dispatch_now"] == ["exec-old"]
    assert calls["prepare"] == [] and calls["reserve"] == []  # 새 Dry-Run·예약 없음


def test_blocked_trigger_never_reserves_dispatches_or_confirms(blocked, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("must not reserve/dispatch without prerequisites")

    monkeypatch.setattr(workflows, "reserve_auto_isolation", forbidden, raising=False)
    monkeypatch.setattr(dispatcher, "dispatch_now", forbidden)
    monkeypatch.setattr(soar, "prepare_isolation", forbidden)  # 가드레일·AWS 조회도 하지 않는다
    monkeypatch.setattr(executions_repo, "list_by_incident", forbidden)
    db = MagicMock()
    with begin_trace() as trace:
        result = soar.trigger_isolation(
            db, "inc-1", trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        )
    assert result == "prerequisite_blocked"
    db.rollback.assert_called_once()
    record = trace.finish("success", confirmed=True)
    assert record["outcome"] == "prerequisite_blocked"
    assert record["confirmed"] is False and record["end_to_end_ms"] is None
    assert record["execution_id"] is None


def test_publish_failure_does_not_lose_reservation_or_skip_dispatch(supported, monkeypatch, caplog):
    calls = _stub_flow(monkeypatch, final_status=ExecutionStatus.SUCCESS)

    def fail(event):
        raise RuntimeError("ws down")

    with caplog.at_level("ERROR", logger="vigilantis.soar"):
        result = soar.trigger_isolation(
            MagicMock(), "inc-1", trigger_source=TriggerSource.PRE_MITIGATION_0_5S, publish=fail,
        )
    assert result == "success"  # 발행 실패는 격리 결과와 별개의 보장이다
    assert calls["dispatch_now"] == ["exec-1"]
    assert "automatic_isolation_publish_failed" in caplog.text


def test_existing_reservation_is_not_republished_but_is_dispatched(supported, monkeypatch):
    calls = _stub_flow(
        monkeypatch, final_status=ExecutionStatus.FAILED, reservation=_reservation(created=False),
    )
    events = []
    result = soar.trigger_isolation(
        MagicMock(), "inc-1", trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        publish=events.append,
    )
    assert result == "failed" and events == [] and calls["dispatch_now"] == ["exec-1"]


def test_reservation_without_execution_is_skipped(supported, monkeypatch):
    calls = _stub_flow(
        monkeypatch, final_status=ExecutionStatus.SUCCESS,
        reservation=workflows.AutoIsolationReservation(
            incident_id="inc-1", skipped_reason=workflows.AutoIsolationSkipReason.RISK_NOT_ELIGIBLE),
    )
    monkeypatch.setattr(executions_repo, "get_by_deduplication_key", lambda *a: pytest.fail("no winner lookup"))
    result = soar.trigger_isolation(
        MagicMock(), "inc-1", trigger_source=TriggerSource.TIMEOUT_ISOLATION_1M,
    )
    assert result == "skipped" and calls["dispatch_now"] == []


def test_already_reserved_loser_dispatches_the_winner_before_returning(supported, monkeypatch):
    """분석 쪽 준비 중 접수 쪽이 이겼다 — 승자 실행을 같은 구간으로 통과시킨 뒤에야 돌아온다."""
    calls = _stub_flow(
        monkeypatch, final_status=ExecutionStatus.SUCCESS,
        reservation=workflows.AutoIsolationReservation(
            incident_id="inc-1", skipped_reason=workflows.AutoIsolationSkipReason.ALREADY_RESERVED),
    )
    lookups = []
    monkeypatch.setattr(executions_repo, "get_by_deduplication_key", lambda db, key: (
        lookups.append(key) or SimpleNamespace(execution_id="winner-1")))
    with begin_trace() as trace:
        result = soar.trigger_isolation(
            MagicMock(), "inc-1", trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        )
        assert trace.execution_id is None  # 패자는 승자의 실행을 자기 응답 시간으로 묶지 않는다
    assert result == "skipped"
    assert lookups == [workflows.auto_isolation_deduplication_key(
        TriggerSource.PRE_MITIGATION_0_5S, "inc-1")]
    assert calls["dispatch_now"] == ["winner-1"]
    assert len(calls["reserve"]) == 1


# ---------------------------------------------------------------- 실제 DB 준비 경로 (공급자·AWS precheck만 계약 대역)

@pytest.fixture
def seed_isolation(committed_sessions, make_incident):
    """커밋된 SECOPS 인시던트 + 근거 + 관리 자산. 정리까지 소유한다."""
    seeded = []

    def _seed(*, managed=True, risk=RiskLevel.HIGH, mode=ResponseMode.PRE_MITIGATION_0_5S,
              status=IncidentStatus.ANALYZING):
        instance_id = f"i-{uuid.uuid4().hex[:17]}"
        arn = f"arn:aws:ec2:ap-northeast-2:123456789012:instance/{instance_id}"
        with committed_sessions() as s:
            incident = make_incident(
                s, subject_arn=arn, status=status, initial_risk_level=risk, response_mode=mode,
            )
            evidence = models.Evidence(
                incident_id=incident.incident_id, evidence_type=EvidenceType.THREAT,
                source_type="test447", source_id="e", content={},
                occurred_at=datetime.now(UTC),
            )
            s.add(evidence)
            if managed:
                s.add(models.Asset(
                    arn=arn, asset_type=AssetType.EC2, resource_id=instance_id,
                    account_id="123456789012", region="ap-northeast-2",
                ))
            s.commit()
            seeded.append((incident.incident_id, arn))
            return SimpleNamespace(
                incident_id=incident.incident_id, arn=arn, instance_id=instance_id,
                evidence_id=evidence.evidence_id,
            )

    yield _seed
    with committed_sessions() as s:
        for incident_id, arn in seeded:
            ids = list(s.scalars(select(models.ActionExecution.execution_id).where(
                models.ActionExecution.incident_id == incident_id)))
            s.execute(delete(models.ExecutionStep).where(models.ExecutionStep.execution_id.in_(ids)))
            s.execute(delete(models.ActionExecution).where(
                models.ActionExecution.incident_id == incident_id))
            s.execute(delete(models.Evidence).where(models.Evidence.incident_id == incident_id))
            s.execute(delete(models.Incident).where(models.Incident.incident_id == incident_id))
            s.execute(delete(models.Asset).where(models.Asset.arn == arn))
        s.commit()


@pytest.fixture
def contracts(monkeypatch):
    """STUB: #452 공급 해석기와 AWS precheck를 계약 대역으로 세운다 (실제 공급·AWS 아님)."""
    state = SimpleNamespace(
        resolver_calls=[], precheck_calls=[], db=None, precheck_outcome=None,
        supply=SimpleNamespace(
            resolved=True, reason_code=None,
            as_lookups=lambda: {"target_group_arn": TG_ARN, "isolation_group_id": SG_ID},
        ),
    )

    def resolve(db, target_arn):
        state.resolver_calls.append(target_arn)
        return state.supply

    def precheck(runbook_id, target_arn, parameters, **kw):
        state.precheck_calls.append({
            "runbook_id": runbook_id, "target_arn": target_arn,
            "parameters": parameters.model_dump(mode="json"),
            "in_transaction": state.db.in_transaction(),
        })
        return state.precheck_outcome or PrecheckOutcome(
            passed=True, verification_summary=PRECHECK_SUMMARY,
        )

    monkeypatch.setattr(workflows, "resolve_isolation_parameters", resolve, raising=False)
    monkeypatch.setattr(soar.executor, "precheck", precheck)
    return state


def test_prepare_passes_exact_command_through_four_stages_without_open_transaction(
    committed_sessions, seed_isolation, contracts,
):
    seed = seed_isolation()
    with committed_sessions() as db:
        contracts.db = db
        prepared = soar.prepare_isolation(db, seed.incident_id, TriggerSource.PRE_MITIGATION_0_5S)
        assert not db.in_transaction()
    execution_id, command, result = prepared
    uuid.UUID(execution_id)
    expected = _command_payload(
        instance_id=seed.instance_id, target_arn=seed.arn, evidence_ids=(seed.evidence_id,),
    )
    assert command == expected
    assert result.result is GuardrailDecision.PASS and result.failed_step is None
    assert [(s.step, s.result) for s in result.steps] == [
        (step, GuardrailStepStatus.PASS) for step in (
            GuardrailStep.SCHEMA_CHECK, GuardrailStep.ACTION_WHITELIST,
            GuardrailStep.ARN_MATCH, GuardrailStep.AWS_DRY_RUN,
        )
    ]
    assert contracts.resolver_calls == [seed.arn]
    (call,) = contracts.precheck_calls
    assert call["runbook_id"] is ISOLATE and call["target_arn"] == seed.arn
    assert call["parameters"] == expected["parameters"]  # ④가 본 값 == 저장할 명령
    assert call["in_transaction"] is False  # AWS 판정은 DB 트랜잭션 밖

def test_prepare_rejection_at_arn_match_returns_fail_result_without_aws_call(
    committed_sessions, seed_isolation, contracts,
):
    seed = seed_isolation(managed=False)
    with committed_sessions() as db:
        contracts.db = db
        execution_id, command, result = soar.prepare_isolation(
            db, seed.incident_id, TriggerSource.PRE_MITIGATION_0_5S,
        )
    assert command is None
    uuid.UUID(execution_id)
    assert result.result is GuardrailDecision.FAIL and result.failed_step is GuardrailStep.ARN_MATCH
    assert [s.result for s in result.steps] == [
        GuardrailStepStatus.PASS, GuardrailStepStatus.PASS,
        GuardrailStepStatus.FAIL, GuardrailStepStatus.NOT_RUN,
    ]
    assert contracts.precheck_calls == []


def test_prepare_rejection_at_aws_dry_run_returns_fail_result(
    committed_sessions, seed_isolation, contracts,
):
    seed = seed_isolation()
    contracts.precheck_outcome = PrecheckOutcome(
        passed=False, reason_code=PrecheckReasonCode.PRECHECK_INVALID_STATE,
        verification_summary=PRECHECK_SUMMARY,
    )
    with committed_sessions() as db:
        contracts.db = db
        _, command, result = soar.prepare_isolation(
            db, seed.incident_id, TriggerSource.PRE_MITIGATION_0_5S,
        )
    assert command is None
    assert result.result is GuardrailDecision.FAIL and result.failed_step is GuardrailStep.AWS_DRY_RUN
    assert len(contracts.precheck_calls) == 1


def test_prepare_returns_none_when_supply_unresolved_or_incident_ineligible(
    committed_sessions, seed_isolation, contracts,
):
    seed = seed_isolation()
    contracts.supply = SimpleNamespace(resolved=False, reason_code=SimpleNamespace(value="TG_MISSING"))
    low = seed_isolation(risk=RiskLevel.LOW, mode=ResponseMode.AGENT_WAIT,
                         status=IncidentStatus.AWAITING_APPROVAL)
    with committed_sessions() as db:
        contracts.db = db
        assert soar.prepare_isolation(db, seed.incident_id, TriggerSource.PRE_MITIGATION_0_5S) is None
        assert contracts.resolver_calls == [seed.arn]
        assert soar.prepare_isolation(db, low.incident_id, TriggerSource.PRE_MITIGATION_0_5S) is None
        assert contracts.resolver_calls == [seed.arn]  # 부적격은 공급 조회도 하지 않는다
    assert contracts.precheck_calls == []


def test_denied_validation_reserves_failed_evaluation_but_never_dispatches(
    committed_sessions, seed_isolation, contracts, supported, monkeypatch,
):
    seed = seed_isolation(managed=False)
    reserved = []

    def reserve(db, incident_id, *, trigger_source, now, execution_id, validated_command,
                guardrail_result):
        reserved.append((execution_id, validated_command, guardrail_result))
        return workflows.AutoIsolationReservation(
            incident_id=incident_id, execution_id=execution_id,
            execution_status=ExecutionStatus.FAILED, created=True, occurred_at=now)

    monkeypatch.setattr(workflows, "reserve_auto_isolation", reserve, raising=False)
    monkeypatch.setattr(dispatcher, "dispatch_now", lambda *a, **k: pytest.fail("dispatched"))
    monkeypatch.setattr(
        executions_repo, "get_execution", lambda db, eid: SimpleNamespace(status=ExecutionStatus.FAILED),
    )
    with committed_sessions() as db:
        contracts.db = db
        with begin_trace() as trace:
            result = soar.trigger_isolation(
                db, seed.incident_id, trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
            )
    assert result == "failed"
    ((_, command, evaluation),) = reserved
    assert command is None and evaluation.result is GuardrailDecision.FAIL
    assert contracts.precheck_calls == []
    assert trace.finish("success", confirmed=True)["confirmed"] is False


def test_existing_execution_skips_preparation_and_fresh_precheck(
    committed_sessions, seed_isolation, make_execution, contracts, supported, monkeypatch,
):
    seed = seed_isolation()
    with committed_sessions() as s:
        old = make_execution(
            s, seed.incident_id, runbook_id=ISOLATE, target_arn=seed.arn,
            trigger_source=TriggerSource.PRE_MITIGATION_0_5S, status=ExecutionStatus.IN_PROGRESS,
        )
        s.commit()
        old_id = old.execution_id
    dispatched = []
    monkeypatch.setattr(dispatcher, "dispatch_now", lambda db, eid, publish=None: dispatched.append(eid))
    monkeypatch.setattr(workflows, "reserve_auto_isolation",
                        lambda *a, **k: pytest.fail("reserved"), raising=False)
    with committed_sessions() as db:
        contracts.db = db
        result = soar.trigger_isolation(
            db, seed.incident_id, trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        )
    assert result == "skipped" and dispatched == [old_id]
    assert contracts.resolver_calls == [] and contracts.precheck_calls == []


def test_failed_existing_execution_is_not_rerun_by_replay(
    committed_sessions, seed_isolation, make_execution, contracts, monkeypatch,
):
    seed = seed_isolation()
    runs = []
    monkeypatch.setitem(dispatcher._RUNNERS, ISOLATE, lambda db, eid: runs.append(eid))
    monkeypatch.setitem(dispatcher._JUDGES, ISOLATE, lambda db, eid: runs.append(eid))
    with committed_sessions() as s:
        old = make_execution(
            s, seed.incident_id, runbook_id=ISOLATE, target_arn=seed.arn,
            trigger_source=TriggerSource.PRE_MITIGATION_0_5S, status=ExecutionStatus.FAILED,
        )
        s.commit()
        old_id = old.execution_id
    monkeypatch.setattr(workflows, "reserve_auto_isolation",
                        lambda *a, **k: pytest.fail("reserved"), raising=False)
    with committed_sessions() as db:
        contracts.db = db
        result = soar.trigger_isolation(
            db, seed.incident_id, trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        )
    assert result == "skipped" and runs == []  # 실제 dispatch_now는 종료 실행을 다시 돌리지 않는다
    assert contracts.precheck_calls == []
    with committed_sessions() as check:
        assert executions_repo.get_execution(check, old_id).status is ExecutionStatus.FAILED


@pytest.mark.parametrize(("judgement", "expected", "final"), [
    (workflows.ExecutionJudgement(next_status=ExecutionStatus.SUCCESS), "success", ExecutionStatus.SUCCESS),
    (workflows.ExecutionJudgement(next_status=ExecutionStatus.FAILED, error_summary="still attached"),
     "failed", ExecutionStatus.FAILED),
    (workflows.ExecutionJudgement(defer_code=PrecheckReasonCode.PRECHECK_AWS_ERROR, defer_reason="throttled"),
     "unconfirmed", ExecutionStatus.IN_PROGRESS),
])
def test_runner_success_is_confirmed_only_by_judge_through_real_dispatcher(
    committed_sessions, seed_isolation, make_execution, supported, monkeypatch,
    judgement, expected, final,
):
    """STUB 한계: runner/judge는 제어용 대역 — 러너가 성공을 반환해도 판정이 SUCCESS가 아니면 확정되지 않는다."""
    seed = seed_isolation()
    judged = []
    monkeypatch.setitem(dispatcher._RUNNERS, ISOLATE, lambda db, eid: SimpleNamespace(
        succeeded=True, deferred=False, steps=[], reason_code=None, error_summary=None))
    monkeypatch.setitem(dispatcher._JUDGES, ISOLATE, lambda db, eid: judged.append(eid) or judgement)
    with committed_sessions() as s:
        # 다른 출처로 시드해 기존 실행 경로가 아니라 예약 경로를 탄다
        row = make_execution(
            s, seed.incident_id, runbook_id=ISOLATE, target_arn=seed.arn,
            trigger_source=TriggerSource.TIMEOUT_ISOLATION_1M, status=ExecutionStatus.IN_PROGRESS,
        )
        s.commit()
        execution_id = row.execution_id
    command_id, command, evaluation = _typed_pass_preparation(execution_id)
    monkeypatch.setattr(soar, "prepare_isolation", lambda db, iid, ts: (command_id, command, evaluation))
    monkeypatch.setattr(
        workflows, "reserve_auto_isolation",
        lambda db, iid, *, trigger_source, now, execution_id, validated_command, guardrail_result:
        workflows.AutoIsolationReservation(
            incident_id=iid, execution_id=execution_id,
            execution_status=ExecutionStatus.IN_PROGRESS, created=True, occurred_at=now),
        raising=False,
    )
    with committed_sessions() as db, begin_trace() as trace:
        result = soar.trigger_isolation(
            db, seed.incident_id, trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
        )
    assert result == expected and judged == [execution_id]
    with committed_sessions() as check:
        assert executions_repo.get_execution(check, execution_id).status is final
    record = trace.finish("success", confirmed=True)
    assert record["confirmed"] is (expected == "success")
    assert (record["end_to_end_ms"] is not None) is (expected == "success")


# ---------------------------------------------------------------- 실제 intake → 자동 발동 호출 (trigger는 기록 대역)

@pytest.fixture
def trigger_calls(monkeypatch):
    calls = []

    def record(db, incident_id, *, trigger_source, publish=None):
        # 호출 시점에 접수가 이미 다른 세션에서 보여야 commit 이후 발동이다.
        calls.append((incident_id, trigger_source))
        return "recorded"

    monkeypatch.setattr(threat_ingress, "trigger_isolation", record)
    return calls


def test_real_high_intake_invokes_automatic_source_after_commit(committed_sessions, trigger_calls):
    with committed_sessions() as db, begin_trace():
        outcome = threat_ingress.receive_threat(db, parse_observation(_raw("evt_ssh_bruteforce_001")))
    assert outcome.created
    assert trigger_calls == [(outcome.incident_id, TriggerSource.PRE_MITIGATION_0_5S)]
    with committed_sessions() as other:
        row = incidents_repo.get_incident(other, outcome.incident_id)
        assert row is not None and row.response_mode.value == "PRE_MITIGATION_0_5S"


@pytest.mark.parametrize("name", ["evt_ssh_bruteforce_002", "evt_ssh_bruteforce_006"])  # LOW, MEDIUM
def test_low_and_medium_intake_do_not_invoke_high_path(committed_sessions, trigger_calls, name):
    with committed_sessions() as db, begin_trace():
        outcome = threat_ingress.receive_threat(db, parse_observation(_raw(name)))
    assert outcome.created
    assert trigger_calls == []
    with committed_sessions() as other:
        assert incidents_repo.get_incident(other, outcome.incident_id).response_mode.value == "AGENT_WAIT"


def test_trigger_failure_preserves_committed_intake_and_marks_failed(
    committed_sessions, monkeypatch, caplog,
):
    def boom(*a, **k):
        raise RuntimeError("trigger exploded")

    monkeypatch.setattr(threat_ingress, "trigger_isolation", boom)
    events = []
    with (
        caplog.at_level("ERROR", logger="vigilantis.threat_ingress"),
        committed_sessions() as db,
        begin_trace() as trace,
    ):
        outcome = threat_ingress.receive_threat(
            db, parse_observation(_raw("evt_ssh_bruteforce_001")), events.append,
        )
    assert outcome.created  # 예외로 전파되지 않는다
    assert "automatic_isolation_trigger_failed" in caplog.text
    assert trace.finish("success", confirmed=True)["outcome"] == "failed"
    assert [e.event_type for e in events] == [WsEventType.INCIDENT_CREATED]
    with committed_sessions() as other:
        assert incidents_repo.get_incident(other, outcome.incident_id) is not None


def test_publish_failure_at_intake_still_triggers_isolation(committed_sessions, trigger_calls):
    def fail(event):
        raise RuntimeError("ws down")

    with committed_sessions() as db, begin_trace():
        outcome = threat_ingress.receive_threat(
            db, parse_observation(_raw("evt_ssh_bruteforce_001")), fail,
        )
    assert trigger_calls == [(outcome.incident_id, TriggerSource.PRE_MITIGATION_0_5S)]


# ---------------------------------------------------------------- 타임아웃 감시

def test_settings_default_poll_is_one_second(monkeypatch):
    from config import Settings

    monkeypatch.delenv("AGENT_WAIT_POLL_SECONDS", raising=False)
    assert Settings(_env_file=None).AGENT_WAIT_POLL_SECONDS == 1.0


def _watcher_env(monkeypatch, enabled: bool):
    from config import get_settings

    monkeypatch.setenv("DISPATCH_ENABLED", "true" if enabled else "false")
    monkeypatch.delenv("AGENT_WAIT_POLL_SECONDS", raising=False)
    get_settings.cache_clear()
    return get_settings


def test_watcher_disabled_returns_none(monkeypatch):
    get_settings = _watcher_env(monkeypatch, False)
    try:
        assert soar.start_timeout_watcher() is None
    finally:
        get_settings.cache_clear()


def test_watcher_lifecycle_registers_one_second_single_instance_job(monkeypatch):
    get_settings = _watcher_env(monkeypatch, True)

    async def scenario():
        scheduler = soar.start_timeout_watcher()
        try:
            assert scheduler is not None and scheduler.running
            job = scheduler.get_job(soar.JOB_ID)
            assert job.trigger.interval == timedelta(seconds=1)
            assert job.max_instances == 1 and job.coalesce is True
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        assert not scheduler.running

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()


def test_app_lifespan_starts_and_shuts_down_registered_watcher(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    get_settings = _watcher_env(monkeypatch, False)
    started = []

    def fake_start(publish=None):
        started.append(publish)

    monkeypatch.setattr(main, "start_timeout_watcher", fake_start)
    try:
        with TestClient(main.create_app()):
            assert len(started) == 1 and callable(started[0])
    finally:
        get_settings.cache_clear()


@contextmanager
def _factory_cm():
    yield MagicMock()


def test_cycle_continues_after_per_item_error_and_counts_each(monkeypatch):
    monkeypatch.setattr(
        incidents_repo, "list_expired_agent_waits",
        lambda db, now: ["a", "b", "c", "d"], raising=False,
    )
    seen = []

    def fake_trigger(db, incident_id, *, trigger_source, publish=None):
        seen.append((incident_id, trigger_source, isolation_timing.current_trace() is not None))
        if incident_id == "b":
            raise RuntimeError("boom")
        return {"a": "success", "c": "prerequisite_blocked", "d": "skipped"}[incident_id]

    monkeypatch.setattr(soar, "trigger_isolation", fake_trigger)
    report = soar.run_timeout_cycle(lambda: _factory_cm())
    assert [s[0] for s in seen] == ["a", "b", "c", "d"]
    assert all(s[1] is TriggerSource.TIMEOUT_ISOLATION_1M and s[2] for s in seen)
    assert (report.scanned, report.success, report.errored,
            report.prerequisite_blocked, report.skipped) == (4, 1, 1, 1, 1)


# ---------------------------------------------------------------- 즉시 실행 vs 스캔 (실제 세션/스레드/DB, 대역 runner)

def test_dispatch_now_and_scan_cannot_enter_runner_while_first_holds_after_commit(
    committed_sessions, make_incident, make_execution, monkeypatch,
):
    """STUB 한계: runner/judge는 제어용 대역이다 — AWS 변경도 격리도 하지 않는다.

    대역이 실제 runner처럼 단계 commit을 한 뒤(행 잠금 해제) 붙들고 있어도, 두 번째
    호출(별도 세션·스레드)이 runner에 들어오지 못함을 _DISPATCH_LOCK이 보장하는지 본다.
    """
    entered = threading.Event()
    release = threading.Event()
    runner_calls = []

    def controlled_runner(db, execution_id):
        runner_calls.append((threading.current_thread().name, execution_id))
        db.commit()  # 행 잠금을 놓는 중간 commit — DB 행 잠금만으로는 재진입을 못 막는다
        entered.set()
        assert release.wait(10)
        return SimpleNamespace(succeeded=True, deferred=False, steps=[], reason_code=None,
                               error_summary=None)

    monkeypatch.setitem(dispatcher._RUNNERS, ISOLATE, controlled_runner)
    monkeypatch.setitem(
        dispatcher._JUDGES, ISOLATE,
        lambda db, eid: workflows.ExecutionJudgement(next_status=ExecutionStatus.SUCCESS),
    )

    class ObservedLock:
        """진입 시도를 먼저 알린 뒤 실제 RLock을 획득한다 — 스레드가 락 앞에 도달했음을 증명."""

        def __init__(self, inner):
            self.inner = inner
            self.attempts = {"immediate": threading.Event(), "scan": threading.Event()}

        def __enter__(self):
            event = self.attempts.get(threading.current_thread().name)
            if event is not None:
                event.set()
            self.inner.acquire()
            return self

        def __exit__(self, *exc):
            self.inner.release()

    observed = ObservedLock(dispatcher._DISPATCH_LOCK)
    monkeypatch.setattr(dispatcher, "_DISPATCH_LOCK", observed)

    with committed_sessions() as seed:
        incident = make_incident(seed)
        execution = make_execution(
            seed, incident, runbook_id=ISOLATE, trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
            status=ExecutionStatus.IN_PROGRESS,
        )
        seed.commit()
        incident_id, execution_id = incident.incident_id, execution.execution_id

    second_done = threading.Event()
    errors = []

    def first():
        try:
            with committed_sessions() as db:
                dispatcher.dispatch_now(db, execution_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def second():
        try:
            with committed_sessions() as db:
                dispatcher.dispatch_pending(db)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            second_done.set()

    t1 = threading.Thread(target=first, name="immediate")
    t2 = threading.Thread(target=second, name="scan")
    try:
        t1.start()
        assert entered.wait(10), "first call never reached the runner"
        t2.start()
        assert observed.attempts["scan"].wait(10), "scan never attempted to enter the lock"
        assert not second_done.wait(1.0), "scan finished while first execution was held"
        assert [c[0] for c in runner_calls] == ["immediate"]  # 스캔은 runner에 못 들어왔다
        with committed_sessions() as check:
            assert executions_repo.get_execution(check, execution_id).status is ExecutionStatus.IN_PROGRESS
    finally:
        release.set()
        t1.join(15)
        t2.join(15)
        try:
            assert not errors, errors
            assert second_done.is_set() and not t1.is_alive() and not t2.is_alive()
            assert [c[0] for c in runner_calls] == ["immediate"]  # 종료 후에도 단 한 번
            with committed_sessions() as check:
                assert executions_repo.get_execution(check, execution_id).status is ExecutionStatus.SUCCESS
        finally:
            with committed_sessions() as cleanup:
                cleanup.execute(delete(models.ExecutionStep).where(
                    models.ExecutionStep.execution_id == execution_id))
                cleanup.execute(delete(models.ActionExecution).where(
                    models.ActionExecution.incident_id == incident_id))
                cleanup.execute(delete(models.Incident).where(
                    models.Incident.incident_id == incident_id))
                cleanup.commit()


def test_held_autoscaling_dispatch_does_not_block_main_lane_but_excludes_its_own_lane(
    committed_sessions, make_incident, make_execution, monkeypatch,
):
    """STUB 한계: runner/judge는 제어용 대역이다 — AWS·AI 호출 없음.

    오래 걸리는 autoscaling 실행(AMI/READY 대기 모사)이 락을 쥔 채여도 main 레인의
    즉시 실행(dispatch_now)과 main 스캔은 끝난다. 같은 autoscaling 레인의 두 번째 스캔은
    runner에 못 들어온다. 시간 성능이 아니라 이벤트·유한 join으로 판정한다.
    """
    asg = RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING
    asg_entered = threading.Event()
    asg_release = threading.Event()
    calls = []

    active = []
    max_active = [0]

    def asg_runner(db, execution_id):
        calls.append(("asg", threading.current_thread().name))
        active.append(1)
        max_active[0] = max(max_active[0], len(active))
        db.commit()
        asg_entered.set()
        try:
            assert asg_release.wait(10)
        finally:
            active.pop()
        return SimpleNamespace(succeeded=True, deferred=False, steps=[], reason_code=None,
                               error_summary=None)

    def main_runner(db, execution_id):
        calls.append(("main", threading.current_thread().name))
        return SimpleNamespace(succeeded=True, deferred=False, steps=[], reason_code=None,
                               error_summary=None)

    success = lambda db, eid: workflows.ExecutionJudgement(next_status=ExecutionStatus.SUCCESS)
    monkeypatch.setitem(dispatcher._RUNNERS, asg, asg_runner)
    monkeypatch.setitem(dispatcher._JUDGES, asg, success)
    monkeypatch.setitem(dispatcher._RUNNERS, ISOLATE, main_runner)
    monkeypatch.setitem(dispatcher._JUDGES, ISOLATE, success)

    class ObservedLock:
        def __init__(self, inner):
            self.inner = inner
            self.attempted = threading.Event()

        def __enter__(self):
            if threading.current_thread().name == "asg-second":
                self.attempted.set()
            self.inner.acquire()
            return self

        def __exit__(self, *exc):
            self.inner.release()

    observed = ObservedLock(dispatcher._AUTOSCALING_DISPATCH_LOCK)
    monkeypatch.setattr(dispatcher, "_AUTOSCALING_DISPATCH_LOCK", observed)

    with committed_sessions() as seed:
        incidents = [make_incident(seed) for _ in range(3)]
        asg_exec = make_execution(seed, incidents[0], runbook_id=asg,
                                  status=ExecutionStatus.IN_PROGRESS)
        now_exec = make_execution(seed, incidents[1], runbook_id=ISOLATE,
                                  trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
                                  status=ExecutionStatus.IN_PROGRESS)
        scan_exec = make_execution(seed, incidents[2], runbook_id=ISOLATE,
                                   status=ExecutionStatus.IN_PROGRESS)
        seed.commit()
        incident_ids = [i.incident_id for i in incidents]
        asg_id, now_id, scan_id = asg_exec.execution_id, now_exec.execution_id, scan_exec.execution_id

    errors = []

    def run(fn, name):
        def body():
            try:
                with committed_sessions() as db:
                    fn(db)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        return threading.Thread(target=body, name=name)

    def status_of(execution_id):
        with committed_sessions() as check:
            return executions_repo.get_execution(check, execution_id).status

    held = run(lambda db: dispatcher.dispatch_pending(db, lane=dispatcher.LANE_AUTOSCALING), "asg-first")
    second = run(lambda db: dispatcher.dispatch_pending(db, lane=dispatcher.LANE_AUTOSCALING), "asg-second")
    now = run(lambda db: dispatcher.dispatch_now(db, now_id), "main-now")
    scan = run(lambda db: dispatcher.dispatch_pending(db), "main-scan")
    threads = [held, second, now, scan]
    try:
        held.start()
        assert asg_entered.wait(10), "autoscaling dispatch never reached the runner"
        now.start()
        now.join(10)
        assert not now.is_alive(), "main immediate dispatch blocked by held autoscaling lane"
        scan.start()
        scan.join(10)
        assert not scan.is_alive(), "main scan blocked by held autoscaling lane"
        assert status_of(now_id) is ExecutionStatus.SUCCESS
        assert status_of(scan_id) is ExecutionStatus.SUCCESS
        assert status_of(asg_id) is ExecutionStatus.IN_PROGRESS

        second.start()
        assert observed.attempted.wait(10), "second autoscaling scan never reached its lock"
        second.join(0.5)
        assert second.is_alive(), "same-lane scan entered while autoscaling lane was held"
        assert [c for c in calls if c[0] == "asg"] == [("asg", "asg-first")]
    finally:
        asg_release.set()
        for t in threads:
            if t.ident is not None:
                t.join(15)
        try:
            assert not errors, errors
            assert not any(t.is_alive() for t in threads)
            # 풀린 뒤 두 번째 스캔은 순차로 이어받는다(판정 대기 실행은 비종료) — 겹치지 않는다.
            assert [c[1] for c in calls if c[0] == "asg"] == ["asg-first", "asg-second"]
            assert max_active == [1]
            assert sorted(c[1] for c in calls if c[0] == "main") == ["main-now", "main-scan"]
        finally:
            with committed_sessions() as cleanup:
                cleanup.execute(delete(models.ExecutionStep).where(
                    models.ExecutionStep.execution_id.in_([asg_id, now_id, scan_id])))
                cleanup.execute(delete(models.ActionExecution).where(
                    models.ActionExecution.incident_id.in_(incident_ids)))
                cleanup.execute(delete(models.Incident).where(
                    models.Incident.incident_id.in_(incident_ids)))
                cleanup.commit()


def test_dispatch_pending_rejects_unknown_lane_before_taking_any_lock():
    with pytest.raises(ValueError):
        dispatcher.dispatch_pending(MagicMock(), lane="unknown")


# ---------------------------------------------------------------- agent_dispatcher High 선차단 훅 (모델은 Fake, #452·계측 없음)

def _prior_execution_creator(db, incident_id, status, events):
    """STUB trigger_isolation: 선차단이 남기는 실행 기록만 만든다 — runner/AWS 없음."""

    def trigger(db_arg, iid, *, trigger_source, publish=None):
        events.append(("trigger", iid, trigger_source, db_arg.in_transaction()))
        row = executions_repo.create_execution(
            db_arg, incident_id=iid, runbook_id=ISOLATE, target_arn=_EC2_ARN_FOR_HOOK,
            trigger_source=trigger_source,
        )
        row.status = status
        db_arg.commit()
        return "recorded"

    return trigger


_EC2_ARN_FOR_HOOK = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc123456789def0"


@pytest.mark.parametrize(("status", "affected"), [
    (ExecutionStatus.FAILED, []),
    (ExecutionStatus.SUCCESS, [_EC2_ARN_FOR_HOOK]),
])
def test_high_hook_finishes_before_graph_input_and_failed_is_not_applied(
    db, monkeypatch, status, affected,
):
    import agent_dispatcher
    from tests.test_agent_dispatcher import _cycle, _pending_secops, _secops_client

    incident_id = _pending_secops(db)  # 실제 접수 분류: HIGH / PRE_MITIGATION_0_5S / SECOPS
    row = incidents_repo.get_incident(db, incident_id)
    assert (row.initial_risk_level, row.response_mode) == (
        RiskLevel.HIGH, ResponseMode.PRE_MITIGATION_0_5S)
    events = []
    monkeypatch.setattr(soar, "trigger_isolation", _prior_execution_creator(db, incident_id, status, events))
    original_build = agent_dispatcher.build_graph_input
    seen = {}

    def build(db_arg, iid):
        events.append(("build", iid))
        seen["input"] = original_build(db_arg, iid)
        return seen["input"]

    monkeypatch.setattr(agent_dispatcher, "build_graph_input", build)
    report = _cycle(db, _secops_client(db, incident_id, candidates=False))
    assert report.errored == 0 and report.claimed == 1
    assert [e[0] for e in events] == ["trigger", "build"]  # 선차단이 먼저 끝난 뒤 이전 실행 맥락을 읽는다
    assert events[0][1:] == (incident_id, TriggerSource.PRE_MITIGATION_0_5S, False)  # 열린 트랜잭션 밖
    context = seen["input"].isolation_execution
    assert context.status is status and context.runbook_id is ISOLATE
    assert context.affected_arns == affected  # 실패·미적용은 영향 대상으로 올리지 않는다

@pytest.mark.parametrize("kind", ["secops_medium", "finops"])
def test_medium_and_finops_bypass_high_hook(db, monkeypatch, kind):
    from ai.agent import CandidateProposalOutput
    from schemas.api.incidents import IncidentCategory
    from tests.test_agent_dispatcher import (
        SUMMARY,
        _client,
        _cycle,
        _pending_incident,
        _pending_secops,
        _secops_client,
    )

    monkeypatch.setattr(soar, "trigger_isolation",
                        lambda *a, **k: pytest.fail("High hook must not run"))
    if kind == "secops_medium":
        incident_id = _pending_secops(db, medium=True)
        client = _secops_client(db, incident_id, candidates=False)
        expected = (IncidentCategory.SECOPS, RiskLevel.MEDIUM, ResponseMode.AGENT_WAIT)
    else:
        incident_id = _pending_incident(db)
        client = _client(SUMMARY, CandidateProposalOutput(candidates=[]))
        expected = (IncidentCategory.FINOPS, None, None)
    row = incidents_repo.get_incident(db, incident_id)
    assert (row.category, row.initial_risk_level, row.response_mode) == expected
    report = _cycle(db, client)
    assert report.claimed == 1 and report.errored == 0

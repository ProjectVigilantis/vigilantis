# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine · QA 인수 2026-09-16)
# E2E 시연 시나리오의 **전 구간 흐름** 회귀입니다. (판정 기준 ⓐ · 원 명세는
# `docs/E2E_DEMO_SCENARIOS.md` 이며 실경로/대체 컷 경계도 그 문서가 갖는다)
#
# ── 왜 여기 있나 ────────────────────────────────────────────────────────────
# 명세와 skip 자리는 `tests/test_e2e_scenario.py`에 있었지만 그 디렉터리에는 DB
# 픽스처가 없다 — `db`·`pg_engine`·`client_pg`·`make_incident` 계열이 전부
# `apps/core-api/tests/conftest.py`에 있고, `tests/execution_harness.py` 헤더가
# 적은 이유(CI가 여러 디렉터리를 한 세션으로 돌릴 때 `conftest` 최상위 이름을
# 이쪽이 먼저 차지한다)로 가져다 쓸 수도 없다. 두 흐름은 **DB 상태 전이**를
# 검증하므로 구현을 이 디렉터리로 옮겼다(SSOT 2026-09-16 확정 · PR #354).
# `tests/test_e2e_scenario.py`에는 옮긴 위치 안내만 남는다.
#
# ── 이 파일이 보는 것 / 보지 않는 것 ────────────────────────────────────────
# **본다**: 감지 → 판정 → Intake → AI 추천 → 승인 → 실행 → 실패 → 자동 원복까지
#   **한 줄로 이어지는 상태 전이**. 프로덕션 진입점만 부르고 중간을 손으로 세우지
#   않는다 — 손으로 세우면 "이어져 있다"가 아니라 "각 조각이 돈다"만 증명된다.
# **보지 않는다**: 계층별 상세. 판정 규칙은 services/tests/test_rule_engine.py,
#   가드레일 단계는 tests/test_guardrails.py, 원복 발동·확정의 조합은
#   tests/test_auto_rollback_workflow.py, 실물 반영은 *_localstack.py가 갖는다.
#   AWS는 가짜다 — 이 파일이 재는 것은 AWS 동작이 아니라 상태 머신이다.
# ==============================================================================

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, WaiterError
from pydantic import TypeAdapter

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from incident_intake import create_incident_from_intake  # noqa: E402
from schemas.agents import AgentGraphOutput, RunbookCandidateDraft  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus, RiskLevel  # noqa: E402
from schemas.assets import MetricSummary as MetricSummaryContract  # noqa: E402
from schemas.events import MockThreatEventInput  # noqa: E402
from schemas.executions import ExecutionEffect  # noqa: E402
from schemas.incidents import AgentInvocationStatus  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws import rollback as rb  # noqa: E402
from services.rule_engine import run_rule_engine  # noqa: E402
from services.scheduler import _build_finops_intakes  # noqa: E402
from threat_ingress import receive_threat  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0a1b2c3d4e5f0e2e1"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"

STARTING_TYPE = "t3.xlarge"   # 조치 이전 = 백업에 남고 원복이 되돌릴 값
TARGET_TYPE = "t3.large"      # AI 가 제안한 축소 대상 = 원복이 대조할 "조치 적용" 값
NOW = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)

# T2 — 골든 SecOps S3(`evt_ssh_bruteforce_001`)의 값 그대로다. 시나리오를 바꾸면
# 정답지와 갈리므로 여기서 새로 짓지 않는다(설계서 §T2 입력).
ACL = "acl-0a1b2c3d4e5f0e2e1"
ACL_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-acl/{ACL}"
THREAT_TARGET = "i-0a1b2c3d4e5f00001"
THREAT_TARGET_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{THREAT_TARGET}"
SOURCE_IP = "203.0.113.10"
BLOCK_CIDR = f"{SOURCE_IP}/32"      # /32 단일 주소 — 서브넷을 끊지 않는다
BLOCK_RULE_NUMBER = 100
S3_OBSERVATION = {
    "event_id": "evt-ssh-bruteforce-001",
    "event_type": "SSH_BRUTE_FORCE",
    "target_arn": THREAT_TARGET_ARN,
    "source_ip": SOURCE_IP,
    "occurred_at": "2026-09-16T06:30:00Z",
    "failed_attempt_count": 120,
    "window_seconds": 300,
}

# 판정 재시도 상한 — 운영 설정과 무관하게 주기 수로 센다 (Issue #249)
RETRY_NOW = workflows.VerificationRetryPolicy(max_attempts=3, interval_seconds=0)


# ----------------------------------------------------------------- 가짜 AWS
# 모듈마다 자기 가짜를 두는 것이 이 저장소의 방식이다(test_dispatcher.py ·
# test_auto_rollback_workflow.py 와 같다). 공용으로 올리면 각 파일이 필요로 하는
# 응답 모양이 서로를 끌어당겨 한쪽 테스트가 다른 쪽 때문에 바뀐다.


def dry_run_ok() -> ClientError:
    """DryRun 통과 신호 — 가드레일 ④는 이 예외만 통과로 인정한다."""
    return ClientError({"Error": {"Code": "DryRunOperation"}}, "Op")


def waiter_timeout() -> WaiterError:
    return WaiterError(name=rb.WAITER_NAME, reason="Max attempts exceeded", last_response={})


class FakeWaiter:
    def __init__(self, state, name):
        self._state, self._name = state, name

    def wait(self, **kwargs):
        self._state["calls"].append((f"wait:{self._name}", kwargs))
        outcome = self._state["overrides"].get(f"waiter:{self._name}")
        if isinstance(outcome, BaseException):
            raise outcome


class FakeEc2:
    def __init__(self, state):
        self._state = state

    def get_waiter(self, name):
        return FakeWaiter(self._state, name)

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            if kwargs.get("DryRun"):
                raise self._state["overrides"].get("dry_run", dry_run_ok())
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if operation == "describe_instances":
                return {
                    "Reservations": [
                        {
                            "Instances": [
                                {
                                    "InstanceId": INSTANCE,
                                    "InstanceType": self._state["current_type"],
                                    "State": {"Name": self._state["current_state"]},
                                }
                            ]
                        }
                    ]
                }
            # 인스턴스도 규칙과 같은 이유로 상태를 들고 있어야 한다 — 원복은 현재 타입을
            # 백업 값·조치 적용 값과 대조해 3분기로 갈린다(ADR-0008 §3-2). 고정 응답을
            # 주면 "이미 백업 스펙 상태" 분기로 빠져 **되돌리는 호출 없이** SUCCESS 가
            # 되고, 축소→실패→원복 경로가 통째로 빠진다. (PR #358 리뷰: 안성일)
            if operation == "modify_instance_attribute":
                self._state["current_type"] = kwargs["InstanceType"]["Value"]
                return {}
            if operation == "stop_instances":
                previous = self._state["current_state"]
                self._state["current_state"] = "stopped"
                return {
                    "StoppingInstances": [
                        {"InstanceId": INSTANCE, "PreviousState": {"Name": previous}}
                    ]
                }
            if operation == "start_instances":
                self._state["current_state"] = "running"
                return {
                    "StartingInstances": [
                        {"InstanceId": INSTANCE, "PreviousState": {"Name": "stopped"}}
                    ]
                }
            # NACL 은 상태를 들고 있어야 한다 — 차단이 넣은 규칙을 판정이 다시 읽고,
            # 해제가 지운 뒤에도 같은 조회가 돈다. 규칙 없이 고정 응답을 주면
            # "넣었는가"와 "지웠는가"가 둘 다 통과해 버린다.
            if operation == "describe_network_acls":
                return {
                    "NetworkAcls": [
                        {"NetworkAclId": ACL, "Entries": list(self._state["acl_entries"])}
                    ]
                }
            if operation == "create_network_acl_entry":
                self._state["acl_entries"].append(
                    {
                        "CidrBlock": kwargs.get("CidrBlock"),
                        "Egress": kwargs.get("Egress", False),
                        "PortRange": kwargs.get("PortRange", {"From": 0, "To": 65535}),
                        "Protocol": kwargs.get("Protocol"),
                        "RuleAction": kwargs.get("RuleAction"),
                        "RuleNumber": kwargs.get("RuleNumber"),
                    }
                )
                return {}
            if operation == "delete_network_acl_entry":
                number = kwargs.get("RuleNumber")
                self._state["acl_entries"] = [
                    e for e in self._state["acl_entries"] if e["RuleNumber"] != number
                ]
                return {}
            return {}

        return call


@pytest.fixture()
def aws(monkeypatch):
    """캡처(backup)·실행(executor)·판정(rollback)이 같은 가짜 EC2를 본다."""
    state = {"overrides": {}, "calls": [], "current_type": STARTING_TYPE,
             "current_state": "running", "acl_entries": []}

    def factory(service, region=None, **_):
        return FakeEc2(state)

    for module in (bk, ex, rb):
        monkeypatch.setattr(module, "aws_client", factory)
    # 판정 대기는 실제로 자지 않는다 — 여기서 보는 것은 대기가 아니라 라우팅이다
    monkeypatch.setattr(
        rb,
        "get_settings",
        lambda: type(
            "S", (), {"STATUS_CHECK_WAIT_DELAY_SECONDS": 1, "STATUS_CHECK_WAIT_MAX_ATTEMPTS": 1}
        )(),
    )

    def configure(current_type=None, **overrides):
        if current_type is not None:
            state["current_type"] = current_type
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    configure.state = state
    return configure


# ----------------------------------------------------------------- 공통 헬퍼


def cycle(db):
    """디스패치 스캔 1주기 — 운영에서 스케줄러가 부르는 것과 같은 함수다."""
    return dispatcher.dispatch_pending(db, None, RETRY_NOW)


def status_of(db, incident_id, execution_id):
    incident = incidents_repo.get_incident(db, incident_id)
    execution = exec_repo.get_execution(db, execution_id)
    return incident.status, execution.status


def _collect_idle_ec2(db):
    """수집 1회 — Idle EC2 한 대를 자산과 메트릭 요약으로 남긴다.

    `evaluate_ec2`가 `COST_CANDIDATE`로 읽을 값을 넣는다(cpu_avg 4.9 < IDLE 5.0 ·
    cpu_max 12.0 < SPIKE 40.0 · 데이터포인트 336 ≥ 48 · prod 태그 없음). 값 자체의
    경계 의미는 골든이 갖고, 여기서는 **판정이 실제로 떨어지는 입력**이면 된다.
    """
    run = assets_repo.start_collection_run(
        db, account_id=ACCOUNT, region=REGION, mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    asset = assets_repo.upsert_asset(
        db,
        arn=INSTANCE_ARN,
        asset_type=AssetType.EC2,
        resource_id=INSTANCE,
        account_id=ACCOUNT,
        region=REGION,
        spec={"instance_type": STARTING_TYPE},
        collection_run_id=run.collection_run_id,
        collected_at=NOW,
        name="golden-ec2-idle-boundary",
        state="running",
    )
    assets_repo.add_metric_summary(
        db,
        asset_id=asset.asset_id,
        collection_run_id=run.collection_run_id,
        summary=MetricSummaryContract(
            cpu_datapoints=336, cpu_avg=4.9, cpu_max=12.0,
            net_in_avg=1024.0, net_out_avg=512.0,
        ),
        window_start=NOW - timedelta(days=14),
        window_end=NOW,
        collected_at=NOW,
    )
    db.commit()
    return run.collection_run_id


def _ai_proposes_rightsizing(db, incident_id, evidence_id):
    """AI 분석 결과 기록 — 운영에서 agent_dispatcher가 그래프 출력으로 부르는 자리.

    모델을 실제로 부르지 않는다. 이 파일이 보는 것은 모델 품질이 아니라 **그 출력이
    후보·가드레일·승인 대기로 이어지는가**이고, 모델 호출은 ai/tests가 갖는다.
    """
    incidents_repo.claim_agent_invocation(db, incident_id, started_at=NOW)
    db.commit()
    output = AgentGraphOutput(
        invocation_status=AgentInvocationStatus.SUCCEEDED,
        summary_lines=[
            "14일간 CPU 평균이 4.9%로 유휴 기준(5%) 아래에 머물렀다.",
            "최대치도 12%라 축소 후 성능 여유가 남는다.",
            "운영(prod) 태그가 없어 축소 대상에서 제외할 사유가 없다.",
        ],
        candidates=[
            RunbookCandidateDraft(
                runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
                target_arn=INSTANCE_ARN,
                parameters={"target_instance_type": TARGET_TYPE},
                evidence_ids=[evidence_id],
            )
        ],
    )
    outcome = workflows.record_agent_analysis(db, incident_id, output)
    db.commit()
    return outcome


# ============================================================== T1 · FinOps


def test_t1_idle_ec2_downsize_and_auto_rollback_flow(db, client_pg, aws):
    """T1 전 구간 — 설계서 §T1 단계표 1~9번.

    검증하는 상태 전이:
      A1 수집 → `COST_CANDIDATE` 판정
      → Incident `ANALYZING` → 추천 `RUNBOOK_EC2_RIGHTSIZING` → `AWAITING_APPROVAL`
      → `POST /actions/execute` **202** → Execution `IN_PROGRESS`
      → Status Check 2/2 실패 → 원본 `ROLLBACK_INITIATED` · Incident `ACTION_IN_PROGRESS`
      → `RUNBOOK_EC2_REVERT_SIZE`(`AUTO_ON_FAILURE`) **자식 실행**(`parent_execution_id`)
      → 자식 `SUCCESS` → 원본 `ROLLED_BACK` · Incident `AWAITING_CLOSURE`

    상태값을 어느 축으로 읽는지가 이 흐름의 함정이다. 7번의 Execution은
    `ROLLBACK_INITIATED`인데 Incident는 `FAILED`가 아니라 `ACTION_IN_PROGRESS`다 —
    되돌릴 것이 남아 있기 때문이다(`workflows._incident_status_after`).

    핵심: **5번 [조치 실행] 이후 사람 입력이 없다.** 8~9번은 전부 시스템이 한다.
    원복 파라미터는 AI도 화면도 아닌 **DB 백업 레코드**에서만 온다.
    """
    # 1~2번 — 수집과 판정. 판정은 프로덕션 엔진을 그대로 부른다.
    _collect_idle_ec2(db)
    judged = run_rule_engine(db)
    db.commit()
    assert judged["counts"].get("COST_CANDIDATE") == 1

    # 2번 — 판정 → Intake. 스케줄러가 부르는 조립·생성을 같은 순서로 부른다(#306).
    intakes = _build_finops_intakes(db, judged["evaluations"])
    assert len(intakes) == 1
    outcome = create_incident_from_intake(db, intakes[0])
    db.commit()
    incident_id = outcome.incident_id
    assert outcome.created is True
    assert incidents_repo.get_incident(db, incident_id).status is IncidentStatus.ANALYZING

    # 3~4번 — AI 근거·추천이 붙고 가드레일을 지나 승인 대기로 선다.
    evidence_id = incidents_repo.list_evidence(db, incident_id)[0].evidence_id
    analysis = _ai_proposes_rightsizing(db, incident_id, evidence_id)
    assert analysis.next_status is IncidentStatus.AWAITING_APPROVAL
    assert analysis.executable == 1 and analysis.rejected == 0

    candidate = incidents_repo.list_candidates(db, incident_id)[0]
    assert candidate.runbook_id is RunbookId.RUNBOOK_EC2_RIGHTSIZING

    # 5번 — 관제자 [조치 실행]. 화면이 부르는 그 HTTP 경로로 접수한다.
    response = client_pg.post(
        "/api/v1/actions/execute",
        json={
            "incident_id": incident_id,
            "runbook_id": RunbookId.RUNBOOK_EC2_RIGHTSIZING.value,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 202, response.text
    origin_id = response.json()["execution_id"]
    assert exec_repo.get_execution(db, origin_id).status is ExecutionStatus.IN_PROGRESS

    # 6번 — 실행 주기. 여기서부터 사람 입력이 없다.
    assert cycle(db).awaiting_status_check == 1

    # 7번 — 2/2 Status Check 실패. 기동하지 못한 인스턴스를 판정이 읽는다.
    aws(
        **{f"waiter:{rb.WAITER_NAME}": waiter_timeout()},
        describe_instance_status={
            "InstanceStatuses": [
                {
                    "InstanceId": INSTANCE,
                    "InstanceState": {"Name": "stopped"},
                    "SystemStatus": {"Status": "not-applicable"},
                    "InstanceStatus": {"Status": "not-applicable"},
                }
            ]
        },
    )
    report = cycle(db)
    assert report.judged == 1 and report.rollback_initiated == 1
    assert status_of(db, incident_id, origin_id) == (
        IncidentStatus.ACTION_IN_PROGRESS,   # FAILED가 아니다 — 되돌릴 것이 남았다
        ExecutionStatus.ROLLBACK_INITIATED,
    )

    # 8번 — 원복 자식이 사람 개입 없이 접수된다. 원본을 옮기지 않고 묶인다.
    assert cycle(db).rollback_started == 1
    children = exec_repo.list_rollback_children(db, origin_id)
    assert len(children) == 1
    child = children[0]
    assert child.runbook_id is RunbookId.RUNBOOK_EC2_REVERT_SIZE
    assert child.trigger_source is TriggerSource.AUTO_ON_FAILURE
    assert child.parent_execution_id == origin_id

    # 축소가 실물에 닿았다 — 원복이 되돌릴 것이 실제로 남아 있다.
    assert aws.state["current_type"] == TARGET_TYPE

    # 9번 — 자식이 실행·확정되면 원본이 되돌려진 것으로 닫히고 Incident가 종료 대기로 간다.
    # 타입을 손으로 되돌리지 않는다. 되돌려 두면 원복이 "이미 백업 스펙 상태" 분기로
    # 끝나 되돌리는 호출 없이 SUCCESS 가 된다(PR #358 리뷰: 안성일). 여기서 지우는 것은
    # Status Check 를 넘어뜨린 일시 장애뿐이다.
    aws.state["overrides"].clear()
    for _ in range(3):  # 실행 → 판정 → 확정. 주기 수는 고정하지 않는다.
        if exec_repo.get_execution(db, origin_id).status is ExecutionStatus.ROLLED_BACK:
            break
        cycle(db)

    incident_status, origin_status = status_of(db, incident_id, origin_id)
    assert origin_status is ExecutionStatus.ROLLED_BACK
    assert incident_status is IncidentStatus.AWAITING_CLOSURE
    assert exec_repo.get_execution(db, child.execution_id).status is ExecutionStatus.SUCCESS

    # 원복이 **실물을 되돌렸다.** 상태 전이만 보면 아무것도 하지 않은 원복과 갈리지 않는다.
    assert aws.state["current_type"] == STARTING_TYPE
    reverts = [
        kwargs for op, kwargs in aws.calls
        if op == "modify_instance_attribute"
        and kwargs["InstanceType"]["Value"] == STARTING_TYPE
        and not kwargs.get("DryRun")   # 가드레일 ④ 탐침은 실물을 바꾸지 않는다
    ]
    assert len(reverts) == 1
    # ADR-0008 §3-2 의 ② 분기 — "우리가 바꾼 그대로"라 정지·변경·기동을 실제로 적용한다.
    # ① 분기("이미 백업 스펙")로 빠지면 여기가 COMPARE_INSTANCE_TYPE/NOT_APPLIED 하나가 된다.
    steps = exec_repo.list_steps(db, child.execution_id)
    applied = [s.step_type for s in steps if s.effect is ExecutionEffect.APPLIED]
    assert applied == [
        ex.STEP_STOP_INSTANCE,
        ex.STEP_MODIFY_INSTANCE_TYPE,
        ex.STEP_START_INSTANCE,
    ]


# ============================================================== T2 · SecOps


def _collect_threat_scene(db):
    """수집 1회 — 위협 대상 EC2와 차단이 겨눌 NACL을 자산으로 남긴다.

    가드레일 ③ ARN Match가 **DB 자산만** 통과시키므로 둘 다 있어야 한다. 게이트
    대본이 T2 앞에 수집 1회를 넣는 이유가 이것이다(§3-4 · §4).
    """
    run = assets_repo.start_collection_run(
        db, account_id=ACCOUNT, region=REGION, mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    for arn, asset_type, resource_id in (
        (THREAT_TARGET_ARN, AssetType.EC2, THREAT_TARGET),
        (ACL_ARN, AssetType.NACL, ACL),
    ):
        assets_repo.upsert_asset(
            db, arn=arn, asset_type=asset_type, resource_id=resource_id,
            account_id=ACCOUNT, region=REGION, spec={},
            collection_run_id=run.collection_run_id, collected_at=NOW,
        )
    db.commit()


def _ai_proposes_block(db, incident_id, evidence_id):
    """AI가 차단 후보를 낸다 — 운영에서 SecOps 그래프(#323)가 내는 자리."""
    incidents_repo.claim_agent_invocation(db, incident_id, started_at=NOW)
    db.commit()
    output = AgentGraphOutput(
        invocation_status=AgentInvocationStatus.SUCCEEDED,
        summary_lines=[
            f"{SOURCE_IP}에서 300초간 SSH 인증 실패 120회가 관측됐다.",
            "대상 서브넷의 NACL에 그 출발지를 막는 규칙이 없다.",
            "출발지만 /32로 막으면 다른 트래픽에 영향이 없다.",
        ],
        reviewed_risk_level=RiskLevel.HIGH,
        candidates=[
            RunbookCandidateDraft(
                runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
                target_arn=ACL_ARN,
                parameters={
                    "rule_number": BLOCK_RULE_NUMBER,
                    "cidr_block": BLOCK_CIDR,
                    "protocol": "tcp",
                },
                evidence_ids=[evidence_id],
            )
        ],
    )
    outcome = workflows.record_agent_analysis(db, incident_id, output)
    db.commit()
    return outcome


def _approve_and_run(db, client_pg, incident_id, runbook_id):
    """관제자 승인 1회 — 화면이 부르는 HTTP 경로로 접수하고 종료까지 주기를 돌린다."""
    response = client_pg.post(
        "/api/v1/actions/execute",
        json={
            "incident_id": incident_id,
            "runbook_id": runbook_id.value,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 202, response.text
    execution_id = response.json()["execution_id"]
    for _ in range(4):
        row = exec_repo.get_execution(db, execution_id)
        if row.status in (ExecutionStatus.SUCCESS, ExecutionStatus.FAILED):
            break
        cycle(db)
    return execution_id


def test_t2_ssh_bruteforce_block_and_one_click_release_flow(db, client_pg, aws):
    """T2 전 구간 — 설계서 §T2 단계표 1~8번.

    **시나리오는 SSH 브루트포스(S3)다.** `0.0.0.0/0` 개방(S1)이 아니다 —
    `tests/test_e2e_scenario.py::test_t2_must_not_use_the_open_ip_case` 참조.

    검증하는 상태 전이:
      S3 주입 → Incident `SECOPS` 생성
      → `RUNBOOK_NACL_ADD_DENY`(`USER_APPROVAL` · `cidr_block: 203.0.113.10/32`)
        → Execution `SUCCESS` · 실물 규칙 1건
      → 차단 뒤 **해제 후보 제안**(#329) → 관제자 [해제]
      → `RUNBOOK_NACL_RESTORE`(`USER_APPROVAL`) → `SUCCESS` · 실물 규칙 0건

    핵심: 막는 것도 푸는 것도 **사람이 판단한다.** 오탐 시 서브넷 전체가 끊기므로
    의도적으로 사람을 넣었다 — 두 번의 `POST /actions/execute`가 그 자리다.
    차단 대상은 `/32` 단일 주소다.
    """
    # 1번 — 수집. 가드레일 ③이 통과시킬 자산을 먼저 세운다.
    _collect_threat_scene(db)

    # 1~3번 — 위협 주입. 정형화·위험 판정·Intake가 프로덕션 경로로 돈다(#322).
    outcome = receive_threat(db, TypeAdapter(MockThreatEventInput).validate_python(S3_OBSERVATION))
    incident_id = outcome.incident_id
    assert outcome.created is True
    incident = incidents_repo.get_incident(db, incident_id)
    assert incident.category is IncidentCategory.SECOPS
    assert incident.initial_risk_level is RiskLevel.HIGH
    assert incident.subject_arn == THREAT_TARGET_ARN

    # 4번 — AI가 차단 후보를 내고 가드레일을 지나 승인 대기로 선다.
    evidence_id = incidents_repo.list_evidence(db, incident_id)[0].evidence_id
    analysis = _ai_proposes_block(db, incident_id, evidence_id)
    assert analysis.next_status is IncidentStatus.AWAITING_APPROVAL
    assert analysis.executable == 1 and analysis.rejected == 0

    # 5~6번 — 관제자가 [차단]을 누른다(사람 판단 1회). 실물에 규칙이 선다.
    block_id = _approve_and_run(db, client_pg, incident_id, RunbookId.RUNBOOK_NACL_ADD_DENY)
    block = exec_repo.get_execution(db, block_id)
    assert block.status is ExecutionStatus.SUCCESS
    assert block.trigger_source is TriggerSource.USER_APPROVAL
    assert aws.state["acl_entries"] == [
        {
            "CidrBlock": BLOCK_CIDR,
            "Egress": False,
            "PortRange": {"From": 0, "To": 65535},
            "Protocol": "6",          # executor가 이름 tcp를 AWS 번호로 바꿔 보낸다
            "RuleAction": "deny",
            "RuleNumber": BLOCK_RULE_NUMBER,
        }
    ]

    # 7번 — 차단이 닫힌 뒤 해제 후보가 선다. 이것이 없으면 [해제] 버튼이 비어 있다(#329).
    for _ in range(2):
        if any(
            c.runbook_id is RunbookId.RUNBOOK_NACL_RESTORE
            for c in incidents_repo.list_candidates(db, incident_id)
        ):
            break
        cycle(db)
    release_candidates = [
        c for c in incidents_repo.list_candidates(db, incident_id)
        if c.runbook_id is RunbookId.RUNBOOK_NACL_RESTORE
    ]
    assert len(release_candidates) == 1

    # 8번 — 관제자가 [해제]를 누른다(사람 판단 2회). 실물 규칙이 사라진다.
    release_id = _approve_and_run(db, client_pg, incident_id, RunbookId.RUNBOOK_NACL_RESTORE)
    release = exec_repo.get_execution(db, release_id)
    assert release.status is ExecutionStatus.SUCCESS
    assert release.trigger_source is TriggerSource.USER_APPROVAL
    assert aws.state["acl_entries"] == []

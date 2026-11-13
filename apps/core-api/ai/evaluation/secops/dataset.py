"""동결한 모의 근거를 서비스 입력 조립 함수에 대조한다.

읽기 과정에서 모델·DB·AWS를 호출하지 않는다. 합성 자산 전제와 명시적으로 주입한
모의 위협 10건(둘은 합성 선행 격리 실행 기록 포함, C10은 출발지만 루프백으로 바꾼 C01 파생)은 실제 환경의 탐지 정책을 정의하지 않는다. 원본 드리프트 검사는
CI 입력 로딩과 분리하며 계측 CLI가 명시적으로 호출한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from agent_dispatcher import assemble_secops_input
from mock_threat_source import MockThreatSubmission
from schemas.agents import SecOpsGraphInput
from schemas.assets import AssetInventory
from schemas.evidence import EvidenceItem, SecOpsEvidenceContext
from schemas.runbooks import TriggerSource
from security.risk_evaluator import evaluate_threat
from security.threat_normalizer import normalize_mock_input

from ai.capabilities import secops_action_targets
from ai.evaluation.common.paths import REPO_ROOT

ROOT = Path(__file__).resolve().parent
FIXED_CASE_IDS = [f"C{i:02}" for i in range(1, 11)]


def fixture_nacl_rule_numbers(graph_input: SecOpsGraphInput) -> dict[str, int]:
    """오프라인 계측용 서버 값 대역. 실제 NACL 조회·가용성 검증 결과가 아니다."""
    return {
        target: 1 for target in secops_action_targets(graph_input.asset_context)["RUNBOOK_NACL_ADD_DENY"]
    }


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    title: str
    graph_input: SecOpsGraphInput
    provenance: dict[str, Any]


@dataclass(frozen=True)
class ServiceFixture:
    inventory: AssetInventory
    submission: MockThreatSubmission


def load_service_fixture(case_id: str) -> ServiceFixture:
    """외부 Golden·corpus를 읽지 않고 동결한 적재·접수 원자료를 반환한다."""
    raw = read_json(ROOT / "service-fixtures.json")["cases"][case_id]
    return ServiceFixture(
        AssetInventory.model_validate(raw["inventory"]),
        MockThreatSubmission.model_validate(raw["submission"]),
    )


def _source_path(relative_path: str) -> Path:
    path = (REPO_ROOT / relative_path).resolve()
    if not path.is_relative_to(REPO_ROOT) or not path.is_file():
        raise ValueError(f"Source outside repository or missing: {relative_path}")
    return path


def load_source(source: str, relative_path: str) -> Any:
    path = _source_path(relative_path)
    if source == "log_records":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return read_json(path)


def check_sources(cases: list[EvalCase]) -> None:
    """계측 시점에만 현재 원본과 동결 당시 출처 지문을 대조한다."""
    checked = set()
    for case in cases:
        for source in ("threat", "risk", "asset", "log_manifest", "log_expected", "log_records", "context"):
            relative_path = case.provenance[f"{source}_source"]
            expected_hash = case.provenance[f"{source}_sha256"]
            identity = (relative_path, expected_hash)
            if identity in checked:
                continue
            value = load_source(source, relative_path)
            if digest(value) != expected_hash:
                raise ValueError(f"Frozen source changed: {relative_path}")
            checked.add(identity)


def _check_context(case_id: str, data: SecOpsGraphInput, fixture: ServiceFixture) -> None:
    inventory = read_json(ROOT / "inventory.json")
    context = SecOpsEvidenceContext(
        **inventory["cases"][case_id], target_status="available", log_evidence=fixture.submission.log_evidence,
    )
    if data.evidences[0].content.context != context or data.asset_context != context.target.asset:
        raise ValueError(f"{case_id}: saved target/related/log context differs from frozen fixtures")


def validate_case(record: dict[str, Any]) -> EvalCase:
    case_id = record["case_id"]
    data = SecOpsGraphInput.model_validate(record["graph_input"])
    provenance = record["provenance"]
    fixture = load_service_fixture(case_id)
    raw = fixture.submission.observation.model_dump(mode="json")
    inventory = fixture.inventory.model_dump(mode="json")
    if len(data.evidences) != 1:
        raise ValueError(f"{case_id}: this service set requires exactly one threat")
    evidence = data.evidences[0]
    event = evidence.content.event
    # 원본 초안의 inc... / te... 표기 대신 DB에 저장할 수 있는 고정 UUID를 쓴다.
    for identity in (data.incident_id, event.threat_event_id, evidence.evidence_id):
        UUID(identity)
    normalized = normalize_mock_input(
        raw, collected_at=event.collected_at, threat_event_id=event.threat_event_id,
    )
    if normalized != event or event.target_arn != data.asset_context.arn:
        raise ValueError(f"{case_id}: frozen event differs from current normalizer/source")
    actual_risk = evaluate_threat(event)
    if actual_risk != data.initial_risk:
        raise ValueError(f"{case_id}: frozen initial risk differs from current service")
    _check_context(case_id, data, fixture)

    asset = data.asset_context.model_dump(mode="json")
    ec2 = next(item for item in inventory["ec2_instances"] if item["arn"] == asset["arn"])
    expected_asset = {
        "resource_id": ec2["instance_id"], "name": ec2["name"], "state": ec2["state"],
        "account_id": inventory["account_id"], "region": inventory["region"],
        "collected_at": inventory["collected_at"],
        "spec": {key: ec2.get(key) for key in (
            "instance_type", "availability_zone", "vpc_id", "subnet_id", "private_ip", "tags",
        )},
    }
    # collector.persist_inventory와 같은 필드를 선택한다. 실제 DB 저장·조회 비교는
    # 별도 통합 검증이며 모델 품질 측정을 대신하지 않는다.
    if any(asset[key] != value for key, value in expected_asset.items()):
        raise ValueError(f"{case_id}: frozen asset differs from its inventory fixture")
    incident = SimpleNamespace(
        incident_id=data.incident_id,
        subject_arn=event.target_arn, threat_event_id=event.threat_event_id,
        initial_risk_level=data.initial_risk.initial_risk_level,
        initial_risk_reason_codes=data.initial_risk.reason_codes,
    )
    threat = EvidenceItem(
        evidence_id=evidence.evidence_id, incident_id=data.incident_id,
        evidence_type=evidence.evidence_type, source_type="threat_event",
        source_id=event.threat_event_id, content=evidence.content,
        occurred_at=event.occurred_at, collected_at=event.collected_at,
    )
    prior = data.isolation_execution
    # 서비스는 선행 실행 행을 조립 함수에 넘긴다. 동결한 기록을 같은 모양의 행으로 되돌려
    # 조립 함수가 isolation_execution을 그대로 재현하는지 대조한다.
    executions = [] if prior is None else [SimpleNamespace(
        execution_id=prior.execution_id, runbook_id=prior.runbook_id, status=prior.status,
        target_arn=prior.affected_arns[0] if prior.affected_arns else event.target_arn,
        error_summary=prior.result_summary, trigger_source=TriggerSource.PRE_MITIGATION_0_5S,
    )]
    assembled = assemble_secops_input(
        incident=incident, threat=threat, executions=executions,
    )
    if assembled != data:
        raise ValueError(f"{case_id}: frozen graph input differs from current assembler")
    return EvalCase(case_id, record["title"], assembled, provenance)


def load_cases(path: Path = ROOT / "inputs.json") -> list[EvalCase]:
    records = read_json(path)["cases"]
    ids = [record["case_id"] for record in records]
    if ids != FIXED_CASE_IDS:
        raise ValueError("Fixed primary set must contain C01 through C10 exactly once in order")
    cases = [validate_case(record) for record in records]
    identities = [case.graph_input.incident_id for case in cases]
    if len(identities) != len(set(identities)):
        raise ValueError("Incident IDs must be unique across cases")
    for record in records:
        if record["evaluation_role"] != "mvp_scenario":
            raise ValueError("Unknown evaluation role")
    return cases

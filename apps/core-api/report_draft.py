"""저장된 기록의 구조화 사실을 복사하고 트랜잭션을 끝낸 뒤 신고 초안을 만든다."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from enum import Enum
from ipaddress import ip_address, ip_network
from uuid import UUID

from ai.model_client import AIModelClient, mask_outbound
from ai.report_draft import generate_narrative, narrative_request
from db import models
from db.repositories import audit as audit_repo
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from evidence_export import (
    ExportFile, ExportFormat, MEDIA_TYPES, build_document, code_span, json_fence,
)
from exceptions import ApiError
from schemas.api.errors import ErrorCode
from schemas.api.report_draft import (
    ReportDraftDocument, ReportEvidenceFlags, ReportFact, ReportFacts,
    ReportHumanField,
)
from sqlalchemy.orm import Session

REPORT_FILE_MAX_BYTES = 2 * 1024 * 1024
NOTICE = (
    "침해사고 신고 작성 보조 초안 — 제출 전 사람이 확인·수정해야 합니다. "
    "인용 검사는 이 요청의 사실 범위만 확인하며 서술의 진실성을 보장하지 않습니다. "
    "기준 시각까지 읽은 저장 기록이며 원천별 조회 시점은 다를 수 있습니다. "
    "신원·자유문은 입력에서 제외했고 IP·ARN·계정 ID는 포함될 수 있습니다. "
    "신고 의무·기한·수신처 판단, 제출, 개인정보 유출 신고·통지는 하지 않습니다."
)

_HUMAN_FIELDS = (
    ("awareness_at", "사고인지시점"),
    ("awareness_basis", "사고 인지 근거"),
    ("occurrence_at", "사고발생시간 확인"),
    ("cause", "사고 원인 확인"),
    ("response_department", "대응 부서"),
    ("response_contact", "대응 연락처"),
    ("damage_categories", "피해 범주"),
    ("damage_count", "피해 수량·규모"),
    ("victim_ip_domain", "피해 IP·도메인 확인"),
    ("company_name", "기업명"),
    ("company_registration", "기업 등록 정보"),
    ("company_address", "기업 주소"),
    ("reporter_name", "신고자 이름"),
    ("reporter_department", "신고자 부서"),
    ("reporter_contact", "신고자 연락처"),
    ("reporter_email", "신고자 이메일"),
)
_EXECUTION_MEANINGS = {
    "IN_PROGRESS": "실행 진행 상태 — AWS 실행 시작·성공을 확정하지 않음",
    "SUCCESS": "기록된 조치 성공 — 사고 처리·신고 완료를 뜻하지 않음",
    "FAILED": "기록된 조치 실패",
    "ROLLBACK_INITIATED": "원복 진행 상태",
    "ROLLED_BACK": "기록된 원복 완료 — 사고 처리·신고 완료를 뜻하지 않음",
    "ROLLBACK_FAILED": "기록된 원복 실패",
    "UNVERIFIED": "결과 확인 불가",
}


def _arn(value: str) -> str:
    # 이 입력에는 계정 소유 리소스 식별자만 필요하다. 이름 자유문은 추측해서 복구하지 않는다.
    if not re.fullmatch(
        r"arn:aws(?:-cn|-us-gov)?:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:[A-Za-z0-9_+=,.:/-]+",
        value,
    ):
        raise ValueError("invalid ARN")
    return value


def _guardduty_code(value: str) -> str:
    """탐지 ID·분류·자원 유형·리전의 코드 표기만 허용하고 설명 자유문은 보내지 않는다."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:/!&.-]*", value):
        raise ValueError("invalid GuardDuty code")
    return value


def human_fields() -> list[ReportHumanField]:
    return [
        *(ReportHumanField(key=key, label=label) for key, label in _HUMAN_FIELDS),
        ReportHumanField(key="consent", label="동의", kind="confirmation", checked=False),
        ReportHumanField(key="confirmation", label="확인", kind="confirmation", checked=False),
    ]


def collect_facts(
    incident: models.Incident,
    evidence: list[models.Evidence],
    audits: list[models.AuditEvent],
    executions: list[models.ActionExecution],
    as_of: datetime,
) -> ReportFacts:
    """#616 문서를 입력 계약으로만 재사용한다. 자유문 원문을 결과에 다시 첨부하지 않는다."""
    eligible = [row for row in evidence if row.occurred_at <= as_of and row.collected_at <= as_of]
    exported = build_document(incident, eligible, ExportFormat.JSON, generated_at=as_of)
    facts: list[ReportFact] = []

    def add(kind, record_id, path, at, label, value, *, reason=None, meaning=None, flags=None, identifier=None):
        if isinstance(value, Enum):
            value = value.value
        if value is not None and identifier:
            try:
                value = str(identifier(value))
            except (ValueError, TypeError):
                value, reason = None, "invalid_identifier"
        if isinstance(value, datetime):
            value = value.isoformat()
        if at > as_of:
            value, reason = None, "after_cutoff"
        if value is None or value == "":
            value, reason = None, reason or "not_recorded"
        if isinstance(value, str) and (
            "@" in value or any(unicodedata.category(c).startswith("C") for c in value)
        ):
            value, reason = None, "identity_omitted"
        facts.append(ReportFact(
            fact_id=f"{incident.incident_id}:F{len(facts) + 1}",
            source_kind=kind, record_id=record_id, json_path=path, recorded_at=at,
            label=label, value=value, status="unknown" if reason else "known",
            reason=reason, interpretation=meaning, flags=flags,
        ))

    head = exported["incident"]
    for field, label, meaning in (
        ("category", "분류", None),
        ("status", "관제 상태", "RESOLVED는 관제 종료이며 사고 처리·신고 완료가 아님"),
        ("created_at", "인시던트 생성 시각", "사고발생시간 후보 — 사고인지시점이 아님"),
        ("updated_at", "인시던트 갱신 시각", None),
        ("subject_arn", "탐지 대상", "피해·침입 확정이 아님"),
    ):
        add("incident", incident.incident_id, f"/incident/{field}",
            incident.updated_at, label, head[field], meaning=meaning,
            identifier=_arn if field == "subject_arn" else None)
    add("incident", incident.incident_id, "/incident/title", incident.updated_at,
        "제목", None, reason="free_text_omitted")

    def selected(item, index, path, label, *, meaning=None, identifier=None):
        value = item
        for key in path.split("/"):
            value = value.get(key) if isinstance(value, dict) else None
        add("evidence", item["evidence_id"], f"/evidence/{index}/{path}",
            datetime.fromisoformat(item["collected_at"].replace("Z", "+00:00")),
            label, value, meaning=meaning, flags=item["flags"], identifier=identifier)

    for index, item in enumerate(exported["evidence"]):
        selected(item, index, "evidence_type", "근거 종류")
        selected(item, index, "occurred_at", "이벤트 발생 시각",
                 meaning="사고발생시간 후보 — 사고인지시점이 아님")
        selected(item, index, "collected_at", "근거 수집 시각")
        if item["evidence_type"] == "THREAT":
            selected(item, index, "content/event/event_type", "탐지 유형",
                     meaning="원인·피해 또는 실제 침입을 확정하지 않음")
            event = item["content"]["event"]
            selected(item, index, "content/event/target_arn", "탐지 대상 ARN", identifier=_arn,
                     meaning=(
                         "EC2가 식별되면 인스턴스 ARN, 그 밖은 자원 ARN이 아닌 finding ARN — 피해 확정이 아님"
                         if event["event_type"] == "GUARDDUTY_FINDING" else None
                     ))
            if event["event_type"] == "SSH_BRUTE_FORCE":
                selected(item, index, "content/event/payload/source_ip", "공격 출발지 IP",
                         meaning="피해 IP가 아님", identifier=ip_address)
                selected(item, index, "content/event/payload/failed_attempt_count", "관측 실패 횟수")
                selected(item, index, "content/event/payload/window_seconds", "관측 구간(초)")
            elif event["event_type"] == "OPEN_IP":
                selected(item, index, "content/event/payload/source_cidr", "접근 허용 CIDR",
                         identifier=ip_network)
                selected(item, index, "content/event/payload/from_port", "개방 시작 포트")
                selected(item, index, "content/event/payload/to_port", "개방 종료 포트")
            elif event["event_type"] == "GUARDDUTY_FINDING":
                for field, label in (
                    ("finding_id", "GuardDuty 탐지 ID"),
                    ("finding_type", "GuardDuty 탐지 분류"),
                    ("resource_type", "GuardDuty 자원 유형"),
                    ("region", "GuardDuty 탐지기를 읽은 리전"),
                    ("finding_region", "GuardDuty 탐지에 표시된 리전"),
                ):
                    selected(item, index, f"content/event/payload/{field}", label,
                             identifier=_guardduty_code)
                selected(item, index, "content/event/payload/severity", "GuardDuty 기록 심각도",
                         meaning="탐지 심각도이며 원인·피해 규모를 확정하지 않음")
                selected(item, index, "content/event/payload/first_seen_at", "GuardDuty 최초 관측 시각",
                         meaning="사고발생시간 후보 — 사고인지시점이 아님")
                selected(item, index, "content/event/payload/last_seen_at", "GuardDuty 최근 관측 시각",
                         meaning="최초 저장 사본에 기록된 시각 — 현재 최신 탐지 상태를 보장하지 않음")
            selected(item, index, "content/context/target_status", "대상 수집 상태")
            context = item["content"].get("context")
            target = context.get("target") if context else None
            asset = target["asset"] if target else None
            if asset and asset["asset_type"] == "EC2":
                selected(item, index, "content/context/target/asset/spec/private_ip", "피해 IP 후보",
                         meaning="저장된 EC2 사설 IP 후보 — 피해 여부는 사람이 확인", identifier=ip_address)
            else:
                add("evidence", item["evidence_id"], f"/evidence/{index}/content/context/target",
                    as_of, "피해 IP 후보", None, reason="not_collected", flags=item["flags"])
        elif item["evidence_type"] == "EXECUTION":
            selected(item, index, "content/execution_id", "근거의 실행 ID", identifier=UUID)
            selected(item, index, "content/runbook_id", "근거의 조치 종류")
            selected(item, index, "content/status", "근거에 기록된 실행 상태",
                     meaning=_EXECUTION_MEANINGS[item["content"]["status"]])
        elif item["evidence_type"] == "ASSET":
            selected(item, index, "content/asset/asset_type", "대상 자산 종류")
            selected(item, index, "content/asset/arn", "자산 ARN", identifier=_arn)
            if item["content"]["asset"]["asset_type"] == "EC2":
                selected(item, index, "content/asset/spec/private_ip", "피해 IP 후보",
                         meaning="저장된 EC2 사설 IP 후보 — 피해 여부는 사람이 확인", identifier=ip_address)
        add("evidence", item["evidence_id"], f"/evidence/{index}/content", as_of,
            "선택하지 않은 근거 내용", None, reason="free_text_omitted", flags=item["flags"],
            meaning="신원 보호를 위해 자유문·로그 본문·태그 등은 모델과 초안에서 제외")

    for row in audits:
        if row.occurred_at > as_of:
            continue
        for field, label, meaning in (
            ("action", "감사 동작", "ACTION_EXECUTION_STARTED는 실행 예약 — AWS 시작·성공이 아님"),
            ("actor_kind", "행위자 종류", "SYSTEM은 사람의 승인이 아님"),
            ("trigger_source", "발동 원천", "시스템 발동은 사람의 승인이 아님"),
            ("execution_id", "연결 실행 ID", None),
            ("resolution", "관제 종료 판단", "사고 처리·신고 완료가 아님"),
        ):
            add("audit_event", row.event_id, f"/{field}", row.occurred_at,
                label, getattr(row, field), meaning=meaning)
        add("audit_event", row.event_id, "/actor", row.occurred_at,
            "행위자 신원", None, reason="identity_omitted")
    for row in executions:
        if row.created_at > as_of:
            continue
        for field, label, meaning in (
            ("runbook_id", "조치 종류", None),
            ("target_arn", "조치 대상", None),
            ("status", "기록된 실행 상태", _EXECUTION_MEANINGS[row.status.value]),
            ("trigger_source", "발동 원천", "시스템 발동은 사람의 승인이 아님"),
            ("created_at", "실행 예약 시각", "AWS 실행 시작·성공을 뜻하지 않음"),
            ("finished_at", "기록된 완료 시각", "시각이 있어도 결과를 증명하지 않음"),
        ):
            add("action_execution", row.execution_id, f"/{field}", row.updated_at,
                label, getattr(row, field), meaning=meaning,
                identifier=_arn if field == "target_arn" else None)
    if not executions:
        add("action_execution", incident.incident_id, "/action_executions", as_of,
            "대응 이력", None, reason="no_history", meaning="조치가 없었다는 뜻이 아님")
    if not audits:
        add("audit_event", incident.incident_id, "/audit_events", as_of,
            "감사 이력", None, reason="no_history", meaning="승인·거절·종료가 없었다는 뜻이 아님")
    if not eligible:
        add("evidence", incident.incident_id, "/evidence", as_of,
            "저장 근거", None, reason="not_collected")
    return ReportFacts(
        facts=facts,
        evidence_flags=[
            ReportEvidenceFlags(evidence_id=item["evidence_id"], flags=item["flags"])
            for item in exported["evidence"]
        ],
        missing=exported["export"]["missing"],
    )


def render_draft(document: ReportDraftDocument, fmt: ExportFormat) -> bytes:
    data = document.model_dump(mode="json")
    if fmt is ExportFormat.JSON:
        content = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        lines = [
            "# 침해사고 신고 작성 보조 초안", "", NOTICE, "",
            f"- 기준 시각: {code_span(data['as_of'])}",
            f"- 생성 시각: {code_span(data['generated_at'])}",
            "", "## 서버 사실", "",
            "### 근거 표시", "", json_fence(data["server_facts"]["evidence_flags"]), "",
            "### 묶음 누락", "", json_fence(data["server_facts"]["missing"]), "",
            "### 사실 목록", "",
        ]
        for fact in document.server_facts.facts:
            value = "미확인" if fact.status == "unknown" else fact.value
            lines += [
                f"- {code_span(fact.fact_id)} {code_span(fact.label)}: {code_span(value)}",
                f"  - 원천: {code_span(fact.source_kind)} / {code_span(fact.record_id)} "
                f"/ {code_span(fact.json_path)} / {code_span(fact.recorded_at.isoformat())}",
            ]
            if fact.reason:
                lines.append(f"  - 미확인 사유: {code_span(fact.reason)}")
            if fact.interpretation:
                lines.append(f"  - 해석: {code_span(fact.interpretation)}")
        lines += ["", "## AI 서술", "", "인용 범위만 검사했습니다. 내용은 사람이 확인해야 합니다."]
        for sentence in document.ai_narrative.sentences:
            lines.append(f"- {code_span(sentence.section)}: {code_span(sentence.text)}")
            lines.append(f"  - 인용: {', '.join(code_span(c) for c in sentence.citations)}")
        lines += ["", "## 사람 입력", ""]
        for field in document.human_input:
            lines.append(f"- {'[ ] ' if field.kind == 'confirmation' else ''}{field.label}: ")
        content = "\n".join(lines) + "\n"
    body = content.encode("utf-8")
    if len(body) > REPORT_FILE_MAX_BYTES:
        raise ApiError(ErrorCode.REPORT_DRAFT_TOO_LARGE)
    return body


def generate_report_draft(
    db: Session, incident: models.Incident, fmt: ExportFormat, client: AIModelClient | None,
) -> ExportFile:
    if client is None:
        raise ApiError(ErrorCode.REPORT_DRAFT_MODEL_UNAVAILABLE)
    # STARTED와 실행 행은 함께 commit된다. 감사부터 읽어야 연결 실행이 조회에서 빠지지 않는다.
    evidence = incidents_repo.list_evidence(db, incident.incident_id)
    audits = audit_repo.list_by_incident(db, incident.incident_id)
    executions = executions_repo.list_by_incident(db, incident.incident_id)
    as_of = datetime.now(timezone.utc)
    facts = collect_facts(incident, evidence, audits, executions, as_of)
    incident_id = incident.incident_id
    request = narrative_request(facts)
    # 모든 ORM 값을 복사한 뒤 종료한다. rollback 뒤 행에 접근하면 새 트랜잭션이 열린다.
    db.rollback()
    narrative = generate_narrative(client, request)
    document = ReportDraftDocument(
        incident_id=incident_id, as_of=as_of, generated_at=datetime.now(timezone.utc),
        notice=NOTICE, server_facts=facts, ai_narrative=narrative, human_input=human_fields(),
    )
    document = ReportDraftDocument.model_validate(mask_outbound(document.model_dump(mode="json")))
    return ExportFile(
        media_type=MEDIA_TYPES[fmt], filename=f"incident-{incident_id}-report-draft.{fmt.value}",
        body=render_draft(document, fmt),
    )

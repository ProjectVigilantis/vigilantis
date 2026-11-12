# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 인시던트 근거 내보내기 파일의 계약입니다 — GET /api/v1/incidents/{incident_id}/evidence/export (Issue #488).
# JSON 파일의 본체(EvidenceExportDocument)이며, Markdown 파일은 같은 값을 사람용으로 그린 것이다.
# 읽는 쪽은 사람(상급자 · 외부 전문가)과 LLM(재검토)이고, 화면은 다운로드 버튼뿐이다(2026-11-09 결정 1).
#
# 계약 원칙
#   - 저장된 근거 행(evidence_items)만 싣는다. 현재 자산 · 수집기 · AWS 를 다시 읽어 당시 자료를 대체하지
#     않는다(이슈 요구사항 · ADR-0005 근거 불변).
#   - 근거의 content 는 내부 계약 EvidenceItem(schemas.evidence)으로 검증한 사본이다 — 계약 밖 키는 파일에
#     실리지 않는다. 그 위에 비밀값(토큰 · 키 · 비밀번호 류)을 마스킹한다. 개인정보(공격 출처 IP · 대상
#     사설 IP · 로그 안 계정명)는 1차에서 가리지 않는다 — 침해 분석 · 신고에 필요하다(결정 3). 그래서 머리말
#     notice 가 「내부 전달용 · 개인정보 포함 가능」을 알린다.
#   - 출처 · 시점 · 누락 · 가공 · 합성 · 발췌(flags)는 content 밖 최상위에 둔다 — LLM 이 content 를 데이터로,
#     flags 를 메타로 다루게. 플래그는 저장 필드에서만 도출한다(원천은 각 필드 주석).
#   - evidence_type 은 EvidenceType 값의 문자열이다. 여기서 schemas.evidence 를 import 하면
#     evidence → events → api.incidents 로 순환이 생겨 문자열로 둔다. 서버는 EvidenceItem 으로 검증한
#     뒤 값을 옮기므로 목록 밖 값은 들어오지 않는다.
# ==============================================================================

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .assets import UtcDateTime
from .incidents import IncidentCategory, IncidentStatus

#: 근거 1건에서 저장 필드만으로 읽어 낸 누락 사유. THREAT 접수 사본(SecOpsEvidenceContext)이 원천이다.
EvidenceMissingReason = Literal[
    # THREAT: 접수 때 자산 · 관계 · 로그 사본을 남기지 않았다(#350 이전 근거 — content.context 가 null)
    "context_not_captured",
    # THREAT: 접수 때 대상 자산 행이 없었다(context.target_status)
    "target_not_collected",
    # THREAT: 대상 자산이 이미 사라진 상태로 기록돼 있었다(context.target_status)
    "target_absent",
    # THREAT: 대상 자산 행에 수집 회차가 없었다(context.target_status)
    "target_missing_collection",
    # THREAT: 직접 SG/NACL 관계 중 사본으로 남기지 못한 것이 있다(context.relation_issues)
    "relation_issues",
    # THREAT: 로그 발췌가 첨부되지 않았다(context.log_evidence 가 null)
    "log_not_attached",
]

#: 인시던트 묶음 수준의 누락 사유.
ExportMissingReason = Literal[
    # 근거 행이 하나도 없다
    "no_evidence",
    # FINOPS: 판정 회차의 관측 요약(METRIC 근거)이 없었다 — 수집 회차에 요약이 없으면 접수가 싣지 않는다
    "metric_evidence_absent",
]


class EvidenceExportFlags(BaseModel):
    """근거 1건의 성격 — 저장 필드에서만 도출한다(현재 자산 · 수집기 재조회 없음)."""

    model_config = ConfigDict(extra="forbid")

    #: True 면 가공된 분석 입력(RULE 판정 · METRIC 요약 · EXECUTION 요약), False 면 원천 사본(ASSET · THREAT).
    processed: bool
    #: 합성 자료가 들어 있다(MockSshLogEvidence.data_kind == "synthetic").
    synthetic: bool
    #: 전체가 아닌 발췌가 들어 있다(MockSshLogEvidence.excerpt_policy).
    excerpt: bool
    missing: list[EvidenceMissingReason] = Field(default_factory=list)


class EvidenceExportItem(BaseModel):
    """근거 1건. flags 를 빼면 내부 계약 EvidenceItem 과 같은 모양이다 — content 는 그 계약으로 검증한 뒤
    비밀값을 마스킹한 사본이라 dict 로 둔다(마스킹이 바꾼 값을 다시 계약으로 묶지 않는다)."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    evidence_type: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    occurred_at: UtcDateTime
    collected_at: UtcDateTime
    flags: EvidenceExportFlags
    content: dict[str, Any]


class EvidenceExportIncident(BaseModel):
    """머리말의 인시던트 — 상세 응답(IncidentResponse)의 식별 · 분류 필드만."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    title: Optional[str]
    subject_arn: str = Field(min_length=1)
    category: IncidentCategory
    status: IncidentStatus
    #: 소속 고객사. MVP 기록(소속 없음)은 null.
    tenant_id: Optional[str]
    created_at: UtcDateTime
    updated_at: UtcDateTime


class EvidenceExportMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    format: Literal["md", "json"]
    generated_at: UtcDateTime
    #: 「내부 전달용 · 개인정보 포함 가능」 고지. 서버 문구다.
    notice: str = Field(min_length=1)
    evidence_count: int = Field(ge=0)
    missing: list[ExportMissingReason] = Field(default_factory=list)


class EvidenceExportDocument(BaseModel):
    """JSON 파일의 본체. 최상위 키는 export(메타) · incident(머리말) · evidence(근거 목록) 셋뿐이다."""

    model_config = ConfigDict(extra="forbid")

    export: EvidenceExportMeta
    incident: EvidenceExportIncident
    evidence: list[EvidenceExportItem] = Field(default_factory=list)

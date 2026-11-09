# ==============================================================================
# [파일 설명]
# 인시던트 근거 내보내기(#488) — 저장된 근거 행을 사람용 Markdown · 기계용 JSON 파일로 조립한다.
# 파일 계약: schemas.api.evidence_export. 다운로드 라우트와 감사 기록은 다음 커밋에서 연결한다.
#
#   - 저장된 행만 쓴다. AWS · 수집기 · 현재 자산 테이블을 다시 읽어 당시 자료를 대체하지 않는다(ADR-0005).
#     이 모듈은 저장 행만 받아 파일 바이트를 만든다.
#   - 마스킹 2층(security 설계 검토 2026-11-09). 1층 스키마 allow-list: 행 → EvidenceItem(mappers.to_evidence_item)
#     → model_dump. 스키마 밖 키는 직렬화에 남지 않고, 검증에서 거절된 행은 500 INTERNAL_ERROR 로 멈춘다.
#     일부 중첩 모델은 미지정 키를 제외하는 기존 계약을 따른다. pydantic 오류문은 입력값을 실으므로 로그에는
#     식별자만 남긴다. 2층 ai.model_client.mask_outbound: 제목까지 포함한 문서 전체를 한 번 지난다. 두 번째
#     regex 세트를 두지 않는다 — 가릴 모양의 원천은 그 하나다.
#   - 플래그는 저장 필드에서만 도출한다. synthetic=MockSshLogEvidence.data_kind, excerpt=excerpt_policy,
#     missing=context 없음 · target_status≠available · 관계 사본 누락 · 로그 미첨부 · (묶음) METRIC 부재,
#     processed=RULE · METRIC · EXECUTION(요약 · 판정) vs ASSET · THREAT(원천 사본).
#   - Markdown 은 구조(헤딩 · 라벨 목록)를 서버 통제 값으로만 만든다. 근거 content 는 통째로 ```json 펜스 안에,
#     제목 같은 한 줄 필드는 코드 스팬 안에 둔다. 펜스 · 스팬 구분자는 내용의 최장 백틱 연속보다 1 길게 잡아
#     내용이 구분자를 닫지 못하게 한다. 표는 쓰지 않는다 — 셀 안 | 이스케이프가 파서마다 달라 스팬이 깨질 수
#     있다(code_span 설명). 원문 HTML 은 내지 않는다.
#   - 크기: 페이지네이션 없이 메모리에서 만든다(적재 상한이 스키마에 있다 — FinOps ≤3건, SecOps 1건 · 관계 ≤64 ·
#     발췌 ≤12행). EVIDENCE_EXPORT_MAX_BYTES 를 넘으면 413 EVIDENCE_EXPORT_TOO_LARGE — 잘라서 보내지 않는다.
#   - 파일명은 저장된 incident_id(정규 UUID)와 고정 접미만 쓴다. 제목 · 고객사명 같은 사용자 문자열은 헤더에
#     싣지 않는다(헤더 주입 · 비ASCII 인코딩 문제).
#
#   - HTTP 반출 경로는 여기서 열지 않는다. 권한 · 범위 검사와 감사 기록을 연결한 뒤 제공한다.
# ==============================================================================

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from ai.model_client import mask_outbound
from db import mappers, models
from exceptions import ApiError
from pydantic import ValidationError
from schemas.api.errors import ErrorCode
from schemas.api.evidence_export import (
    EvidenceExportDocument,
    EvidenceExportFlags,
    EvidenceExportIncident,
    EvidenceExportItem,
    EvidenceExportMeta,
)
from schemas.api.incidents import IncidentCategory
from schemas.evidence import EvidenceItem, EvidenceType, ThreatEvidence

logger = logging.getLogger("vigilantis.evidence_export")

#: 파일 하나의 상한(바이트). 넘으면 413 — 잘라서 보내지 않는다.
EVIDENCE_EXPORT_MAX_BYTES = 2 * 1024 * 1024

#: 머리말 고지(결정 3 — 1차는 개인정보를 가리지 않는다).
EXPORT_NOTICE = (
    "내부 전달용 — 개인정보(공격 출처 IP · 대상 사설 IP · 로그 안 계정명)가 포함될 수 있습니다. "
    "알려진 형식의 비밀값만 마스킹했으며, 다른 형식의 비밀값은 남아 있을 수 있습니다."
)

#: 가공된 분석 입력 — 원천 사본(ASSET · THREAT)과 구분해 표시한다(이슈 요구사항 3).
_PROCESSED_TYPES = frozenset({EvidenceType.RULE, EvidenceType.METRIC, EvidenceType.EXECUTION})


class ExportFormat(str, Enum):
    MD = "md"
    JSON = "json"


MEDIA_TYPES: dict[ExportFormat, str] = {
    ExportFormat.MD: "text/markdown; charset=utf-8",
    ExportFormat.JSON: "application/json; charset=utf-8",
}


@dataclass(frozen=True)
class ExportFile:
    media_type: str
    filename: str
    body: bytes


# --- 플래그 -----------------------------------------------------------------------------


def derive_flags(item: EvidenceItem) -> EvidenceExportFlags:
    """근거 1건의 성격 — 검증된 content 의 저장 필드만 본다."""
    synthetic = excerpt = False
    missing: list[str] = []
    content = item.content
    if isinstance(content, ThreatEvidence):
        context = content.context
        if context is None:
            missing.append("context_not_captured")
        else:
            if context.target_status != "available":
                missing.append(f"target_{context.target_status}")
            if context.relation_issues:
                missing.append("relation_issues")
            logs = context.log_evidence
            if logs is None:
                missing.append("log_not_attached")
            else:
                synthetic = logs.data_kind == "synthetic"
                excerpt = logs.excerpt_policy is not None
    return EvidenceExportFlags(
        processed=item.evidence_type in _PROCESSED_TYPES,
        synthetic=synthetic,
        excerpt=excerpt,
        missing=missing,
    )


def bundle_missing(category: IncidentCategory, items: Sequence[EvidenceItem]) -> list[str]:
    """인시던트 묶음 수준의 누락 — 행이 없거나, FinOps 인데 판정 회차의 METRIC 요약이 없다."""
    if not items:
        return ["no_evidence"]
    if category is IncidentCategory.FINOPS and not any(
        item.evidence_type is EvidenceType.METRIC for item in items
    ):
        return ["metric_evidence_absent"]
    return []


# --- 문서 조립 ---------------------------------------------------------------------------


def _validated(row: models.Evidence) -> EvidenceItem:
    """1층 allow-list. 계약을 어긴 행은 500 봉투로 멈춘다 — pydantic 오류문은 입력값(비밀값일 수 있다)을 싣기 때문에
    그대로 올리지 않고(미처리 예외 로그가 원문을 남긴다) 식별자만 로그에 남긴다."""
    try:
        return mappers.to_evidence_item(row)
    except ValidationError as exc:
        logger.error(
            "evidence_export_stored_content_invalid",
            extra={
                "incident_id": row.incident_id,
                "evidence_id": row.evidence_id,
                "evidence_type": getattr(row.evidence_type, "value", str(row.evidence_type)),
                "error_count": exc.error_count(),
            },
        )
        raise ApiError(ErrorCode.INTERNAL_ERROR) from None


def _mask_mapping_keys(value: Any) -> Any:
    """기존 마스커가 보존하는 키에도 같은 문자열 규칙을 적용한다. 태그 키 역시 수집 입력이다."""
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            masked_key = mask_outbound(key)
            if masked_key in masked:
                # 두 키가 같은 마스크가 되면 근거 하나를 조용히 덮어쓰지 않는다.
                logger.error("evidence_export_redacted_key_collision")
                raise ApiError(ErrorCode.INTERNAL_ERROR)
            masked[masked_key] = _mask_mapping_keys(item)
        return masked
    if isinstance(value, list):
        return [_mask_mapping_keys(item) for item in value]
    return value


def build_document(
    incident: models.Incident,
    rows: Sequence[models.Evidence],
    fmt: ExportFormat,
    *,
    generated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """행 → 검증(1층) → 플래그 → JSON 호환 dict → 마스킹(2층). 반환값이 파일에 실리는 값 그 자체다."""
    items = [_validated(row) for row in rows]
    document = EvidenceExportDocument(
        export=EvidenceExportMeta(
            format=fmt.value,
            generated_at=generated_at or datetime.now(timezone.utc),
            notice=EXPORT_NOTICE,
            evidence_count=len(items),
            missing=bundle_missing(incident.category, items),
        ),
        incident=EvidenceExportIncident(
            incident_id=incident.incident_id,
            title=incident.title,
            subject_arn=incident.subject_arn,
            category=incident.category,
            status=incident.status,
            tenant_id=incident.tenant_id,
            created_at=incident.created_at,
            updated_at=incident.updated_at,
        ),
        evidence=[
            EvidenceExportItem(
                evidence_id=item.evidence_id,
                incident_id=item.incident_id,
                evidence_type=item.evidence_type.value,
                source_type=item.source_type,
                source_id=item.source_id,
                occurred_at=item.occurred_at,
                collected_at=item.collected_at,
                flags=derive_flags(item),
                content=item.content.model_dump(mode="json"),
            )
            for item in items
        ],
    )
    return _mask_mapping_keys(mask_outbound(document.model_dump(mode="json")))


# --- Markdown ------------------------------------------------------------------------------

_BACKTICK_RUN = re.compile(r"`+")


def _longest_backtick_run(text: str) -> int:
    return max((len(match.group()) for match in _BACKTICK_RUN.finditer(text)), default=0)


def code_span(value: Any) -> str:
    """한 줄 값을 코드 스팬으로. 줄바꿈은 공백으로, 구분자는 내용의 백틱보다 1 길게.

    표 안에 두지 않는다 — 표 셀은 | 이스케이프가 필요한데 `\\|` 처리 방식이 파서마다 달라(marked 는 셀을 쪼개
    스팬이 깨진다) 셀 밖으로 HTML · 링크가 샐 수 있다(리뷰 지적). 목록 줄에서는 스팬 안 | 가 의미가 없다.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    ticks = "`" * (_longest_backtick_run(text) + 1)
    if not text.strip() or text.startswith("`") or text.endswith("`"):
        text = f" {text} "
    return f"{ticks}{text}{ticks}"


def json_fence(value: Any) -> str:
    """값을 통째로 ```json 펜스에. 펜스 길이 = 내용의 최장 백틱 연속 + 1(최소 3)이라 내용이 펜스를 닫지 못한다."""
    text = json.dumps(value, ensure_ascii=False, indent=2)
    fence = "`" * max(3, _longest_backtick_run(text) + 1)
    return f"{fence}json\n{text}\n{fence}"


def _cell(value: Any) -> str:
    """목록 줄의 값. None 은 서버 문구, bool 은 예 · 아니오, 나머지는 코드 스팬."""
    if value is None:
        return "(없음)"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    return code_span(value)


def _reasons(reasons: Sequence[str]) -> str:
    return " · ".join(code_span(reason) for reason in reasons) if reasons else "없음"


def _fields(rows: Sequence[tuple[str, str]]) -> list[str]:
    """라벨 · 값 목록. 표가 아니라 목록인 이유는 code_span 의 설명 — 줄머리 `- **라벨**: ` 뒤는 전부 스팬 안이다."""
    return [f"- **{label}**: {value}" for label, value in rows]


def render_markdown(document: dict[str, Any]) -> str:
    """마스킹된 문서(build_document 반환값) → 사람용 Markdown. 헤딩 · 목록의 라벨은 전부 서버 문구다."""
    export, incident, evidence = document["export"], document["incident"], document["evidence"]
    lines = [
        "# 인시던트 근거 내보내기",
        "",
        f"> {export['notice']}",
        "",
        *_fields([
            ("인시던트", _cell(incident["incident_id"])),
            ("제목", _cell(incident["title"])),
            ("분류 · 상태", f"{_cell(incident['category'])} · {_cell(incident['status'])}"),
            ("대상", _cell(incident["subject_arn"])),
            ("고객사", _cell(incident["tenant_id"]) if incident["tenant_id"] else "(소속 없음)"),
            ("인시던트 생성", _cell(incident["created_at"])),
            ("파일 생성", _cell(export["generated_at"])),
            ("근거 건수", _cell(export["evidence_count"])),
            ("묶음 수준 누락", _reasons(export["missing"])),
        ]),
    ]
    for index, item in enumerate(evidence, start=1):
        flags = item["flags"]
        lines += [
            "",
            f"## 근거 {index}/{export['evidence_count']} — {_cell(item['evidence_type'])}",
            "",
            *_fields([
                ("근거 ID", _cell(item["evidence_id"])),
                ("출처 종류", _cell(item["source_type"])),
                ("출처 ID", _cell(item["source_id"])),
                ("발생 시각", _cell(item["occurred_at"])),
                ("수집 시각", _cell(item["collected_at"])),
                ("성격", "가공된 분석 입력(요약 · 판정)" if flags["processed"] else "원천 사본"),
                ("합성 자료", _cell(flags["synthetic"])),
                ("발췌", _cell(flags["excerpt"])),
                ("누락", _reasons(flags["missing"])),
            ]),
            "",
            "### 내용",
            "",
            json_fence(item["content"]),
        ]
    return "\n".join(lines) + "\n"


# --- 파일 -------------------------------------------------------------------------------------


def render(document: dict[str, Any], fmt: ExportFormat) -> bytes:
    """문서 → 파일 바이트. 상한을 넘으면 413 — 부분 파일을 만들지 않는다."""
    if fmt is ExportFormat.MD:
        text = render_markdown(document)
    else:
        text = json.dumps(document, ensure_ascii=False, indent=2)
    body = text.encode("utf-8")
    if len(body) > EVIDENCE_EXPORT_MAX_BYTES:
        raise ApiError(ErrorCode.EVIDENCE_EXPORT_TOO_LARGE)
    return body


def export_filename(incident_id: str, fmt: ExportFormat) -> str:
    """저장된 incident_id(정규 UUID)와 고정 접미만 — 사용자 문자열은 쓰지 않는다."""
    return f"incident-{incident_id}-evidence.{fmt.value}"


def build_export_file(
    incident: models.Incident,
    rows: Sequence[models.Evidence],
    fmt: ExportFormat,
    *,
    generated_at: Optional[datetime] = None,
) -> ExportFile:
    document = build_document(incident, rows, fmt, generated_at=generated_at)
    return ExportFile(
        media_type=MEDIA_TYPES[fmt],
        filename=export_filename(incident.incident_id, fmt),
        body=render(document, fmt),
    )

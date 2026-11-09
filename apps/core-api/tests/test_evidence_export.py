# ==============================================================================
# [파일 설명]
# 인시던트 근거 내보내기(#488) — GET /api/v1/incidents/{incident_id}/evidence/export?format=md|json.
#
# 조립 모듈(evidence_export) 단위 — DB 없이 행 객체만으로 검증 · 플래그 · 마스킹 · Markdown · 크기 · 파일명.
# 라우트 · 권한 · 감사 기록 통합 테스트는 HTTP 경로를 연결하는 다음 커밋에 둔다.
#
# 계약(브리프 · security 설계 검토 2026-11-09 반영)
#   - 의존성 순서: protected(Origin · 세션) → require_permission(EXECUTE_ACTION) → require_tenant_scope.
#     대상은 _load_incident 재사용 — 403 이 404 보다 앞서고, 범위 밖 · 형식 오류는 404 INCIDENT_NOT_FOUND.
#   - 저장된 행만 쓴다. AWS · 수집기 · 현재 자산 테이블 재조회 금지(ADR-0005 — 근거 불변).
#   - 마스킹 2층: EvidenceItem.model_validate(스키마 allow-list) → model_dump → ai.model_client.mask_outbound.
#   - 감사: 200 에서 platform_events 1행(action INCIDENT_EVIDENCE_EXPORT · actor 종류 · 대상 인시던트 · 고객사 ·
#     format · 근거 건수), 같은 트랜잭션. 403 · 404 의 공통 ACCESS_* 기록 호출은 별도 작업이며 여기 행은 없다.
#   - 크기: EVIDENCE_EXPORT_MAX_BYTES(2 MiB) 초과는 413 EVIDENCE_EXPORT_TOO_LARGE 봉투. 잘라서 보내지 않는다.
#   - 헤더: Content-Type(text/markdown · application/json; charset=utf-8), Content-Disposition
#     attachment; filename="incident-{canonical_id}-evidence.{md|json}"(ASCII · 사용자 문자열 없음),
#     Cache-Control no-store · X-Content-Type-Options nosniff 는 auth.http 미들웨어 몫(여기서는 확인만).
#   - Markdown: 구조는 서버 통제 값만. 근거 content 는 ```json 펜스(길이 = 최장 백틱 연속 + 1, 최소 3),
#     한 줄 필드는 \r\n 제거 후 목록의 코드 스팬, 원문 HTML 없음.
#   - 플래그는 저장 필드에서만: synthetic=data_kind, excerpt=excerpt_policy, missing=target_status≠available ·
#     context None · METRIC 부재, processed=RULE/METRIC(요약 · 판정) vs ASSET/THREAT(원천 사본).
#     JSON 도 같은 플래그 · 메타를 content 밖 최상위에 둔다.
# ==============================================================================

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import evidence_export
import pytest
from db import models
from evidence_export import ExportFormat, build_document, build_export_file, code_span, json_fence
from exceptions import ApiError, _STATUS_BY_CODE
from pydantic import ValidationError
from schemas.api.assets import AssetItem
from schemas.api.errors import ErrorCode
from schemas.api.evidence_export import EvidenceExportDocument
from schemas.api.incidents import IncidentCategory, IncidentStatus
from schemas.assets import MetricName, MetricSummary
from schemas.evidence import (
    DetectionAssetSnapshot,
    EvidenceItem,
    EvidenceType,
    MetricEvidence,
    RuleEvidence,
    SecOpsEvidenceContext,
    ThreatEvidence,
)
from schemas.events import NormalizedThreatEvent
from schemas.mock_logs import MockLogRecord, MockSshLogEvidence
from tests.test_incident_intake import ACCOUNT, EC2_ARN, REGION, asset_payload, finops_intake

MASK = "[REDACTED]"  # ai.model_client._MASK — ai/tests/test_model_client.py 와 같은 리터럴
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # ai/tests/test_model_client.py 와 같은 예시 키
SOURCE_IP = "203.0.113.10"
OCCURRED_AT = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
SG_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/sg-0123456789abcdef0"
RUN_ID = "1f2e3d4c-5b6a-4978-8899-aabbccddee00"










def _fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _mock_log(message: str = "Failed password for root from 203.0.113.10 port 22 ssh2") -> MockSshLogEvidence:
    """발췌 1행짜리 합성 SSH 로그 — ThreatEvidence 가 요구하는 다섯 필드(대상 · IP · 시각 · 실패 수 · 창)와 맞춘다."""
    window_end = OCCURRED_AT
    window_start = window_end - timedelta(seconds=300)
    return MockSshLogEvidence(
        case_id="C01",
        corpus_fingerprint=_fingerprint("corpus"),
        source_id="aws-directory-service-password-example",
        source_fingerprint=_fingerprint("source"),
        selection_policy="ssh-observed-password-results-v1",
        host="mock-ec2-C01",
        target_arn=EC2_ARN,
        source_ip=SOURCE_IP,
        window_start=window_start,
        window_end=window_end,
        record_count=1,
        failed_attempt_count=1,
        accepted_count=0,
        auxiliary_count=0,
        records=[
            MockLogRecord(
                record_id="r-001", source_time=window_start, received_at=window_start,
                host="mock-ec2-C01", process_id=1234, message=message,
            )
        ],
    )


def _threat_event(**over) -> NormalizedThreatEvent:
    base = {
        "threat_event_id": str(uuid.uuid4()),
        "source_event_id": "evt-mock-001",
        "event_type": "SSH_BRUTE_FORCE",
        "target_arn": EC2_ARN,
        "occurred_at": OCCURRED_AT,
        "payload": {"source_ip": SOURCE_IP, "failed_attempt_count": 1, "window_seconds": 300},
        "deduplication_key": f"SSH_BRUTE_FORCE:i-0abc123456789def0:{SOURCE_IP}",
        "collected_at": OCCURRED_AT + timedelta(seconds=1),
    }
    base.update(over)
    return NormalizedThreatEvent.model_validate(base)




# ===== 1층. 조립 모듈 단위(DB 없음) =====================================================================

def _incident(category: IncidentCategory = IncidentCategory.SECOPS, **over) -> models.Incident:
    """메모리 위 인시던트 행 — 조립이 읽는 칸만 채운다(flush 기본값은 돌지 않으므로 시각도 직접)."""
    base = dict(
        incident_id=str(uuid.uuid4()),
        title="SSH 브루트포스 탐지" if category is IncidentCategory.SECOPS else None,
        subject_arn=EC2_ARN, category=category, status=IncidentStatus.ANALYZING,
        tenant_id=str(uuid.uuid4()), created_at=OCCURRED_AT, updated_at=OCCURRED_AT,
    )
    base.update(over)
    return models.Incident(**base)


def _row(incident: models.Incident, evidence_type: EvidenceType, content, *, source_id: str = "seed") -> models.Evidence:
    """메모리 위 근거 행. dict 를 주면 스키마를 거치지 않고 그대로 둔다(스키마 필터 테스트용)."""
    return models.Evidence(
        evidence_id=str(uuid.uuid4()), incident_id=incident.incident_id, evidence_type=evidence_type,
        source_type=evidence_type.value.lower(), source_id=source_id,
        content=content if isinstance(content, dict) else content.model_dump(mode="json"),
        occurred_at=OCCURRED_AT, collected_at=OCCURRED_AT + timedelta(seconds=1),
    )


def _finops_rows(incident: models.Incident, *, with_metric: bool = True) -> list[models.Evidence]:
    """FinOps 3건(RULE · ASSET · METRIC) — 접수(incident_intake)가 싣는 것과 같은 계약 객체."""
    intake = finops_intake()
    rows = [
        _row(incident, EvidenceType.RULE, RuleEvidence(evaluation=intake.rule_evaluation), source_id=RUN_ID),
        _row(incident, EvidenceType.ASSET, intake.asset_snapshot, source_id=EC2_ARN),
    ]
    if with_metric:
        rows.append(_row(incident, EvidenceType.METRIC, MetricEvidence(
            metric_name=MetricName.CPU_UTILIZATION,
            window_start=OCCURRED_AT - timedelta(days=2), window_end=OCCURRED_AT,
            summary=MetricSummary(cpu_datapoints=336, cpu_avg=4.9, cpu_max=7.2, net_in_avg=1024.0, net_out_avg=512.0),
        ), source_id=RUN_ID))
    return rows


def _available_context(*, tags: dict | None = None, sg_description: str | None = None,
                       log_message: str | None = None) -> SecOpsEvidenceContext:
    """target_status=available — 대상 EC2(태그) + 직접 SG(description) 사본, 선택적 로그 발췌."""
    ec2 = AssetItem.model_validate(asset_payload(
        spec={"instance_type": "t3.xlarge", "private_ip": "10.0.1.23", "tags": tags or {}},
        relationships=[{"relation_type": "SECURED_BY", "target_arn": SG_ARN}],
    ))
    sg = AssetItem.model_validate({
        "arn": SG_ARN, "resource_id": "sg-0123456789abcdef0", "asset_type": "SG", "resource_role": "PRIMARY",
        "name": "web-sg", "account_id": ACCOUNT, "region": REGION, "state": None,
        "spec": {"description": sg_description, "attached": True},
        "relationships": [], "evaluation_status": "PENDING", "collected_at": OCCURRED_AT,
    })
    return SecOpsEvidenceContext(
        captured_at=OCCURRED_AT, target_status="available",
        target=DetectionAssetSnapshot(collection_run_id=RUN_ID, asset=ec2),
        related_assets=[DetectionAssetSnapshot(collection_run_id=RUN_ID, asset=sg)],
        log_evidence=_mock_log(log_message) if log_message is not None else None,
    )


def _items_of(document: dict) -> list[EvidenceItem]:
    """JSON 문서의 근거를 내부 계약으로 되살린다 — flags 만 떼면 EvidenceItem 과 같은 모양이어야 한다."""
    return [
        EvidenceItem.model_validate({k: v for k, v in item.items() if k != "flags"})
        for item in document["evidence"]
    ]


def _fenced_blocks(markdown: str) -> list[list[str]]:
    """```json 펜스 안 줄들. 펜스는 조립이 고른 길이라 여는 줄의 백틱 수로 닫는 줄을 찾는다."""
    blocks, current, fence = [], None, None
    for line in markdown.splitlines():
        if current is None:
            if line.startswith("```") and line.endswith("json"):
                fence, current = line[: len(line) - len("json")], []
            continue
        if line == fence:
            blocks.append(current)
            current = None
        else:
            current.append(line)
    assert current is None, "닫히지 않은 펜스"
    return blocks


def _outside_fences(markdown: str) -> list[str]:
    outside, inside, fence = [], False, None
    for line in markdown.splitlines():
        if not inside and line.startswith("```") and line.endswith("json"):
            inside, fence = True, line[: len(line) - len("json")]
            continue
        if inside and line == fence:
            inside = False
            continue
        if not inside:
            outside.append(line)
    return outside


# ----- 1. 정상 — 두 형식 · 되살리기 · 헤더 값 -------------------------------------------------

@pytest.mark.parametrize("fmt", [ExportFormat.MD, ExportFormat.JSON])
def test_unit_bundle_builds_for_finops_and_secops(fmt):
    finops_incident = _incident(IncidentCategory.FINOPS)
    secops_incident = _incident()
    secops_rows = [_row(secops_incident, EvidenceType.THREAT, ThreatEvidence(
        event=_threat_event(), context=_available_context(log_message="Failed password for root from 203.0.113.10"),
    ))]

    for incident, rows, expected in ((finops_incident, _finops_rows(finops_incident), 3), (secops_incident, secops_rows, 1)):
        file = build_export_file(incident, rows, fmt, generated_at=OCCURRED_AT)

        assert file.media_type == evidence_export.MEDIA_TYPES[fmt]
        assert file.filename == f"incident-{incident.incident_id}-evidence.{fmt.value}"
        assert file.filename.isascii()
        document = build_document(incident, rows, fmt, generated_at=OCCURRED_AT)
        assert document["export"]["evidence_count"] == expected == len(document["evidence"])
        assert document["export"]["notice"] == evidence_export.EXPORT_NOTICE
        if fmt is ExportFormat.JSON:
            parsed = json.loads(file.body)
            assert parsed == document
            EvidenceExportDocument.model_validate(parsed)
            restored = _items_of(parsed)
            assert [item.evidence_type for item in restored] == [row.evidence_type for row in rows]
        else:
            text = file.body.decode("utf-8")
            assert text.startswith("# 인시던트 근거 내보내기\n")
            assert evidence_export.EXPORT_NOTICE in text
            assert len(_fenced_blocks(text)) == expected


def test_unit_processed_flag_separates_summaries_from_source_copies():
    incident = _incident(IncidentCategory.FINOPS)
    document = build_document(incident, _finops_rows(incident), ExportFormat.JSON)
    processed = {item["evidence_type"]: item["flags"]["processed"] for item in document["evidence"]}
    assert processed == {"RULE": True, "ASSET": False, "METRIC": True}


# ----- 6. 비밀값 — 태그 · SG description · 로그 message 세 자리, 두 형식 ----------------------------

@pytest.mark.parametrize("fmt", [ExportFormat.MD, ExportFormat.JSON])
def test_unit_secrets_are_masked_in_both_formats(fmt):
    incident = _incident(title="token: hunter2 를 쓰는 인시던트")
    context = _available_context(
        tags={"db_password": "hunter2", "Name": "web-01", AWS_KEY: "credential-shaped-key"},
        sg_description="token: abc12345secret for ops",
        log_message=f"Accepted publickey for ops key {AWS_KEY} from 203.0.113.10",
    )
    rows = [_row(incident, EvidenceType.THREAT, ThreatEvidence(event=_threat_event(), context=context))]

    text = build_export_file(incident, rows, fmt).body.decode("utf-8")

    for secret in ("hunter2", "abc12345secret", AWS_KEY):
        assert secret not in text
    assert text.count(MASK) >= 4  # 태그 값 · description 값 · 로그 키 · 제목 값
    assert "web-01" in text and SOURCE_IP in text  # 개인정보 · 식별자는 1차에서 가리지 않는다(결정 3)
    document = build_document(incident, rows, fmt)
    asset = document["evidence"][0]["content"]["context"]["target"]["asset"]
    assert asset["spec"]["tags"] == {
        "db_password": MASK, "Name": "web-01", MASK: "credential-shaped-key",
    }
    assert document["incident"]["title"] == f"token: {MASK} 를 쓰는 인시던트"


# ----- 7. 스키마 필터 — 계약 밖 키는 파일이 아니라 결함이다 -------------------------------------------

def test_unit_key_outside_the_schema_fails_loudly_without_logging_the_value(caplog):
    """500 으로 멈추되, pydantic 오류문(입력값 포함)은 올리지도 로그에 남기지도 않는다 — 리뷰 지적."""
    incident = _incident()
    content = ThreatEvidence(event=_threat_event()).model_dump(mode="json")
    content["leaked_blob"] = {"aws_secret_access_key": "LEAKED-VALUE-9f3a"}
    row = _row(incident, EvidenceType.THREAT, content)

    with caplog.at_level("ERROR", logger="vigilantis.evidence_export"), pytest.raises(ApiError) as caught:
        build_document(incident, [row], ExportFormat.JSON)

    assert caught.value.code is ErrorCode.INTERNAL_ERROR
    assert caught.value.__suppress_context__ and not isinstance(caught.value.__cause__, ValidationError)
    [record] = caplog.records
    assert record.getMessage() == "evidence_export_stored_content_invalid"
    assert (record.evidence_id, record.evidence_type, record.error_count) == (row.evidence_id, "THREAT", 1)
    assert "LEAKED-VALUE-9f3a" not in caplog.text and "leaked_blob" not in caplog.text


def test_redacted_tag_key_collision_does_not_silently_drop_evidence():
    incident = _incident()
    context = _available_context(tags={AWS_KEY: "first", MASK: "second"})
    rows = [_row(incident, EvidenceType.THREAT, ThreatEvidence(
        event=_threat_event(), context=context,
    ))]
    with pytest.raises(ApiError) as caught:
        build_export_file(incident, rows, ExportFormat.JSON)
    assert caught.value.code is ErrorCode.INTERNAL_ERROR


# ----- 8. Markdown 주입 — 펜스 · 코드 스팬 밖으로 나가지 않는다 -----------------------------------------

def test_unit_markdown_injection_stays_inside_fences_and_spans():
    hostile_line = "``` # heading [x](http://evil.example) | cell <img src=x>"
    # 리뷰 지적 사례: 백슬래시 + | 는 표 셀에서 파서마다 다르게 쪼개져 스팬이 깨진다 — 표를 쓰지 않아 그대로 남는다
    incident = _incident(title="# 제목 주입 ``` <img src=x> a\\|tail [x](http://evil.example) `tick`")
    rows = [
        _row(incident, EvidenceType.THREAT, ThreatEvidence(
            event=_threat_event(), context=_available_context(log_message=hostile_line),
        )),
        _row(incident, EvidenceType.THREAT, ThreatEvidence(event=_threat_event())),
    ]

    text = build_export_file(incident, rows, ExportFormat.MD).body.decode("utf-8")

    outside = _outside_fences(text)
    headings = [line for line in outside if line.startswith("#")]
    assert headings == ["# 인시던트 근거 내보내기", "## 근거 1/2 — `THREAT`", "### 내용", "## 근거 2/2 — `THREAT`", "### 내용"]
    assert all(not line.startswith(("[", "<", "|")) for line in outside)
    assert not any(line.startswith("|") for line in outside)  # 표 없음 — 셀 분할로 스팬이 깨질 자리가 없다
    assert any(hostile_line in line for block in _fenced_blocks(text) for line in block)
    assert hostile_line not in "\n".join(outside)
    # 제목은 목록 줄의 한 줄 코드 스팬 — 내용의 백틱(3연속)보다 긴 구분자, 끝이 백틱이라 CommonMark 규칙대로 양쪽 공백.
    # 줄머리 `- **제목**: ` 뒤 첫 글자부터 끝까지 스팬 하나다 — 그 안의 <img> · [x](…) · \| 는 글자일 뿐이다
    title_row = next(line for line in outside if line.startswith("- **제목**: "))
    assert title_row == "- **제목**: ```` # 제목 주입 ``` <img src=x> a\\|tail [x](http://evil.example) `tick` ````"
    # 그 근거의 펜스는 내용(3연속)보다 길다
    assert any(line.startswith("````json") for line in text.splitlines())


@pytest.mark.parametrize("value,expected", [
    ("plain", "`plain`"),
    ("a`b", "``a`b``"),
    ("`edge`", "`` `edge` ``"),
    ("line1\r\nline2", "`line1  line2`"),
    ("a|b", "`a|b`"),  # 표를 쓰지 않으므로 | 는 그대로다
    ("a\\|b", "`a\\|b`"),
    ("", "`  `"),  # 빈 값도 스팬으로 남는다(`` `` 는 스팬이 아니다)
])
def test_unit_code_span_rules(value, expected):
    assert code_span(value) == expected


def test_unit_json_fence_outgrows_backticks_in_content():
    fenced = json_fence({"message": "x ```` y"})
    assert fenced.startswith("`````json\n") and fenced.endswith("\n`````")


# ----- 9. 플래그 — 저장 필드에서만 -------------------------------------------------------------------

def test_unit_missing_flags_follow_stored_fields():
    incident = _incident()
    not_collected = SecOpsEvidenceContext(captured_at=OCCURRED_AT, target_status="not_collected", log_evidence=_mock_log())
    rows = [
        _row(incident, EvidenceType.THREAT, ThreatEvidence(event=_threat_event(), context=not_collected)),
        _row(incident, EvidenceType.THREAT, ThreatEvidence(event=_threat_event())),
        _row(incident, EvidenceType.THREAT, ThreatEvidence(event=_threat_event(), context=_available_context())),
    ]

    flags = [item["flags"] for item in build_document(incident, rows, ExportFormat.JSON)["evidence"]]

    assert flags[0] == {"processed": False, "synthetic": True, "excerpt": True, "missing": ["target_not_collected"]}
    assert flags[1] == {"processed": False, "synthetic": False, "excerpt": False, "missing": ["context_not_captured"]}
    assert flags[2] == {"processed": False, "synthetic": False, "excerpt": False, "missing": ["log_not_attached"]}


def test_unit_bundle_missing_marks_absent_metric_and_empty_bundle():
    finops = _incident(IncidentCategory.FINOPS)
    assert build_document(finops, _finops_rows(finops, with_metric=False), ExportFormat.JSON)["export"]["missing"] == ["metric_evidence_absent"]
    assert build_document(finops, _finops_rows(finops), ExportFormat.JSON)["export"]["missing"] == []
    assert build_document(_incident(), [], ExportFormat.JSON)["export"]["missing"] == ["no_evidence"]


# ----- 10. 크기 ------------------------------------------------------------------------------------

def test_unit_oversized_bundle_is_413_and_no_file(monkeypatch):
    incident = _incident()
    rows = [_row(incident, EvidenceType.THREAT, ThreatEvidence(event=_threat_event()))]
    monkeypatch.setattr(evidence_export, "EVIDENCE_EXPORT_MAX_BYTES", 64)

    with pytest.raises(ApiError) as caught:
        build_export_file(incident, rows, ExportFormat.JSON)
    assert caught.value.code is ErrorCode.EVIDENCE_EXPORT_TOO_LARGE
    assert _STATUS_BY_CODE[ErrorCode.EVIDENCE_EXPORT_TOO_LARGE] == 413


# ----- 13. 파일명 — 사용자 문자열 없음 ---------------------------------------------------------------

def test_unit_filename_never_carries_the_title():
    incident = _incident(title='evil"; filename="x.html\r\nX-Injected: 1')
    file = build_export_file(incident, [], ExportFormat.MD)
    assert file.filename == f"incident-{incident.incident_id}-evidence.md"
    assert file.filename.isascii() and "evil" not in file.filename

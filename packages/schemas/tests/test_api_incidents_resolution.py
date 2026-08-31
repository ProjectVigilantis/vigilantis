# ==============================================================================
# [파일 설명]
# IncidentResponse의 종료 판단 불변식 — resolution·resolved_at 짝, RESOLVED 한정.
# (Issue #199)
#
# 라우터로는 만들 수 없는 조합(종료되지 않았는데 판단만 있는 응답)을 계약 쪽에서
# 직접 찌른다. 같은 불변식을 DB CheckConstraint도 강제한다
# (db/models.py resolution_with_resolved_status).
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from schemas.api.incidents import (
    IncidentCategory,
    IncidentResponse,
    IncidentStatus,
    ResolutionJudgement,
    ResolveIncidentRequest,
    RiskLevel,
)

T0 = datetime(2026, 8, 28, 2, 0, 0, tzinfo=timezone.utc)


def _detail(**overrides) -> dict:
    payload = {
        "incident_id": "inc-1",
        "title": "SSH 브루트포스 탐지",
        "subject_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa",
        "category": IncidentCategory.SECOPS,
        "status": IncidentStatus.RESOLVED,
        "initial_risk_level": RiskLevel.MEDIUM,
        "summary_lines": ["요약 1", "요약 2", "요약 3"],
        "created_at": T0,
        "updated_at": T0,
    }
    payload.update(overrides)
    return payload


def test_resolution_and_resolved_at_are_accepted_together():
    detail = IncidentResponse.model_validate(
        _detail(resolution=ResolutionJudgement.JUSTIFIED, resolved_at=T0)
    )
    assert detail.resolution is ResolutionJudgement.JUSTIFIED
    assert detail.resolved_at == T0


def test_resolved_without_judgement_is_allowed():
    """관제자 판단 없이 종료되는 경로가 뒤에 생길 수 있어 강제하지 않는다."""
    detail = IncidentResponse.model_validate(_detail())
    assert detail.resolution is None
    assert detail.resolved_at is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"resolution": ResolutionJudgement.JUSTIFIED},
        {"resolved_at": T0},
    ],
)
def test_half_filled_judgement_is_rejected(overrides):
    with pytest.raises(ValidationError, match="함께 채워지거나"):
        IncidentResponse.model_validate(_detail(**overrides))


def test_judgement_outside_resolved_status_is_rejected():
    with pytest.raises(ValidationError, match="RESOLVED가 아니면"):
        IncidentResponse.model_validate(
            _detail(
                status=IncidentStatus.FAILED,
                resolution=ResolutionJudgement.EXCESSIVE,
                resolved_at=T0,
            )
        )


def test_request_rejects_unknown_field_and_value():
    assert ResolveIncidentRequest(resolution="JUSTIFIED").resolution is (
        ResolutionJudgement.JUSTIFIED
    )
    with pytest.raises(ValidationError):
        ResolveIncidentRequest(resolution="MAYBE")
    with pytest.raises(ValidationError):
        ResolveIncidentRequest(resolution="JUSTIFIED", note="x")

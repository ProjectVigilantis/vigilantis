"""Detection → Incident 생성 입력 계약 테스트 (Issue #254).

계약이 지키는 것은 넷이다.
  - Incident가 되는 자산 판정은 COST_CANDIDATE·UNUSED 2종뿐이다(THREAT·SKIP 거절).
  - FINOPS Intake의 자산 스냅샷과 판정은 같은 자산·같은 수집 회차에서 나온 값이다.
  - SECOPS Intake의 위험 판정과 위협 이벤트는 같은 이벤트를 가리킨다.
  - category가 두 형태를 가른다(discriminated union).
"""

import pytest
from pydantic import ValidationError

from schemas.api.assets import Verdict
from schemas.api.incidents import IncidentCategory
from schemas.intake import (
    INCIDENT_INTAKE_ADAPTER,
    INCIDENT_TRIGGERING_VERDICTS,
    FinOpsIncidentIntake,
    SecOpsIncidentIntake,
)

EC2_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f00001"
SG_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-0abc1234"
EBS_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-0abc1234"
RUN_ID = "run-20260902-001"
COLLECTED_AT = "2026-09-02T09:00:00Z"
EVALUATED_AT = "2026-09-02T09:00:05Z"


def make_ec2_asset(**over):
    base = {
        "arn": EC2_ARN,
        "resource_id": "i-0a1b2c3d4e5f00001",
        "asset_type": "EC2",
        "resource_role": "PRIMARY",
        "name": "batch-dev",
        "account_id": "123456789012",
        "region": "ap-northeast-2",
        "state": "running",
        "spec": {
            "instance_type": "t3.xlarge",
            "availability_zone": "ap-northeast-2a",
            "vpc_id": "vpc-0123",
            "subnet_id": "subnet-0123",
            "private_ip": "10.0.1.10",
        },
        "relationships": [],
        "evaluation_status": "COMPLETED",
        "health_score": 4,
        "verdict": "COST_CANDIDATE",
        "skip_reason_code": None,
        "collected_at": COLLECTED_AT,
    }
    base.update(over)
    return base


def make_sg_asset(**over):
    base = make_ec2_asset(
        arn=SG_ARN, resource_id="sg-0abc1234", asset_type="SG", name="legacy-web",
        state=None,
        spec={"description": "구 웹 계층", "vpc_id": "vpc-0123", "attached": False,
              "open_to_world": []},
        health_score=None, verdict="UNUSED",
    )
    base.update(over)
    return base


def make_ebs_asset(**over):
    base = make_ec2_asset(
        arn=EBS_ARN, resource_id="vol-0abc1234", asset_type="EBS",
        resource_role="RUNBOOK_SUPPORT", name=None, state="available",
        spec={"volume_type": "gp3", "size_gib": 100,
              "availability_zone": "ap-northeast-2a", "encrypted": True,
              "attached_instance_ids": []},
        health_score=None, verdict="UNUSED",
    )
    base.update(over)
    return base


def make_rule_evaluation(**over):
    base = {
        "asset_arn": EC2_ARN,
        "collection_run_id": RUN_ID,
        "evaluation_status": "COMPLETED",
        "verdict": "COST_CANDIDATE",
        "health_score": 4,
        "skip_reason_code": None,
        "reason": "2일 평균 CPU 4.9% — 다운사이징 후보",
        "evaluated_at": EVALUATED_AT,
    }
    base.update(over)
    return base


def make_finops(asset=None, run_id=RUN_ID, **over):
    base = {
        "asset_snapshot": {
            "collection_run_id": run_id,
            "asset": asset if asset is not None else make_ec2_asset(),
        },
        "rule_evaluation": make_rule_evaluation(),
    }
    base.update(over)
    return base


def make_threat_event(**over):
    base = {
        "threat_event_id": "thr-20260902-001",
        "source_event_id": "evt-mock-001",
        "event_type": "SSH_BRUTE_FORCE",
        "target_arn": EC2_ARN,
        "occurred_at": "2026-09-02T09:00:00Z",
        "payload": {
            "source_ip": "203.0.113.10",
            "failed_attempt_count": 120,
            "window_seconds": 300,
        },
        "deduplication_key": "SSH_BRUTE_FORCE:i-0a1b2c3d4e5f00001:203.0.113.10",
        "collected_at": "2026-09-02T09:00:01Z",
    }
    base.update(over)
    return base


def make_initial_risk(**over):
    base = {
        "threat_event_id": "thr-20260902-001",
        "initial_risk_level": "HIGH",
        "response_mode": "PRE_MITIGATION_0_5S",
        "reason_codes": ["RISK_SSH_BRUTEFORCE"],
    }
    base.update(over)
    return base


def make_secops(**over):
    base = {
        "title": "SSH 브루트포스 시도",
        "threat_event": make_threat_event(),
        "initial_risk": make_initial_risk(),
    }
    base.update(over)
    return base


# --- FinOps: Incident이 되는 판정 ----------------------------------------------


def test_finops_cost_candidate_valid():
    intake = FinOpsIncidentIntake.model_validate(make_finops())
    assert intake.category is IncidentCategory.FINOPS
    assert intake.subject_arn == EC2_ARN
    assert intake.asset_snapshot.asset.spec.instance_type == "t3.xlarge"


@pytest.mark.parametrize("asset_factory,arn", [
    (make_sg_asset, SG_ARN),    # 미부착 SG
    (make_ebs_asset, EBS_ARN),  # 미부착 EBS 볼륨(state=available)
])
def test_finops_unused_valid_for_sg_and_ebs(asset_factory, arn):
    """UNUSED 대상은 둘이다 — 어느 쪽도 위협 이벤트가 없어 FINOPS로만 만들 수 있다."""
    intake = FinOpsIncidentIntake.model_validate(
        make_finops(
            asset=asset_factory(),
            rule_evaluation=make_rule_evaluation(
                asset_arn=arn, verdict="UNUSED", health_score=None,
                reason="부착된 자원이 없는 유휴 자산",
            ),
        )
    )
    assert intake.subject_arn == arn


def test_finops_rejects_threat_verdict():
    """전체개방 SG(THREAT)는 이 경로로 Incident가 되지 않는다 — 초기 위험도·사유
    코드를 채울 값이 없어 SECOPS 형태(DB CHECK category_risk_shape)를 못 만든다."""
    with pytest.raises(ValidationError):
        FinOpsIncidentIntake.model_validate(
            make_finops(
                asset=make_sg_asset(verdict="THREAT",
                                    spec={"description": "구 웹 계층", "vpc_id": "vpc-0123",
                                          "attached": True,
                                          "open_to_world": [{"protocol": "tcp",
                                                             "from_port": 22, "to_port": 22}]}),
                rule_evaluation=make_rule_evaluation(
                    asset_arn=SG_ARN, verdict="THREAT", health_score=None,
                ),
            )
        )


@pytest.mark.parametrize("over", [
    {"verdict": "SKIP", "skip_reason_code": "SKIP_PROD_PROTECTED"},  # 판정 단계가 이미 거른 자산
    {"evaluation_status": "PENDING", "verdict": None, "health_score": None,
     "skip_reason_code": None, "reason": None},                     # 판정 미완
    {"evaluation_status": "FAILED", "verdict": None, "health_score": None,
     "skip_reason_code": None, "reason": None},                     # 판정 실패
])
def test_finops_rejects_non_incident_evaluations(over):
    with pytest.raises(ValidationError):
        FinOpsIncidentIntake.model_validate(
            make_finops(rule_evaluation=make_rule_evaluation(**over))
        )


# --- FinOps: 스냅샷과 판정이 같은 시점인가 --------------------------------------


def test_finops_rejects_arn_mismatch():
    with pytest.raises(ValidationError):
        FinOpsIncidentIntake.model_validate(make_finops(asset=make_ec2_asset(
            arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0other",
            resource_id="i-0other",
        )))


def test_finops_rejects_run_id_mismatch():
    """예전 판정에 최신 회차 자산을 붙이는 조합 — 자산 행은 회차마다 덮어써진다."""
    with pytest.raises(ValidationError):
        FinOpsIncidentIntake.model_validate(make_finops(run_id="run-20260902-002"))


def test_finops_rejects_rendered_verdict_mismatch():
    """같은 회차라 적어 놓고 자산에 실린 판정 표기가 다른 경우."""
    with pytest.raises(ValidationError):
        FinOpsIncidentIntake.model_validate(
            make_finops(asset=make_ec2_asset(verdict="SKIP",
                                             skip_reason_code="SKIP_LOW_UTIL"))
        )


def test_finops_rejects_evaluation_before_collection():
    """판정 시각이 관측 시각보다 앞서면 관측한 적 없는 상태를 판정한 것이 된다."""
    with pytest.raises(ValidationError):
        FinOpsIncidentIntake.model_validate(make_finops(
            asset=make_ec2_asset(collected_at="2026-09-02T09:00:10Z"),
        ))


def test_finops_accepts_same_instant():
    intake = FinOpsIncidentIntake.model_validate(make_finops(
        asset=make_ec2_asset(collected_at=EVALUATED_AT),
    ))
    assert intake.subject_arn == EC2_ARN


def test_finops_rejects_wrong_category():
    with pytest.raises(ValidationError):
        FinOpsIncidentIntake.model_validate(make_finops(category="SECOPS"))


def test_finops_rejects_extra_field():
    # title은 FINOPS Intake에 없다 — 진단명이라 분석 전에는 null이다
    with pytest.raises(ValidationError):
        FinOpsIncidentIntake.model_validate(make_finops(title="저활성 EC2"))


def test_finops_roundtrip():
    intake = FinOpsIncidentIntake.model_validate(make_finops())
    assert FinOpsIncidentIntake.model_validate_json(intake.model_dump_json()) == intake


# --- SecOps ------------------------------------------------------------------


def test_secops_valid():
    intake = SecOpsIncidentIntake.model_validate(make_secops())
    assert intake.category is IncidentCategory.SECOPS
    assert intake.subject_arn == EC2_ARN


def test_secops_rejects_mismatched_threat_event_id():
    """다른 이벤트의 위험도로 이 이벤트를 대응하게 되는 조합."""
    with pytest.raises(ValidationError):
        SecOpsIncidentIntake.model_validate(
            make_secops(initial_risk=make_initial_risk(threat_event_id="thr-20260902-999"))
        )


def test_secops_rejects_empty_title():
    # 비면 카드 제목이 자원 ID가 된다 (Issue #200)
    with pytest.raises(ValidationError):
        SecOpsIncidentIntake.model_validate(make_secops(title=""))


def test_secops_rejects_missing_title():
    payload = make_secops()
    del payload["title"]
    with pytest.raises(ValidationError):
        SecOpsIncidentIntake.model_validate(payload)


def test_secops_roundtrip():
    intake = SecOpsIncidentIntake.model_validate(make_secops())
    assert SecOpsIncidentIntake.model_validate_json(intake.model_dump_json()) == intake


# --- union -------------------------------------------------------------------


def test_adapter_discriminates_by_category():
    finops = INCIDENT_INTAKE_ADAPTER.validate_python(
        dict(make_finops(), category="FINOPS")
    )
    secops = INCIDENT_INTAKE_ADAPTER.validate_python(
        dict(make_secops(), category="SECOPS")
    )
    assert isinstance(finops, FinOpsIncidentIntake)
    assert isinstance(secops, SecOpsIncidentIntake)


@pytest.mark.parametrize("category", [None, "ROLLBACK", ""])
def test_adapter_rejects_unknown_category(category):
    with pytest.raises(ValidationError):
        INCIDENT_INTAKE_ADAPTER.validate_python(dict(make_finops(), category=category))


def test_triggering_verdicts_exclude_threat_and_skip():
    """이 집합이 늘면 Incident가 되는 판정이 늘어난다 — 형태를 채울 수 있는지 먼저 본다."""
    assert INCIDENT_TRIGGERING_VERDICTS == {Verdict.COST_CANDIDATE, Verdict.UNUSED}

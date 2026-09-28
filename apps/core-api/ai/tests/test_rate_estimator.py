"""V1 단가 수용·서버 계산 계약. 합성 단가를 쓰며 모델의 가격 정확도는 검증하지 않는다."""

import pytest
from ai.rate_estimator import ProposedHourlyRates, accept_hourly_rates
from schemas.agents import AgentAssetContext
from schemas.savings import SavingsStatus


@pytest.fixture
def asset():
    return AgentAssetContext.model_validate({
        "arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc",
        "resource_id": "i-0abc", "asset_type": "EC2", "resource_role": "PRIMARY",
        "account_id": "123456789012", "region": "ap-northeast-2", "state": "running",
        "spec": {"instance_type": "t3.large"}, "relationships": [],
        "evaluation_status": "COMPLETED", "health_score": 3, "verdict": "COST_CANDIDATE",
        "collected_at": "2026-09-16T00:00:00Z",
    })


@pytest.mark.parametrize("current,target,amount", [
    ("0.104", "0.026", "56.94"),
    ("0.100", "0.0995", "0.37"),  # 0.365 USD를 HALF_UP으로 센트 반올림
    ("0.050", "0.050", "0.00"),  # 정상 0달러는 미산출과 다르다.
])
def test_server_calculates_amount_and_supplies_context(asset, current, target, amount):
    result = accept_hourly_rates(ProposedHourlyRates(
        status="ESTIMATED", current_hourly_rate=current, target_hourly_rate=target,
    ), asset)

    assert result.status is SavingsStatus.ESTIMATED
    assert result.model_dump(mode="json")["amount"] == amount
    assert result.reason is None
    assert result.basis.target_arn == asset.arn
    assert result.basis.region == "ap-northeast-2"
    assert result.basis.current_instance_type == "t3.large"
    assert result.basis.target_instance_type == "t3.small"
    assert result.basis.assumptions.pricing_source == "MODEL_KNOWLEDGE"
    assert result.basis.explanation_source.value == "SERVER_TEMPLATE"
    assert "t3.large에서 t3.small" in result.basis.explanation
    assert "실제 요금 조회 결과가 아닙니다" in result.basis.explanation


@pytest.mark.parametrize("status,current,target,expected", [
    ("UNAVAILABLE", None, None, "UNAVAILABLE"),
    ("UNAVAILABLE", "0.208", None, "INVALID"),
    ("ESTIMATED", None, "0.052", "INVALID"),
    ("ESTIMATED", "0.052", "0.208", "INVALID"),
    ("ESTIMATED", "0.1040001", "0.052", "INVALID"),
])
def test_invalid_or_unavailable_rates_do_not_become_zero(asset, status, current, target, expected):
    proposal = ProposedHourlyRates.model_construct(
        status=status, current_hourly_rate=current, target_hourly_rate=target,
    )
    result = accept_hourly_rates(proposal, asset)
    assert result.status.value == expected
    assert result.amount is None and result.basis is None


def test_unstorable_server_context_is_rejected_before_persistence(asset):
    asset = asset.model_copy(update={"region": "nul\x00region"})
    diagnostics = []
    result = accept_hourly_rates(ProposedHourlyRates(
        status="ESTIMATED", current_hourly_rate="0.208", target_hourly_rate="0.052",
    ), asset, diagnostics=diagnostics)
    assert result.status is SavingsStatus.INVALID and result.basis is None
    assert diagnostics and "nul" not in str(diagnostics)

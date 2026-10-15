"""MVP 서비스 전환 계약 — 입력 거절·계획 불변·관측 판정이 사실에서만 나오는가.

굳히는 것: 운영 리스너·검증 포트·ALB 결속의 형식 거절, 복제본과 원래 경로 기대 본문의 분리,
PASSED가 실패한 확인·HTTP 증거와 함께 저장되지 않음, 비용 관문이 빠진 운영자 값을 추정하지 않음.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from schemas.service_transition import (
    CheckResult,
    CleanupItem,
    CleanupManifest,
    CleanupOptions,
    HttpProbeEvidence,
    RuntimeFacts,
    ServiceObservation,
    ServiceTransitionPlan,
    ServiceTransitionPlanOptions,
    compute_verdict,
    simultaneous_cost,
    transition_target_group_name,
)

ACCOUNT = "123456789012"
LB = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCOUNT}:loadbalancer/app/smoke-alb/0123456789abcdef"
LISTENER = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCOUNT}:listener/app/smoke-alb/0123456789abcdef/1111111111111111"
TG = f"arn:aws:elasticloadbalancing:ap-northeast-2:{ACCOUNT}:targetgroup/smoke-tg/2222222222222222"
NOW = datetime(2026, 10, 13, tzinfo=UTC)

OPTIONS = {
    "account_id": ACCOUNT,
    "region": "ap-northeast-2",
    "vpc_id": "vpc-0aaaaaaaaaaaaaaa1",
    "load_balancer_arn": LB,
    "listener_arn": LISTENER,
    "validation_listener_port": 8080,
    "probe_instance_id": "i-0000000000000000d",
    "probe_security_group_id": "sg-0000000000000000c",
    "clone_expected_body": "vigilantis-smoke-idle-dev\n",
    "original_target_expected_bodies": {
        "i-0000000000000000b": "vigilantis-smoke-web-1\n",
        "i-0000000000000000c": "vigilantis-smoke-web-2\n",
    },
}


def _plan(**overrides):
    transition_id = str(uuid.uuid4())
    data = {
        "transition_id": transition_id,
        "execution_id": str(uuid.uuid4()),
        "planned_at": NOW.isoformat(),
        "account_id": ACCOUNT,
        "region": "ap-northeast-2",
        "vpc_id": "vpc-0aaaaaaaaaaaaaaa1",
        "service_unit": "smoke-http.service",
        "service_port": 80,
        "service_path": "/",
        "expected_status": 200,
        "clone_expected_body": "vigilantis-smoke-idle-dev\n",
        "load_balancer_arn": LB,
        "original_route": {
            "listener_arn": LISTENER,
            "listener_port": 80,
            "listener_protocol": "HTTP",
            "target_group_arn": TG,
            "captured_at": NOW.isoformat(),
            "members": [
                {
                    "instance_id": "i-0000000000000000b",
                    "port": 80,
                    "health_state": "healthy",
                    "quarantined": False,
                    "expected_body": "vigilantis-smoke-web-1\n",
                }
            ],
        },
        "validation_listener_port": 8080,
        "target_group_name": transition_target_group_name(transition_id),
        "probe": {
            "instance_id": "i-0000000000000000d",
            "security_group_id": "sg-0000000000000000c",
            "private_ip": "10.42.1.9",
        },
        "runtime_facts": {},
    }
    data.update(overrides)
    return data


def test_plan_options_accept_the_documented_example():
    options = ServiceTransitionPlanOptions.model_validate(OPTIONS)
    assert options.runtime_facts == RuntimeFacts()


@pytest.mark.parametrize(
    "change",
    [
        {"validation_listener_port": 80},
        {
            "listener_arn": LISTENER.replace(
                "smoke-alb/0123456789abcdef", "other-alb/0123456789abcdee"
            )
        },
        {"region": "us-east-1"},
        {"original_target_expected_bodies": {}},
        {
            "expected_body": "x"
        },  # 옛 단일 기대값 키는 받지 않는다(복제본·원래 경로 분리)
        {"apply": True},  # --apply는 CLI 몫이다 — 옵션으로 흘러들지 않는다
    ],
)
def test_plan_options_reject_unbound_or_unknown_inputs(change):
    with pytest.raises(ValidationError):
        ServiceTransitionPlanOptions.model_validate({**OPTIONS, **change})


def test_plan_json_roundtrip_and_derived_target_group_name():
    plan = ServiceTransitionPlan.model_validate(_plan())
    assert ServiceTransitionPlan.model_validate(plan.model_dump(mode="json")) == plan
    with pytest.raises(ValidationError):
        ServiceTransitionPlan.model_validate(_plan(target_group_name="vgl-svc-other"))
    with pytest.raises(ValidationError):
        ServiceTransitionPlan.model_validate(_plan(validation_listener_port=80))
    with pytest.raises(ValidationError):
        plan.model_copy(update={"clone_expected_body": "changed"}).__setattr__(
            "clone_expected_body", "x"
        )


def _evidence(**overrides):
    data = {
        "path_kind": "member_direct",
        "instance_id": "i-0000000000000000e",
        "host": "10.42.1.5",
        "port": 80,
        "path": "/",
        "request_id": "r",
        "status": 200,
        "body_bytes": 3,
        "body_sha256": "0" * 64,
        "expected_sha256": ("0" * 64,),
        "body_matches": True,
        "location": None,
        "peer_ip": "10.42.1.5",
        "peer_allowed": ("10.42.1.5",),
        "error": None,
        "observed_at": NOW,
    }
    data.update(overrides)
    return HttpProbeEvidence(**data)


@pytest.mark.parametrize(
    "change",
    [
        {"status": 302, "location": "http://elsewhere/"},
        {"location": "http://elsewhere/"},
        {"peer_ip": "10.0.0.99"},
        {"peer_ip": None},
        {"body_matches": False},
        {"error": "timeout"},
    ],
)
def test_http_evidence_passes_only_on_observed_matching_response(change):
    assert _evidence().passed
    assert not _evidence(**change).passed


def test_observation_cannot_claim_passed_against_its_evidence():
    ok = CheckResult(name="a", passed=True, detail="a")
    bad = CheckResult(name="b", passed=False, detail="b")
    assert compute_verdict((), ()) == "UNKNOWN"
    assert compute_verdict((ok,), (_evidence(),)) == "PASSED"
    assert compute_verdict((ok,), (_evidence(body_matches=False),)) == "FAILED"
    with pytest.raises(ValidationError):
        ServiceObservation(
            kind="VERIFY",
            verdict="PASSED",
            observed_at=NOW,
            summary="s",
            checks=(ok, bad),
        )
    with pytest.raises(ValidationError):
        ServiceObservation(
            kind="VERIFY",
            verdict="PASSED",
            observed_at=NOW,
            summary="s",
            checks=(ok,),
            http=(_evidence(peer_ip="10.9.9.9"),),
        )


RUNNING_SOURCE = {
    "instance_id": "i-0000000000000000a",
    "state": "running",
    "instance_type": "t3.medium",
}
PRICED = RuntimeFacts(
    captured_instance_type_hourly_usd=Decimal("0.05"),
    simultaneous_cost_cap_usd_per_hour=Decimal("0.10"),
    vcpu_quota=8,
    rto_minutes=30,
)


def _cost(facts=PRICED, *, source=RUNNING_SOURCE, members=1, max_size=4):
    return simultaneous_cost(
        facts,
        captured_instance_type="t3.medium",
        source=source,
        asg_members=members,
        asg_max_size=max_size,
    )


def test_cost_gate_counts_the_clone_source_once_with_current_members():
    # 복제 원본(idle-dev)은 원래 TG 대상이 아니다 — 원래 TG 대상 수는 입력에도 합계에도 없다
    result = _cost()
    assert result["blockers"] == []
    assert (
        result["source_compute_usd_per_hour"],
        result["asg_compute_usd_per_hour"],
    ) == ("0.05", "0.05")
    assert (
        result["simultaneous_compute_usd_per_hour"] == "0.10"
        and result["cap_usd_per_hour"] == "0.10"
    )
    assert (
        result["asg_at_max_size_usd_per_hour"] == "0.20"
        and "정보용" in result["asg_at_max_size_note"]
    )
    assert (
        "web-1" in result["excluded"]
        and "ALB" in result["excluded"]
        and "operator_supplied" in result["label"]
    )
    over = _cost(members=2)
    assert (
        over["simultaneous_compute_usd_per_hour"] == "0.15"
        and "상한" in over["blockers"][0]
    )


def test_cost_gate_never_guesses_missing_values_as_zero():
    missing = _cost(RuntimeFacts())
    assert (
        missing["simultaneous_compute_usd_per_hour"] is None
        and len(missing["blockers"]) == 4
    )
    no_quota_rto = _cost(
        RuntimeFacts(
            captured_instance_type_hourly_usd=Decimal("0.05"),
            simultaneous_cost_cap_usd_per_hour=Decimal("0.10"),
        )
    )
    assert no_quota_rto["simultaneous_compute_usd_per_hour"] == "0.10"
    assert no_quota_rto["blockers"] == [
        "운영자 공급값 없음: runtime_facts.vcpu_quota",
        "운영자 공급값 없음: runtime_facts.rto_minutes",
    ]
    only_rto = _cost(PRICED.model_copy(update={"vcpu_quota": None}))
    assert only_rto["blockers"] == ["운영자 공급값 없음: runtime_facts.vcpu_quota"]
    only_quota = _cost(PRICED.model_copy(update={"rto_minutes": None}))
    assert only_quota["blockers"] == ["운영자 공급값 없음: runtime_facts.rto_minutes"]
    zero = _cost(PRICED.model_copy(update={"vcpu_quota": 0, "rto_minutes": 0}))
    assert zero["blockers"] == [] and zero["missing"] == [
        "source_instance_hourly_usd"
    ]
    unobserved = _cost(source=None)
    assert unobserved["simultaneous_compute_usd_per_hour"] is None
    assert any("원본" in b for b in unobserved["blockers"])
    resized = _cost(source={**RUNNING_SOURCE, "instance_type": "t3.small"})
    assert (
        resized["source_compute_usd_per_hour"] is None
        and "source_instance_hourly_usd" in resized["blockers"][0]
    )
    explicit = _cost(
        PRICED.model_copy(update={"source_instance_hourly_usd": Decimal("0.02")}),
        source={**RUNNING_SOURCE, "instance_type": "t3.small"},
    )
    assert (
        explicit["blockers"] == []
        and explicit["simultaneous_compute_usd_per_hour"] == "0.07"
    )
    stopped = _cost(source={**RUNNING_SOURCE, "state": "stopped"}, members=2)
    assert stopped["source_compute_usd_per_hour"] == "0" and stopped["blockers"] == []
    assert _cost(members=None)["simultaneous_compute_usd_per_hour"] is None


def test_manifest_digest_tracks_exact_items_and_attribution():
    item = CleanupItem(
        order=9,
        kind="snapshot",
        resource_id="snap-0aaaaaaaaaaaaaaa1",
        action="delete_snapshot",
    )
    manifest = CleanupManifest(execution_id="e", transition_id="t", items=(item,))
    assert (
        manifest.digest()
        == CleanupManifest(execution_id="e", transition_id="t", items=(item,)).digest()
    )
    other = item.model_copy(update={"resource_id": "snap-0aaaaaaaaaaaaaaa2"})
    assert (
        manifest.digest()
        != CleanupManifest(execution_id="e", transition_id="t", items=(other,)).digest()
    )
    owned = item.model_copy(update={"transition_id": "t"})
    assert (
        manifest.digest()
        != CleanupManifest(execution_id="e", transition_id="t", items=(owned,)).digest()
    )
    # 보존 결정 없이는 정리를 승인할 수 없다 — 셋 중 하나만 받는다
    with pytest.raises(ValidationError):
        CleanupOptions.model_validate(
            {"manifest_sha256": manifest.digest(), "resource_ids": ["x"]}
        )
    with pytest.raises(ValidationError):
        CleanupOptions.model_validate(
            {
                "retention_decision": "retain_everything",
                "manifest_sha256": manifest.digest(),
                "resource_ids": ["x"],
            }
        )
    assert CleanupOptions.model_validate(
        {
            "retention_decision": "retain_execution_infrastructure",
            "manifest_sha256": manifest.digest(),
            "resource_ids": ["x"],
        }
    )


def test_replacement_is_an_explicit_optional_input():
    assert (
        ServiceTransitionPlanOptions.model_validate(OPTIONS).replaces_transition_id
        is None
    )
    replaced = ServiceTransitionPlanOptions.model_validate(
        {**OPTIONS, "replaces_transition_id": str(uuid.uuid4())}
    )
    assert replaced.replaces_transition_id

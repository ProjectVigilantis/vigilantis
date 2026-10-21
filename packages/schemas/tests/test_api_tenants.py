"""/api/v1/tenants 외부 DTO 계약 테스트 (Post-MVP T-4).

상태와 그 상태에서만 있는 칸(정지 기록 · 온보딩 링크)의 교차 불변식, 역할 ARN 과 계정의
일치, 연결 확인 결과와 사유의 짝, 부분 수정 요청의 규칙, "Z" 직렬화를 검증한다.
"""

import pytest
from pydantic import ValidationError

from schemas.api import (
    TenantCreateRequest,
    TenantCreateResponse,
    TenantDetail,
    TenantListItem,
    TenantOnboardingResponse,
    TenantStatus,
    TenantSubscription,
    TenantSuspendRequest,
    TenantsResponse,
    TenantUpdateRequest,
    TenantVerification,
    TenantVerificationResult,
    TenantVerifyResponse,
)
from schemas.api.tenants import (
    DELETABLE_STATUSES,
    RESUMABLE_STATUSES,
    SUSPENDABLE_STATUSES,
    VERIFIABLE_STATUSES,
    default_access_role_arn,
)

ACCOUNT = "475763379672"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access"
OK = {"result": "OK", "verified_at": "2026-10-19T03:00:00Z", "reason_code": None}
ONBOARDING = "https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?templateURL=x"


def make_detail(**over):
    base = {
        "tenant_id": "t-1",
        "name": "학원",
        "status": "ACTIVE",
        "aws_account_id": ACCOUNT,
        "last_verification": OK,
        "asset_count": 12,
        "open_incident_count": 1,
        "access_role_arn": ROLE,
        "regions": ["ap-northeast-2"],
        "collect_vpc_ids": [],
        "auto_action_enabled": True,
        "onboarding_url": None,
        "suspended_at": None,
        "suspended_reason": None,
        "subscription": None,
        "created_at": "2026-10-19T00:00:00Z",
        "updated_at": "2026-10-19T03:00:00Z",
    }
    base.update(over)
    return base


# ----- 열거값 — 리터럴 집합이 기대값이다 -----

def test_status_values_match_contract():
    assert {s.value for s in TenantStatus} == {"PENDING_CONNECTION", "ACTIVE", "SUSPENDED"}


def test_verification_results_match_contract():
    assert {r.value for r in TenantVerificationResult} == {
        "OK", "ASSUME_DENIED", "ACCOUNT_MISMATCH", "READ_DENIED", "UNREACHABLE",
    }


# ----- 상세 — 상태와 칸의 교차 불변식 -----

def test_active_detail_is_valid_and_serializes_z():
    d = TenantDetail.model_validate(make_detail())
    dumped = d.model_dump(mode="json")
    assert dumped["created_at"] == "2026-10-19T00:00:00Z"
    assert dumped["last_verification"]["verified_at"] == "2026-10-19T03:00:00Z"


def test_suspended_requires_time_and_reason():
    ok = make_detail(status="SUSPENDED", suspended_at="2026-10-19T04:00:00Z", suspended_reason="계약 만료")
    assert TenantDetail.model_validate(ok).status == TenantStatus.SUSPENDED
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(status="SUSPENDED", suspended_at="2026-10-19T04:00:00Z"))
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(status="SUSPENDED", suspended_reason="계약 만료"))


def test_suspension_fields_only_when_suspended():
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(suspended_at="2026-10-19T04:00:00Z", suspended_reason="x"))


def test_onboarding_url_only_and_always_when_pending():
    pending = make_detail(status="PENDING_CONNECTION", last_verification=None, onboarding_url=ONBOARDING)
    assert TenantDetail.model_validate(pending).onboarding_url == ONBOARDING
    with pytest.raises(ValidationError):  # 연결 전인데 링크가 없다 — 고객이 역할을 만들 길이 없다
        TenantDetail.model_validate(make_detail(status="PENDING_CONNECTION", last_verification=None))
    with pytest.raises(ValidationError):  # 연결된 뒤에는 링크(= ExternalId)를 내보내지 않는다
        TenantDetail.model_validate(make_detail(onboarding_url=ONBOARDING))


def test_onboarding_url_must_be_https():
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(
            status="PENDING_CONNECTION", last_verification=None, onboarding_url="http://x"))


def test_active_needs_a_verification_record():
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(last_verification=None))


def test_active_may_carry_a_failed_latest_verification():
    # ACTIVE 뒤의 확인 실패로 자동 정지하지 않는다 — 그 상태를 계약이 표현할 수 있어야 한다
    failed = {"result": "ASSUME_DENIED", "verified_at": "2026-10-19T05:00:00Z", "reason_code": "AccessDenied"}
    assert TenantDetail.model_validate(make_detail(last_verification=failed)).status == TenantStatus.ACTIVE


def test_role_arn_account_must_match():
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(access_role_arn="arn:aws:iam::111122223333:role/vigilantis-access"))


@pytest.mark.parametrize("field,value", [
    ("aws_account_id", "47576337967"),         # 11자리
    ("aws_account_id", "47576337967a"),
    ("regions", []),                            # 수집 리전 없음
    ("regions", ["Seoul"]),
    ("collect_vpc_ids", ["subnet-0123"]),
    ("access_role_arn", "arn:aws:iam::475763379672:user/x"),  # 역할이 아니라 사용자
    ("access_role_arn", "arn:aws:iam::475763379672:role/역할"),  # IAM 역할 이름은 ASCII 만
    ("asset_count", -1),
])
def test_field_shapes_are_enforced(field, value):
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(**{field: value}))


def test_updated_at_not_before_created_at():
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(updated_at="2026-10-18T00:00:00Z"))


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(external_id="should-not-be-here"))


def test_list_item_is_a_subset_of_detail():
    assert set(TenantListItem.model_fields) < set(TenantDetail.model_fields)
    assert "external_id" not in TenantDetail.model_fields  # 생성 응답에서만 나간다
    TenantsResponse.model_validate({"tenants": [
        {k: v for k, v in make_detail().items() if k in TenantListItem.model_fields}]})


# ----- 연결 확인 -----

def test_ok_has_no_reason_and_failure_needs_one():
    TenantVerification.model_validate(OK)
    with pytest.raises(ValidationError):
        TenantVerification.model_validate({**OK, "reason_code": "x"})
    with pytest.raises(ValidationError):
        TenantVerification.model_validate({**OK, "result": "READ_DENIED"})


def test_verify_response_echoes_the_verification_into_the_tenant():
    TenantVerifyResponse.model_validate({"tenant": make_detail(), "verification": OK})
    other = {**OK, "verified_at": "2026-10-19T09:00:00Z"}
    with pytest.raises(ValidationError):
        TenantVerifyResponse.model_validate({"tenant": make_detail(), "verification": other})


# ----- 생성 -----

def test_create_request_defaults_and_role_account():
    req = TenantCreateRequest.model_validate(
        {"name": "학원", "aws_account_id": ACCOUNT, "regions": ["ap-northeast-2"]})
    assert req.access_role_arn is None and req.collect_vpc_ids == []
    assert default_access_role_arn(ACCOUNT) == ROLE
    with pytest.raises(ValidationError):
        TenantCreateRequest.model_validate({
            "name": "학원", "aws_account_id": ACCOUNT, "regions": ["ap-northeast-2"],
            "access_role_arn": "arn:aws:iam::111122223333:role/vigilantis-access"})


@pytest.mark.parametrize("field,value", [
    ("regions", ["ap-northeast-2", "ap-northeast-2"]),
    ("collect_vpc_ids", ["vpc-0a", "vpc-0a"]),
])
def test_create_request_rejects_duplicates(field, value):
    with pytest.raises(ValidationError):
        TenantCreateRequest.model_validate(
            {"name": "학원", "aws_account_id": ACCOUNT, "regions": ["ap-northeast-2"], field: value})


def test_create_response_is_pending_with_external_id():
    pending = make_detail(status="PENDING_CONNECTION", last_verification=None, onboarding_url=ONBOARDING)
    TenantCreateResponse.model_validate({"tenant": pending, "external_id": "e" * 32})
    with pytest.raises(ValidationError):
        TenantCreateResponse.model_validate({"tenant": make_detail(), "external_id": "e" * 32})
    with pytest.raises(ValidationError):  # 짧은 ExternalId 는 추측된다
        TenantCreateResponse.model_validate({"tenant": pending, "external_id": "short"})


# ----- 정지 · 수정 -----

def test_suspend_requires_reason():
    TenantSuspendRequest.model_validate({"reason": "계약 만료"})
    with pytest.raises(ValidationError):
        TenantSuspendRequest.model_validate({"reason": ""})
    with pytest.raises(ValidationError):
        TenantSuspendRequest.model_validate({})


def test_update_needs_at_least_one_field():
    with pytest.raises(ValidationError):
        TenantUpdateRequest.model_validate({})


@pytest.mark.parametrize("field", ["name", "regions", "collect_vpc_ids", "auto_action_enabled"])
def test_update_rejects_explicit_null(field):
    # null 은 「지운다」로도 「안 바꾼다」로도 읽혀 갈린다 — 안 바꾸려면 칸을 뺀다
    with pytest.raises(ValidationError):
        TenantUpdateRequest.model_validate({field: None})


def test_update_allows_clearing_subscription():
    assert TenantUpdateRequest.model_validate({"subscription": None}).subscription is None


@pytest.mark.parametrize("field", ["aws_account_id", "access_role_arn", "status"])
def test_update_cannot_touch_identity_or_status(field):
    # 다른 계정이면 다른 테넌트 · 상태는 suspend/resume/verify 로만 바뀐다
    with pytest.raises(ValidationError):
        TenantUpdateRequest.model_validate({field: "x"})


def test_subscription_period_order():
    TenantSubscription.model_validate({"plan": "basic", "period_start": "2026-10-01", "period_end": "2026-12-31"})
    with pytest.raises(ValidationError):
        TenantSubscription.model_validate({"period_start": "2026-12-31", "period_end": "2026-10-01"})


# ----- 복구 경로와 정지 중 확인 (PR #489 리뷰 A · B · C) -----

def test_only_never_connected_tenants_can_be_deleted():
    # 계정 ID 오입력(연결 확인에서 ASSUME_DENIED 로 나타난다)을 고치는 길 = 지우고 다시 만든다. 연결된 적 있으면 기록이 있다
    assert DELETABLE_STATUSES == frozenset({TenantStatus.PENDING_CONNECTION})


def test_suspended_tenant_is_not_verified_directly():
    # 정지 중 계정에 들어가는 길은 resume 하나다 — T-5 가 「정지 테넌트에는 AssumeRole 0회」를 단정할 근거
    assert VERIFIABLE_STATUSES == frozenset({TenantStatus.PENDING_CONNECTION, TenantStatus.ACTIVE})
    assert TenantStatus.SUSPENDED not in VERIFIABLE_STATUSES


def test_onboarding_reissue_carries_the_link_and_external_id():
    reissued = TenantOnboardingResponse.model_validate({"onboarding_url": ONBOARDING, "external_id": "e" * 32})
    assert reissued.external_id == "e" * 32
    with pytest.raises(ValidationError):
        TenantOnboardingResponse.model_validate({"onboarding_url": "http://x", "external_id": "e" * 32})
    with pytest.raises(ValidationError):
        TenantOnboardingResponse.model_validate({"onboarding_url": ONBOARDING, "external_id": "short"})


def test_detail_still_does_not_carry_the_external_id():
    # 재발급이 따로 생겨도 상세 응답의 노출 범위는 그대로다(리뷰 포인트 3)
    assert "external_id" not in TenantDetail.model_fields
    with pytest.raises(ValidationError):
        TenantDetail.model_validate(make_detail(external_id="e" * 32))


def test_resume_failure_keeps_the_tenant_suspended():
    # resume 의 확인이 실패하면 SUSPENDED 그대로 — 응답 모양이 그 상태를 표현할 수 있어야 한다
    failed = {"result": "ASSUME_DENIED", "verified_at": "2026-10-19T06:00:00Z", "reason_code": "AccessDenied"}
    tenant = make_detail(status="SUSPENDED", last_verification=failed,
                         suspended_at="2026-10-19T04:00:00Z", suspended_reason="계약 만료")
    TenantVerifyResponse.model_validate({"tenant": tenant, "verification": failed})


# ----- suspend · resume 기준 (PR #503 리뷰 확인 부탁 ①) -----

def test_only_active_tenants_can_be_suspended():
    # 연결 전은 들어간 적이 없어 회수할 것이 없고(지우면 된다), 이미 정지면 할 일이 없다
    assert SUSPENDABLE_STATUSES == frozenset({TenantStatus.ACTIVE})


def test_only_suspended_tenants_can_be_resumed():
    # 연결 전 테넌트를 ACTIVE 로 올리는 길은 verify 다 — resume 이 아니다
    assert RESUMABLE_STATUSES == frozenset({TenantStatus.SUSPENDED})


def test_every_status_has_a_way_forward_and_no_action_is_shared_ambiguously():
    # 상태마다 열리는 동작이 하나 이상 있고, 고객 계정에 들어가는 동작(verify · resume)은
    # 같은 상태에서 함께 열리지 않는다 — 정지 중 들어가는 길은 resume 하나
    for status in TenantStatus:
        opened = {name for name, allowed in (
            ("delete", DELETABLE_STATUSES), ("verify", VERIFIABLE_STATUSES),
            ("suspend", SUSPENDABLE_STATUSES), ("resume", RESUMABLE_STATUSES),
        ) if status in allowed}
        assert opened, status
        assert not {"verify", "resume"} <= opened, status

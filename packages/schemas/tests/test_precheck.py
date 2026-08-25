"""가드레일 ④ precheck 계약 테스트 (Issue #128, ADR-0007 §1·§3).

핵심: PASS/FAIL과 reason_code의 짝이 고정이고, verification_summary는 PASS·FAIL
모두 필수이며 형식(방식 | 확인 | 미확인)이 계약으로 강제된다.
"""

import pytest
from pydantic import ValidationError

from schemas.precheck import (
    PrecheckOutcome,
    PrecheckReasonCode,
    VerificationMethod,
    build_verification_summary,
)

# ADR-0007 §3의 예시 문자열 그대로 — 문서와 코드가 같은 형식을 말하는지 고정한다
ADR_EXAMPLE = (
    "DRY_RUN(ec2.modify_instance_attribute)"
    " | 확인: 파라미터·대상 자원 유효"
    " | 미확인: IAM 권한(LocalStack iam disabled)"
)


def test_reason_codes_match_adr_exactly():
    assert {c.value for c in PrecheckReasonCode} == {
        "PRECHECK_UNAUTHORIZED",
        "PRECHECK_TARGET_NOT_FOUND",
        "PRECHECK_INVALID_STATE",
        "PRECHECK_NOT_IMPLEMENTED",
        "PRECHECK_PARAM_INVALID",
        "PRECHECK_AWS_ERROR",
    }


def test_verification_methods_match_adr_exactly():
    assert {m.value for m in VerificationMethod} == {"DRY_RUN", "DESCRIBE", "MIXED"}


def test_adr_example_summary_is_accepted():
    outcome = PrecheckOutcome(passed=True, verification_summary=ADR_EXAMPLE)
    assert outcome.reason_code is None


def test_fail_requires_reason_code():
    with pytest.raises(ValidationError, match="reason_code가 필요"):
        PrecheckOutcome(passed=False, verification_summary=ADR_EXAMPLE)


def test_pass_rejects_reason_code():
    with pytest.raises(ValidationError, match="PASS에는 reason_code"):
        PrecheckOutcome(
            passed=True,
            reason_code=PrecheckReasonCode.PRECHECK_AWS_ERROR,
            verification_summary=ADR_EXAMPLE,
        )


def test_summary_is_required_on_fail_too():
    with pytest.raises(ValidationError):
        PrecheckOutcome(
            passed=False, reason_code=PrecheckReasonCode.PRECHECK_UNAUTHORIZED
        )


def test_extra_field_is_rejected():
    with pytest.raises(ValidationError):
        PrecheckOutcome(
            passed=True, verification_summary=ADR_EXAMPLE, aws_request_id="r-1"
        )


@pytest.mark.parametrize(
    "summary",
    [
        "파라미터 유효",                                   # 방식 없음
        "DRYRUN | 확인: a | 미확인: b",                     # 방식 오타
        "DESCRIBE | 확인: a",                              # 미확인 절 누락
        "DESCRIBE | 미확인: b",                            # 확인 절 누락
        "DESCRIBE | 확인: a | 미확인: ",                    # 미확인 빈 값
        "DESCRIBE | 확인:  | 미확인: b",                    # 확인 빈 값
        "DESCRIBE | 미확인: b | 확인: a",                   # 절 순서 뒤바뀜
        "DESCRIBE|확인: a|미확인: b",                       # 구분자 공백 없음
    ],
)
def test_summary_format_violations_are_rejected(summary):
    with pytest.raises(ValidationError, match="verification_summary 형식 위반"):
        PrecheckOutcome(passed=True, verification_summary=summary)


@pytest.mark.parametrize("method", list(VerificationMethod))
def test_builder_output_passes_the_contract(method):
    summary = build_verification_summary(
        method,
        operations=["ec2.describe_network_acls"],
        verified=["ACL 존재", "규칙 번호 미사용"],
        unverified=["IAM 권한"],
    )
    assert summary.startswith(f"{method.value}(ec2.describe_network_acls) | 확인: ")
    assert "확인: ACL 존재·규칙 번호 미사용" in summary
    # 계약이 실제로 받아들이는 문자열이어야 한다
    PrecheckOutcome(passed=True, verification_summary=summary)


def test_builder_allows_omitting_operations():
    summary = build_verification_summary(
        VerificationMethod.DESCRIBE, verified=["대상 존재"], unverified=["IAM 권한"]
    )
    assert summary == "DESCRIBE | 확인: 대상 존재 | 미확인: IAM 권한"
    PrecheckOutcome(passed=True, verification_summary=summary)


def test_builder_refuses_empty_unverified():
    """확인 한계를 비워 두는 것은 계약 위반이다 — 이 필드의 존재 이유가 그것이다."""
    with pytest.raises(ValueError, match="unverified 항목이 비어"):
        build_verification_summary(
            VerificationMethod.DRY_RUN, verified=["대상 존재"], unverified=[]
        )


def test_builder_refuses_empty_verified():
    with pytest.raises(ValueError, match="verified 항목이 비어"):
        build_verification_summary(
            VerificationMethod.DRY_RUN, verified=[], unverified=["IAM 권한"]
        )


@pytest.mark.parametrize("bad", ["", "   "])
def test_builder_refuses_blank_items(bad):
    with pytest.raises(ValueError, match="빈 항목"):
        build_verification_summary(
            VerificationMethod.DESCRIBE, verified=[bad], unverified=["IAM 권한"]
        )


def test_builder_refuses_separator_in_items():
    with pytest.raises(ValueError, match="구분자"):
        build_verification_summary(
            VerificationMethod.DESCRIBE,
            verified=["대상 존재 | 규칙 없음"],
            unverified=["IAM 권한"],
        )


def test_builder_refuses_parentheses_in_operations():
    with pytest.raises(ValueError, match="괄호"):
        build_verification_summary(
            VerificationMethod.DRY_RUN,
            operations=["ec2.delete_volume(v)"],
            verified=["대상 존재"],
            unverified=["IAM 권한"],
        )


def test_summary_body_may_contain_parentheses():
    """확인·미확인 본문의 괄호는 허용한다 — ADR 예시가 그 형태다."""
    summary = build_verification_summary(
        VerificationMethod.MIXED,
        verified=["ENI DryRun 통과"],
        unverified=["TG 등록 상태(elbv2 로컬 미지원)"],
    )
    PrecheckOutcome(passed=True, verification_summary=summary)

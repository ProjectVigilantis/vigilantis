"""rule_engine 순수 단위 테스트. AWS/DB 불필요 — 판정 로직만 검증한다.
seed_localstack 의 기대 판정(정답지) 8줄을 함수 직접 호출로 재현한다."""

import sys
from pathlib import Path

import pytest

# apps/core-api 를 import 경로에 추가 (services 패키지 로드용)
CORE_API = Path(__file__).resolve().parents[2]
if str(CORE_API) not in sys.path:
    sys.path.insert(0, str(CORE_API))

from services.rule_engine import (  # noqa: E402
    SkipReason,
    Verdict,
    evaluate_ebs,
    evaluate_ec2,
    evaluate_sg,
)

# (state, attached_instance_ids) -> (verdict, skip_reason)
EBS_CASES = [
    ("available", [], Verdict.UNUSED, None),                 # 미부착·available → 정리 후보
    ("in-use", ["i-abc"], Verdict.SKIP, SkipReason.SKIP_ACTIVE),  # 부착 → 정상 사용
    ("available", ["i-abc"], Verdict.SKIP, SkipReason.SKIP_ACTIVE),  # 모순(available+부착) → 부착 우선
    # 전이·비정상·미상 상태 → UNUSED 아님, "정상 가동"도 아님 → 판정 보류(#276)
    ("creating", [], Verdict.SKIP, SkipReason.SKIP_UNSUPPORTED_STATE),
    ("deleting", [], Verdict.SKIP, SkipReason.SKIP_UNSUPPORTED_STATE),
    ("error", [], Verdict.SKIP, SkipReason.SKIP_UNSUPPORTED_STATE),
    ("deleted", [], Verdict.SKIP, SkipReason.SKIP_UNSUPPORTED_STATE),
    (None, [], Verdict.SKIP, SkipReason.SKIP_UNSUPPORTED_STATE),  # state 미상(null) fail-safe
]

# (name, cpu_avg, cpu_max, datapoints, tags) -> (verdict, skip_reason)
EC2_CASES = [
    ("dev-idle-api-01", 1.95, 3.5, 332, {}, Verdict.COST_CANDIDATE, None),
    ("dev-idle-batch-02", 2.04, 3.5, 332, {}, Verdict.COST_CANDIDATE, None),
    ("prod-web-01", 54.5, 71.9, 332, {"Environment": "production"}, Verdict.SKIP, SkipReason.SKIP_PROD_PROTECTED),
    ("dev-spiky-worker-03", 4.9, 91.82, 332, {}, Verdict.SKIP, SkipReason.SKIP_LOW_UTIL),
    ("dev-new-04", 3.23, 4.98, 24, {}, Verdict.SKIP, SkipReason.SKIP_INSUFFICIENT_DATA),
]

# (name, attached, open_to_world) -> (verdict, skip_reason)
SG_CASES = [
    ("demo-open-ssh", True, True, Verdict.THREAT, None),
    ("demo-open-rdp", True, True, Verdict.THREAT, None),
    ("demo-orphan-unused", False, False, Verdict.UNUSED, None),
    ("demo-internal-https", True, False, Verdict.SKIP, SkipReason.SKIP_ACTIVE),
    ("default", False, False, Verdict.SKIP, SkipReason.SKIP_WHITELISTED),
]


@pytest.mark.parametrize("name,avg,mx,dp,tags,exp_v,exp_s", EC2_CASES)
def test_ec2_verdicts(name, avg, mx, dp, tags, exp_v, exp_s):
    verdict, skip, health = evaluate_ec2(avg, mx, dp, tags)
    assert verdict == exp_v
    assert skip == exp_s
    assert health == round(avg, 2)


@pytest.mark.parametrize("name,attached,openw,exp_v,exp_s", SG_CASES)
def test_sg_verdicts(name, attached, openw, exp_v, exp_s):
    verdict, skip = evaluate_sg(name, attached, openw)
    assert verdict == exp_v
    assert skip == exp_s


@pytest.mark.parametrize("state,attached_ids,exp_v,exp_s", EBS_CASES)
def test_ebs_verdicts(state, attached_ids, exp_v, exp_s):
    verdict, skip = evaluate_ebs(state, attached_ids)
    assert verdict == exp_v
    assert skip == exp_s


def test_prod_protected_via_tag():
    # 이름에 prod 가 없어도 태그로 운영 자산이면 보호
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 300, {"Environment": "production"})
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_PROD_PROTECTED


def test_insufficient_data_takes_precedence():
    # 데이터부족이 최우선 — idle 처럼 보여도 판정 보류
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 10)
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_INSUFFICIENT_DATA


# _is_prod 태그 인식 (미해결 #4): 키 대소문자 무시 + 값 정확일치
PROD_TAG_CASES = [
    {"env": "prod"},              # 키 소문자
    {"Env": "prod"},             # Env 키(#95 명시)
    {"environment": "production"},  # 소문자 키 + production
    {"Stage": "PRD"},            # Stage 키 + prd 값
    {"tier": "Production"},       # tier 키 + 값 대소문자
    {"ENVIRONMENT": "prod"},     # 키 대문자
    {"Environment": " PROD "},   # 값 공백 트림(.strip) 커버
]
NON_PROD_TAG_CASES = [
    {"Environment": "staging"},   # staging 은 prod 아님
    {"Environment": "dev"},
    {"Environments": "prod"},     # near-miss 키(오인식 방지)
    {"Name": "product-service"},  # 값 부분일치 오탐 없어야(#81)
    {},
]


@pytest.mark.parametrize("tags", PROD_TAG_CASES)
def test_is_prod_recognized(tags):
    # idle 지표라도 운영 태그면 보호 스킵
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 300, tags)
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_PROD_PROTECTED


@pytest.mark.parametrize("tags", NON_PROD_TAG_CASES)
def test_non_prod_idle_is_candidate(tags):
    # 비운영 idle 은 다운사이징 후보(오탐 없어야)
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 300, tags)
    assert verdict == Verdict.COST_CANDIDATE
    assert skip is None

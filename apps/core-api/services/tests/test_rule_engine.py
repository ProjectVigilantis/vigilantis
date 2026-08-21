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
    evaluate_ec2,
    evaluate_sg,
)

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
    verdict, skip, health = evaluate_ec2(avg, mx, dp, name, tags)
    assert verdict == exp_v
    assert skip == exp_s
    assert health == round(avg, 2)


@pytest.mark.parametrize("name,attached,openw,exp_v,exp_s", SG_CASES)
def test_sg_verdicts(name, attached, openw, exp_v, exp_s):
    verdict, skip = evaluate_sg(name, attached, openw)
    assert verdict == exp_v
    assert skip == exp_s


def test_prod_protected_via_tag():
    # 이름에 prod 가 없어도 태그로 운영 자산이면 보호
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 300, "svc-01", {"Environment": "production"})
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_PROD_PROTECTED


def test_insufficient_data_takes_precedence():
    # 데이터부족이 최우선 — idle 처럼 보여도 판정 보류
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 10, "dev-brandnew")
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_INSUFFICIENT_DATA

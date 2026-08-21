"""LocalStack 시드 데이터셋의 판정 분포 고정 테스트. AWS/DB 불필요 — 순수 함수 호출만 한다.

seed_localstack 의 INSTANCES·CPU 프로필을 그대로 evaluate_ec2 에 태워, 시드가 의도한
Verdict 분포를 유지하는지 검증한다. test_rule_engine 은 합성 케이스로 판정 규칙 자체를
검증하고, 이쪽은 "실제 시드가 그 규칙 아래에서 어떤 분포가 되는가"를 고정한다.

배경(#81/#88 회귀): Environment 태그 도입으로 idle 인스턴스가 prod 보호에 흡수되면서
COST_CANDIDATE 가 0대가 됐지만, 기존 테스트는 자산 개수만 >= 1 로 확인해 이를 놓쳤다.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(CORE_API) not in sys.path:
    sys.path.insert(0, str(CORE_API))

from services.rule_engine import SkipReason, Verdict, evaluate_ec2  # noqa: E402


def _load_seed_module():
    """scripts/seed_localstack.py 를 경로로 로드(패키지가 아니라 스크립트라 import 불가)."""
    path = REPO_ROOT / "scripts" / "seed_localstack.py"
    spec = importlib.util.spec_from_file_location("seed_localstack", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED = _load_seed_module()

# 시드 인스턴스별 기대 판정 — 시드를 늘리면 여기도 함께 갱신해야 한다(아래 커버리지 테스트가 강제).
EXPECTED = {
    "vigilantis-seed-idle": (Verdict.SKIP, SkipReason.SKIP_PROD_PROTECTED),
    "vigilantis-seed-normal": (Verdict.SKIP, SkipReason.SKIP_ACTIVE),
    "vigilantis-seed-spike": (Verdict.SKIP, SkipReason.SKIP_LOW_UTIL),
    "vigilantis-seed-idle-dev": (Verdict.COST_CANDIDATE, None),
}


def _evaluate(name, profile, environment):
    series = SEED._cpu_series(profile)
    tags = {"Name": name}
    if environment:
        tags["Environment"] = environment
    return evaluate_ec2(sum(series) / len(series), max(series), len(series), name, tags)


def test_expected_covers_every_seed_instance():
    """시드에 인스턴스를 추가/삭제하면 기대 판정 표도 함께 고쳐야 한다."""
    assert {i[0] for i in SEED.INSTANCES} == set(EXPECTED)


@pytest.mark.parametrize("entry", SEED.INSTANCES, ids=lambda e: e[0])
def test_seed_instance_verdict(entry):
    name, _itype, _sg, profile, environment = entry
    verdict, skip, _health = _evaluate(name, profile, environment)
    assert (verdict, skip) == EXPECTED[name]


def test_cost_candidate_exists():
    """FinOps 경로(RIGHTSIZING) 시연에는 절감 후보가 최소 1대 있어야 한다 — #81/#88 회귀 가드."""
    verdicts = [_evaluate(n, p, e)[0] for n, _t, _s, p, e in SEED.INSTANCES]
    assert verdicts.count(Verdict.COST_CANDIDATE) >= 1


def test_prod_idle_is_protected_and_large():
    """'대형이어도 prod면 끄지 않는다' 확인용 자산 — prod 태그 + 저활성 + 대형 타입 조합을 고정."""
    entry = next(i for i in SEED.INSTANCES if i[0] == "vigilantis-seed-idle")
    _name, itype, _sg, profile, environment = entry
    assert environment == "production"
    assert profile == "idle"
    assert not itype.endswith((".micro", ".small"))  # 절감 여지가 커 보이는 타입이어야 대조가 성립

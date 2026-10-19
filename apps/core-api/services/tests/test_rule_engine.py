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
    # cpu_max 없음 — 수집기는 avg·max를 같은 목록에서 뽑아 이 조합을 만들지 않는다(도달 불가 입력).
    # 계약이 아니라 수집기 구현의 성질이다 — 스키마·DB는 두 값이 독립 nullable이라 막지 않는다.
    # 그래도 null 가드가 빠지면 TypeError라 방어 동작을 여기서 고정한다. 골든 A7에서 옮겨 왔다(#243).
    ("null-max-guard", 4.9, None, 48, {}, Verdict.COST_CANDIDATE, None),
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
    verdict, skip, health = evaluate_ec2(avg, mx, dp, tags, state="running")
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
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 300, {"Environment": "production"}, state="running")
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_PROD_PROTECTED


def test_insufficient_data_takes_precedence():
    # 데이터부족이 최우선 — idle 처럼 보여도 판정 보류
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 10, state="running")
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
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 300, tags, state="running")
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_PROD_PROTECTED


@pytest.mark.parametrize("tags", NON_PROD_TAG_CASES)
def test_non_prod_idle_is_candidate(tags):
    # 비운영 idle 은 다운사이징 후보(오탐 없어야)
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 300, tags, state="running")
    assert verdict == Verdict.COST_CANDIDATE
    assert skip is None


# ----- #453 — 인스턴스 상태와 이미 줄인 인스턴스 -----

# running 이 아닌 상태 전부. 각 상태에 관측치 300 · 평균 1.0 — running 이면 후보가 되는 조합이다.
# 종료 인스턴스가 지금 후보가 되는 것이 바로 이 조합이다(CloudWatch 가 지난 지표를 보관한다).
NON_RUNNING_STATES = ["terminated", "shutting-down", "stopped", "stopping", "pending", None, "unknown-x"]


@pytest.mark.parametrize("state", NON_RUNNING_STATES)
def test_non_running_state_is_not_a_candidate(state):
    verdict, skip, health = evaluate_ec2(1.0, 2.0, 300, {}, state=state)
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_UNSUPPORTED_STATE
    assert health == 1.0  # 지표는 그대로 싣는다 — 판정만 보류한다


def test_state_check_precedes_insufficient_data_and_prod():
    # 상태가 먼저다 — 관측치 부족이나 운영 보호보다 앞에서 「판정 대상 아님」을 낸다
    assert evaluate_ec2(1.0, 2.0, 10, {}, state="terminated")[1] == SkipReason.SKIP_UNSUPPORTED_STATE
    assert evaluate_ec2(1.0, 2.0, 300, {"Environment": "prod"}, state="stopped")[1] == SkipReason.SKIP_UNSUPPORTED_STATE


def test_running_state_is_case_insensitive():
    verdict, _, _ = evaluate_ec2(1.0, 2.0, 300, {}, state="Running")
    assert verdict == Verdict.COST_CANDIDATE


def test_state_is_required():
    # 기본값을 두지 않는다 — 호출부가 상태를 빠뜨리면 조용히 running 으로 판정되는 것을 막는다
    with pytest.raises(TypeError):
        evaluate_ec2(1.0, 2.0, 300, {})


def test_ec2_and_ebs_share_the_hold_code():
    # 한 모듈 안에서 기준이 갈리지 않는다 — 판정 대상 상태 밖은 둘 다 같은 보류 코드다
    assert evaluate_ec2(1.0, 2.0, 300, {}, state="terminated")[1] == evaluate_ebs("deleted", [])[1]


def test_already_rightsized_idle_is_not_a_candidate_again():
    # #434 5-2 — 줄인 뒤에도 창에 옛 타입의 낮은 CPU 가 남아 다시 후보가 되던 조합
    verdict, skip, _ = evaluate_ec2(1.0, 2.0, 300, {}, state="running", already_rightsized=True)
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_LOW_UTIL


def test_already_rightsized_active_stays_active():
    # 이미 줄였어도 정상 가동은 정상 가동이다 — 보류 사유를 덮어쓰지 않는다
    verdict, skip, _ = evaluate_ec2(50.0, 70.0, 300, {}, state="running", already_rightsized=True)
    assert verdict == Verdict.SKIP
    assert skip == SkipReason.SKIP_ACTIVE

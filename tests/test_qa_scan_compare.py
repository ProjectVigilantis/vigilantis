# scripts/qa_scan_compare.py 검증 — 스모크 수집·판정 대조가 코드로 지키는 약속.
#
#   ① `error_summary` 에서 **라벨별 사유 코드**를 잃지 않는다. 그 코드가 LocalStack 의
#      라이선스 실패(`InternalFailure`)와 실 AWS 의 권한 누락(`AccessDenied`)을 가르는
#      유일한 값이다(ADR-0009 §6-1).
#   ② 읽을 수 없는 `error_summary` 를 **빈 값으로 삼키지 않는다.** 삼키면 실패가 없었던
#      것처럼 기록되고, 그 기록은 이월 4행의 처분 근거로 쓰인다.
#   ③ §2-2 표가 **자산 7유형을 모두** 들고 있다. 수집기가 못 본 유형은
#      `asset_inventory_counts` 에 행을 남기지 않으므로(models.AssetInventoryCount),
#      관측 결과에서 유형을 끌어오면 그 유형이 표에서 사라진다 — "0건" 과 "모름" 이
#      같은 칸이 되는 순간 대조가 무의미해진다.
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다. DB·AWS를 부르지 않는다.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "apps" / "core-api", REPO_ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from schemas.api.assets import AssetType  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


qa = _load("qa_scan_compare")


# ① 라벨별 사유 코드를 잃지 않는다
def test_failures_keeps_label_and_reason_code():
    failures, how = qa._failures(
        '{"alb_target_groups":"InternalFailure","auto_scaling_groups":"AccessDenied"}'
    )
    assert failures == {
        "alb_target_groups": "InternalFailure",
        "auto_scaling_groups": "AccessDenied",
    }
    assert how == "JSON"


def test_failures_empty_summary_is_not_a_failure():
    assert qa._failures(None) == ({}, "빈 값")
    assert qa._failures("") == ({}, "빈 값")


# ② 읽을 수 없는 값을 빈 값으로 삼키지 않는다 — 원문을 들고 돌아온다
@pytest.mark.parametrize(
    "raw",
    [
        "AccessDenied: autoscaling",          # JSON 이 아닌 과거/수기 값
        '"alb_target_groups"',                # JSON 이지만 dict 가 아니다
        '{"_truncated":"5"}',                 # 상한 초과로 항목이 버려진 표식
    ],
)
def test_failures_unreadable_summary_reports_raw_text(raw):
    failures, how = qa._failures(raw)
    assert failures == {}
    assert how != "빈 값"
    # 사람이 원문을 보고 판단할 수 있어야 한다 — 설명에 원문이 남는다.
    assert raw in how or "상한 초과" in how


# ③ §2-2 표가 7유형을 모두 들고 있다 — 유형이 늘면 이 테스트가 먼저 깨진다
def test_inventory_table_covers_every_asset_type():
    assert set(qa._ALL_TYPES) == set(AssetType)
    assert len(qa._ALL_TYPES) == 7


# ④ 출력 단계가 "읽지 못했다"를 "(없음)"으로 삼키지 않는다 (PR #421 리뷰 ③-1)
#    파싱이 깨진 회차를 실패 없음으로 적으면 그 기록이 이월 4행 처분의 근거가 된다.
@pytest.mark.parametrize(
    "raw",
    [
        "AccessDenied: autoscaling",   # JSON 이 아닌 값
        '"alb_target_groups"',         # dict 가 아닌 JSON
        '{"_truncated":"5"}',          # 상한 초과로 항목이 버려진 표식
    ],
)
def test_failure_lines_never_report_none_when_summary_is_unreadable(raw):
    lines = qa._failure_lines(raw, "localstack")
    joined = "\n".join(lines)
    assert "(없음)" not in joined
    assert "확인 불가" in joined


def test_failure_lines_report_none_only_for_an_empty_summary():
    assert qa._failure_lines(None, "localstack") == ["  collector_failures : (없음)"]
    assert qa._failure_lines("", "aws") == ["  collector_failures : (없음)"]
    # 빈 묶음은 읽기는 됐으므로 "확인 불가" 가 아니다 — 둘을 구별해 적는다.
    assert "빈 묶음" in "\n".join(qa._failure_lines("{}", "localstack"))


# ⑤ InternalFailure 설명은 회차의 mode 로 갈린다 — 실 AWS 에서도 나오는 코드다
def test_internal_failure_note_depends_on_run_mode():
    raw = '{"auto_scaling_groups":"InternalFailure"}'
    local = "\n".join(qa._failure_lines(raw, "localstack"))
    real = "\n".join(qa._failure_lines(raw, "aws"))
    assert "LocalStack 라이선스 밖" in local
    assert "LocalStack" not in real
    assert "AWS 측 내부 오류" in real


# ⑥ AccessDenied 는 환경과 무관하게 권한 누락을 가리킨다
@pytest.mark.parametrize("mode", ["localstack", "aws"])
def test_access_denied_points_at_a_missing_permission(mode):
    line = "\n".join(qa._failure_lines('{"alb_target_groups":"AccessDenied"}', mode))
    assert "권한 누락" in line


# ⑦ 다중 리전 주의 줄은 원인을 단정하지 않고 리전=상태 사실만 적는다
def test_region_notice_states_facts_without_claiming_a_cause():
    lines = qa._region_notice([("ap-northeast-2", "PARTIAL"), ("us-east-1", "FAILED")])
    head = lines[0]
    assert "리전이 2개" in head
    assert "ap-northeast-2=PARTIAL" in head and "us-east-1=FAILED" in head
    # 원인 단정("매번 FAILED 로 마감된다")이 주의 줄에 섞이지 않는다 — 참고 줄로 내려간다.
    assert "매번" not in head
    assert any("참고:" in line and "ADR-0009" in line for line in lines[1:])


@pytest.mark.parametrize("pairs", [[], [("ap-northeast-2", "SUCCESS")]])
def test_region_notice_is_silent_for_one_region_or_none(pairs):
    assert qa._region_notice(pairs) == []


# ⑧ 다른 계정 표시의 근거 — ARN 계정 칸을 읽는다
def test_arn_account_reads_the_account_field():
    arn = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/x/y"
    assert qa._arn_account(arn) == "123456789012"
    assert qa._arn_account("not-an-arn") == ""


# ⑨ 계정 칸을 읽지 못한 것을 "다른 계정"으로 적지 않는다 (이슈 #427)
#    기록하는 사람이 "남의 것"으로 보고 빼 버리면 조사 대상이 조용히 사라진다.
def test_account_mark_does_not_call_an_unreadable_account_someone_elses():
    runs = {"000000000000"}
    assert qa._account_mark("", runs) == "  (계정 확인 불가)"
    assert "다른 계정" not in qa._account_mark("", runs)


def test_account_mark_marks_only_a_readable_foreign_account():
    runs = {"000000000000"}
    assert qa._account_mark("123456789012", runs) == "  (다른 계정)"
    assert qa._account_mark("000000000000", runs) == ""


def test_account_mark_compares_against_every_run_account():
    # 기본 실행은 리전별 최신 회차를 모두 찍으므로 계정이 섞일 수 있다 — 집합 전체와 비교한다.
    runs = {"000000000000", "111111111111"}
    assert qa._account_mark("111111111111", runs) == ""
    assert qa._account_mark("222222222222", runs) == "  (다른 계정)"

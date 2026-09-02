"""collector 순수 단위 테스트 — DB·LocalStack 불필요.

_paginate(전 페이지 순회)와 _safe_describe(실패 흡수·degrade 라벨) 만 가짜 클라이언트로 검증한다.
(#161 리뷰: 페이지네이션 누락·degrade 표면화)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.assets import MetricSummary  # noqa: E402
from services.collector import (  # noqa: E402
    _is_alb_target_group,
    _paginate,
    _registered_instance_ids,
    _reusable_summaries,
    _safe_describe,
)


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self):
        return iter(self._pages)


class _FakeClient:
    """get_paginator 만 흉내내는 최소 가짜 클라이언트."""

    def __init__(self, pages_by_op):
        self._pages_by_op = pages_by_op

    def get_paginator(self, name):
        return _FakePaginator(self._pages_by_op[name])


def test_paginate_flattens_all_pages():
    # 2페이지에 걸친 자산이 전부 수집되어야 한다(단일 호출이면 lt-2 가 누락된다).
    client = _FakeClient(
        {
            "describe_launch_templates": [
                {"LaunchTemplates": [{"LaunchTemplateId": "lt-1"}]},
                {"LaunchTemplates": [{"LaunchTemplateId": "lt-2"}]},
            ]
        }
    )
    out = _paginate(client, "describe_launch_templates", "LaunchTemplates")
    assert [x["LaunchTemplateId"] for x in out] == ["lt-1", "lt-2"]


def test_paginate_empty_pages():
    client = _FakeClient({"describe_auto_scaling_groups": [{"AutoScalingGroups": []}]})
    assert _paginate(client, "describe_auto_scaling_groups", "AutoScalingGroups") == []


def test_safe_describe_passes_through_and_no_failure():
    failures: dict[str, str] = {}
    out = _safe_describe(lambda: [1, 2, 3], "launch_templates", failures)
    assert out == [1, 2, 3]
    assert failures == {}


def test_safe_describe_absorbs_client_error_and_records_reason():
    # LocalStack 의 Pro 전용 InternalFailure(=ClientError) 를 모사. 사유=AWS 오류 코드 (C4).
    err = ClientError(
        {"Error": {"Code": "InternalFailure", "Message": "not included within your LocalStack license"}},
        "DescribeAutoScalingGroups",
    )
    failures: dict[str, str] = {}

    def _boom():
        raise err

    out = _safe_describe(_boom, "auto_scaling_groups", failures)
    assert out == []
    assert failures == {"auto_scaling_groups": "InternalFailure"}


def test_safe_describe_absorbs_botocore_error_with_class_name():
    # 엔드포인트 접속 실패(BotoCoreError 계열)도 흡수 — 사유=예외 클래스명.
    failures: dict[str, str] = {}

    def _boom():
        raise EndpointConnectionError(endpoint_url="http://localhost:4566")

    assert _safe_describe(_boom, "auto_scaling_groups", failures) == []
    assert failures == {"auto_scaling_groups": "EndpointConnectionError"}


def test_safe_describe_does_not_swallow_unexpected_error():
    # AWS 예외가 아닌 버그성 예외까지 삼키면 안 된다.
    failures: dict[str, str] = {}

    def _boom():
        raise KeyError("AutoScalingGroupARN")

    with pytest.raises(KeyError):
        _safe_describe(_boom, "auto_scaling_groups", failures)
    assert failures == {}


def test_registered_instance_ids_dedups_multiport():
    # 같은 인스턴스가 한 TG 에 여러 포트로 등록되면 target health 가 중복 반환한다.
    # REGISTERED_IN 은 (source, relation, target) unique 라 중복 제거가 필수(#165 리뷰 ①).
    health = [
        {"Target": {"Id": "i-aaa", "Port": 80}},
        {"Target": {"Id": "i-aaa", "Port": 8080}},  # 같은 인스턴스, 다른 포트
        {"Target": {"Id": "i-bbb", "Port": 80}},
        {"Target": {"Id": "192.0.2.10", "Port": 80}},  # ip 대상 — 제외
    ]
    assert _registered_instance_ids(health) == ["i-aaa", "i-bbb"]


def test_is_alb_target_group_filters_by_protocol():
    # ALB(HTTP/HTTPS)만 통과. NLB(TCP/UDP/TLS)·GWLB(GENEVE)·lambda(프로토콜 없음) 제외(#165 리뷰 ②).
    assert _is_alb_target_group({"Protocol": "HTTP"}) is True
    assert _is_alb_target_group({"Protocol": "HTTPS"}) is True
    assert _is_alb_target_group({"Protocol": "TCP"}) is False
    assert _is_alb_target_group({"Protocol": "GENEVE"}) is False
    assert _is_alb_target_group({}) is False


# ---- C4: 리전 격리·부분 리트라이 제어 흐름 (no-DB) ----

class _FakeResult:
    def all(self):
        return []


class _FakeSession:
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass
    # 재사용 가능한 메트릭 요약 조회(#255) — 이 흐름 테스트는 적재분이 없는 상태를 본다
    def execute(self, *_a, **_kw): return _FakeResult()


def test_collect_store_region_retries_once_then_succeeds(monkeypatch):
    from services import collector as C

    calls = {"n": 0}

    def flaky(region, cfg=None, fresh_metrics=None, fresh_window_end=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EndpointConnectionError(endpoint_url="http://localhost:4566")  # 일시 연결 블립
        return "INV"

    monkeypatch.setattr(C, "collect_region", flaky)
    monkeypatch.setattr(C, "persist_inventory", lambda inv, db: {"region": "r", "total": 1})
    res = C._collect_store_region("r", {"lookback_days": 14, "period_seconds": 3600}, lambda: _FakeSession())
    assert res["total"] == 1
    assert calls["n"] == 2  # 일시 오류 → 1회 재시도 후 성공


def test_collect_store_region_isolates_failure_and_records(monkeypatch):
    from services import collector as C

    calls = {"n": 0}

    def boom(region, cfg=None, fresh_metrics=None, fresh_window_end=None):
        calls["n"] += 1
        raise ClientError({"Error": {"Code": "InternalFailure"}}, "DescribeInstances")

    recorded = {}
    monkeypatch.setattr(C, "collect_region", boom)
    monkeypatch.setattr(
        C, "_record_failed_region",
        lambda region, cfg, exc, sf: recorded.update(region=region, reason=C._failure_reason(exc)),
    )
    res = C._collect_store_region("bad", {"lookback_days": 14, "period_seconds": 3600}, lambda: _FakeSession())
    assert res["status"] == "FAILED"      # 이 리전만 실패로 마감(예외 삼킴 → 다른 리전 계속)
    assert res["error"] == "InternalFailure"
    assert recorded == {"region": "bad", "reason": "InternalFailure"}
    assert calls["n"] == 1  # 비재시도성(InternalFailure)은 재시도하지 않고 즉시 실패


# ---- #255: 메트릭 재사용 판정 (no-DB) ----


def _summary(avg: float) -> MetricSummary:
    return MetricSummary(cpu_datapoints=336, cpu_avg=avg, cpu_max=avg + 1)


def test_reusable_returns_map_when_every_instance_covered():
    # 전량이 덮이면 CloudWatch 를 건너뛴다 — 5분 스캔이 1시간 입자를 12번 받는 것을 막는 지점.
    fresh = {"i-1": _summary(1.0), "i-2": _summary(2.0)}
    assert _reusable_summaries(["i-1", "i-2"], fresh) is fresh


def test_reusable_is_none_when_one_instance_missing():
    # 신규 인스턴스 1대라도 있으면 전량 재조회한다. get_metric_data 가 배치라 부분 재사용은
    # 호출 수를 줄이지 못하고 창만 어긋나게 한다.
    fresh = {"i-1": _summary(1.0)}
    assert _reusable_summaries(["i-1", "i-2"], fresh) is None


def test_reusable_is_none_without_fresh_rows():
    assert _reusable_summaries(["i-1"], None) is None
    assert _reusable_summaries(["i-1"], {}) is None


def test_reusable_is_none_when_no_instances():
    # 인스턴스가 없으면 애초에 조회하지 않으므로 재사용 판정도 성립하지 않는다.
    assert _reusable_summaries([], {"i-1": _summary(1.0)}) is None

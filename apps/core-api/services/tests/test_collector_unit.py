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

from services.collector import _paginate, _safe_describe  # noqa: E402


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


def test_safe_describe_passes_through_and_no_label():
    degraded: list[str] = []
    out = _safe_describe(lambda: [1, 2, 3], "launch_templates", degraded)
    assert out == [1, 2, 3]
    assert degraded == []


def test_safe_describe_absorbs_client_error_and_records_label():
    # LocalStack 의 Pro 전용 InternalFailure(=ClientError) 를 모사.
    err = ClientError(
        {"Error": {"Code": "InternalFailure", "Message": "not included within your LocalStack license"}},
        "DescribeAutoScalingGroups",
    )
    degraded: list[str] = []

    def _boom():
        raise err

    out = _safe_describe(_boom, "auto_scaling_groups", degraded)
    assert out == []
    assert degraded == ["auto_scaling_groups"]


def test_safe_describe_absorbs_botocore_error():
    # 엔드포인트 접속 실패(BotoCoreError 계열)도 흡수한다.
    degraded: list[str] = []

    def _boom():
        raise EndpointConnectionError(endpoint_url="http://localhost:4566")

    assert _safe_describe(_boom, "auto_scaling_groups", degraded) == []
    assert degraded == ["auto_scaling_groups"]


def test_safe_describe_does_not_swallow_unexpected_error():
    # AWS 예외가 아닌 버그성 예외까지 삼키면 안 된다.
    degraded: list[str] = []

    def _boom():
        raise KeyError("AutoScalingGroupARN")

    with pytest.raises(KeyError):
        _safe_describe(_boom, "auto_scaling_groups", degraded)
    assert degraded == []

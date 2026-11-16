"""활동 로그의 접속지를 어디서 읽는가(#589 D · request_origin.py · CLIENT_ADDRESS_SOURCE).

지키는 것: 둘 중 **하나만** 읽는다 — socket(기본)은 소켓의 상대 주소만, cloudfront-viewer-address 는
`CloudFront-Viewer-Address` 헤더만 · 그 헤더는 「주소:포트」 하나일 때만 받는다(IPv6 는 대괄호가 있든 없든) ·
헤더가 없거나 둘 이상이거나 읽히지 않으면 접속지는 비고 소켓(ALB) 주소로 물러서지 않는다 ·
`X-Forwarded-For` 는 어느 쪽에서도 읽지 않는다 · 목록 밖 설정값은 기동 때 거절한다 · 미들웨어가 설정대로 읽는다.
"""

from __future__ import annotations

from typing import get_args

import pytest
from config import Settings, get_settings
from fastapi.testclient import TestClient
from pydantic import ValidationError
from request_origin import (
    CLIENT_ADDRESS_SOURCES,
    CLOUDFRONT_VIEWER_ADDRESS,
    SOCKET,
    VIEWER_ADDRESS_HEADER,
    current_origin,
    origin_from_request,
)
from starlette.requests import Request

_ALB = "10.0.1.23"  # 소켓의 상대 주소 — 배포에서는 ALB 의 사설 주소다


def _request(headers: list[tuple[str, str]], client=(_ALB, 41000)) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        # 헤더 값은 선에서 latin-1 로 읽힌다(Starlette) — 테스트도 그 바이트로 싣는다
        "headers": [(k.lower().encode(), v.encode("latin-1")) for k, v in headers],
        "client": client,
    }
    return Request(scope)


def _viewer(*values: str, extra: tuple[tuple[str, str], ...] = ()) -> str | None:
    headers = [(VIEWER_ADDRESS_HEADER, value) for value in values] + list(extra)
    return origin_from_request(
        _request(headers), "r", address_source=CLOUDFRONT_VIEWER_ADDRESS
    ).source_ip


# --- cloudfront-viewer-address: 헤더의 주소 ------------------------------------------


@pytest.mark.parametrize(
    ("value", "address"),
    [
        ("198.51.100.10:46532", "198.51.100.10"),
        ("198.51.100.10:0", "198.51.100.10"),
        ("198.51.100.10:65535", "198.51.100.10"),
        # IPv6 꼴은 AWS 문서에 없다 — 대괄호가 있든 없든 받는다. 포트는 마지막 콜론 뒤다
        ("2001:db8::1:46532", "2001:db8::1"),
        ("2001:0db8:85a3:0000:0000:8a2e:0370:7334:46532", "2001:db8:85a3::8a2e:370:7334"),
        ("[2001:db8::1]:46532", "2001:db8::1"),
        (" 198.51.100.10:46532 ", "198.51.100.10"),
    ],
)
def test_viewer_address_is_the_address_before_the_port(value, address):
    assert _viewer(value) == address


@pytest.mark.parametrize(
    "value",
    [
        "",
        "198.51.100.10",  # 포트 없음
        "2001:db8::1",  # 포트 없음 — 끝 묶음을 포트로 떼면 주소가 안 된다
        "198.51.100.10:",
        ":46532",
        "198.51.100.10:65536",
        "198.51.100.10:465320",
        "198.51.100.10:-1",
        "198.51.100.10:+80",
        "198.51.100.10:4 6",
        "198.51.100.10:8²",  # 위첨자 2 — 숫자로 분류되지만 수로 읽히지 않는다(걸러 내지 않으면 요청이 깨진다)
        "198.51.100.10:http",
        "example.com:443",
        "198.51.100.300:46532",
        "[198.51.100.10:46532",
        "198.51.100.10]:46532",
        "fe80::1%eth0:46532",  # 영역 표시가 붙은 주소는 DB 주소 칸이 받지 않는다
        # 한 줄에 둘 — 어느 것이 CloudFront 가 쓴 것인지 알 수 없다
        "203.0.113.9:1, 198.51.100.10:46532",
        "198.51.100.10:46532, 203.0.113.9:1",
        "198.51.100.10:46532,203.0.113.9:1",
    ],
)
def test_unreadable_viewer_address_leaves_the_address_empty(value):
    assert _viewer(value) is None


def test_two_viewer_address_headers_leave_the_address_empty():
    assert _viewer("203.0.113.9:1", "198.51.100.10:46532") is None
    assert _viewer("198.51.100.10:46532", "198.51.100.10:46532") is None


def test_missing_viewer_address_does_not_fall_back_to_the_socket():
    assert _viewer() is None


def test_forwarded_for_is_never_read():
    forwarded = (("X-Forwarded-For", "203.0.113.9, 198.51.100.10"), ("X-Real-IP", "203.0.113.9"))
    assert _viewer(extra=forwarded) is None
    assert _viewer("198.51.100.10:46532", extra=forwarded) == "198.51.100.10"
    by_socket = origin_from_request(_request(list(forwarded)), "r", address_source=SOCKET)
    assert by_socket.source_ip == _ALB


# --- socket(기본): 소켓의 상대 주소 --------------------------------------------------


def test_socket_source_ignores_the_viewer_address_header():
    request = _request([(VIEWER_ADDRESS_HEADER, "198.51.100.10:46532")])
    assert origin_from_request(request, "r").source_ip == _ALB  # 기본값이 socket 이다
    assert origin_from_request(request, "r", address_source=SOCKET).source_ip == _ALB


def test_socket_source_drops_a_scoped_address():
    request = _request([], client=("fe80::1%eth0", 41000))
    assert origin_from_request(request, "r").source_ip is None


def test_unknown_source_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        origin_from_request(_request([]), "r", address_source="x-forwarded-for")


# --- 설정 ---------------------------------------------------------------------------


def test_setting_defaults_to_the_socket_and_refuses_values_off_the_list(monkeypatch):
    monkeypatch.delenv("CLIENT_ADDRESS_SOURCE", raising=False)
    assert Settings(_env_file=None).CLIENT_ADDRESS_SOURCE == SOCKET
    monkeypatch.setenv("CLIENT_ADDRESS_SOURCE", CLOUDFRONT_VIEWER_ADDRESS)
    assert Settings(_env_file=None).CLIENT_ADDRESS_SOURCE == CLOUDFRONT_VIEWER_ADDRESS
    for wrong in ("CloudFront-Viewer-Address", "x-forwarded-for", ""):
        monkeypatch.setenv("CLIENT_ADDRESS_SOURCE", wrong)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


def test_setting_values_are_exactly_the_sources_the_reader_handles():
    allowed = get_args(Settings.model_fields["CLIENT_ADDRESS_SOURCE"].annotation)
    assert set(allowed) == set(CLIENT_ADDRESS_SOURCES)
    for source in allowed:  # 설정이 받는 값은 읽는 쪽이 모두 처리한다(ValueError 가 아니다)
        origin_from_request(_request([]), "r", address_source=source)


# --- 미들웨어 -----------------------------------------------------------------------


def _probe(source: str | None, monkeypatch, headers: dict[str, str]):
    if source is None:
        monkeypatch.delenv("CLIENT_ADDRESS_SOURCE", raising=False)
    else:
        monkeypatch.setenv("CLIENT_ADDRESS_SOURCE", source)
    get_settings.cache_clear()
    try:
        from main import create_app

        app = create_app()
        seen: list = []

        @app.get("/__origin_probe")
        def probe():
            seen.append(current_origin())
            return {}

        # 테스트 클라이언트의 소켓 주소는 실제 주소 꼴로 준다 — 헤더가 아니라 소켓에서 읽으면 이 값이 나온다
        with TestClient(app, client=(_ALB, 41000)) as test_client:
            assert test_client.get("/__origin_probe", headers=headers).status_code == 200
        return seen[0]
    finally:
        get_settings.cache_clear()


def test_middleware_reads_the_viewer_address_when_the_setting_says_so(monkeypatch):
    headers = {VIEWER_ADDRESS_HEADER: "198.51.100.10:46532", "X-Forwarded-For": "203.0.113.9"}
    origin = _probe(CLOUDFRONT_VIEWER_ADDRESS, monkeypatch, headers)
    assert origin.source_ip == "198.51.100.10"
    # 헤더가 없으면 비운다 — 소켓(ALB) 주소가 접속지 칸에 들어가지 않는다
    assert _probe(CLOUDFRONT_VIEWER_ADDRESS, monkeypatch, {}).source_ip is None


def test_middleware_reads_the_socket_by_default(monkeypatch):
    headers = {VIEWER_ADDRESS_HEADER: "198.51.100.10:46532"}
    assert _probe(None, monkeypatch, headers).source_ip == _ALB

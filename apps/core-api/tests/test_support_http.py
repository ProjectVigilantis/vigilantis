# ==============================================================================
# [파일 설명]
# support_http.SignupBodyLimitMiddleware — 공개 접수 본문 상한 단위 테스트. (#552)
#
#   - 실제 계약 DTO(SignupRequestCreate) · require_origin · 오류 봉투 처리기를 쓰는 작은 앱으로
#     검증한다. 바깥에 request_context 를 본뜬 미들웨어를 두어 413 봉투가 요청 문맥(request_id ·
#     X-Request-ID)을 싣고 바깥 층을 통과하는지 본다. 라우터 뒤 서비스 · DB 는 없다.
#   - 조각 전달 · Content-Length 부재 · 선행 거절은 ASGI 호출을 직접 구동해 receive 횟수까지 본다.
#   - 상한 수치는 계약 상수에서 독립 계산해 유도 결과와 대조한다.
#   - 413 봉투 코드(ErrorCode.REQUEST_BODY_TOO_LARGE · 413 매핑)는 exceptions.py 가 가진다.
# ==============================================================================

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Annotated

import pytest
from auth.http import require_origin
from exceptions import register_error_handlers
from fastapi import Depends, FastAPI, Header, Request
from fastapi.testclient import TestClient
from pydantic import UUID4
from schemas.api.signup_requests import SignupRequestCreate
from schemas.api.support import SHORT_TEXT_MAX_LENGTH, SUPPORT_TEXT_MAX_LENGTH
from starlette.middleware.base import BaseHTTPMiddleware
from support_http import (
    SIGNUP_BODY_MAX_BYTES,
    SIGNUP_PATH,
    SignupBodyLimitMiddleware,
    derive_signup_body_max_bytes,
)

REQUEST_ID = "rid-test"
EMAIL_MAX_LENGTH = 254  # schemas.api.auth.Email
VALID_BODY = {
    "company_name": "Acme",
    "contact_name": "Kim",
    "email": "kim@example.com",
}


class _RequestContext(BaseHTTPMiddleware):
    """main.request_context 의 요청 문맥 부분만 본뜬다 — request_id 확정 + 응답 헤더."""

    async def dispatch(self, request, call_next):
        request.state.request_id = REQUEST_ID
        response = await call_next(request)
        response.headers["X-Request-ID"] = REQUEST_ID
        return response


def _build_app(max_body_bytes: int | None = None) -> tuple[FastAPI, list]:
    app = FastAPI()
    calls: list = []

    @app.post(SIGNUP_PATH, status_code=202, dependencies=[Depends(require_origin)])
    def create(
        body: SignupRequestCreate,
        idempotency_key: Annotated[UUID4, Header(alias="Idempotency-Key")],
    ) -> dict:
        calls.append(body)
        return {"request_id": "r"}

    @app.get(SIGNUP_PATH)
    def list_requests() -> dict:
        return {"items": []}

    @app.put(SIGNUP_PATH + "/{request_id}/status")
    async def change_status(request: Request) -> dict:
        return {"received": len(await request.body())}

    @app.post("/api/v1/other")
    async def other(request: Request) -> dict:
        return {"received": len(await request.body())}

    register_error_handlers(app)
    overrides = {} if max_body_bytes is None else {"max_body_bytes": max_body_bytes}
    app.add_middleware(SignupBodyLimitMiddleware, **overrides)
    app.add_middleware(_RequestContext)
    return app, calls


@pytest.fixture()
def app_calls():
    return _build_app()


@pytest.fixture()
def client(app_calls, allowed_origin):
    app, _ = app_calls
    headers = {
        "Origin": allowed_origin,
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    with TestClient(app, headers=headers) as test_client:
        yield test_client


def _scope(path: str, *, headers=None, root_path="", method="POST") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": root_path + path,
        "raw_path": (root_path + path).encode(),
        "root_path": root_path,
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
    }


def _asgi_post(app, chunks: list[bytes], *, path=SIGNUP_PATH, headers=None, root_path="", method="POST"):
    """ASGI 직접 구동 — Content-Length 없이 조각을 차례로 준다 → (status, headers, body, receive 횟수)."""
    sent: list[dict] = []
    queue = list(chunks)
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if queue:
            return {"type": "http.request", "body": queue.pop(0), "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(_scope(path, headers=headers, root_path=root_path, method=method), receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    response_headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], response_headers, body, receive_calls


def _escaped(text: str) -> str:
    return "".join(f"\\u{ord(ch):04x}" for ch in text)


def _worst_case_document() -> bytes:
    """계약이 허용하는 최대 문서 — 모든 필드 최대 길이, 모든 문자 최악 이스케이프, indent=4 · CRLF."""
    astral = "\\ud83d\\ude00"  # U+1F600 을 surrogate 쌍으로 — 코드 포인트 1 = 12바이트
    half = EMAIL_MAX_LENGTH // 2
    fields = {
        "company_name": astral * SHORT_TEXT_MAX_LENGTH,
        "contact_name": astral * SHORT_TEXT_MAX_LENGTH,
        "email": astral * (half - 1) + _escaped("@") + astral * (EMAIL_MAX_LENGTH - half),
        "phone": astral * SHORT_TEXT_MAX_LENGTH,
        "note": astral * SUPPORT_TEXT_MAX_LENGTH,
    }
    lines = [f'    "{_escaped(key)}": "{value}"' for key, value in fields.items()]
    return ("{\r\n" + ",\r\n".join(lines) + "\r\n}\r\n").encode("ascii")


def _assert_413(status: int, payload: bytes) -> dict:
    assert status == 413
    error = json.loads(payload)["error"]
    assert error["code"] == "REQUEST_BODY_TOO_LARGE"
    assert error["request_id"] == REQUEST_ID
    return error


# --- 상한 유도 ---------------------------------------------------------------


def test_cap_derives_from_contract_field_limits():
    """계약 상수(100·100·254·100·1000)에서 독립 계산한 값과 같다."""
    code_points = 3 * SHORT_TEXT_MAX_LENGTH + EMAIL_MAX_LENGTH + SUPPORT_TEXT_MAX_LENGTH
    assert code_points == 1554
    keys = ("company_name", "contact_name", "email", "phone", "note")
    expected = (
        code_points * 12
        + sum(map(len, keys)) * 6
        + len(keys) * 5
        + (len(keys) - 1)
        + 2
        + len(keys) * 7
        + 4
    )
    assert expected == 18946
    assert SIGNUP_BODY_MAX_BYTES == expected
    assert derive_signup_body_max_bytes(SignupRequestCreate) == expected


def test_worst_case_document_fits_and_is_accepted(client, app_calls):
    _, calls = app_calls
    document = _worst_case_document()
    assert len(document) <= SIGNUP_BODY_MAX_BYTES
    response = client.post(SIGNUP_PATH, content=document)
    assert response.status_code == 202, response.text
    assert len(calls) == 1
    assert len(calls[0].company_name) == SHORT_TEXT_MAX_LENGTH
    assert len(calls[0].note) == SUPPORT_TEXT_MAX_LENGTH
    assert len(calls[0].email) == EMAIL_MAX_LENGTH


# --- 경계 ---------------------------------------------------------------------


def test_exact_cap_forwards_and_one_more_byte_rejects(client, app_calls):
    _, calls = app_calls
    document = _worst_case_document()
    padded = document + b" " * (SIGNUP_BODY_MAX_BYTES - len(document))
    assert len(padded) == SIGNUP_BODY_MAX_BYTES

    assert client.post(SIGNUP_PATH, content=padded).status_code == 202
    assert len(calls) == 1

    response = client.post(SIGNUP_PATH, content=padded + b" ")
    _assert_413(response.status_code, response.content)
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert response.headers["content-type"].startswith("application/json")
    assert len(calls) == 1  # 라우터가 불리지 않았다


def test_oversized_rejected_before_origin_and_without_side_effects(app_calls):
    """출처가 틀려도 상한 거절이 먼저다 — 라우터(의존성 포함)가 돌지 않는다. 입력은 봉투에 실리지 않는다."""
    app, calls = app_calls
    body = b"x" * (SIGNUP_BODY_MAX_BYTES + 1)
    status, _, payload, _ = _asgi_post(
        app, [body], headers={"content-length": str(len(body)), "origin": "https://evil.example"}
    )
    error = _assert_413(status, payload)
    assert "x" * 8 not in error["message"]
    assert calls == []


def test_bad_origin_within_cap_still_403(client):
    response = client.post(SIGNUP_PATH, json=VALID_BODY, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_ORIGIN_REJECTED"


# --- 조각 · Content-Length --------------------------------------------------


def test_content_length_over_cap_rejects_without_reading(app_calls):
    app, calls = app_calls
    status, _, payload, receive_calls = _asgi_post(
        app, [b"{}"], headers={"content-length": str(SIGNUP_BODY_MAX_BYTES + 1)}
    )
    _assert_413(status, payload)
    assert receive_calls == 0
    assert calls == []


def test_chunks_without_content_length_are_summed(app_calls):
    """CL 없음 · 조각마다 상한 이내지만 합이 넘는다 → 넘는 조각에서 멈춘다."""
    app, calls = app_calls
    chunk = b"x" * 1024
    chunks = [chunk] * 100  # 102400 B ≫ 상한
    status, _, payload, receive_calls = _asgi_post(app, chunks)
    _assert_413(status, payload)
    assert receive_calls == SIGNUP_BODY_MAX_BYTES // 1024 + 1
    assert calls == []


@pytest.mark.parametrize(
    "declared",
    [
        "1" * 5000,  # int() 변환 한도(4300자리) 밖 — 자릿수로 413
        "9" * 10,
    ],
)
def test_huge_numeric_content_length_rejects_without_int_conversion(app_calls, declared):
    app, calls = app_calls
    status, _, payload, receive_calls = _asgi_post(app, [b"{}"], headers={"content-length": declared})
    _assert_413(status, payload)
    assert receive_calls == 0
    assert calls == []


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param("0" * 5000 + "2", id="leading-zeros"),  # 값은 2
        pytest.param("１２", id="fullwidth-digits"),  # isdigit 이지만 Content-Length 숫자가 아니다
        "abc",
        "-1",
    ],
)
def test_odd_content_length_is_ignored_and_counting_enforces(app_calls, allowed_origin, declared):
    """선행 판정이 못 쓰는 헤더는 500 없이 넘기고, 본문 합산이 상한을 지킨다."""
    app, calls = app_calls
    headers = {"content-length": declared, "content-type": "application/json", "origin": allowed_origin}
    status, _, _, _ = _asgi_post(app, [b"{}"], headers=headers)
    assert status == 422  # 상한 이내 → 라우터(계약 검증)까지 간다
    oversized = b"x" * (SIGNUP_BODY_MAX_BYTES + 1)
    status, _, payload, _ = _asgi_post(app, [oversized], headers={"content-length": declared})
    _assert_413(status, payload)
    assert calls == []


def _downstream_recorder():
    """라우터 자리의 ASGI 앱 — receive 로 받은 메시지를 disconnect 까지 기록하고 204 로 응답한다."""
    received: list[dict] = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            received.append(message)
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app, received


def test_many_empty_chunks_are_coalesced_into_one_message():
    """빈 조각 10만 개 — 메시지 객체가 아니라 바이트만 남고, 라우터는 합쳐진 메시지 하나를 본다."""
    downstream, received = _downstream_recorder()
    app = SignupBodyLimitMiddleware(downstream)
    chunks = [b""] * 100_000 + [b'{"a":', b"1}"]
    status, _, _, receive_calls = _asgi_post(app, chunks)
    assert status == 204
    assert receive_calls == len(chunks)  # 미들웨어가 조각 전부를 읽었다
    assert received == [{"type": "http.request", "body": b'{"a":1}', "more_body": False}]


def test_disconnect_mid_body_replays_partial_bytes_then_disconnect():
    downstream, received = _downstream_recorder()
    app = SignupBodyLimitMiddleware(downstream)
    messages = [
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b'{"comp', "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[dict] = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(app(_scope(SIGNUP_PATH), receive, send))
    assert received == [
        {"type": "http.request", "body": b'{"comp', "more_body": True},
        {"type": "http.disconnect"},
    ]
    assert messages == []


def test_lying_content_length_is_not_trusted(app_calls):
    app, calls = app_calls
    body = b"x" * (SIGNUP_BODY_MAX_BYTES + 1)
    status, _, payload, _ = _asgi_post(app, [body[:10], body[10:]], headers={"content-length": "10"})
    _assert_413(status, payload)
    assert calls == []


def test_valid_multi_chunk_body_is_replayed_intact(app_calls, allowed_origin):
    app, calls = app_calls
    document = json.dumps(VALID_BODY).encode()
    chunks = [document[i : i + 7] for i in range(0, len(document), 7)]
    assert len(chunks) > 3
    headers = {
        "origin": allowed_origin,
        "idempotency-key": str(uuid.uuid4()),
        "content-type": "application/json",
    }
    status, _, payload, _ = _asgi_post(app, chunks, headers=headers)
    assert status == 202, payload
    assert len(calls) == 1 and calls[0].email == VALID_BODY["email"]


def test_disconnect_mid_body_is_passed_through(app_calls, allowed_origin):
    """상한 이내에서 끊기면 라우터가 원래대로 disconnect 를 본다 — 413 도 202 도 아니다."""
    app, calls = app_calls
    sent: list[dict] = []
    messages = [
        {"type": "http.request", "body": b'{"company', "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    headers = {"origin": allowed_origin, "content-type": "application/json"}
    asyncio.run(app(_scope(SIGNUP_PATH, headers=headers), receive, send))
    assert calls == []
    assert messages == []  # disconnect 까지 라우터가 소비했다
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses != [413] and statuses != [202]


# --- 상한 이내 위임 -----------------------------------------------------------


def test_invalid_json_under_cap_is_delegated_422(client, app_calls):
    _, calls = app_calls
    response = client.post(SIGNUP_PATH, content=b"{not json")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert calls == []


def test_valid_body_forwards(client, app_calls):
    _, calls = app_calls
    response = client.post(SIGNUP_PATH, json=VALID_BODY)
    assert response.status_code == 202
    assert len(calls) == 1 and calls[0].company_name == "Acme"


# --- 경로 범위 ---------------------------------------------------------------


def test_other_paths_and_methods_unaffected(client):
    oversized = b"x" * (SIGNUP_BODY_MAX_BYTES + 1)
    assert client.post("/api/v1/other", content=oversized).json() == {"received": len(oversized)}
    put = client.put(SIGNUP_PATH + "/abc/status", content=oversized)
    assert put.json() == {"received": len(oversized)}
    get = client.get(SIGNUP_PATH, headers={"Content-Length": str(len(oversized))})
    assert get.status_code == 200


def test_trailing_slash_keeps_router_redirect(app_calls):
    app, calls = app_calls
    status, headers, _, receive_calls = _asgi_post(
        app, [b"x"], path=SIGNUP_PATH + "/", headers={"content-length": str(SIGNUP_BODY_MAX_BYTES + 1)}
    )
    assert status == 307
    assert headers["location"].endswith(SIGNUP_PATH)
    assert receive_calls == 0
    assert calls == []


def test_percent_encoded_and_root_path_variants_are_guarded(app_calls, client):
    app, calls = app_calls
    oversized = b"x" * (SIGNUP_BODY_MAX_BYTES + 1)
    response = client.post("/api/v1/signup%2Drequests", content=oversized)
    _assert_413(response.status_code, response.content)

    status, _, payload, _ = _asgi_post(app, [oversized], root_path="/svc")
    _assert_413(status, payload)
    assert calls == []


def test_trailing_newline_path_alias_is_guarded(app_calls, client):
    """Starlette 의 `^…$` 매처는 `$` 앞 꼬리 개행을 허용해 라우터가 받는다 — 가드도 같은 매처를 쓴다."""
    app, calls = app_calls
    oversized = b"x" * (SIGNUP_BODY_MAX_BYTES + 1)
    chunks = [b"x" * 1024] * 100

    status, _, payload, receive_calls = _asgi_post(app, chunks, path=SIGNUP_PATH + "\n")
    _assert_413(status, payload)
    assert receive_calls == SIGNUP_BODY_MAX_BYTES // 1024 + 1  # 넘는 조각에서 멈춘다
    assert calls == []

    response = client.post(SIGNUP_PATH + "%0A", content=oversized)
    _assert_413(response.status_code, response.content)
    assert calls == []

    # 상한 이내면 라우터가 원래대로 그 경로를 받는다(동작 변화 없음)
    response = client.post(SIGNUP_PATH + "%0A", json=VALID_BODY)
    assert response.status_code == 202
    assert len(calls) == 1


def test_budget_override_is_constructor_only():
    app, calls = _build_app(max_body_bytes=16)
    status, _, payload, _ = _asgi_post(app, [b"x" * 17])
    _assert_413(status, payload)
    status, _, _, _ = _asgi_post(app, [b"x" * 16], headers={"content-type": "application/json"})
    assert status != 413
    assert calls == []

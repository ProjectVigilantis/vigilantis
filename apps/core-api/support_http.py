# ==============================================================================
# [파일 설명]
# 공개 접수 HTTP 경계 — POST /api/v1/signup-requests 본문 크기 상한. (#552)
#
#   - 세션 없는 공개 경로라 JSON 파싱 전에 본문 바이트를 세어 상한을 넘으면 413 봉투
#     (ErrorCode.REQUEST_BODY_TOO_LARGE)로 거절한다. 다른 경로 · 메서드는 건드리지 않는다.
#   - 상한은 계약(schemas.api.signup_requests.SignupRequestCreate)의 필드 최대 길이에서 유도한다 —
#     아래 derive_signup_body_max_bytes 가 계산식이다. 임의 상수(16KB 등)가 아니다.
#   - Content-Length 는 선행 거절에만 쓴다(없거나 거짓이어도 조각 합산이 상한을 지킨다).
#     상한 이내의 본문은 받은 메시지를 그대로 라우터에 재생하고, 그 뒤 receive · disconnect 는
#     원래 채널에 위임한다. 상한을 넘기면 나머지 본문을 읽지 않고 라우터를 부르지 않는다.
#   - 예외를 던지지 않고 응답을 직접 보낸다 — FastAPI 는 본문 읽기 중 예외를 400 HTTPException 으로
#     바꾸고, request_context 는 미처리 예외를 500 으로 감싸기 때문이다. 그래서 미들웨어 순서에
#     의존하지 않는다.
#   - 입력 값 · 바이트는 기록하지 않는다. 요청 횟수 · 신원 보호가 아니다(WAF · 세션 몫).
# ==============================================================================

from __future__ import annotations

from exceptions import api_error_response
from schemas.api.errors import ErrorCode
from schemas.api.signup_requests import SignupRequestCreate
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.routing import compile_path, get_route_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SIGNUP_PATH = "/api/v1/signup-requests"
# 라우터와 같은 컴파일된 경로 매처 — 문자열 동등 비교는 라우터가 받아들이는 변형(`$` 앞 꼬리 개행
# 등)을 놓친다. 라우터가 이 경로로 보는 요청은 모두 여기에 걸린다.
_SIGNUP_PATH_REGEX = compile_path(SIGNUP_PATH)[0]

# JSON 직렬화 최악 경우 — 계약 max_length 는 코드 포인트 수(len)라, 코드 포인트 하나가 본문에서
# 차지할 수 있는 최대 바이트로 환산한다.
#   값: BMP 밖 코드 포인트를 이스케이프한 surrogate 쌍 "\ud83d\ude00" = 12바이트(원시 UTF-8 은 4).
#   키: ASCII 키를 "\u0063" 식으로 이스케이프하면 6바이트.
_ESCAPED_VALUE_CODE_POINT_BYTES = 12
_ESCAPED_KEY_CHAR_BYTES = 6
# 속성 구문 — 키 따옴표 2 + 콜론 1 + 값 따옴표 2
_PROPERTY_SYNTAX_BYTES = 5
# 통상 들여쓰기 직렬화(indent=4 · CRLF) — 속성마다 CRLF 2 + 들여쓰기 4 + 콜론 뒤 공백 1.
# 닫는 CRLF 2 + 본문 끝 개행 2. 이를 넘는 공백 채우기는 계약상 거절 대상이다.
_PROPERTY_WHITESPACE_BYTES = 7
_CLOSING_WHITESPACE_BYTES = 4


def _max_length(prop: dict) -> int:
    """JSON Schema 속성의 maxLength — Optional 은 anyOf 안의 string 쪽을 본다."""
    return max(c["maxLength"] for c in (prop, *prop.get("anyOf", ())) if "maxLength" in c)


def derive_signup_body_max_bytes(model: type[SignupRequestCreate] = SignupRequestCreate) -> int:
    """계약 DTO 의 JSON Schema 에서 상한을 유도한다.

    모든 필드가 최대 길이이고 모든 문자가 최악 이스케이프이며 통상 들여쓰기로 직렬화된 문서 크기다.
    계약(필드 수 · 최대 길이)이 바뀌면 함께 바뀐다.
    """
    properties = model.model_json_schema()["properties"]
    count = len(properties)
    value_code_points = sum(_max_length(prop) for prop in properties.values())
    key_chars = sum(len(key) for key in properties)
    return (
        value_code_points * _ESCAPED_VALUE_CODE_POINT_BYTES
        + key_chars * _ESCAPED_KEY_CHAR_BYTES
        + count * _PROPERTY_SYNTAX_BYTES
        + (count - 1)  # 쉼표
        + 2  # 중괄호
        + count * _PROPERTY_WHITESPACE_BYTES
        + _CLOSING_WHITESPACE_BYTES
    )


SIGNUP_BODY_MAX_BYTES = derive_signup_body_max_bytes()


def _declared_exceeds(declared: str | None, max_body_bytes: int) -> bool:
    """Content-Length 선행 거절 판정. ASCII 숫자가 아니면 판정하지 않는다(조각 합산이 지킨다).

    자릿수로 먼저 비교한다 — 헤더 길이 한도 안의 수천 자리 숫자를 int() 로 바꾸면
    정수 문자열 변환 한도(4300자리)에 걸려 500 이 된다.
    """
    if declared is None or not (declared.isascii() and declared.isdigit()):
        return False
    digits = declared.lstrip("0") or "0"
    if len(digits) > len(str(max_body_bytes)):
        return True
    return int(digits) > max_body_bytes


class SignupBodyLimitMiddleware:
    """순수 ASGI 미들웨어 — POST SIGNUP_PATH 본문을 max_body_bytes 이내로 묶는다.

    max_body_bytes 재정의는 테스트 예산용이다. 운영 등록은 기본값(SIGNUP_BODY_MAX_BYTES)으로 한다.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = SIGNUP_BODY_MAX_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    def _guarded(self, scope: Scope) -> bool:
        # 라우터와 같은 경로 해석(get_route_path: root_path 제거 + 컴파일된 매처)이다.
        # 꼬리 슬래시 변형은 라우터의 307 재지정 그대로 둔다(본문을 읽지 않는 응답).
        return (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and _SIGNUP_PATH_REGEX.match(get_route_path(scope)) is not None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._guarded(scope):
            await self.app(scope, receive, send)
            return

        if _declared_exceeds(Headers(scope=scope).get("content-length"), self.max_body_bytes):
            await self._reject(scope, receive, send)
            return

        # 조각은 바이트로 합친다 — 메시지 객체를 쌓으면 빈 조각을 무한히 보내는 클라이언트가
        # 바이트 상한과 무관하게 메모리를 키운다. 메모리는 상한 + 마지막 조각 이내다.
        body = bytearray()
        tail: Message | None = None  # 본문 도중 온 disconnect 등 — 라우터에 그대로 전달한다
        while True:
            message = await receive()
            if message["type"] != "http.request":
                tail = message
                break
            body += message.get("body", b"")
            if len(body) > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replay = iter(
            [{"type": "http.request", "body": bytes(body), "more_body": tail is not None}]
            + ([tail] if tail is not None else [])
        )

        async def replaying_receive() -> Message:
            message = next(replay, None)
            return message if message is not None else await receive()

        await self.app(scope, replaying_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = api_error_response(Request(scope), ErrorCode.REQUEST_BODY_TOO_LARGE)
        await response(scope, receive, send)

# ==============================================================================
# [파일 설명]  담당: 김승철 (BE)
# 요청 문맥 — 플랫폼 활동 로그(platform_events · #589)가 행마다 싣는 요청 식별자 · 접속지 · 브라우저 정보.
#
#   - 미들웨어(main.request_context)가 요청마다 한 번 정하고 ContextVar 로 전파한다. 기록 함수
#     (db/repositories/platform_events.py)가 여기서 읽으므로 기능 코드는 넘기지 않는다.
#   - 요청 밖(스케줄러 · 디스패처 루프)에서는 None 이다 — 그 행은 세 칸이 비어 있다.
#   - 접속지를 어디서 읽을지는 설정(CLIENT_ADDRESS_SOURCE · #589 D)이 정한다. 둘 중 **하나만** 읽는다:
#       · socket(기본) — 소켓의 상대 주소(request.client). 앱에 바로 붙는 로컬 · 테스트용이다. 프록시 뒤에서는
#         프록시(ALB) 주소가 찍힌다.
#       · cloudfront-viewer-address — CloudFront 가 오리진 요청에 싣는 `CloudFront-Viewer-Address`(「주소:포트」)의
#         주소. 배포(scripts/provision_platform_aws.py)가 켠다. 이 값을 믿는 근거는 앱 밖에 있다 — ALB 가
#         CloudFront 를 거친 요청만 받고(prefix list · 비밀 헤더), 그 헤더는 CloudFront 가 채운다.
#     `X-Forwarded-For` 는 어느 쪽에서도 읽지 않는다 — 앞부분을 클라이언트가 꾸밀 수 있다.
#   - cloudfront-viewer-address 인데 헤더가 없거나, 둘 이상이거나, 「주소:포트」로 읽히지 않으면 접속지는
#     **비운다.** 소켓 주소로 물러서지 않는다 — 그 값은 ALB 주소라 접속지 칸에 섞이면 읽는 쪽이 가를 수 없다.
#     헤더가 둘 이상이면 어느 것이 CloudFront 가 쓴 것인지 알 수 없어 고르지 않는다.
#   - 이 테이블은 고쳐 쓰지 않는다 — 전환 전에 남은 행의 접속지는 프록시가 본 주소 그대로다.
#   - 브라우저 정보는 잘라서 둔다(USER_AGENT_MAX_LENGTH). 주소로 읽히지 않는 값은 버린다.
# ==============================================================================

from __future__ import annotations

import ipaddress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from schemas.platform_events import REQUEST_ID_MAX_LENGTH, USER_AGENT_MAX_LENGTH
from starlette.requests import Request


@dataclass(frozen=True)
class RequestOrigin:
    request_id: Optional[str]
    source_ip: Optional[str]
    user_agent: Optional[str]


request_origin_var: ContextVar[Optional[RequestOrigin]] = ContextVar(
    "request_origin", default=None
)

#: 접속지를 읽는 곳(config.Settings.CLIENT_ADDRESS_SOURCE 의 값). 목록 밖 값은 설정 검증이 기동 때 거절한다.
SOCKET = "socket"
CLOUDFRONT_VIEWER_ADDRESS = "cloudfront-viewer-address"
CLIENT_ADDRESS_SOURCES = (SOCKET, CLOUDFRONT_VIEWER_ADDRESS)

VIEWER_ADDRESS_HEADER = "CloudFront-Viewer-Address"


def _ip(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    # 영역 표시(fe80::1%eth0)가 붙은 주소는 DB 주소 칸(INET)이 받지 않는다 — 쓰다가 요청이 깨지지 않게 버린다
    if getattr(parsed, "scope_id", None):
        return None
    return str(parsed)


def _viewer_address(request: Request) -> Optional[str]:
    """`CloudFront-Viewer-Address`(「주소:포트」)의 주소. 헤더가 정확히 하나이고 형식이 맞을 때만 돌려준다.

    포트는 마지막 콜론 뒤다. AWS 문서의 예는 IPv4(198.51.100.10:46532)뿐이고 IPv6 꼴은 적혀 있지 않다 — 그래서
    대괄호 없는 꼴(2001:db8::1:46532)과 대괄호로 싼 꼴([2001:db8::1]:46532)을 모두 받는다. 포트 없는 값은
    받지 않는다(IPv6 에서 주소 끝과 포트를 가를 수 없다)."""
    values = request.headers.getlist(VIEWER_ADDRESS_HEADER)
    if len(values) != 1:
        return None
    host, colon, port = values[0].strip().rpartition(":")
    if not colon or not (port.isascii() and port.isdigit() and len(port) <= 5 and int(port) <= 65535):
        return None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return _ip(host)


def _user_agent(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # 제어 문자는 빼 둔다 — 화면 · 내보내기에서 줄이 갈리지 않게
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:USER_AGENT_MAX_LENGTH] or None


def origin_from_request(
    request: Request, request_id: Optional[str], *, address_source: str = SOCKET
) -> RequestOrigin:
    if address_source == CLOUDFRONT_VIEWER_ADDRESS:
        source_ip = _viewer_address(request)
    elif address_source == SOCKET:
        client = request.client
        source_ip = _ip(client.host if client else None)
    else:  # 설정 검증을 거치지 않은 값 — 어느 쪽인지 모르면 읽지 않는다
        raise ValueError(f"unknown client address source: {address_source!r}")
    return RequestOrigin(
        request_id=(request_id or None) and request_id[:REQUEST_ID_MAX_LENGTH],
        source_ip=source_ip,
        user_agent=_user_agent(request.headers.get("user-agent")),
    )


def current_origin() -> Optional[RequestOrigin]:
    return request_origin_var.get()

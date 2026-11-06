# ==============================================================================
# [파일 설명]  담당: 김승철 (BE)
# 요청 문맥 — 플랫폼 활동 로그(platform_events · #589)가 행마다 싣는 요청 식별자 · 접속지 · 브라우저 정보.
#
#   - 미들웨어(main.request_context)가 요청마다 한 번 정하고 ContextVar 로 전파한다. 기록 함수
#     (db/repositories/platform_events.py)가 여기서 읽으므로 기능 코드는 넘기지 않는다.
#   - 요청 밖(스케줄러 · 디스패처 루프)에서는 None 이다 — 그 행은 세 칸이 비어 있다.
#   - 접속지는 지금 소켓의 상대 주소(request.client)다. CloudFront → ALB 뒤에서는 ALB 주소가 찍힌다 —
#     실제 접속지를 믿고 읽는 설정은 #589 D(Q3)에서 이 함수 하나만 바꾼다.
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


def _ip(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _user_agent(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # 제어 문자는 빼 둔다 — 화면 · 내보내기에서 줄이 갈리지 않게
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:USER_AGENT_MAX_LENGTH] or None


def origin_from_request(request: Request, request_id: Optional[str]) -> RequestOrigin:
    client = request.client
    return RequestOrigin(
        request_id=(request_id or None) and request_id[:REQUEST_ID_MAX_LENGTH],
        source_ip=_ip(client.host if client else None),
        user_agent=_user_agent(request.headers.get("user-agent")),
    )


def current_origin() -> Optional[RequestOrigin]:
    return request_origin_var.get()

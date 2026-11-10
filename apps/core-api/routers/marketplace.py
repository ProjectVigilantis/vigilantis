# ==============================================================================
# [파일 설명]  담당: 김세혁 (BE · Marketplace 실연동)
# POST /api/v1/marketplace/register — AWS Marketplace 등록 랜딩(ADR-0012 2차 개정). 처리는 marketplace_sync.register.
#
#   - 구매자 브라우저가 AWS 에서 폼으로 보낸다(`x-amzn-marketplace-token` · application/x-www-form-urlencoded).
#     세션 · Origin 검사가 없다 — 토큰 자체를 AWS 가 확인하고, 계정 ID 는 AWS 응답만 믿는다.
#   - 응답은 화면으로 보내는 303 이다. 같은 AWS 계정의 테넌트가 있으면 구독 화면, 없으면 가입 화면이다.
#     토큰을 받지 않으면 가입 화면에 실패 표시를 붙인다(이유는 로그에만 남긴다).
#   - 상품 코드가 비어 있으면 경로가 없는 것과 같다(404).
# ==============================================================================

from __future__ import annotations

from urllib.parse import parse_qs, urlencode

import marketplace_sync
from config import MarketplaceSettings, get_marketplace_settings
from db.session import get_db
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"])

TOKEN_FIELD = "x-amzn-marketplace-token"
#: 토큰 하나를 담은 폼보다 크게 받을 이유가 없다.
_MAX_BODY = 16 * 1024


def _with_query(url: str, **params: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"


@router.post("/register", status_code=303, include_in_schema=False)
async def register_landing(
    request: Request,
    db: Session = Depends(get_db),
    settings: MarketplaceSettings = Depends(get_marketplace_settings),
) -> RedirectResponse:
    if not settings.enabled:
        raise HTTPException(status_code=404)
    body = await request.body()
    token = ""
    if len(body) <= _MAX_BODY:
        values = parse_qs(body.decode("utf-8", errors="replace")).get(TOKEN_FIELD) or []
        token = values[0] if len(values) == 1 else ""
    # AWS 호출과 DB 쓰기는 블로킹이다 — 이벤트 루프(WebSocket 등)를 막지 않게 스레드에서 돈다
    result = await run_in_threadpool(marketplace_sync.register, db, token, settings=settings)
    if not result.accepted:
        target = _with_query(settings.MARKETPLACE_SIGNUP_URL, marketplace="failed")
    elif result.tenant_id is None:
        target = _with_query(settings.MARKETPLACE_SIGNUP_URL, marketplace="registered")
    else:
        target = settings.MARKETPLACE_SUBSCRIPTION_URL
    return RedirectResponse(target, status_code=303)

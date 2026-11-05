"""GET /api/v1/threat-events (#572 10번) — 실제 PostgreSQL.

지키는 것: 두 유형의 응답 모양(payload 는 정규화 모델 그대로) · 최신순(같은 시각은 id 내림차순) · 기간
[지금 − days, 지금](미래 시각 제외) · days 는 1 · 7 · 30 만 · 상한 500건과 실제 건수(total) · 범위(고객사 =
자기 고객사, 담당 직원 = 배정 고객사, 중앙 관리계정 = 전체 · 소속 없는 이벤트 포함) · 관제 리전((고객사, ARN 리전)
쌍) · 정지 고객사도 조회 · 미인증 401 · 계약 검증.

같은 DB 를 다른 테스트도 쓰므로 조회 시각(`_now`)을 이 테스트만의 먼 미래 무작위 시각으로 옮겨, 「전체」를
보는 중앙 관리계정도 이 테스트가 넣은 이벤트만 보게 한다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from db import models
from pydantic import ValidationError
from schemas.api.auth import PlatformRole, UserRole
from schemas.api.tenants import TenantStatus
from schemas.api.threat_events import THREAT_EVENTS_LIMIT, ThreatEventsResponse

URL = "/api/v1/threat-events"
REGION = "ap-northeast-2"  # make_tenant 의 관제 리전 · 설정 리전
TOKYO = "ap-northeast-1"
LEGACY_ACCOUNT = "123456789012"

SSH = {"source_ip": "203.0.113.7", "failed_attempt_count": 12, "window_seconds": 300}
OPEN = {"protocol": "tcp", "from_port": 22, "to_port": 22, "source_cidr": "0.0.0.0/0"}


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def now(monkeypatch):
    """이 테스트만의 조회 시각 — 다른 테스트의 이벤트와 겹치지 않는 먼 미래의 무작위 시각."""
    at = datetime(2031, 6, 1, tzinfo=UTC) + timedelta(minutes=uuid.uuid4().int % 10**6)
    monkeypatch.setattr("routers.threat_events._now", lambda: at)
    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])
    return at


@pytest.fixture()
def event(db):
    """위협 이벤트 1건을 직접 넣는다(쓰기 경로는 test_incident_intake 가 본다) → threat_event_id."""

    def _event(tenant_id, at, *, ssh=True, region=REGION, account=None, event_id=None) -> str:
        if account is None:
            account = db.get(models.Tenant, tenant_id).aws_account_id if tenant_id else LEGACY_ACCOUNT
        key = uuid.uuid4().hex
        row = models.ThreatEvent(
            threat_event_id=event_id or str(uuid.uuid4()),
            tenant_id=tenant_id,
            source_event_id=key,
            event_type="SSH_BRUTE_FORCE" if ssh else "OPEN_IP",
            target_arn=(
                f"arn:aws:ec2:{region}:{account}:instance/i-{key[:12]}" if ssh
                else f"arn:aws:ec2:{region}:{account}:security-group/sg-{key[:12]}"
            ),
            payload=SSH if ssh else OPEN,
            deduplication_key=key,
            occurred_at=at,
        )
        db.add(row)
        db.flush()
        return row.threat_event_id

    return _event


def _get(client, days=7):
    response = client.get(URL, params={"days": days})
    assert response.status_code == 200, response.text
    body = response.json()
    ThreatEventsResponse.model_validate(body)
    return body


def _ids(body):
    return [i["threat_event_id"] for i in body["items"]]


# --- 응답 모양 · 순서 -------------------------------------------------------------


def test_고객사_계정은_자기_고객사의_이벤트를_최신순으로_받는다(client_pg, login_as, make_tenant, event, now):
    mine, other = make_tenant(), make_tenant()
    older = event(mine, now - timedelta(hours=3), ssh=False)
    newer = event(mine, now - timedelta(hours=1))
    event(other, now - timedelta(hours=2))
    event(None, now - timedelta(hours=2))
    login_as(UserRole.VIEWER, tenant_id=mine)

    body = _get(client_pg, days=1)

    assert body["window_start"] == _z(now - timedelta(days=1))
    assert body["window_end"] == _z(now)
    assert body["total"] == 2
    assert body["items"][0] == {
        "threat_event_id": newer, "event_type": "SSH_BRUTE_FORCE",
        "target_arn": body["items"][0]["target_arn"], "occurred_at": _z(now - timedelta(hours=1)),
        "payload": SSH,
    }
    assert body["items"][1]["threat_event_id"] == older
    assert body["items"][1]["event_type"] == "OPEN_IP"
    assert body["items"][1]["payload"] == OPEN


def test_같은_시각은_id_내림차순이다(client_pg, login_as, make_tenant, event, now):
    mine = make_tenant()
    at = now - timedelta(minutes=5)
    ids = sorted(str(uuid.uuid4()) for _ in range(3))
    for i in ids:
        event(mine, at, event_id=i)
    login_as(UserRole.ADMIN, tenant_id=mine)

    assert _ids(_get(client_pg)) == list(reversed(ids))


def test_기간은_지금부터_days일_전까지이고_미래_시각은_빠진다(client_pg, login_as, make_tenant, event, now):
    mine = make_tenant()
    edge = event(mine, now - timedelta(days=1))
    event(mine, now - timedelta(days=1, seconds=1))
    at_now = event(mine, now)
    event(mine, now + timedelta(seconds=1))
    login_as(UserRole.ADMIN, tenant_id=mine)

    assert _ids(_get(client_pg, days=1)) == [at_now, edge]
    assert _get(client_pg, days=7)["total"] == 3


@pytest.mark.parametrize("days", ["0", "2", "31", "-1", "x", ""])
def test_days는_1_7_30만_받는다(client_pg, now, days):
    response = client_pg.get(URL, params={"days": days})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_days가_없으면_422(client_pg, now):
    response = client_pg.get(URL)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_상한을_넘으면_500건만_싣고_total이_실제_건수를_알린다(client_pg, login_as, make_tenant, event, now):
    mine = make_tenant()
    newest = [event(mine, now - timedelta(seconds=i)) for i in range(THREAT_EVENTS_LIMIT + 1)]
    login_as(UserRole.ADMIN, tenant_id=mine)

    body = _get(client_pg, days=1)

    assert body["total"] == THREAT_EVENTS_LIMIT + 1
    assert _ids(body) == newest[:THREAT_EVENTS_LIMIT]


def test_이벤트가_없으면_빈_목록이다(client_pg, login_as, make_tenant, now):
    login_as(UserRole.ADMIN, tenant_id=make_tenant())

    body = _get(client_pg, days=30)

    assert (body["total"], body["items"]) == (0, [])


# --- 범위 · 관제 리전 --------------------------------------------------------------


def test_관제하지_않는_리전의_이벤트는_빠진다(client_pg, login_as, make_tenant, event, now):
    mine = make_tenant()  # 관제 리전 [REGION]
    seoul = event(mine, now - timedelta(hours=1))
    event(mine, now - timedelta(hours=1), region=TOKYO)
    login_as(UserRole.ADMIN, tenant_id=mine)

    assert _ids(_get(client_pg)) == [seoul]


def test_담당_직원은_배정_고객사의_이벤트만_받는다(client_pg, login_as, make_tenant, event, now):
    mine, other = make_tenant(), make_tenant()
    assigned = event(mine, now - timedelta(hours=1))
    event(other, now - timedelta(hours=1))
    event(None, now - timedelta(hours=1))
    login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=(mine,))

    assert _ids(_get(client_pg)) == [assigned]


def test_배정이_없는_담당_직원은_빈_목록이다(client_pg, login_as, make_tenant, event, now):
    event(make_tenant(), now - timedelta(hours=1))
    login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)

    assert _get(client_pg)["total"] == 0


def test_중앙_관리계정은_소속_없는_이벤트까지_모두_받는다(client_pg, make_tenant, event, now):
    a, b = make_tenant(), make_tenant()
    seen = {
        event(a, now - timedelta(hours=1)),
        event(b, now - timedelta(hours=2)),
        event(None, now - timedelta(hours=3)),
    }
    event(None, now - timedelta(hours=3), region=TOKYO)  # 소속 없는 기록은 설정 리전에서만 본다

    assert set(_ids(_get(client_pg))) == seen


def test_정지된_고객사도_조회된다(client_pg, login_as, make_tenant, event, now):
    suspended = make_tenant(TenantStatus.SUSPENDED)
    mine = event(suspended, now - timedelta(hours=1))
    login_as(UserRole.ADMIN, tenant_id=suspended)

    assert _ids(_get(client_pg)) == [mine]


def test_로그인하지_않으면_401(client_pg_anon):
    response = client_pg_anon.get(URL, params={"days": 7})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


# --- 계약 -------------------------------------------------------------------------


def _item(at: datetime, event_id: str | None = None) -> dict:
    return {
        "threat_event_id": event_id or str(uuid.uuid4()), "event_type": "SSH_BRUTE_FORCE",
        "target_arn": "arn:aws:ec2:ap-northeast-2:000000000000:instance/i-0", "occurred_at": _z(at),
        "payload": SSH,
    }


def _response(items, *, total=None, start=None, end=None) -> dict:
    end = end or datetime(2031, 1, 2, tzinfo=UTC)
    return {
        "window_start": _z(start or end - timedelta(days=1)), "window_end": _z(end),
        "total": len(items) if total is None else total, "items": items,
    }


END = datetime(2031, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    "body",
    [
        _response([_item(END)], total=0),  # total < 항목 수
        _response([_item(END)], total=2),  # 상한 전인데 덜 실음
        _response([_item(END - timedelta(hours=2)), _item(END - timedelta(hours=1))]),  # 오래된 것부터
        _response([_item(END + timedelta(seconds=1))]),  # 기간 밖
        _response([], start=END),  # 기간이 비었다
        _response([{**_item(END), "payload": OPEN}]),  # 유형과 다른 payload
        _response([{**_item(END), "tenant_id": None}]),  # 계약 밖 필드
    ],
)
def test_계약은_어긋난_응답을_받지_않는다(body):
    with pytest.raises(ValidationError):
        ThreatEventsResponse.model_validate(body)


def test_계약은_상한에_잘린_응답을_받는다():
    items = [_item(END - timedelta(seconds=i)) for i in range(THREAT_EVENTS_LIMIT)]
    ThreatEventsResponse.model_validate(_response(items, total=THREAT_EVENTS_LIMIT + 7))

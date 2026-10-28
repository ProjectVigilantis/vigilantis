"""T-5 테넌트 격리 회귀 테스트 — 골격(무조건 보류).

검증 층은 **DB 행 단위 격리**(pytest + PostgreSQL)다. 플랫폼(ECS·RDS) 위 E2E는
Cognito 전달·고객 계정 연결(#523) 뒤의 별도 카드이며 이 파일의 범위가 아니다.
ADR-0012 §3 ②(IAM 회수 = 앱 밖 운영자 권한)도 이 파일 밖이다 — 아래 「정지 409」는
앱 안 상태 전이만 본다.

판정 순서(계약 원문 `packages/schemas/api/tenants.py` 머리말 「기존 경로의 테넌트 범위」):
    역할(403) → 범위(404) → 같은 Key 재요청 → 정지(409) → 상태(409)

전제(아직 `dev`에 없음 — 들어오면 모듈 skip을 떼고 본문을 채운다):
    ① `CurrentUser.tenant_id` + 플랫폼 운영자 표시(T-1 PR 4 · `auth/service.py`)
    ② 인시던트 목록·상세·실행·종료 경로의 테넌트 범위(T-4 2단계 · `routers/incidents.py`)
    ③ `make_auth_session(role, tenant_id=...)` 픽스처 인자(①과 함께 오지 않으면 여기서 conftest에 추가)

이슈: #529(T-5) — 기대값 17행·전제·열린 질문의 원천.

시드 골격: 테넌트 A·B(`test_tenants_api._seed` 재사용) · 세션 A-OPERATOR·A-VIEWER·B-OPERATOR·
플랫폼 운영자 · 인시던트 A 1건(대조군)·B 1건(EXECUTABLE 후보 1) · 실행 경로의 디스패처는
`test_actions_api.py`의 monkeypatch 패턴으로 끊고 `executions` 행 수를 직접 센다.
"""

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "무조건 보류 — T-5 전제 ①(CurrentUser.tenant_id · T-1 PR 4) "
        "②(인시던트 경로 테넌트 범위 · T-4 2단계)가 dev에 들어오면 켠다(#529)"
    )
)

_PENDING = "T-5 2차 PR(전제 ①② 머지 뒤)에서 채운다"


# --- 범위: A 세션 → B 인시던트 -------------------------------------------------


def test_list_hides_other_tenants_incidents(client_pg, db, login_as):
    """1. A-OPERATOR `GET /incidents` → 200, items에 A만, B `incident_id` 없음."""
    raise NotImplementedError(_PENDING)


def test_detail_of_other_tenant_is_404(client_pg, db, login_as):
    """2. A-OPERATOR `GET /incidents/{B}` → 404 `INCIDENT_NOT_FOUND`(403 아님 — 존재가 새지 않는다)."""
    raise NotImplementedError(_PENDING)


def test_execute_on_other_tenant_is_404_and_writes_nothing(client_pg, db, login_as, make_executable, monkeypatch):
    """3. A-OPERATOR `POST /actions/execute`(B 후보) → 404 · `executions` 행 수 변화 0."""
    raise NotImplementedError(_PENDING)


def test_resolve_of_other_tenant_is_404_and_changes_nothing(client_pg, db, login_as):
    """4. A-OPERATOR `POST /incidents/{B}/resolve` → 404 · B 상태 그대로 · WS 발행 0."""
    raise NotImplementedError(_PENDING)


# --- 역할이 범위보다 먼저 -------------------------------------------------------


def test_viewer_cannot_execute_own_tenant_403(client_pg, db, login_as, make_executable):
    """5. A-VIEWER `POST /actions/execute`(A 후보) → 403 `AUTH_FORBIDDEN`."""
    raise NotImplementedError(_PENDING)


def test_viewer_on_other_tenant_is_403_not_404(client_pg, db, login_as):
    """6. A-VIEWER `POST /incidents/{B}/resolve` → 403(404 아님 — 역할 검사가 대상을 읽기 전)."""
    raise NotImplementedError(_PENDING)


# --- 플랫폼 운영자 · 대조군 -----------------------------------------------------


def test_platform_operator_lists_every_tenant(client_pg, db, login_as):
    """7. 플랫폼 운영자 `GET /incidents` → 200 · A+B(ADR-0012 §4)."""
    raise NotImplementedError(_PENDING)


def test_platform_operator_reads_and_resolves_other_tenant(client_pg, db, login_as):
    """8. 플랫폼 운영자 `GET /incidents/{B}` · `resolve` → 200."""
    raise NotImplementedError(_PENDING)


def test_own_tenant_detail_and_resolve_are_200(client_pg, db, login_as):
    """9. A-OPERATOR `GET /incidents/{A}` · `resolve` → 200(격리가 자기 것까지 막지 않는다)."""
    raise NotImplementedError(_PENDING)


# --- 정지(SUSPENDED) --------------------------------------------------------------


def test_suspended_tenant_execute_is_409_for_its_operator(client_pg, db, login_as, make_executable):
    """10. B 정지 · B-OPERATOR `POST /actions/execute`(B) → 409 `TENANT_SUSPENDED`."""
    raise NotImplementedError(_PENDING)


def test_suspended_tenant_execute_is_409_for_platform_operator(client_pg, db, login_as, make_executable):
    """11. B 정지 · 플랫폼 운영자 `POST /actions/execute`(B) → 409(운영자 포함)."""
    raise NotImplementedError(_PENDING)


def test_suspended_tenant_resolve_is_409_for_its_operator(client_pg, db, login_as):
    """12. B 정지 · B-OPERATOR `resolve`(B) → 409."""
    raise NotImplementedError(_PENDING)


def test_suspended_tenant_resolve_is_200_for_platform_operator(client_pg, db, login_as):
    """13. B 정지 · 플랫폼 운영자 `resolve`(B) → 200."""
    raise NotImplementedError(_PENDING)


def test_other_tenants_suspension_is_404_not_409(client_pg, db, login_as, make_executable):
    """14. B 정지 · A-OPERATOR `POST /actions/execute`(B) → **404**(범위 → 정지 순서 · 정지 사실이 새지 않는다)."""
    raise NotImplementedError(_PENDING)


# --- 회귀 1건 · 자산·메트릭 · WS -------------------------------------------------


def test_unauthenticated_list_is_401(client_pg_anon):
    """15. 비로그인 `GET /incidents` → 401(#502 회귀 방지 1건)."""
    raise NotImplementedError(_PENDING)


def test_assets_and_metrics_hide_other_tenant(client_pg, db, login_as):
    """16. A-OPERATOR `GET /assets` · `GET /metrics/timeseries` → B 자산 없음(필터 · 404 자리 없음)."""
    raise NotImplementedError(_PENDING)


def test_ws_does_not_deliver_other_tenants_events(ws_client, db):
    """17. A WS 연결 중 B 인시던트 갱신 → 이벤트 미수신."""
    raise NotImplementedError(_PENDING)

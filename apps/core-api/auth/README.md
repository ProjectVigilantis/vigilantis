# auth — 로그인 · 서버 세션 · 계정 종류 권한 (Post-MVP T-1, #492)

Cognito Admin 인증 API로 로그인을 진행하고, **이번 로그인에서 TOTP 단계 성공 응답을 서버가 직접 받은 뒤에만** 앱 서버 세션을 발급한다. 공개 계약은 `packages/schemas/api/auth.py`, 오류 코드는 `packages/schemas/api/errors.py`다.

이 문서는 PR 1(로그인 기반), PR 2(API · WebSocket 접근 보호 · 역할 검사), PR 3(ADMIN 사용자 관리 · 사용자당 세션 1개 · 비밀번호 재설정 · 새 비밀번호 정책 · 사용자 관리 이력), 계정 종류(`account_type`)에 따른 테넌트 · 플랫폼 권한(§계정 종류와 테넌트 소속), PR 5(플랫폼 계정의 권한 등급 `platform_role`과 담당 고객 배정 · 업무 API의 고객 범위 · 직원 관리 API · 운영 CLI의 직원 생성과 첫 중앙 관리자 지정 — §계정 종류와 테넌트 소속 · §접근 보호 · §직원 관리 · §플랫폼 계정 CLI)의 백엔드 기준이다. 웹 화면(SSR) 쪽 처리는 `apps/web` 범위다 — PR 3 화면(사용자 관리 · 비밀번호 재설정 · 새 비밀번호 조건 · 한글 입력 경고 · 세션 종료 안내)은 최소 구현이고 시각 다듬기는 포함하지 않으며, `apps/web/src/lib/session-expiry.ts`의 시계 보정은 PR 3 범위 밖이다. 계정 종류에 따른 화면 기능(`apps/web/src/lib/home-path.ts` · `components/gnb.tsx` 포함)은 §화면이 다룬다. 분실한 TOTP의 재설정은 PR 3에서 제외했고, 플랫폼 등급과 직원 관리도 TOTP 정책 · 복구를 바꾸지 않는다(TOTP 분실 복구는 [#515](https://github.com/ProjectVigilantis/vigilantis/issues/515) 범위, §운영 주의점). 실제 Cognito · HTTPS 배포 환경 · 환경 이전에서의 검증과 환경별 설정 · 배포 권한 반영은 이 문서가 보증하지 않는 배포 전제다(§User Pool · 앱 클라이언트 전제 · §테스트).

근거 표시: **[표준]** 외부 표준 · 공식 문서, **[팀 결정]** 이슈 #492 확정 정책, **[판단]** 이 구현의 선택. 동작의 근거는 현재 코드와 자동 테스트(대역 · 실제 PostgreSQL)이며, 자동 테스트가 보장하지 않는 범위(실제 Cognito · HTTPS 배포 환경)는 §테스트에 적었다.

## 구조

| 파일 | 책임 |
| --- | --- |
| `cognito.py` | boto3 `cognito-idp` 어댑터. `SECRET_HASH` 계산, Cognito 오류를 `AUTH_*` 코드와 1:1인 도메인 예외로 분류. (PR 3) 관리 조회 · 초대(`AdminGetUser` · `AdminCreateUser`)와 재설정(`ForgotPassword` · `ConfirmForgotPassword`), 호출 종류별 오류 분류 |
| `login_flow.py` | 로그인 상태 머신. 세션 발급 조건 판정, 코드 시도 상한(흐름별 · 사용자별), refresh token 회수. (PR 3) 최초 비밀번호 변경은 Cognito 호출 전에 `password_policy`로 확인 |
| `flow_store.py` | 진행 중인 로그인과 사용자별 코드 실패 횟수의 프로세스 메모리 보관(TTL · 슬라이딩 윈도). DB · 로그에 남기지 않는다 |
| `sessions.py` | 서버 세션 발급 · 조회 · 활동 갱신 · 회수. 설정을 읽지 않고 commit하지 않는다. (PR 3) 사용자 단위 일괄 회수(`revoke_user_sessions`) |
| `service.py` | 트랜잭션 소유자. 로그인 완료(사용자 조회 · 세션 발급), 현재 사용자, 활동, 로그아웃. (PR 3) 로그인 완료를 `app_users` 잠금으로 직렬화하고 그 사용자의 이전 세션을 전부 회수. (#492) `CurrentUser`에 `account_type` · `tenant_id`와 내부 전용 `tenant_status`. UNASSIGNED는 로그인 완료 403 · 세션 판정 401. `CurrentUser.platform_role`과 `assigned_tenant_ids`(TENANT_MANAGER의 담당 고객, 정규 UUID 튜플) — 판정마다 DB의 현재 값을 읽는다(`/me`에도 싣는다) |
| `password_policy.py` | (PR 3) 새 비밀번호 정책 순수 함수 `check_password` — 최초 비밀번호 변경 · 재설정 확정 공용 |
| `password_reset.py` | (PR 3) 비밀번호 재설정 요청 · 확정, 확정 성공 뒤 앱 세션 회수 |
| `admin_service.py` | (PR 3) ADMIN 사용자 관리 트랜잭션 소유자 — 초대 · 재발송 · 역할 · 상태 · 세션 회수 · 이력 조회. 잠금 · 행위자 재판정 · 마지막 활성 ADMIN 보호. (#492) 행위자의 고객 범위(`auth.access.tenant_scope_ids`)에 따른 관리 범위, 초대 대상 테넌트 결정 · ACTIVE 확인, 정지 테넌트 행위자의 쓰기 거절, 테넌트별 마지막 활성 ADMIN, 기존 계정 연결(`recover_existing`)의 플랫폼 관리 권한자 한정. 행위자 권한은 `auth.access`의 같은 판정(`require_admin` · `require_central_admin`)으로 가르고, 직원 관리와 이력 응답(`events_response`) · 잠금 트랜잭션을 공유한다 |
| `staff_service.py` | (#492) 직원 관리 트랜잭션 소유자 — 목록 · 등급 · 상태 · 담당 고객 배정 · 이력. 대상은 PLATFORM만, 자기 변경 금지(잠금 안 행위자 재판정과 함께 ACTIVE 중앙 관리자를 한 명 이상 남긴다), 비활성화의 세션 회수, 등급 변경 때 배정 비움(§직원 관리) |
| `http.py` | 쿠키 · Origin 검사(HTTP 상태 변경 · WebSocket 공용 목록 `allowed_origins`) · 보안 헤더(`/api/v1/*`) · `AuthError` → 오류 봉투 매핑. (PR 3) 관리 · 재설정 경로용 Cognito 어댑터 조립(`get_cognito_adapter`, 설정이 비면 503) |
| `access.py` | (PR 2) 보호 API 의존성 — 세션(`require_user`) → 역할(`require_action_role`), 신원을 `request.state.current_user`에 둔다. WebSocket 세션 판정(`resolve_session_user`, 판정마다 짧은 DB 세션). (PR 3) 사용자 관리용 `require_admin`. (#492) 플랫폼 판정 `is_platform_account` · `require_platform_account`(`/api/v1/tenants` 라우터 의존성). 등급 판정 `is_central_admin` · `is_tenant_manager`, `require_central_admin`(`/api/v1/staff` 라우터 의존성 · 고객 생성) · `require_tenant_management`(고객 수정 · 삭제 · 정지 · 온보딩 경로 의존성). 고객 범위 `tenant_scope_ids` · `can_access_tenant` · `tenant_scope_of` · `require_tenant_scope`(업무 조회 라우트 의존성). 조치 역할 `require_action_role`은 TENANT의 `role`(ADMIN · OPERATOR)과 PLATFORM의 명시 등급을 본다. 메트릭 관문 `require_business_access`(관리 등급만). `require_admin`은 TENANT ADMIN과 CENTRAL_ADMIN · TENANT_MANAGER만 통과시킨다 |
| `../tenancy.py` | (#492 · #533) 업무 기록의 조회 범위 `TenantScope(tenant_ids)` — `None`은 명시 전체 범위(CENTRAL_ADMIN), 튜플은 그 고객들, 빈 튜플은 고객 없음. 소속 없는 기록은 전체 범위에서만 보인다(`covers`). 신원에서 범위를 만드는 것은 `access.py`다 |
| `provider.py` | Cognito 클라이언트 · `LoginFlow` · (PR 3) 단독 어댑터(`build_adapter`) 조립(엔드포인트 고정) |
| `bootstrap_admin.py` | (#492) 플랫폼 계정 생성 · UNASSIGNED 계정의 플랫폼 지정 운영 CLI. 만들거나 지정한 계정은 담당 고객이 없는 TENANT_MANAGER, 첫 중앙 관리자 지정은 ACTIVE 중앙 관리자가 0명일 때만(§플랫폼 계정 CLI) |
| `routers/auth.py` | `/api/v1/auth` 라우터(얇게). (PR 3) `/password-reset` · `/password-reset/confirm`. (#492) `/me`에 `account_type` · `tenant_id` · `platform_role` · `assigned_tenant_ids` |
| `routers/users.py` | (PR 3) `/api/v1/users` 라우터(얇게). 라우터 의존성 `require_admin`. (#492) 관리 범위는 서비스가 판정한다. 초대 · 재발송의 Cognito 어댑터는 사전 판정 뒤에 조립한다(`get_cognito_adapter_factory`) |
| `routers/staff.py` | (#492) `/api/v1/staff` 라우터(얇게). 라우터 의존성 `require_central_admin`. 담당 고객 조회 · 교체(`/{user_id}/tenants`)를 포함한다. 생성 경로는 없다 |
| `routers/tenants.py` · `tenant_admin.py` | (#492, 접근 판정만) 목록 · 상세 · 온보딩 조회를 행위자의 고객 범위로 거른다(범위 밖 404). 생성에 `require_central_admin`, 수정 · 삭제 · 정지 · 온보딩 조회에 `require_tenant_management`. 쓰기는 `app_users` 잠금 안에서 세션 · 등급 · 배정을 다시 판정한다(`tenant_admin._management`). 테넌트 API 자체는 이 모듈 밖이다 |
| `main.py` | (PR 2) 포함 시점 보호 배선(Origin → 세션), 접근 로그의 `user_id` · `role`, API 문서 토글. (PR 3) 사용자 관리 라우터도 같은 배선으로 포함. (#492 · #533) 조치 · 자산 · 인시던트 라우터는 Origin → 세션 배선만 쓰고 고객 범위는 각 라우트가 건다. 메트릭 라우터에만 `require_business_access`를 더한다. 테넌트 라우터(`routers/tenants.py`)와 직원 라우터(`routers/staff.py`)는 Origin → 세션 배선으로만 포함하고, 플랫폼 · 등급 판정은 각 라우터의 의존성이다 |
| `routers/ws.py` | (PR 2) WebSocket 연결 시 세션 확인 · 연결 중 재확인(4401). 닫기 코드 계약은 `packages/schemas/api/ws.py`. (#492) PLATFORM 계정만 연결을 받고, 이벤트마다 보내기 직전에 세션 · 등급 · 담당 고객과 저장된 인시던트의 고객으로 다시 판정한다 |
| `routers/actions.py` · `routers/incidents.py` | (PR 2) 조치 실행 · 종료가 역할 검사 결과(`CurrentUser`)를 받는다. (#533) 조회 · 실행 · 종료를 요청자의 고객 범위로 거르고, 실행 예약 · 종료는 Workflow(`workflows.py`)가 `app_users` 잠금 안에서 행위자와 범위를 다시 판정한다 |
| `db/models.py` · `db/repositories/auth.py` | `app_users` · `auth_sessions`. (PR 3) `user_admin_events`(`UserAdminEvent`), 세션 회수 사유 추가, 마지막 로그인 · 활성 ADMIN 집계. (#492) `AppUser.account_type`과 CHECK 2개, 목록 · 활성 ADMIN 집계의 테넌트 범위. `AppUser.platform_role`과 CHECK `ck_app_users_platform_role_by_account_type`, PLATFORM 목록(`list_platform_users_with_last_login`) · ACTIVE 중앙 관리자 집계(`count_active_central_admins`) · 등급 변경(`set_platform_role`). 담당 고객 `AppUserTenantAssignment`(`app_user_tenant_assignments`)와 조회 · 전체 교체(`list_assigned_tenant_ids` · `replace_assigned_tenants`). 등급 없이 만드는 PLATFORM 계정은 `create_user`가 TENANT_MANAGER로 둔다(배정 없음) |
| `db/migrations/versions/20261020_e492a3b5c701_user_admin_events.py` | (PR 3) 회수 사유 `ADMIN_REVOKED` · `ACCOUNT_DISABLED` · `PASSWORD_RESET` 추가, `user_admin_events` 생성과 append-only 트리거 |
| `db/migrations/versions/20261022_f492c4a8d2e1_user_account_type.py` | (#492) `app_users.account_type` 추가 · 기존 행 분류 · CHECK 2개(§마이그레이션 `f492c4a8d2e1`) |
| `db/migrations/versions/20261026_c492d6e8f4a2_merge_account_type_auto_action.py` | (#492) 빈 병합 revision — #492 가지와 dev 가지를 하나로 잇는다(§마이그레이션 그래프 · 병합 `c492d6e8f4a2`) |
| `db/migrations/versions/20261029_b492e7d1a605_user_platform_role.py` | (#492) `app_users.platform_role` 추가 · 기존 PLATFORM → CENTRAL_ADMIN · CHECK · 빈 `app_user_tenant_assignments` · 이력 동작 `PLATFORM_ROLE_CHANGED` · `TENANT_ASSIGNMENTS_CHANGED`(§마이그레이션 `b492e7d1a605`) |
| `db/migrations/versions/20261030_d492a544e001_merge_platform_roles_audit.py` | (#492) 빈 병합 revision — 플랫폼 등급 가지(`b492e7d1a605`)와 조치 감사 기록 가지(`b544a7d1e2f3`, `audit_events`, 이 모듈 밖)를 head 하나로 잇는다(§마이그레이션 그래프) |

## 로그인 흐름

```
POST /login                  비밀번호 → NEW_PASSWORD_REQUIRED | TOTP_SETUP_REQUIRED | TOTP_REQUIRED
POST /login/new-password     초대 사용자 최초 비밀번호 변경 → TOTP_SETUP_REQUIRED
POST /login/totp-setup       등록 비밀키 · otpauth URI (no-store)
POST /login/totp-setup/verify  VerifySoftwareToken SUCCESS + MFA_SETUP 응답 → (EMAIL_VERIFICATION_REQUIRED | COMPLETED)
POST /login/totp             SOFTWARE_TOKEN_MFA 응답 → (EMAIL_VERIFICATION_REQUIRED | COMPLETED)
POST /login/email-verification  이메일 코드 → COMPLETED
POST /logout · POST /activity · GET /me
POST /password-reset         (PR 3) 이메일 → 204(계정 · 발송 정보 없음)
POST /password-reset/confirm (PR 3) 이메일 · 재설정 코드 · 새 비밀번호 → 204(세션 발급 없음)
```

**세션을 발급하는 경우는 둘뿐이다** [팀 결정]:

1. 이번 로그인에서 `AdminRespondToAuthChallenge(SOFTWARE_TOKEN_MFA)`가 `AuthenticationResult`를 돌려줌
2. 이번 로그인에서 `VerifySoftwareToken`이 `Status=SUCCESS`를 돌려주고 이어서 `AdminRespondToAuthChallenge(MFA_SETUP)`가 `AuthenticationResult`를 돌려줌

비밀번호 단계나 최초 비밀번호 변경 직후에 토큰이 나오거나, 그 밖의 challenge(`SELECT_MFA_TYPE` · `EMAIL_OTP` · `SMS_MFA` 등)가 나오면 세션 없이 거절한다(`AUTH_FAILED`, 받은 refresh token 즉시 회수). `acr` · `auth_time` · 사용자 MFA 설정 상태(`PreferredMfaSetting` · `UserMFASettingList`)는 근거로 쓰지 않는다 — 앱은 이 값들이 채워지거나 이번 로그인의 TOTP 통과를 반영한다고 전제하지 않고, 위 두 응답만 본다 [판단].

최초 TOTP 등록(`MFA_SETUP`)으로 토큰을 받으면 먼저 `SetUserMFAPreference`로 TOTP를 선호 MFA로 지정한다. 이어서 `GetUser`로 `sub` · `email_verified`를 확인하고, 아래 중 하나라도 해당하면 이메일 검증 코드를 보내 확인한 뒤에야 완료한다.

- 최초 TOTP 등록 경로다.
- Cognito의 `email_verified`가 `true`가 아니다.
- 이 앱이 그 사용자의 이메일 코드 확인을 기록한 적이 없다(`app_users.email_verified_at`이 비어 있음).

마지막 조건이 필요한 이유: Cognito는 `VerifySoftwareToken` 성공 시점에 인증 앱을 계정에 묶는데, 이메일 코드를 보내는 API는 그 뒤에 받는 access token이 있어야 쓸 수 있다. 등록 전에 이메일을 확인시킬 수 없으므로, 비밀번호만 가진 사람이 자기 인증 앱을 등록한 뒤 이메일 단계를 버리고 다시 로그인하는 경로를 앱 DB의 확인 기록으로 막는다 [판단]. `email_verified_at`은 이 앱의 로그인 흐름에서 `VerifyUserAttribute`가 성공했을 때만 기록한다.

정상 완료 · 실패 · 요청 중 만료가 확인된 흐름은 그 자리에서 refresh token을 `RevokeToken`으로 회수한다. 사용자가 중간에 떠난 흐름은 다음 인증 요청의 정리(sweep) 때 회수되며, 그 전에 프로세스가 재시작되면 회수되지 않는다(토큰은 프로세스 메모리에만 있었다). 토큰은 응답 · DB · 로그 어디에도 두지 않는다.

완료된 신원 `(issuer, sub)`로 `app_users`를 찾는다. 없거나 `account_type`이 `UNASSIGNED`이면 403 `ACCOUNT_NOT_PROVISIONED`(이 판정이 비활성 판정보다 먼저다), 비활성이면 403 `ACCOUNT_DISABLED`이며 둘 다 세션을 발급하지 않는다. 소속 테넌트의 상태(정지 포함)는 로그인을 막지 않는다. 이메일로 계정을 찾거나 연결하지 않는다 [팀 결정].

## 정책과 근거

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| MFA | 전 역할 TOTP 필수. User Pool `MfaConfiguration=ON`, TOTP만 사용 | [팀 결정] |
| 인증 흐름 | `ADMIN_USER_PASSWORD_AUTH`(`AdminInitiateAuth` · `AdminRespondToAuthChallenge`). AWS가 AWS 자격증명을 가진 서버 측 기밀 클라이언트의 경로로 안내하며, 로그인 호출을 client secret에 더해 IAM 서명으로 보호한다. Admin 흐름은 기억된 기기를 지원하지 않아 기기 기억으로 MFA를 건너뛸 수 없다 | [표준] [AWS: Server-side and client-side authentication](https://docs.aws.amazon.com/cognito/latest/developerguide/authentication-flows-public-server-side.html) |
| SRP 미채택 | 브라우저가 이미 HTTPS로 서버에 비밀번호를 보내므로 SRP는 서버–Cognito 구간만 더 보호한다. 그 대가로 SDK 밖 암호 코드가 로그인 경로에 들어간다 | [판단] |
| 세션 최대 수명 | 발급 시점 + 8시간(`AUTH_SESSION_MAX_SECONDS=28800`). 활동으로 늘지 않는다 | [팀 결정] [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html#session-expiration) |
| 유휴 만료 | 마지막 사용자 활동 + 30분(`AUTH_SESSION_IDLE_SECONDS=1800`). 최대 수명을 넘지 않는다. 두 제한 중 먼저 도달한 쪽에서 서버가 만료한다 | [팀 결정] |
| 유휴 연장 | `POST /activity`만 연장한다(FE는 실제 사용자 입력이 있을 때만, 1분에 1번까지 호출). `GET /me` · 자동 조회 · 보호 API 요청 전부 · WebSocket 연결과 재확인은 연장하지 않는다(`touch=False`) | [팀 결정] |
| 세션 저장 | 서버 세션. 토큰 `secrets.token_urlsafe(32)`, DB에는 SHA-256 hex만. JWT를 브라우저에 두지 않고 Cognito refresh token을 저장하지 않으며 Remember Me가 없다 | [팀 결정] [판단: 고엔트로피 무작위 토큰이라 솔트 없는 SHA-256] |
| 세션 쿠키 | `__Host-vg_session`: `Secure; HttpOnly; SameSite=Lax; Path=/`, `Domain` 없음(host-only), `Max-Age=최대 수명` | [팀 결정] [표준: `__Host-` 접두 요건] |
| 로그인 진행 쿠키 | `__Host-vg_login`, 속성 동일, `Max-Age=300`. 완료 · `AUTH_FLOW_EXPIRED` 때 삭제 | [판단] |
| 세션 고정 방지 | 로그인 완료 때 요청에 실린 이전 세션은 `SUPERSEDED`로 회수. PR 3부터는 그 사용자의 다른 살아 있는 세션도 함께 회수한다(§사용자당 세션 1개) | [판단] |
| CSRF | 모든 상태 변경(POST) 요청은 `Origin`이 `AUTH_ALLOWED_ORIGINS`와 정확히 같아야 한다. 없거나 다르면 403 `AUTH_ORIGIN_REJECTED` — Cognito · 흐름 저장소 · DB에 닿기 전에 거절. 쿠키는 `SameSite=Lax` | [표준] [OWASP CSRF: Verifying Origin](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) |
| CORS | 자격증명 허용(`allow_credentials=True`). 그래서 `CORS_ALLOW_ORIGINS` · `AUTH_ALLOWED_ORIGINS`에 `*`를 받지 않는다(Starlette는 `*` + 자격증명에서 요청 Origin을 되돌린다) | [판단] |
| 보안 헤더 | `/api/v1/*`의 모든 응답(성공 · 401 · 403 · 404 · 422 · 500 봉투)에 `Cache-Control: no-store`, `Referrer-Policy: strict-origin`, `X-Content-Type-Options: nosniff`. PR 1은 `/api/v1/auth/*`에만 붙였고, PR 2에서 보호 API의 관제 데이터도 브라우저 · 중간 캐시에 남지 않게 넓혔다. `/health`와 CORS 사전 요청(CORS 미들웨어가 바깥에서 답한다)은 대상이 아니다 | [판단] |
| 코드 시도 상한(흐름별) | 한 로그인에서 TOTP · 이메일 코드 제출은 3번까지(`AUTH_TOTP_MAX_ATTEMPTS=3`). 3번째 실패에서 로그인을 폐기한다. 틀린 TOTP 뒤에 Cognito가 같은 challenge 세션을 무효화한다고 전제하지 않고, 서버가 횟수를 세어 막는다 | [판단] |
| 코드 시도 상한(사용자별) | 로그인을 다시 시작해 흐름별 상한을 초기화하지 못하도록, Cognito 사용자 단위로 15분 동안 코드 실패 10회(`AUTH_CODE_FAILURE_LIMIT` · `AUTH_CODE_FAILURE_WINDOW_SECONDS`)에 이르면 429 `AUTH_RATE_LIMITED`. 코드 단계는 Cognito를 부르지 않고 거절하고, 비밀번호 단계는 사용자를 식별한 뒤 흐름을 만들지 않고 거절한다. 코드 실패는 비밀번호를 통과해야만 쌓이므로 비밀번호를 모르는 사람이 계정을 잠글 수 없다. 이 때문에 한도에 이른 사용자의 올바른 비밀번호에는 429, 틀린 비밀번호에는 401이 간다 — 비밀번호를 이미 아는 사람에게만 보이는 차이라 열거 수단이 아니며, 401로 바꾸면 정상 사용자가 잠금 사실을 알 수 없다. IP 단위 제한(비밀번호 대입 방어)은 이 모듈에 없고 배포 환경(WAF · 클라이언트 IP 신뢰 설정)의 몫이다 | [판단] |
| 이메일 검증 | 첫 세션 전 필수. 계정 복구는 검증된 이메일만 쓰며, 초대 사용자의 `email_verified`가 비밀번호 변경 · TOTP 등록만으로 채워진다고 전제하지 않는다. 최초 TOTP 등록 경로와 앱 DB에 확인 기록이 없는 사용자는 Cognito 값과 관계없이 이메일 코드를 요구한다 | [팀 결정] [판단] |
| 비밀번호 정책 | User Pool 정책(12자 이상, 대소문자 · 숫자 · 기호)을 유지한다. OWASP ASVS 2.1.7(유출 비밀번호 확인) · 2.1.9(문자 종류 규칙 금지)는 적용하지 않는다. 유출 비밀번호 확인은 Cognito Essentials 요금제에 없고 Plus 요금제에 있으며, 그 확인 없이 문자 종류 규칙을 없애지 않는다. TOTP 필수와 코드 시도 제한으로 비밀번호만으로 계정이 열리는 위험은 제한된다. PR 3부터 앱 API로 설정하는 새 비밀번호에는 Pool보다 좁은 앱 정책을 Cognito 호출 전에 더 적용한다(§새 비밀번호 정책) | [팀 결정(#492)] [표준: 미적용 항목 명시] |
| MFA 선호 | 최초 등록 직후 TOTP를 선호 MFA로 지정한다. User Pool이 실수로 `OPTIONAL`이 되면 비밀번호 단계에서 토큰이 나오는데, 이 경우 앱은 세션 없이 거절한다(fail closed) | [판단] |
| 401 · 403 | 미인증(세션 없음 · 만료 · 회수 · 비활성 사용자의 세션 · (#492) UNASSIGNED 계정의 세션)은 401 `AUTH_REQUIRED`, 인증 후 계정 종류 · 역할이 허용하지 않으면 403 `AUTH_FORBIDDEN`(PR 2 · #492) | [팀 결정] [판단: #492 UNASSIGNED 처리] |
| 비밀값 | 비밀번호 · 코드는 `SecretStr`, 검증 오류에 입력값을 싣지 않는다(`hide_input_in_errors`). 로그에는 AWS 오류 코드와 호출 이름만 | [팀 결정] |
| 플랫폼 계정 | 운영자가 CLI로 명시 생성 · 지정할 때만 생긴다(§플랫폼 계정 CLI). 공개 API · 화면의 등록 경로는 없다. 새로 만들거나 지정한 계정은 담당 고객이 없는 `TENANT_MANAGER`다 — 로그인은 되지만 고객 데이터를 보지 못한다. CLI가 `CENTRAL_ADMIN`을 주는 것은 운영자가 명시하고 ACTIVE 중앙 관리자가 0명일 때뿐이고, 그 밖의 등급 변경과 담당 고객 배정은 중앙 관리자가 직원 관리 API로 한다(§직원 관리). 첫 방문자를 승격하지 않고, ADMIN 역할이나 `tenant_id` NULL에서 플랫폼 권한 · 관리 권한 · 고객 범위를 추론하지 않는다 | [팀 결정: CLI 명시 생성 · 지정, 첫 방문자 승격 · 권한 추론 없음, 명시적 권한 부여 · 회수] [판단: 배정 없는 TENANT_MANAGER 기본값 · CLI 첫 중앙 관리자 지정 조건] |

## 계정 종류와 테넌트 소속 (#492)

플랫폼 계정인지 고객사 계정인지의 원천은 `app_users.account_type` 하나이고, 플랫폼 계정 안의 관리 권한의 원천은 `app_users.platform_role` 하나다. `role`은 고객사 계정 안의 역할이고(PLATFORM의 `role=ADMIN`은 호환 값), `tenant_id`가 NULL인 것은 소속이 없다는 뜻일 뿐 권한이 아니다.

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 계정 종류 | `PLATFORM` · `TENANT` · `UNASSIGNED`(enum `app_user_account_type`), NOT NULL. ORM 기본값과 서버 기본값이 없어 모든 쓰기가 값을 명시해야 하고, 빠뜨린 쓰기는 DB가 거절한다 | [판단] |
| 소속 | `TENANT`이면 `tenant_id`가 반드시 있고(테넌트 FK), `PLATFORM` · `UNASSIGNED`이면 NULL이다. CHECK `ck_app_users_tenant_binding_by_account_type`이 강제한다 | [판단] |
| 플랫폼 역할(호환) | `PLATFORM`의 `role`은 호환 값 `ADMIN`으로 고정한다(CHECK `ck_app_users_platform_role_is_admin`). 이 값은 권한 판정에 쓰지 않는다 — 관리 권한과 고객 범위는 `platform_role`과 담당 고객 배정에서만 나온다 | [판단: `role=ADMIN` 호환 값] |
| 플랫폼 등급 | `platform_role`(enum `app_user_platform_role`)은 `CENTRAL_ADMIN` · `TENANT_MANAGER` 둘뿐이다. `CENTRAL_ADMIN` — 모든 고객과 그 사용자 · 업무 기록, 고객 생성, 직원 관리와 담당 고객 배정. 여러 명일 수 있다. `TENANT_MANAGER` — 배정된 고객에 한해 고객 조회 · 수정 · 정지 · 삭제 · 온보딩 조회, 고객 사용자 관리, 업무 조회와 기술 지원 조치(실행 · 종료). 고객 생성 · 직원 관리 · 자기 배정 변경은 없다. 담당 고객이 없는 TENANT_MANAGER는 로그인은 되지만 고객 데이터를 보지 못한다. PLATFORM이면 반드시 있고 TENANT · UNASSIGNED는 NULL이다(CHECK `ck_app_users_platform_role_by_account_type`). 서버 기본값이 없다. 판정은 `auth.access`의 `is_central_admin` · `is_tenant_manager`이며, 등급이 없거나 PLATFORM이 아니면 관리 권한이 없다 | [팀 결정: 중앙 관리자와 그 밖의 플랫폼 계정 구분, 명시적 권한 부여 · 회수] [판단: 등급 2종 · 배정 범위 · 복수 중앙 관리자 · 판정 위치] |
| 담당 고객 배정 | TENANT_MANAGER의 고객 범위는 `app_user_tenant_assignments`(`user_id` · `tenant_id` 복합 PK, 두 FK 모두 `ON DELETE CASCADE`)다. `account_type` · `tenant_id` 소속과는 별개 관계라, 배정은 계정 종류 · 소속을 바꾸지 않고 소속도 배정을 만들지 않는다. 중앙 관리자만 `/api/v1/staff/{user_id}/tenants`로 바꾼다(§직원 관리). 세션 판정마다 DB에서 새로 읽으므로 교체 · 회수는 재로그인 없이 그 계정의 다음 판정부터 반영된다. 고객을 지우면 그 고객의 배정 행도 함께 지워진다 — 이 연쇄 삭제는 영향받은 직원의 `TENANT_ASSIGNMENTS_CHANGED` 이력을 남기지 않는다(§이력) | [판단] |
| 고객 범위 | `auth.access.tenant_scope_ids` 하나가 정한다 — CENTRAL_ADMIN은 `None`(전체), TENANT_MANAGER는 배정된 고객(없으면 빈 범위), TENANT 계정은 자기 소속 하나, 그 밖은 빈 범위다. 업무 라우트는 같은 값을 `tenancy.TenantScope`로 받는다. 계정 종류(PLATFORM) · 소속 NULL · `role`에서 전체 범위를 추론하지 않고, 빈 범위를 「전체」로 읽지 않는다. 소속 없는(소속을 모르는 이전 · 미등록) 기록은 전체 범위에서만 보인다 | [판단] |
| UNASSIGNED | 접근 권한이 없는 계정이다. 로그인은 완료 단계에서 403 `ACCOUNT_NOT_PROVISIONED`(세션 미발급), 이미 있던 세션은 다음 판정부터 401 `AUTH_REQUIRED`다. 사용자 목록과 공개 응답에 나오지 않는다 | [판단] |
| 공개 계약 | `CurrentUserResponse`(`GET /me`) · `UserListItem`에 `account_type`(`PLATFORM` \| `TENANT`)과 짝 `tenant_id`(UUID \| `null`), `platform_role`(PLATFORM은 필수, TENANT는 `null` — 사용자 목록 항목은 언제나 `null`)을 싣는다. `CurrentUserResponse`에는 필수 칸 `assigned_tenant_ids`(UUID 목록)가 더 있다 — 실제 ID는 TENANT_MANAGER만 갖고, CENTRAL_ADMIN(전체 범위)과 TENANT 계정(소속은 `tenant_id`)은 언제나 빈 목록이다. `AccountScope` · `UserListItem`에는 배정 ID가 없다. 짝 규칙은 `AccountScope`(`packages/schemas/api/auth.py`)가 검증하며, 내부 `CurrentUser`와 TS 타입(`apps/web/src/types/api.ts`의 `AccountScope`)도 같은 규칙이다. 테넌트 상태(`tenant_status`)는 내부 판정용이라 공개하지 않는다 | [판단] |
| 플랫폼 전용 라우트 | `auth.access.require_platform_account` — 테넌트 관리 라우터 `/api/v1/tenants`(`routers/tenants.py`, 경로 7개: 목록 · 생성 · 상세 · 수정 · 삭제 · 온보딩 조회 · 정지)의 라우터 의존성이다. `main.py`가 이 라우터를 Origin → 세션 배선으로 포함하고, 이 판정은 본문 필드 검증 전에 돈다. TENANT 계정은 역할 · 소속 테넌트 상태와 관계없이 403 `AUTH_FORBIDDEN`이고 PLATFORM 계정만 통과한다. 목록 · 상세 · 온보딩 조회는 행위자의 고객 범위로 거른다 — CENTRAL_ADMIN은 전체, TENANT_MANAGER는 배정된 고객만(배정이 없으면 빈 목록)이고, 범위 밖 고객은 없는 고객과 같은 404 `TENANT_NOT_FOUND`다. 범위 안이면 `PENDING_CONNECTION` 상세의 `onboarding_url`도 받는다. 생성은 `require_central_admin`이 걸려 CENTRAL_ADMIN만 하며, 새 고객은 어떤 담당자에게도 자동 배정되지 않는다. 수정 · 삭제 · 정지와 온보딩 조회(`GET /{id}/onboarding`)에는 `require_tenant_management`가 걸리고 대상이 범위 안이어야 한다. 쓰기는 `tenant_admin`이 `app_users` 잠금 안에서 세션 · 등급 · 배정을 다시 판정한다(그 사이 세션이 회수되면 401, 등급 · 배정이 바뀌어 권한이 없어지면 403 · 404이고 쓰지 않는다). 테넌트 API의 계약 · 동작은 `packages/schemas/api/tenants.py`와 그 라우터가 맡고, 이 문서는 접근 판정만 다룬다 | [판단] |

### 마이그레이션 `f492c4a8d2e1`

`e492a3b5c701` 다음 revision이다(`db/migrations/versions/20261022_f492c4a8d2e1_user_account_type.py`).

- **upgrade**: enum과 열을 만들고 기존 행을 `tenant_id` NULL → `UNASSIGNED`, 값 있음 → `TENANT`로 채운 뒤 NOT NULL과 CHECK 2개를 건다. `PLATFORM`은 만들지 않는다(기존 계정 승격 없음). 기존 행이 있다고 전제하지 않으며, 행 · 신원(`issuer` · `cognito_sub`) · 세션 행은 그대로 둔다.
- 소속 없는 기존 계정이 있었다면 업그레이드 뒤 `UNASSIGNED`가 되어 로그인 403 · 기존 세션 401이 된다. 그 계정을 플랫폼 계정으로 쓰려면 §플랫폼 계정 CLI의 지정을 실행한다(지정 결과는 담당 고객이 없는 TENANT_MANAGER다).
- **downgrade**: CHECK 2개 · 열 · enum을 지운다. `PLATFORM` · `UNASSIGNED` 구분은 복원되지 않는다 — 다시 upgrade하면 소속 NULL인 계정은 이전에 `PLATFORM`이었어도 모두 `UNASSIGNED`가 되어 다시 지정해야 한다.
- **호환성**: 기본값 없는 NOT NULL 열이라, upgrade 뒤에는 `account_type`을 모르는 이전 앱 · CLI의 `app_users` INSERT(초대 · 관리자 생성)가 거절된다. 환경에 적용할 때는 마이그레이션 · 새 앱 · 새 CLI를 함께 맞춰 올린다. 실제 환경 적용과 그 검증은 이 문서가 보증하지 않는다(§테스트).

### 마이그레이션 그래프 · 병합 `c492d6e8f4a2`

`e492a3b5c701`에서 가지 둘로 갈렸다가 빈 병합 revision `c492d6e8f4a2`에서 다시 합쳐진다. 그 뒤 `b492e7d1a605`(플랫폼 등급 · 담당 고객 배정)와 `b544a7d1e2f3`(조치 감사 기록 `audit_events`, T-2 · #544, 이 모듈 밖)가 둘 다 `c492d6e8f4a2`를 부모로 갈라졌다가 빈 병합 revision `d492a544e001`에서 합쳐진다. 현재 head는 `d492a544e001` 하나다.

```
e492a3b5c701 ─┬─ f492c4a8d2e1 ─┬─ c492d6e8f4a2 ─┬─ b492e7d1a605 ─┬─ d492a544e001 (head)
              └─ f514a6c3d201 ─┘                └─ b544a7d1e2f3 ─┘
```

- **가지**: 첫 갈래에서 #492 가지는 `f492c4a8d2e1`(`app_users.account_type`) 하나이고, dev 가지는 `f514a6c3d201`(`tenants.auto_action_resumed_at`, 이 모듈 밖)이다. 두 revision 모두 부모가 `e492a3b5c701`이다. 둘째 갈래의 `b492e7d1a605` · `b544a7d1e2f3`는 모두 부모가 `c492d6e8f4a2`이다.
- **병합**: `c492d6e8f4a2`(`db/migrations/versions/20261026_c492d6e8f4a2_merge_account_type_auto_action.py`)의 부모는 `f492c4a8d2e1` · `f514a6c3d201` 둘이고, `d492a544e001`(`db/migrations/versions/20261030_d492a544e001_merge_platform_roles_audit.py`)의 부모는 `b492e7d1a605` · `b544a7d1e2f3` 둘이다. 두 병합 revision 모두 upgrade · downgrade가 비어 있다. 각 병합의 두 가지가 서로 다른 객체를 바꾸므로 스키마 · 데이터 변경이 필요 없고, 병합하면서 기존 revision을 고치지 않았다.
- **기존 DB**: dev 가지 끝(`f514a6c3d201`)의 DB와 #492 가지 끝(`f492c4a8d2e1`)의 DB 모두 `alembic upgrade head`가 아직 적용하지 않은 쪽 가지, 병합 revision `c492d6e8f4a2`, `b492e7d1a605` · `b544a7d1e2f3`, 병합 revision `d492a544e001`을 적용해 head에 이른다. 이미 `c492d6e8f4a2`인 DB는 둘째 갈래의 두 revision과 `d492a544e001`을 적용한다. `b492e7d1a605`까지 올린 DB(현재 등급 2종 스키마)는 `b544a7d1e2f3`와 `d492a544e001`만 적용해 등급 · 배정을 그대로 두고, `b544a7d1e2f3`까지 올린 DB는 `b492e7d1a605`와 `d492a544e001`을 적용한다. downgrade나 `stamp` 없이 앞으로만 올린다. dev 가지 DB에는 이때 `f492c4a8d2e1`이 처음 적용되므로, 그 upgrade 규칙(소속 NULL → `UNASSIGNED`, 값 있음 → `TENANT`, 승격 없음)과 호환성 주의(`account_type`을 모르는 이전 앱 · CLI의 `app_users` INSERT 거절)가 그대로 적용된다(§마이그레이션 `f492c4a8d2e1`). UNASSIGNED 계정의 동작도 위 표 그대로다. `b492e7d1a605`를 처음 적용하는 경로에서는 그 규칙(기존 PLATFORM → CENTRAL_ADMIN, 담당 고객 배정 없음)이 적용된다(§마이그레이션 `b492e7d1a605`).
- **새 DB**: base → head가 같은 그래프의 모든 가지와 두 병합 revision을 거친다.
- **downgrade**: 병합 revision의 downgrade는 아무것도 바꾸지 않는다. head에서 내려갈 때 어느 가지의 downgrade가 실행되는지는 대상 revision이 정한다 — 대상의 조상이 아닌 revision만 되돌린다. 대상이 `b492e7d1a605`이면 `b544a7d1e2f3`만 되돌려 감사 기록 테이블을 지우고, 대상이 `b544a7d1e2f3`나 그보다 아래면 `b492e7d1a605`의 downgrade가 실행되어 등급 · 배정을 잃는다(§마이그레이션 `b492e7d1a605`). 그 아래 revision의 downgrade 주의점은 §마이그레이션 `f492c4a8d2e1`에 적힌 그대로다.
- **등록 링크 스키마가 있는 DB**: 이 그래프에는 `platform_enrollments`(enum `platform_enrollment_status` 포함)가 없다. 등록 링크를 제거하기 전의 #527 그래프로 올린 DB — 기록 revision이 `b492e5d7c3a1`인 DB, 또는 기록 revision은 같은 `c492d6e8f4a2`이지만 `platform_enrollments`가 있는 DB — 는 이 그래프의 자동 upgrade 경로가 지원하지 않는다. revision ID만 보고 head라고 판단하지 않는다. 이런 DB에는 이 그래프로 upgrade · downgrade · `stamp`를 실행하지 않고 이 코드를 연결하지 않는다. 그 스키마를 만든 이전 코드와 짝지어 보존하거나, 따로 승인된 전환 절차로만 다룬다.
- **등급 3종 초안을 적용한 DB**: 기록 revision이 `b492e7d1a605`여도 이전 초안 스키마일 수 있다(§마이그레이션 `b492e7d1a605`). 이 경우도 위와 같이 이 그래프의 자동 경로 대상이 아니다.
- 실제 배포 환경 DB의 현재 revision과 그 환경에서의 적용 결과는 이 문서가 보증하지 않는다(§테스트).

### 마이그레이션 `b492e7d1a605`

`c492d6e8f4a2` 다음 revision이고, 병합 revision `d492a544e001`에서 감사 기록 가지와 합쳐진다(`db/migrations/versions/20261029_b492e7d1a605_user_platform_role.py`, §마이그레이션 그래프).

- **upgrade**: ① `user_event_action`에 `PLATFORM_ROLE_CHANGED` · `TENANT_ASSIGNMENTS_CHANGED`를 더한다(`ADD VALUE IF NOT EXISTS`, autocommit 블록이라 이 값들은 나머지 단계와 따로 확정된다). ② enum `app_user_platform_role`(`CENTRAL_ADMIN` · `TENANT_MANAGER`)과 `app_users.platform_role`(NULL 허용, 서버 기본값 없음)을 만든다. ③ 기존 PLATFORM 행을 모두 `CENTRAL_ADMIN`으로 채우고 TENANT · UNASSIGNED는 NULL로 둔다. ④ CHECK `ck_app_users_platform_role_by_account_type` — `(account_type = 'PLATFORM') = (platform_role IS NOT NULL)`을 건다. ⑤ 빈 `app_user_tenant_assignments`(복합 PK `(user_id, tenant_id)`, 두 FK `ON DELETE CASCADE`, `tenant_id` 인덱스)를 만든다. 배정은 백필하지 않는다. 행 · 신원(`issuer` · `cognito_sub`) · 세션 행은 그대로이고, `ck_app_users_platform_role_is_admin`은 호환 제약으로 남는다.
- **전환 결과**: 이전 스키마의 PLATFORM은 모든 고객에 미치는 전역 권한이었다. 기존 PLATFORM 계정은 같은 범위의 CENTRAL_ADMIN이 되어 세션을 잃지 않고 지금까지의 운영(고객 관리 · 고객 사용자 관리 · 업무 조회 · 조치)을 그대로 이어 가며, 직원 관리 · 담당 고객 배정도 갖는다. 기존 운영을 끊지 않으려는 선택이다 [판단]. upgrade 직후 TENANT_MANAGER와 배정은 0건이다. ACTIVE PLATFORM 계정이 하나라도 있던 DB에서는 CLI의 첫 중앙 관리자 지정이 필요 없다(§플랫폼 계정 CLI).
- **downgrade**: 배정 테이블 · 인덱스 · CHECK를 지우고, `TENANT_MANAGER` 행을 `account_type=UNASSIGNED`로 바꾼 뒤 열과 enum을 지운다. 이전 스키마의 PLATFORM은 전역 권한이라, 배정으로 범위가 제한된 TENANT_MANAGER를 PLATFORM으로 남기면 모든 고객으로 권한이 넓어지기 때문이다. CENTRAL_ADMIN은 같은 전역 범위인 PLATFORM으로 남는다. UNASSIGNED가 된 이전 TENANT_MANAGER는 로그인 403 `ACCOUNT_NOT_PROVISIONED` · 기존 세션 401이다(§계정 종류와 테넌트 소속). 등급과 배정은 잃는다. 두 이력 enum 값과 그 이력 행은 남는다(PostgreSQL은 enum 값을 지우지 못하고, 이력은 append-only다).
- **다시 upgrade**: 남은 PLATFORM 계정(이전 CENTRAL_ADMIN)은 다시 CENTRAL_ADMIN이 된다. UNASSIGNED로 내려간 이전 TENANT_MANAGER와 그 배정은 되살아나지 않으므로 이전 담당 범위도 돌아오지 않는다 — `--designate-unassigned-user-id`로 다시 지정하고 중앙 관리자가 다시 배정한다. 이력 값은 `IF NOT EXISTS`라 다시 더해도 오류가 없다.
- **등급 3종 초안을 적용한 DB**: 같은 revision ID `b492e7d1a605`에는 게시되지 않은 이전 초안(`STAFF`를 포함한 등급 3종, 배정 테이블 없음)이 있었다. 그 초안을 적용한 DB는 기록 revision이 `b492e7d1a605`라, `alembic upgrade head`가 감사 기록 가지와 병합 revision은 적용해도 이 revision은 이미 적용된 것으로 보고 다시 실행하지 않는다 — 초안의 등급 · 배정 스키마는 고쳐지지 않는다. 이 그래프가 지원하지 않는 DB다. `app_user_platform_role`에 `STAFF` 값이 있거나 `app_user_tenant_assignments`가 없으면 그런 DB다. 이 코드를 연결하지 않고, 이 그래프로 upgrade · downgrade · `stamp`를 실행하지 않으며, DB는 그대로 보존한다. 지우고 다시 만들어도 되는 것은 일회용 테스트 DB뿐이고, 데이터를 보존해야 하는 DB의 전환은 따로 승인된 절차로만 한다.
- **호환성**: upgrade 뒤에는 `platform_role`을 채우지 않는 이전 CLI의 PLATFORM 생성 · 지정을 CHECK가 거절한다. 이전 앱은 `platform_role` · 배정을 보지 않으므로 TENANT_MANAGER에게도 이전의 전역 PLATFORM 권한(모든 고객 · 고객 사용자 관리)을 준다 — upgrade한 DB에 이전 앱을 연결하지 않는다. 반대로 새 앱 · 새 CLI는 upgrade 전 DB에 `app_users.platform_role`이 없어 사용자 조회가 실패한다. 마이그레이션 · 새 앱 · 새 CLI를 함께 올린다. 실제 환경 적용과 그 검증은 이 문서가 보증하지 않는다(§테스트).

## 접근 보호 (PR 2)

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 공개 경로 | `GET /health`, `POST /api/v1/auth/login*`(6단계), (PR 3) `POST /api/v1/auth/password-reset` · `/password-reset/confirm`, `POST /api/v1/auth/logout`(멱등 — 세션이 없으면 지울 것이 없어 204). `GET /me` · `POST /activity`는 세션이 없으면 401이다. PR 1 · 2에서는 Cognito 미구성이면 503이었고, PR 3부터 Cognito 설정과 무관하게 동작한다(§설정). 그 밖의 `/api/v1/*`와 WebSocket은 전부 세션이 필요하다. 공개 목록은 `test_api_access.py`의 `PUBLIC_ROUTES`가 실제 앱 경로와 대조한다 — 새 경로는 목록에 넣지 않는 한 401이어야 통과한다 | [팀 결정] |
| 세션 판정 | `auth.service.resolve_current_user(touch=False)` 하나. 보호 API · WebSocket은 유휴 시간을 늘리지 않는다. Cognito 설정을 읽지 않는다 — Cognito가 비어도 보호 API는 세션만으로 열린다 | [팀 결정] [판단] |
| 배선 | `main.py`가 조치 · 자산 · 인시던트 · 메트릭 라우터를 `include_router(..., dependencies=[Origin, 세션])`로 포함한다. 역할은 조치 실행 · 종료 라우트의 의존성이다. 검사 순서: Origin(상태 변경만, DB 전) → 세션 → 역할 → 본문 검증 → 라우터. (#492) 메트릭 라우터는 세션 다음에 `require_business_access`(관리 등급만)를 거친다. 조치 · 자산 · 인시던트의 고객 범위는 각 라우트와 Workflow가 건다(아래 업무 범위) | [판단] |
| 업무 범위(#492 · #533) | 조치 · 자산 · 인시던트 API는 요청자의 고객 범위(`tenant_scope_of`)로 거른다 — CENTRAL_ADMIN은 전체(소속 없는 기록 포함), TENANT_MANAGER는 배정된 고객의 기록, TENANT 계정은 자기 고객의 기록만 본다(소속이 정지된 TENANT 계정도 조회는 된다). 범위 밖 · 소속 없는 기록은 목록에서 빠지고, 상세 · 실행 · 종료는 없는 기록과 같은 404다. 메트릭(`/api/v1/metrics/timeseries`)은 관리 등급(CENTRAL_ADMIN · TENANT_MANAGER)만 쓰고 TENANT 계정은 403이다. TENANT_MANAGER의 메트릭은 모든 DB 축이 배정 고객의 기록으로 좁혀지고, CloudWatch 축은 그 자산을 소유한 고객의 자격으로만 부르며 결과 재사용도 소유 고객별로 나뉜다. 전역 WebSocket은 PLATFORM만 받는다(아래). 범위는 요청마다 세션 판정 시점의 등급 · 배정 · 소속으로 다시 만들고, 켜고 끄는 설정이 없다 | [판단] |
| 역할 | 아래 표. TENANT 계정은 `role`(ADMIN · OPERATOR), PLATFORM 계정은 명시 등급(CENTRAL_ADMIN · TENANT_MANAGER)으로 조치 권한을 판정한다. 역할 · Origin 거절은 `workflows.reserve_execution` · `workflows.resolve_incident` 전이다 — 실행 행(디스패처가 보는 비종결 `action_executions`) · 후보 전이 · AWS 호출이 생기지 않는다 | [팀 결정] |
| 정지 고객 · 쓰기 재판정(#492 · #533) | 정지는 요청자 소속이 아니라 대상 인시던트의 고객으로 판정한다. 정지 고객의 인시던트에 대한 새 실행(관제자 복구 포함)은 행위자와 관계없이 409 `TENANT_SUSPENDED`다. 인시던트 종료는 플랫폼 기술 지원(CENTRAL_ADMIN, 범위 안의 TENANT_MANAGER)이면 정지 고객의 것도 할 수 있고, TENANT 계정은 409다. 실행 예약과 종료는 쓰기 직전에 `app_users` 잠금 안에서 행위자의 세션 · 조치 권한 · 담당 고객을 다시 판정하고, 감사 기록의 행위자도 이 재판정한 사용자로 남긴다. 이 재판정은 그 시점까지 확정된 회수 · 강등 · 배정 변경만 반영한다 — 이미 예약됐거나 실행 중인 작업은 회수 · 강등 · 고객 정지로 자동 취소되지 않는다 | [판단] |
| 401 · 403 | 세션 없음 · 만료(유휴 · 최대 수명) · 회수(`LOGOUT` · `SUPERSEDED`, PR 3의 `ADMIN_REVOKED` · `ACCOUNT_DISABLED` · `PASSWORD_RESET`) · 비활성 사용자 · (#492) UNASSIGNED 계정 → 401 `AUTH_REQUIRED`(사유를 구분하지 않는다). 역할 · 계정 종류 · 등급 불허 → 403 `AUTH_FORBIDDEN`. (#492) 범위 밖 대상 → 대상별 404. 정지 고객 대상의 새 실행(TENANT 계정은 종료도) → 409 `TENANT_SUSPENDED`. 허용 밖 Origin → 403 `AUTH_ORIGIN_REJECTED` | [팀 결정] [판단: #492 UNASSIGNED · 범위 404 · 정지 409] |
| Origin 범위 | 쿠키로 승인되는 모든 상태 변경 요청(GET · HEAD · OPTIONS 외). `AUTH_ALLOWED_ORIGINS`와 정확히 일치(비면 `CORS_ALLOW_ORIGINS`), 세션 조회 전에 거절. 조회(GET)는 Origin을 보지 않는다. WebSocket Handshake도 같은 목록(`auth.http.allowed_origins`)을 쓴다 | [표준] [OWASP CSRF: Verifying Origin](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) |
| 깨진 JSON 본문 | 잠금된 FastAPI 0.141.1은 JSON 본문을 의존성보다 먼저 파싱한다(`fastapi/routing.py` 요청 처리기). 그래서 파싱할 수 없는 본문의 POST는 세션 · Origin과 관계없이 422 `REQUEST_VALIDATION_FAILED`다. 이때 Origin · 세션 · DB · Workflow는 하나도 돌지 않는다. 파싱되는 본문의 필드 오류는 세션 · 역할 뒤(미인증이면 401)다. 이대로 둔다 — 상태 변경 · DB 접근 · 입력 반향이 없고, 바꾸려면 본문 파싱 앞에 같은 관문을 한 번 더 두어야 한다(중복) | [팀 결정: 유지] |
| WebSocket 연결 | Handshake Origin은 HTTP 상태 변경 검사와 같은 목록(`AUTH_ALLOWED_ORIGINS`, 비면 `CORS_ALLOW_ORIGINS`)과 정확히 일치해야 한다 — `AUTH_ALLOWED_ORIGINS`가 더 좁으면 CORS에만 있는 Origin은 WebSocket도 1008. Origin 없는 비브라우저 연결은 #73처럼 통과하지만 세션 쿠키는 필요하다. 세션 쿠키가 없거나 무효면 accept 전에 close 1008 — 브라우저에는 HTTP 403으로 보인다. 사유는 구분하지 않는다. (#492) PLATFORM이 아닌 계정도 무효 세션과 같이 다룬다 — 연결 때 1008, 연결 중 재확인에서는 4401. TENANT 계정의 실시간 연결은 아직 없다 | [팀 결정] [판단: #492 PLATFORM 전용 연결] |
| WebSocket 이벤트 범위(#492) | PLATFORM 계정이면 등급 · 배정과 관계없이 연결을 받지만, 큐에 들어온 이벤트마다 보내기 직전에 세션 · 등급 · 담당 고객을 다시 판정한다. CENTRAL_ADMIN은 모든 이벤트를, 그 밖은 이벤트가 가리키는 인시던트의 저장된 고객이 범위 안일 때만 받는다. 인시던트가 없거나 소속이 없거나 판정이 실패한 이벤트는 보내지 않는다. 공개 이벤트 모양은 바뀌지 않는다. 판정을 마치고 이미 시작한 전송은 그 뒤의 회수로 끊지 않는다 | [판단] |
| WebSocket 재확인 | 연결마다 감시 task가 `min(지금 + WS_SESSION_CHECK_SECONDS, 유휴 만료, 최대 수명)`에 다시 판정하고, 무효면 close 4401(`WS_CLOSE_AUTH_REQUIRED`) reason `"AUTH_REQUIRED"`. 반영 지연 상한: 로그아웃 · 재로그인 회수 · 사용자 비활성은 주기 1회(기본 30초) + 확인 조회 시간, 유휴 · 최대 수명 만료는 그 시각. 확인 자체가 실패하면(DB 오류) 1011로 닫고 오류 종류만 로그에 남긴다(세션 무효가 아니므로 FE는 재연결). 판정마다 짧은 DB 세션을 스레드풀에서 연다 — 연결 수명 동안 DB 세션을 쥐지 않는다. close reason에 세션 · 사용자 정보를 싣지 않는다 | [팀 결정] [판단: 주기 30초] |
| 신원 전달 | 통과한 `CurrentUser`(user_id · role)를 `request.state.current_user`에 둔다. 접근 로그 `http_request`에 `user_id` · `role`을 싣는다(세션을 통과한 요청만). 이메일 · 쿠키 · 토큰은 싣지 않는다. 조치 실행 · 종료 라우터는 신원을 직접 넘기지 않고 요청 세션의 자격(Actor)만 Workflow에 넘기며, Workflow가 그 자격으로 행위자를 다시 판정한다. 조치 감사 기록(`audit_events`, T-2 · #544)의 행위자는 Workflow가 쓰기 직전 `app_users` 잠금 안에서 다시 판정한 같은 사용자이고, 기록은 실행 예약 · 인시던트 종료와 같은 트랜잭션에 들어간다. 같은 Key 재요청과 거절된 요청은 새 기록을 남기지 않고, 자동 실행 · 자동 원복은 SYSTEM으로 남는다. 감사 기록 조회는 `GET /api/v1/audit-events`(`routers/audit.py`, 계약 `schemas.api.audit`)이며 그 조회 범위 · 응답 칸은 이 모듈 밖이다 | [판단] |
| API 문서 | `API_DOCS_ENABLED`(기본 false)일 때만 `/docs` · `/redoc` · `/openapi.json`을 둔다(끄면 경로가 없어 404). `.env.example`은 로컬 개발용으로 true. 배포 스크립트(`scripts/provision_platform_aws.py`)는 태스크 환경변수를 명시 목록으로 넘기므로 꺼진 채로 뜬다 | [판단] |

PLATFORM 계정은 모두 `role=ADMIN`(호환 값)이고, 아래 PLATFORM 열은 `platform_role`로 나눈다. TENANT_MANAGER 열의 「배정 고객」은 그 계정에 배정된 고객이며, 배정이 없으면 목록은 비고 대상 경로는 404다.

| 동작 | PLATFORM CENTRAL_ADMIN | PLATFORM TENANT_MANAGER | TENANT ADMIN | TENANT OPERATOR | TENANT VIEWER |
| --- | --- | --- | --- | --- | --- |
| 로그인 · `/me` · `/activity` · `/logout` · 비밀번호 재설정 | 허용 | 허용(배정이 없어도) | 허용(테넌트 정지와 무관) | 허용(테넌트 정지와 무관) | 허용(테넌트 정지와 무관) |
| 업무 조회 `GET /api/v1/assets` · `/incidents` · `/incidents/{id}` | 전체(소속 없는 기록 포함) | 배정 고객의 기록 | 자기 고객의 기록 | 자기 고객의 기록 | 자기 고객의 기록 |
| 메트릭 `GET /api/v1/metrics/timeseries` | 전체 | 배정 고객의 기록 | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |
| WebSocket | 모든 이벤트 | 배정 고객 인시던트의 이벤트 | 1008 · 4401 | 1008 · 4401 | 1008 · 4401 |
| 조치 실행 `POST /api/v1/actions/execute` | 허용(정지 고객 대상은 409) | 배정 고객의 인시던트(정지 고객 대상은 409) | 자기 고객의 인시던트(정지면 409) | 자기 고객의 인시던트(정지면 409) | 403 `AUTH_FORBIDDEN`(역할) |
| 인시던트 종료 `POST /api/v1/incidents/{id}/resolve` | 허용(정지 고객 포함) | 배정 고객의 인시던트(정지 고객 포함) | 자기 고객의 인시던트(정지면 409) | 자기 고객의 인시던트(정지면 409) | 403 `AUTH_FORBIDDEN`(역할) |
| 사용자 관리 `/api/v1/users*` | 모든 TENANT 계정 | 배정 고객의 TENANT 계정 | 자기 테넌트의 TENANT 계정(소속 정지면 조회만) | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |
| 고객 조회 `GET /api/v1/tenants` · `/api/v1/tenants/{id}` | 전체 | 배정 고객 | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |
| 고객 생성 `POST /api/v1/tenants` | 허용 | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |
| 고객 수정 · 삭제 · 정지 · 온보딩 조회 `GET /api/v1/tenants/{id}/onboarding` | 허용 | 배정 고객 | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |
| 직원 관리 · 담당 고객 배정 `/api/v1/staff*` | 허용(자기 등급 · 상태 변경은 403) | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |

TENANT 계정의 메트릭 · 실시간 연결과 고객용 업무 화면은 아직 열리지 않았다(T-4 · T-5). 정지 고객 사용자의 자기 기록 조회 요구([ADR-0012](../../../docs/adr/0012-saas-transition-account-structure-and-tenant-model.md))도 업무 API 조회 범위에서만 충족되고, 화면 · 메트릭 · 실시간 연결은 그 범위다. 이 모듈은 테넌트 데이터 격리가 완성됐다고 주장하지 않는다.

**기록의 고객 소속에는 한계가 있다.** 새 인시던트는 그 AWS 계정에 등록된 고객을 소속으로 받고, 그 인시던트의 실행은 인시던트의 소속을 따른다. 등록된 고객이 없는 계정의 인시던트는 소속 없이 남는다. 수집기(`start_collection_run` · `upsert_asset`)는 아직 수집 회차 · 자산 행에 고객 소속을 채우지 않는다. 그래서 실제로 수집된 자산과 그 판정 기록은 소속 없는 기록으로 남아 CENTRAL_ADMIN에게만 보이고, 배정된 TENANT_MANAGER와 TENANT 계정의 결과에는 나오지 않는다. 소속을 모르는 이전 · 미등록 기록을 고객 범위로 추정하거나 백필하지 않는다. 자산 목록과 메트릭 여섯 축은 기록을 (소속 고객, 그 고객의 관제 리전) 짝으로 고른다 — 한 고객이 관제에서 뺀 리전의 기록은 범위 안의 다른 고객(예: 같은 TENANT_MANAGER의 다른 담당 고객)이 그 리전을 관제하더라도 다시 들어오지 않는다. 소속 없는 이전 기록은 서버 설정의 관제 리전에 있는 것만 CENTRAL_ADMIN에게 보인다. 담당 범위(어느 고객을 보는가)는 이 리전 선택과 따로 판정한다. 자산 목록의 수집 상태는 범위 안의 고객별 · 리전별 최신 회차(CENTRAL_ADMIN은 소속 없는 회차 포함)를 함께 접은 값이고, 커버리지는 고객별이 아니라 리전별로 본다 — 관제 리전마다 어떤 고객이든 회차가 있는지만 볼 뿐 고객마다 수집기가 연결됐다는 근거가 아니다. 범위 필터의 자동 테스트는 소속을 명시한 기록으로 돌기 때문에, 실제 수집 데이터의 고객 소속을 보증하지 않는다. 수집기 · Workflow 같은 백그라운드 처리의 테넌트 격리도 이 범위(T-1)가 아니다.

## 사용자당 세션 1개 (PR 3)

- 로그인 완료(`service.complete_login`)는 `app_users` 잠금 안에서 사용자 상태 · 이메일 확인 기록을 새로 읽는다. 이어서 요청 쿠키의 이전 세션(다른 사용자의 것이어도)과 그 사용자의 살아 있는 세션 전부를 `SUPERSEDED`로 회수한 뒤 새 세션 하나를 발급한다. 동시 로그인도 같은 잠금으로 직렬화되어 살아 있는 세션은 1개만 남는다 [판단].
- 회수 시점은 다음 로그인의 **완료**다. 비밀번호 · TOTP 단계에서 멈춘 로그인은 기존 세션을 건드리지 않는다. 이 변경 전에 이미 있던 사용자당 여러 세션은 일괄 회수하지 않는다(마이그레이션은 세션 행을 바꾸지 않는다). 그 세션들은 그 사용자의 다음 로그인 완료 · ADMIN 회수 · 만료 때 정리된다.
- 교체된 브라우저는 다음 요청에서 일반 401 `AUTH_REQUIRED`를 받는다. 열린 WebSocket은 재확인 주기(기본 30초) 안에 4401로 닫힌다. 응답 · close reason에 회수 사유나 새 로그인의 기기 정보를 싣지 않는다.
- 관제 화면을 상황판(wallboard)으로 상시 띄우려면 그 화면 전용 VIEWER 계정을 따로 둔다. 같은 계정으로 다른 곳에서 로그인하면 상황판 세션이 교체된다.

## 사용자 관리 (PR 3)

계약은 `packages/schemas/api/users.py`, 판정 · 잠금 · 트랜잭션은 `admin_service.py`다. 모든 경로에 Origin(상태 변경만) → 세션 → 관리자 검사(`require_admin`)가 걸린다. 통과하는 행위자는 TENANT ADMIN과 플랫폼 관리 권한자(CENTRAL_ADMIN · TENANT_MANAGER)뿐이며, PLATFORM의 `role=ADMIN`은 근거가 아니다. 미인증은 401, 그 밖의 계정(TENANT OPERATOR · VIEWER, 등급 없는 계정)은 본문 필드 검증과 Cognito 설정 확인보다 먼저 403 `AUTH_FORBIDDEN`이다(DB 변경 · Cognito 호출 없음). (#492) 그다음 서비스가 행위자의 고객 범위(`auth.access.tenant_scope_ids`)로 관리 범위를 정한다. 범위 · 대상 · 테넌트 판정은 모두 Cognito 호출 전이다.

| 행위자 | 관리 범위 (#492) |
| --- | --- |
| PLATFORM CENTRAL_ADMIN | 모든 테넌트의 TENANT 계정. 정지 테넌트 사용자의 역할 · 상태 · 세션 회수도 관리한다(초대 · 재발송은 ACTIVE 테넌트만) |
| PLATFORM TENANT_MANAGER | 배정된 고객의 TENANT 계정만. 그 밖은 중앙 관리자와 같다. 배정이 없으면 목록이 비고, 대상 경로는 404 `USER_NOT_FOUND`, 초대는 404 `TENANT_NOT_FOUND`다 |
| TENANT ADMIN | 자기 테넌트의 TENANT 계정. 소속 테넌트가 정지되면 조회(목록 · 이력)만 하고 쓰기는 409 `TENANT_SUSPENDED` |
| TENANT OPERATOR · VIEWER | 사용자 관리 API를 쓰지 않는다(403 `AUTH_FORBIDDEN`) |

| 경로 | 동작 | 이력 |
| --- | --- | --- |
| `GET /api/v1/users` | 관리 범위의 TENANT 계정 `user_id` · `email` · `role` · `account_type` · `tenant_id` · `status` · `last_login_at`. PLATFORM · UNASSIGNED 계정은 나오지 않는다. `last_login_at`은 그 사용자 세션의 `created_at` 최댓값이다(회수 · 만료 세션 포함, 세션이 없으면 `null`). Cognito를 부르지 않는다 | — |
| `POST /api/v1/users` | 초대 `{email, role, tenant_id, recover_existing}` → 201 `UserListItem`(항상 TENANT 계정) | `USER_INVITED` |
| `POST /api/v1/users/{user_id}/invite/resend` | 초대 재발송 → 204 | `INVITE_RESENT` |
| `PUT /api/v1/users/{user_id}/role` | 역할 변경 → 204 | `ROLE_CHANGED` |
| `PUT /api/v1/users/{user_id}/status` | `ACTIVE` · `DISABLED` 전환 → 204 | `STATUS_CHANGED` |
| `POST /api/v1/users/{user_id}/sessions/revoke` | 대상의 세션 전부 회수 → 204 | `SESSIONS_REVOKED` |
| `GET /api/v1/users/{user_id}/events?limit=` | 대상의 이력, 최신순. `limit` 1–100(기본 50, 범위 밖 422) | — |

없는 `user_id`(UUID 형식이 아닌 값 포함), (#492) 관리 범위 밖의 대상(TENANT_MANAGER에게 배정되지 않은 고객의 사용자 포함), PLATFORM · UNASSIGNED 대상은 모두 같은 404 `USER_NOT_FOUND`이며 Cognito를 부르지 않는다. 목록 · 초대 · 재발송 · 역할 · 상태 · 세션 회수 · 이력 전부에 같은 범위가 걸린다. PLATFORM 계정의 등급 · 상태는 §직원 관리가 다룬다.

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 본인 변경 | 자기 역할 · 상태 변경은 403 `AUTH_FORBIDDEN`. 자기 세션 회수는 허용한다(그 뒤 요청은 401). PLATFORM 행위자 자신은 관리 대상이 아니므로 404다 | [판단] |
| 같은 값 | 현재와 같은 역할 · 상태로 바꾸는 요청은 409 `USER_STATE_CONFLICT` | [판단] |
| 마지막 활성 ADMIN | 활성 ADMIN을 강등 · 비활성화해 그 테넌트에 활성 TENANT ADMIN이 남지 않게 되면 409 `USER_STATE_CONFLICT`. (#492) 테넌트별로 세며 PLATFORM 계정은 세지 않는다. 행위자가 PLATFORM이어도 같다. 집계는 아래 테이블 잠금 안에서 한다 | [판단] |
| 동시성 | 모든 쓰기는 `app_users` 테이블 잠금(`LOCK TABLE app_users IN EXCLUSIVE MODE`) 안에서 한다. 로그인 완료와 재설정 뒤 세션 회수도 같은 잠금을 잡는다. 잠금을 잡은 직후 세션 캐시를 비우고 행위자 세션을 다시 판정한다(`touch=False`). 그 사이 회수 · 비활성 · 강등된 행위자는 401 · 403으로, 대상 고객의 배정이 회수된 TENANT_MANAGER는 404로 끝나고 아무것도 쓰지 않는다. 재판정은 그 시점까지 확정된 변경만 본다. 활성 ADMIN 수도 잠금 안에서 세므로, 두 ADMIN이 동시에 서로를 강등 · 비활성화하면 한쪽만 성공한다 | [판단] |
| 비활성화 | 앱 DB의 `status`만 바꾸고 그 사용자의 세션 전부를 `ACCOUNT_DISABLED`로 회수한다. 재활성화는 회수된 세션을 되살리지 않는다(다시 로그인). Cognito 사용자는 비활성화 · 삭제하지 않는다 — Cognito 인증은 계속 통과하지만 로그인 완료가 403 `ACCOUNT_DISABLED`다 | [판단] |
| 세션 회수 | 대상의 미회수 세션 전부(만료된 행 포함)를 `ADMIN_REVOKED`로 회수하고, 회수한 개수를 이력에 남긴다 | [판단] |
| 초대 | 기본은 새 Cognito 사용자 생성(`AdminCreateUser`)이다. 초대 메일은 Cognito가 이메일로 보내며, 앱은 임시 비밀번호를 요청하지도 읽지도 않는다. Cognito에 이미 있으면 409 `USER_STATE_CONFLICT`이고 자동 연결하지 않는다. Cognito 생성이 성공한 뒤에 앱 사용자(`ACTIVE`, `email_verified_at` 비어 있음)와 `USER_INVITED` 이력을 한 commit으로 만든다. 첫 로그인은 최초 비밀번호 변경 · TOTP 등록 · 이메일 코드를 거친다 | [판단] |
| 기존 계정 연결 | `recover_existing=true`는 플랫폼 관리 권한자(CENTRAL_ADMIN, 배정 고객으로 초대하는 TENANT_MANAGER)만 고를 수 있는 명시적 선택이다. (#492) 그 밖의 행위자(TENANT ADMIN)가 보내면 403 `AUTH_FORBIDDEN`이고, 어댑터 조립 · SDK 호출 · DB 변경이 없다. 이 판정은 쓰기 행위자 판정(401 · 403 · 소속 정지 409 `TENANT_SUSPENDED`) 다음, 초대 대상 테넌트 판정 전이다 — 소속 테넌트가 정지된 TENANT ADMIN은 409 `TENANT_SUSPENDED`가 먼저다. `recover_existing=false`인 일반 초대는 TENANT ADMIN도 자기 테넌트에 그대로 할 수 있다. 플랫폼 관리 권한자의 요청이면 `AdminGetUser`로 기존 Cognito 사용자를 찾아(없으면 404 `USER_NOT_FOUND`) 앱에 아직 없는 신원이면 연결한다(이미 있으면 409). 직전 초대가 실패했는지는 확인하지 않고 Cognito 사용자 상태와 관계없이 연결한다. 이메일 소유 증명이 아니므로 첫 로그인에서 이메일 코드를 요구한다. 이 선택은 기존 Cognito 사용자를 앱 계정에 잇는 것뿐이며 TOTP 분실 복구가 아니다(#515 범위) | [판단] |
| 재발송 | 앱 사용자의 issuer가 설정된 User Pool issuer와 같아야 한다. 앱 사용자의 이메일로 `AdminGetUser`를 호출해, 돌아온 Cognito 사용자의 `sub`가 앱 사용자의 `sub`와 같고 Cognito 상태가 `FORCE_CHANGE_PASSWORD`일 때만 `AdminCreateUser(MessageAction=RESEND)`를 보낸다. Cognito 쪽 이메일 값은 비교하지 않는다. AWS 문서상 RESEND는 새 임시 비밀번호를 보내고 임시 비밀번호 유효 기간을 다시 시작한다. 조건이 다르면 409(재발송 없음), 대상이 없으면 404(Cognito 미호출). SDK 호출 뒤 앱 DB의 대상 신원(issuer · `sub` · 이메일)을 다시 읽어 처음 읽은 값과 다르면 409다(재발송은 이미 나갔고 이력은 남지 않는다) | [표준] [AWS: AdminCreateUser `MessageAction`](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_AdminCreateUser.html) |
| Cognito 오류 | 초대 · 재발송: `UsernameExistsException` · `UnsupportedUserStateException` → 409 `USER_STATE_CONFLICT`, 발송 실패(`CodeDeliveryFailureException` · `InvalidEmailRoleAccessPolicyException`) → 503 `AUTH_EMAIL_DELIVERY_FAILED`, 요청 제한 → 429 `AUTH_RATE_LIMITED`, 그 밖 → 503 `AUTH_PROVIDER_UNAVAILABLE`. `AdminGetUser`는 `UserNotFoundException`일 때만 "없음"이고, 그 밖의 실패 · 비정상 응답은 429 · 503이다 | [판단] |
| Cognito 미설정 | 초대 · 재발송만 503 `AUTH_PROVIDER_UNAVAILABLE`. 목록 · 역할 · 상태 · 세션 회수 · 이력은 동작한다. (#492) 어댑터는 지연 조립(`routers.users.get_cognito_adapter_factory`)이라, 서비스가 행위자 · 대상 · 테넌트 사전 판정(422 · 404 · 409)을 마친 요청만 503에 이른다 — 설정 누락이 그 거절을 가리지 않는다 | [판단] |
| 계정 종류 부여(#492) | 사용자 관리 API로 만드는 계정은 항상 TENANT다. 이 API로 PLATFORM을 주거나 UNASSIGNED를 만들거나 소속(`tenant_id`)을 옮기는 경로가 없다(역할 · 상태 변경 요청에는 소속 칸이 없다). PLATFORM은 운영 CLI(§플랫폼 계정 CLI)로만 생긴다. `platform_role`도 이 API로 주거나 바꾸지 않는다 | [팀 결정: 명시적 플랫폼 권한 · 고객사 범위] [판단: UNASSIGNED] |
| 초대 대상 테넌트(#492) | `tenant_id`는 스키마상 선택이고 최종 값은 서버가 정한다. 플랫폼 관리 권한자는 반드시 보낸다(없으면 422 `REQUEST_VALIDATION_FAILED`, 없는 테넌트와 TENANT_MANAGER에게 배정되지 않은 테넌트는 404 `TENANT_NOT_FOUND`). TENANT ADMIN은 생략하거나 자기 테넌트만 보낼 수 있고, 다른 값은 404 `TENANT_NOT_FOUND`다 | [팀 결정: 고객사 범위] [판단: 요청 형태 · 오류 코드] |
| 테넌트 상태(#492) | 초대 · 재발송은 대상 테넌트가 ACTIVE일 때만 받는다(그 밖은 409 `TENANT_STATE_CONFLICT`). Cognito 호출 전에 확인하고, DB 쓰기 직전에 잠금 안에서 다시 확인한다(테넌트 행 `FOR SHARE` — commit까지 상태 전이를 막는다). 역할 · 상태 · 세션 회수 · 조회는 대상 테넌트 상태를 보지 않는다 | [판단] |
| 정지 테넌트의 행위자(#492) | 소속 테넌트가 SUSPENDED인 TENANT 행위자의 쓰기(초대 · 재발송 · 역할 · 상태 · 세션 회수)는 409 `TENANT_SUSPENDED`이고, 잠금 안에서 다시 판정한다. 조회는 허용한다. 로그인 · 로그아웃 · 활동 · 비밀번호 재설정은 테넌트 정지와 관계없이 동작한다. 사용자 `DISABLED`는 이와 별개 상태다(로그인 403 `ACCOUNT_DISABLED` · 세션 401) | [판단] |

### 부분 실패

Cognito 호출은 DB 트랜잭션 밖에서 DB 쓰기보다 먼저 한다(짧은 읽기 트랜잭션으로 행위자를 확인하고 닫은 뒤 호출). 제공자 쪽이 성공한 뒤 DB 쓰기가 실패하면 500 `INTERNAL_ERROR`(오류 상세 비노출)이고, 제공자 쪽 결과는 되돌리지 않는다. 승인된 한계다 [판단].

SDK 호출 뒤 잠금 안의 재확인(행위자 · 대상 신원 · 대상 테넌트 ACTIVE)은 메일이 이미 나간 뒤에 요청을 거절할 수 있다. 따라서 오류 코드만으로 메일이 나가지 않았다고 판단하지 않는다. 앱은 Cognito 사용자를 자동으로 지우지 않고, 부작용이 있는 호출을 자동으로 재시도하지 않는다 [판단].

| 경우 | 남는 상태 | 복구 |
| --- | --- | --- |
| 초대: Cognito 생성(초대 메일 발송) 뒤 DB 실패 | Cognito 사용자가 남는다. 앱 사용자 · 이력은 없다 | 플랫폼 관리 권한자가 `recover_existing=true`로 다시 초대해 연결 |
| 재발송: RESEND 뒤 DB 실패 | 새 임시 비밀번호 메일은 나갔고 `INVITE_RESENT` 이력은 없다 | 다시 재발송하면 이력이 남는다(새 임시 비밀번호가 다시 나간다) |
| 초대 · 재발송: SDK 호출 중 행위자 세션이 회수 · 비활성 · 강등됨 | 401 · 403. 제공자 결과는 남고 DB는 쓰지 않는다 | 유효한 ADMIN이 다시 수행(초대의 기존 계정 연결은 플랫폼 관리 권한자가 `recover_existing=true`로) |
| (#492) 초대 · 재발송: SDK 호출 뒤 대상 테넌트가 ACTIVE가 아니게 됨 · 행위자 소속 테넌트가 정지됨 | 409(`TENANT_STATE_CONFLICT` · `TENANT_SUSPENDED`). 메일은 나갔을 수 있고 DB는 쓰지 않는다 | 테넌트가 ACTIVE로 돌아온 뒤 유효한 행위자가 다시 수행(초대의 기존 계정 연결은 플랫폼 관리 권한자가 `recover_existing=true`로) |
| 재설정 확정: 비밀번호 변경 뒤 세션 회수 실패 | 비밀번호는 바뀌었고 이전 세션이 남을 수 있다 | §비밀번호 재설정 |

### 이력 (`user_admin_events`)

- 동작 7종: `USER_INVITED` · `INVITE_RESENT` · `ROLE_CHANGED` · `STATUS_CHANGED` · `SESSIONS_REVOKED` · `PLATFORM_ROLE_CHANGED`(직원 관리의 등급 변경) · `TENANT_ASSIGNMENTS_CHANGED`(담당 고객 교체 · 회수). 직원 관리의 상태 변경은 `STATUS_CHANGED`로 남는다. 행위자 · 대상 · 전후 상태 · 시각을 상태 변경과 같은 commit에 넣는다.
- 전후 상태는 `UserEventState`의 닫힌 목록(`role` · `status` · `revoked_session_count` · `recover_existing` · (#492) 초대 대상 `tenant_id` · `platform_role` · 담당 고객 `tenant_ids`)만 저장하고 모르는 키는 거절한다. 이메일 · 비밀번호 · 코드 · 토큰 · Cognito 원본 응답은 싣지 않는다.
- append-only: `BEFORE UPDATE OR DELETE` 트리거가 수정 · 삭제를 거절한다(INSERT만 허용). 이력이 참조하는 사용자는 FK `RESTRICT`로 지울 수 없다. 테이블 소유자(DB owner)는 트리거를 끄고 우회할 수 있으므로 소유자에 대한 방어는 아니다.
- 비밀번호 재설정 · 로그인 교체 · 로그아웃은 이력 대상이 아니다(세션 행의 `revoke_reason`으로만 남는다). 조치 실행 감사 기록(T-2)과는 별개다.
- (#492) CLI의 플랫폼 계정 생성 · 지정 · 중앙 관리자 지정과 그때 비운 배정도 이 이력에 남지 않는다(행위자 앱 계정이 없다. 지정의 세션 회수는 세션 행의 `ADMIN_REVOKED`로만 남는다). 고객 삭제로 함께 지워진 배정도 `TENANT_ASSIGNMENTS_CHANGED`를 남기지 않는다.

### OWASP ASVS 4.0.3 V4와의 관계 (#492)

원문: [V4 Access Control](https://raw.githubusercontent.com/OWASP/ASVS/v4.0.3/4.0/en/0x12-V4-Access-Control.md). 아래는 사용자 관리 API · 직원 관리 API와 계정 종류 · 플랫폼 등급 · 고객 범위 권한(업무 API 포함) 범위에서 정책이 따르는 항목이다. 이 README는 모듈 범위의 정책만 적고 앱 전체의 ASVS 준수를 주장하지 않는다. 표에 없는 V4 항목은 이 문서가 다루지 않는다.

| 항목 | 이 범위의 정책 |
| --- | --- |
| 4.1.1 신뢰할 수 있는 서버 계층에서 접근 제어 강제 | 계정 종류 · 플랫폼 등급 · 역할 · 관리 범위 판정은 서버(`auth.access` · `admin_service` · `staff_service` · `tenant_admin`)가 한다. 화면의 메뉴 숨김 · 첫 화면 분기는 편의이며 차단 근거가 아니다 |
| 4.1.2 접근 제어 속성을 최종 사용자가 조작할 수 없음 | `account_type` · `tenant_id`는 API 입력으로 바꿀 수 없다. 초대의 `tenant_id`는 플랫폼 관리 권한자만 고르고, TENANT ADMIN은 서버가 행위자 소속으로 정한다. 기존 Cognito 계정 연결(`recover_existing`)도 플랫폼 관리 권한자만 고른다. PLATFORM 생성 · 지정과 첫 중앙 관리자 지정은 운영 CLI 직접 실행뿐이다. `platform_role`과 담당 고객 배정은 중앙 관리자가 `/api/v1/staff`에서 다른 PLATFORM 계정의 것만 바꾼다(자기 등급 · 상태 변경 403). TENANT_MANAGER는 자기 배정을 바꾸지 못한다 |
| 4.1.3 최소 권한 | TENANT ADMIN은 자기 테넌트만, OPERATOR · VIEWER는 사용자 관리 없음. 새로 만들거나 지정한 PLATFORM 계정은 담당 고객이 없는 TENANT_MANAGER(고객 데이터 없음)이고, TENANT_MANAGER는 배정 고객만 다루며 고객 생성 · 직원 관리가 없다. 마이그레이션은 기존 PLATFORM의 전역 권한을 넓히거나 좁히지 않고 CENTRAL_ADMIN으로 옮긴다 |
| 4.1.5 안전한 실패 | UNASSIGNED와 그 밖의 종류는 권한 없음으로 처리한다(세션 판정 401, 관리 범위 403). 소속 NULL과 빈 배정을 「전체」로 읽지 않는다. 소속 없는 기록은 전체 범위(CENTRAL_ADMIN)에서만 보인다. 등급이 없거나 PLATFORM이 아닌 계정은 플랫폼 관리 권한이 없고, `role=ADMIN`을 관리 권한으로 읽지 않는다 |
| 4.2.1 IDOR | `user_id` 경로 대상은 관리 범위 안의 TENANT 계정만이다. 범위 밖 · PLATFORM · UNASSIGNED · 없는 대상은 같은 404 `USER_NOT_FOUND`이고 Cognito를 부르지 않는다. `/api/v1/staff`의 대상은 PLATFORM 계정만이고 그 밖은 같은 404다. 고객 · 인시던트도 범위 밖이면 없는 대상과 같은 404(`TENANT_NOT_FOUND` · `INCIDENT_NOT_FOUND`)다 |

## 직원 관리 (#492)

계약은 `packages/schemas/api/staff.py`, 판정 · 잠금 · 트랜잭션은 `staff_service.py`, 라우터는 `routers/staff.py`다. `main.py`가 Origin(상태 변경만) → 세션 배선으로 포함하고, 라우터 의존성 `require_central_admin`이 본문 필드 검증보다 먼저 돈다. 미인증은 401, CENTRAL_ADMIN이 아닌 계정(TENANT_MANAGER — 배정이 있어도 — 과 TENANT 계정 전부)은 403 `AUTH_FORBIDDEN`이며 DB를 바꾸지 않는다. Cognito를 부르지 않는다.

| 경로 | 동작 | 이력 |
| --- | --- | --- |
| `GET /api/v1/staff` | PLATFORM 계정 전부(모든 등급 · 상태)의 `user_id` · `email` · `platform_role` · `status` · `last_login_at`(§사용자 관리 목록과 같은 정의). TENANT · UNASSIGNED 계정은 나오지 않는다. 담당 고객은 싣지 않는다 | — |
| `PUT /api/v1/staff/{user_id}/role` | `{platform_role}`(`CENTRAL_ADMIN` · `TENANT_MANAGER`) → 204. 저장된 담당 고객을 같은 commit에서 비운다 | `PLATFORM_ROLE_CHANGED`(비운 배정이 있으면 `TENANT_ASSIGNMENTS_CHANGED`도) |
| `PUT /api/v1/staff/{user_id}/status` | `{status}`(`ACTIVE` · `DISABLED`) → 204 | `STATUS_CHANGED` |
| `GET /api/v1/staff/{user_id}/tenants` | 대상의 담당 고객 `{tenant_ids: [UUID...]}` | — |
| `PUT /api/v1/staff/{user_id}/tenants` | `{tenant_ids: [UUID...]}`로 담당 고객 전체를 교체 → 204. 빈 목록은 모든 배정 회수 | `TENANT_ASSIGNMENTS_CHANGED`(전후 `tenant_ids`) |
| `GET /api/v1/staff/{user_id}/events?limit=` | 대상의 이력, 최신순. `limit` 1–100(기본 50) | — |

직원을 만들거나 초대하는 경로는 없다 — PLATFORM 계정은 운영 CLI로만 생긴다(§플랫폼 계정 CLI). 요청 본문은 닫힌 모양이라 모르는 칸이나 목록 밖 값(`platform_role: "ADMIN"` · `"STAFF"` 등)은 받지 않는다. 직원 관리 · 담당 고객 배정 웹 화면은 없고 API만 있다.

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 대상 | PLATFORM 계정만. 없는 `user_id`(UUID 형식이 아닌 값 포함) · TENANT · UNASSIGNED 계정은 같은 404 `USER_NOT_FOUND`다. 반대로 `/api/v1/users`는 PLATFORM 계정을 대상으로 받지 않는다(404) | [판단] |
| 본인 변경 | 자기 등급 · 상태 변경은 403 `AUTH_FORBIDDEN` | [판단] |
| 같은 값 | 현재와 같은 등급 · 상태로 바꾸는 요청은 409 `USER_STATE_CONFLICT` | [판단] |
| 복수 중앙 관리자 · 마지막 보호 | 중앙 관리자는 여러 명일 수 있고, 다른 중앙 관리자를 지정 · 강등 · 비활성화할 수 있다. 자기 등급 · 상태 변경은 403이고(본인 변경), 행위자는 잠금 안에서 ACTIVE 중앙 관리자로 다시 판정된다. 그래서 허용되는 강등 · 비활성화에는 언제나 대상과 다른 ACTIVE 중앙 관리자(행위자)가 있고, 변경 뒤에도 그 행위자가 남는다 — API로는 ACTIVE 중앙 관리자가 0명이 되지 않는다. `staff_service`의 마지막 ACTIVE 중앙 관리자 검사(409)는 이 두 판정에 더해 둔 방어 검사라 API 응답으로 나오지 않는다. 집계 · 판정 · 쓰기는 같은 잠금 안이다 | [판단] |
| 동시성 | 모든 쓰기는 사용자 관리와 같은 `app_users` 테이블 잠금 안에서 캐시를 비우고 행위자의 세션 · 등급을 다시 판정한다(`admin_service`의 트랜잭션 공유). 그 사이 회수 · 비활성 · 강등된 중앙 관리자는 401 · 403이고 쓰지 않는다. 두 중앙 관리자가 동시에 서로를 강등 · 비활성화하면 한쪽만 성공한다 | [판단] |
| 비활성화 | 대상의 앱 세션 전부를 같은 commit에서 `ACCOUNT_DISABLED`로 회수한다. 재활성화는 세션을 되살리지 않는다(다시 로그인). Cognito 사용자는 바꾸지 않는다 | [판단] |
| 담당 고객 배정 | 대상은 TENANT_MANAGER뿐이다 — CENTRAL_ADMIN 대상은 409 `USER_STATE_CONFLICT`(전체 범위라 배정이 없다), TENANT · UNASSIGNED · 없는 대상은 404 `USER_NOT_FOUND`다. 요청은 전체 집합을 보내는 교체이고 빈 목록은 모든 배정 회수다. 중복 ID · 형식 오류 · 모르는 칸은 422, 없는 고객이 하나라도 있으면 404 `TENANT_NOT_FOUND`, 지금과 같은 집합이면 409이며 모두 아무것도 쓰지 않는다. 정지된 고객도 배정할 수 있다(그 고객에 대한 조작 가능 여부는 고객 · 조치 쪽 판정이 정한다). 교체와 이력은 `app_users` 잠금 안의 같은 commit이다. 배정은 계정 종류 · 소속을 바꾸지 않는다 | [판단] |
| 등급 변경과 배정 | 등급을 바꾸면 저장된 담당 고객을 같은 commit에서 비운다 — CENTRAL_ADMIN으로 올렸다가 다시 TENANT_MANAGER로 내려도 예전 배정이 되살아나지 않는다. 비운 배정이 있으면 `TENANT_ASSIGNMENTS_CHANGED`(전 → 빈 목록)도 남긴다 | [판단] |
| 변경의 반영 | 등급 · 배정 변경은 세션을 회수하지 않는다. 세션 판정이 요청마다 DB의 현재 `platform_role`과 배정을 읽으므로, 그 계정의 기존 세션은 다음 판정부터 새 범위로 판정된다(`/me`의 `platform_role` · `assigned_tenant_ids`도 바뀐다). 이미 처리 중인 요청, 이미 예약됐거나 실행 중인 작업, 이미 시작한 WebSocket 전송은 취소하지 않는다. 쓰기(사용자 관리 · 직원 관리 · 고객 쓰기 · 조치 예약 · 종료)는 잠금 안에서 다시 판정하므로 그 뒤에 시작한 쓰기는 막히지만, 초대 · 재발송의 Cognito 호출처럼 잠금 전에 나간 외부 호출의 결과는 남을 수 있다(§부분 실패). 열린 WebSocket은 닫히지 않고, 그 뒤 보내는 이벤트부터 새 범위로 거른다. 비활성화의 세션 회수는 재확인 주기 안에 4401로 반영된다 | [판단] |
| 이력 | 등급 변경은 `PLATFORM_ROLE_CHANGED`(전후 `platform_role`), 배정 교체 · 회수는 `TENANT_ASSIGNMENTS_CHANGED`(전후 `tenant_ids`), 상태 변경은 `STATUS_CHANGED`로 상태 변경과 같은 commit에 `user_admin_events`에 남는다(§이력). CLI의 생성 · 지정 · 중앙 관리자 지정과 고객 삭제로 지워진 배정은 남지 않는다. 조치 실행 감사 기록(T-2)과는 별개다 | [판단] |

## 비밀번호 재설정 (PR 3)

공개 경로 2개다(세션 불필요, Origin 검사 적용 — 거절은 Cognito · DB 전 403). 로그인하지 못하는 사용자가 Cognito가 계정 복구용 이메일(`verified_email`)로 보내는 확인 코드로 비밀번호를 바꾼다. 성공해도 세션을 발급하지 않으며, 새 비밀번호로 처음부터 로그인한다(TOTP 필요).

| 경로 | 처리 |
| --- | --- |
| `POST /api/v1/auth/password-reset` `{email}` | `ForgotPassword` → 204 빈 본문. 사용자 없음 · 자격 없음 · 이메일 미인증(`UserNotFoundException` · `NotAuthorizedException` · `InvalidParameterException` · `UserNotConfirmedException`)도 같은 204이고, Cognito의 발송 정보(`CodeDeliveryDetails`)는 버린다. 응답에 계정 존재 여부나 발송 대상을 싣지 않는다. 요청 제한 429 `AUTH_RATE_LIMITED`, 발송 실패 503 `AUTH_EMAIL_DELIVERY_FAILED`, 그 밖 503 `AUTH_PROVIDER_UNAVAILABLE`. 이 429 · 503은 204와 구분되는 응답으로 둔다(계약). 따라서 계정 열거 방지를 완전하다고 주장하지 않는다 |
| `POST /api/v1/auth/password-reset/confirm` `{email, code, new_password}` | ① 스키마: 새 비밀번호 12–256자 · 6자리 코드, 위반 422 `REQUEST_VALIDATION_FAILED` → ② 앱 정책: 위반 422 `AUTH_PASSWORD_POLICY`(Cognito 미호출) → ③ DB 사용 가능 확인: 불가 500 `INTERNAL_ERROR`(Cognito 미호출) → ④ `AdminGetUser`로 `sub` 확인: 실패 429 · 503(비밀번호 변경 전에 중단) → ⑤ `ConfirmForgotPassword` → ⑥ 성공 뒤에만 `app_users` 잠금 안에서 그 사용자의 세션 전부를 `PASSWORD_RESET`으로 회수 → 204(쿠키 없음). Cognito 호출 중에는 DB 트랜잭션을 열어 두지 않는다 |

- 확정 오류: 코드 불일치 · 만료 · 사용자 없음 · `NotAuthorizedException` → 401 `AUTH_CODE_INVALID`(구분하지 않는다), Cognito 정책 거절(`InvalidPasswordException`) → 422 `AUTH_PASSWORD_POLICY`, 요청 제한 → 429, 그 밖 → 503. 실패하면 세션은 그대로다.
- 재설정이 회수하는 것은 이미 발급된 앱 세션뿐이다. 앱은 재설정 때 진행 중인 로그인 흐름을 따로 무효로 만들지 않으며, 재설정 전에 비밀번호 단계를 통과한 흐름을 Cognito가 거절한다고 전제하지 않는다. 그런 흐름은 재설정 뒤에도 TOTP 단계를 마치면 새 세션을 받을 수 있다(TOTP 단계를 건너뛰지는 않는다). 새 로그인에서 비밀번호의 유효성은 Cognito가 판정한다.
- 앱 사용자 행이 없는 `sub`는 회수할 세션이 없으므로 204다.
- **부분 실패**: 비밀번호 변경이 성공한 뒤 세션 회수가 실패하거나, `AdminGetUser`가 "없음"이었는데 확정이 성공하면(회수할 `sub`를 모른다) 500 `INTERNAL_ERROR`다. 비밀번호는 이미 바뀌었고 이전 세션이 남을 수 있다(승인된 한계). 남은 세션은 그 사용자의 다음 로그인 완료(세션 전부 교체) · ADMIN 세션 회수 · 새 재설정 · 만료로 정리된다.
- 로그에는 예외 종류 · AWS 오류 코드만 남긴다. 이메일 · 코드 · 비밀번호 · SQL은 싣지 않는다.

## 새 비밀번호 정책 (PR 3)

적용 대상은 앱 API로 설정하는 새 비밀번호 두 가지 — 최초 비밀번호 변경(`/login/new-password`)과 재설정 확정(`/password-reset/confirm`)이다. 두 경로 모두 `password_policy.check_password`(순수 함수)를 Cognito 호출 전에 부르고, 위반이면 Cognito를 부르지 않는다(최초 변경은 로그인 흐름을 유지한다).

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 길이 | 12–256자(코드 포인트). 스키마(`NewPasswordRequest`)가 막아 위반은 422 `REQUEST_VALIDATION_FAILED`. 256은 Cognito 비밀번호 최대 길이 | [판단] |
| 내용 | 영문 대문자 · 소문자 · 숫자 · 기호를 각 1자 이상. 허용 문자는 기본 라틴 대소문자 · 숫자와 AWS가 기호로 인정하는 ASCII 문장부호 32자(`PASSWORD_SYMBOLS`, 공백 제외)뿐이다. 공백 · 한글 · 그 밖의 비 ASCII · 제어 문자는 거절한다. 위반은 422 `AUTH_PASSWORD_POLICY` | [판단] [AWS: User pool password requirements](https://docs.aws.amazon.com/cognito/latest/developerguide/managing-users-passwords.html) |
| 입력 처리 | trim · 유니코드 정규화 · 대소문자 변환을 하지 않는다. 받은 문자열 그대로 판정하고 그대로 Cognito에 보낸다 | [판단] |
| 적용하지 않는 곳 | 로그인 비밀번호(기존 비밀번호 · 초대 임시 비밀번호)는 이 정책을 보지 않는다(`LoginRequest`는 1–256자만) | [판단] |
| User Pool 정책과의 관계 | 앱 정책은 전제한 Pool 정책(12자 이상, 4종 필수 — §User Pool · 앱 클라이언트 전제)보다 **좁다**. Pool은 앞뒤가 아닌 공백도 기호로 인정하고 최소 문자 충족 뒤에는 다른 문자도 받지만, 앱은 거절한다. 앱은 Pool 정책을 바꾸지 않는다. 앱 API를 거치지 않는 비밀번호 설정(Cognito API · 콘솔 직접 호출)에는 Pool 정책만 적용된다. 앱 정책 통과가 Cognito 수락을 보장하지 않으며, Cognito가 거절하면(`InvalidPasswordException`) 같은 422 `AUTH_PASSWORD_POLICY`다 | [표준] [판단] |
| 화면(`apps/web`) | 조건 6개(길이 · 대문자 · 소문자 · 숫자 · 기호 · 허용 문자만)와 확인 칸 일치를 입력 중에 표시하고, 제출 직전에 같은 판정으로 호출을 막는다(`apps/web/src/lib/password-policy.ts`). 붙여넣기와 비밀번호 관리자(`autocomplete="new-password"`)를 막지 않는다. 한글 입력 경고는 로그인 비밀번호와 이메일 칸(로그인 · 재설정 · 초대)에만 표시하며 값을 바꾸거나 제출을 막지 않는다(`apps/web/src/lib/korean-input.ts`). 이메일 칸은 브라우저 기본 `type="email"` 검증을 쓰고, 서버는 기존 `Email` 계약 외의 규칙을 더하지 않는다 | [판단] |

**KISA 권고와의 관계.** KISA 안내서(2019, KISA-GD-2018-0033)는 5장(5쪽)에서 새 비밀번호를 설정할 때 안전하지 않은 비밀번호를 사용자에게 알리고 거부하도록 권고한다. 이 구현의 조건 표시 · 거절은 그 방향과 같지만 KISA 기준 준수를 주장하지 않는다. ASCII 한정 · 한글 금지 · 기호 허용 목록 · 12자 하한은 KISA 요구가 아니라 Vigilantis 서비스 정책이다.

**OWASP ASVS 4.0.3** — 원문 [XML](https://raw.githubusercontent.com/OWASP/ASVS/v4.0.3/4.0/docs_en/OWASP%20Application%20Security%20Verification%20Standard%204.0.3-en.xml) 기준:

| 항목 | 처분 |
| --- | --- |
| 2.1.2 64자 이상 허용 · 128자 초과 거부 | 128자 초과 거부는 적용하지 않는다. 최대 256자를 유지한다 |
| 2.1.4 공백 · 이모지를 포함한 인쇄 가능한 유니코드 허용 | 적용하지 않는다(ASCII 허용 목록) |
| 2.1.7 유출 비밀번호 확인 · 2.1.9 문자 종류 규칙 금지 | 기존대로 미적용(§정책과 근거) |
| 2.1.8 비밀번호 강도 측정기 | 미적용. 조건 6개와 일치 표시는 강도 측정기가 아니다 |
| 2.5.6 안전한 복구 수단 | 재설정은 Cognito 확인 코드로 하고 재설정 뒤 로그인에도 TOTP가 필요하다. 이 항목의 근거 후보일 뿐 전체 충족을 주장하지 않는다 |
| 2.5.7 OTP · MFA 분실 시 등록 때와 같은 수준의 신원 확인 | 미구현. TOTP 분실 복구는 후속이다(§운영 주의점) |
| 3.3.3 비밀번호 변경 뒤 다른 세션 종료 | 재설정 성공 시 그 사용자의 앱 세션을 자동으로 전부 회수한다(선택이 아니라 자동). 범위는 이 앱의 세션뿐이며 연합 로그인 · 다른 신뢰 당사자까지의 종료를 주장하지 않는다 |
| 3.3.4 사용자가 활성 세션 · 기기를 보고 재인증 뒤 로그아웃 | 미구현. ADMIN 세션 회수만으로는 이 항목을 충족하지 않는다 |

## 화면 (`apps/web`, #492)

화면은 `/me`의 `account_type` · `role` · `platform_role` · `assigned_tenant_ids`만으로 가른다(`apps/web/src/lib/roles.ts` · `home-path.ts`). PLATFORM 계정의 기능은 `role`이 아니라 등급과 배정으로 연다 — 고객 범위가 있는 플랫폼 계정은 CENTRAL_ADMIN과 담당 고객이 하나 이상인 TENANT_MANAGER다(`roles.ts`의 `hasCustomerScope`). 특정 고객에 대한 버튼은 `canAccessTenant`가 정한다. 분기와 숨김은 편의이고 차단은 서버다 — `/me` 값은 낡을 수 있어, 화면이 연 버튼도 서버가 거절할 수 있다.

| 계정 | 첫 화면 | 메뉴 | 업무 조회 · 실시간 |
| --- | --- | --- | --- |
| PLATFORM CENTRAL_ADMIN | `/tenants`(고객) | 대시보드 · 자산 · 보안 · 고객 · 사용자 | 사용 |
| PLATFORM TENANT_MANAGER(담당 고객 있음) | `/tenants`(담당 고객만) | 대시보드 · 자산 · 보안 · 고객 · 사용자 | 사용 — 받는 데이터 · 이벤트는 서버가 담당 고객으로 거른다 |
| PLATFORM TENANT_MANAGER(담당 고객 없음) | `/tenants`(배정 없음 안내) | 고객 | 시작하지 않는다 — `/`는 배정 없음 안내다 |
| TENANT ADMIN | `/users`(사용자) | 사용자 | 시작하지 않는다 — 자산 주기 조회와 WebSocket 연결을 열지 않는다 |
| TENANT OPERATOR · VIEWER | `/`(업무 화면 이용 제한) | 없음(사이드바를 그리지 않는다) | 위와 같음 |
| 계정 확인 중 · 미로그인 · `/me` 실패 | — | 없음(사이드바를 그리지 않는다) | 위와 같음 |

- 첫 화면은 로그인 뒤 돌아갈 곳(`?next=`)이 없을 때의 이동처다(`home-path.ts`의 `homePathFor`). 로그인 직후 `/me`를 읽지 못하면 `/`로 보내 거기서 다시 확인한다.
- `/`(`apps/web/src/app/page.tsx`)는 업무 조회 전에 `/me`로 계정을 확인한다. 고객 범위가 있는 플랫폼 계정에게는 기존 대시보드를, 그 밖의 계정에게는 업무 API를 부르지 않고 짧은 안내(TENANT 계정은 `업무 화면 이용 제한`, 담당 고객이 없는 TENANT_MANAGER는 배정 없음과 중앙 관리자에게 배정을 요청하라는 안내)를 보인다. 이 안내는 서버 403 오류 화면이 아니며, 고객용 업무 대시보드를 여는 것도 아니다. `/me` 실패는 오류 화면으로 알리며(401은 로그인 이동) 이용 제한 안내나 빈 대시보드로 바꾸지 않는다.
- 셸의 자산 주기 조회(수집 · 연결 인디케이터)와 전역 WebSocket(`components/gnb.tsx` · `realtime-provider.tsx`)은 `/me`가 고객 범위가 있는 플랫폼 계정으로 확인된 뒤에만 시작한다. 확인 중 · 미로그인 · 확인 실패 · 담당 고객 없는 TENANT_MANAGER · TENANT 계정에서는 업무 요청을 보내지 않는다. 서버의 업무 API가 TENANT 계정의 자기 범위 조회를 받더라도 고객용 업무 화면은 아직 없다.
- 상단바 오른쪽 이메일(`components/auth/user-menu.tsx`)을 누르면 `계정 정보` 대화상자가 열린다(별도 화면은 없다). SessionProvider가 이미 가진 `/me` 값으로 이메일 · 역할(사용자 관리와 같은 이름. PLATFORM은 `platform_role` 이름 — 중앙 관리자 · 고객 담당자이며, 호환 값 `role=ADMIN`을 관리자로 보이지 않는다) · 계정 종류를 보인다. TENANT 계정이면 자기 고객 ID를, PLATFORM 계정이면 고객 범위(전체 고객 · 담당 고객 N곳 · 배정 없음)를 더하며 담당 고객 ID는 나열하지 않는다(`apps/web/src/lib/account-summary.ts`). 고객 이름은 `/me` 계약에 없어 보이지 않으며, 이를 위해 새 조회 API · PLATFORM 전용 테넌트 API를 부르거나 공개 계약을 넓히지 않는다.
- TENANT 계정이 화면에서 쓸 수 있는 것은 TENANT ADMIN의 자기 테넌트 사용자 관리다. 자산 · 보안 화면과 조치 실행 · 인시던트 종료 버튼은 TENANT 계정에게 열리지 않는다 — 메트릭 · WebSocket이 TENANT 계정에게 막혀 있고 고객용 화면은 T-4 · T-5 몫이다(§접근 보호 업무 범위).
- 조치 버튼은 고객 범위가 있는 플랫폼 계정과 TENANT ADMIN · OPERATOR에게 열린다(`roles.ts`의 `canOperate`). 인시던트 응답에 고객 ID가 없어 대상별 판정은 서버가 하고(범위 밖 404 · 403), 담당 고객이 없는 TENANT_MANAGER에게는 배정 없음을 이유로 든다.
- 사용자 변경 이력은 기존 ADMIN 관리 범위의 `user_admin_events` 조회(§이력)이며, 로그인 기록이나 조치 실행 감사 기록(T-2)을 포함한 전체 감사가 아니다.
- 사용자 관리 화면은 고객 ID(`tenant_id`) 열을 보인다. 고객 범위가 있는 플랫폼 계정은 초대할 고객 ID(UUID)를 직접 입력하며, TENANT_MANAGER가 담당이 아닌 고객 ID를 넣으면 화면이 보내지 않는다(서버도 404). TENANT ADMIN은 자기 소속을 읽기 전용으로 보며 다른 고객을 고를 수 없다. 초대의 기존 계정 연결(복구) 선택은 고객 범위가 있는 플랫폼 계정에게만 보이고, 그 밖의 계정의 초대 본문은 언제나 `recover_existing=false`다(`apps/web/src/lib/user-admin.ts`의 `inviteRequestFor`). 담당 고객이 없는 TENANT_MANAGER에게는 사용자 메뉴가 없고, 주소로 들어오면 서버 응답에 따른 안내를 본다(`apps/web/src/app/users/page.tsx`).
- 오류는 서버 판정대로 알린다 — 범위 밖 사용자는 대상 없음과 같은 문구, 고객 상태 거절은 고객 사유, 초대 · 재발송의 500 · 네트워크 실패는 메일이 나갔을 수 있다는 안내다(`apps/web/src/lib/user-admin.ts`).
- 백엔드에는 테넌트 목록 · 상세 API(`GET /api/v1/tenants` · `/api/v1/tenants/{id}`, PLATFORM 전용)가 있지만, 사용자 관리 화면은 이를 쓰지 않아 고객 선택 목록이 없다. 초대할 고객 ID는 위처럼 직접 입력한다. `/tenants` 화면과 테넌트 API 자체는 이 모듈의 범위가 아니다.
- 고객 화면(`components/tenants/tenants-view.tsx` · `tenant-detail-view.tsx`)의 목록은 서버가 정한 범위(중앙 관리자 전체 · TENANT_MANAGER는 담당 고객)이고, 담당 고객이 없으면 배정 없음 안내를 보인다. 고객 추가는 CENTRAL_ADMIN에게만, 상태 동작 · 자동 조치 전환 · 삭제 · 온보딩 링크 보기는 CENTRAL_ADMIN과 그 고객의 담당자에게만 그린다. 숨김은 편의이고 차단은 서버 403 · 404다.
- 직원 관리와 담당 고객 배정 화면은 없다. 등급 · 상태 · 배정은 중앙 관리자가 `/api/v1/staff`를 직접 호출해 바꾼다(§직원 관리).

## 설정

`apps/core-api/config.py`의 `AuthSettings`(PR 2의 두 키는 `Settings`). 환경변수와 루트 `.env`를 읽는다. 키 목록과 설명은 루트 `.env.example`에 있다. #492는 설정 키를 추가하지 않으며, 업무 범위 · 플랫폼 등급 · 담당 고객 배정에도 켜고 끄는 설정이 없다.

| 키 | 기본값 | 비고 |
| --- | --- | --- |
| `COGNITO_REGION` · `COGNITO_USER_POOL_ID` · `COGNITO_CLIENT_ID` · `COGNITO_CLIENT_SECRET` | 없음 | 하나라도 비면 앱은 뜨고, Cognito를 부르는 경로 — 로그인 6단계 · 비밀번호 재설정 2종 · 초대(`POST /api/v1/users`) · 재발송 — 만 503 `AUTH_PROVIDER_UNAVAILABLE`이다. `/me` · `/activity` · `/logout`, 그 밖의 사용자 관리 경로와 보호 API는 미설정이어도 동작한다(PR 2까지는 `/api/v1/auth/*` 전체가 503). 관리 권한이 없는 계정(TENANT OPERATOR · VIEWER 등)의 초대 · 재발송은 설정과 관계없이 403이 먼저이고, (#492) 범위 · 테넌트 사전 판정(422 · 404 · 409)과 TENANT ADMIN의 기존 계정 연결 거절(403)도 503보다 먼저다(§사용자 관리). 기동 로그에 누락 키 이름만 남긴다 |
| `COGNITO_AWS_PROFILE` | 없음 | 로컬 개발에서 이름 있는 AWS 프로파일을 쓸 때만. 배포는 비운다(태스크 역할) |
| `AUTH_SESSION_MAX_SECONDS` | 28800 | |
| `AUTH_SESSION_IDLE_SECONDS` | 1800 | |
| `AUTH_TOTP_MAX_ATTEMPTS` | 3 | 한 로그인 흐름의 코드 제출 횟수 |
| `AUTH_CODE_FAILURE_LIMIT` | 10 | 사용자별 코드 실패 한도 |
| `AUTH_CODE_FAILURE_WINDOW_SECONDS` | 900 | 사용자별 한도의 슬라이딩 윈도(초) |
| `AUTH_ALLOWED_ORIGINS` | `CORS_ALLOW_ORIGINS`와 같음 | 콤마 구분, 정확히 일치, `*` 금지 |
| `WS_SESSION_CHECK_SECONDS` | 30 | `Settings`. 열린 WebSocket의 세션 재확인 주기(초, 양수) — 회수 · 비활성 반영 지연의 상한 |
| `API_DOCS_ENABLED` | false | `Settings`. API 문서 3종을 연다. 로컬 개발만 켠다 |

Cognito 클라이언트는 `https://cognito-idp.{region}.amazonaws.com`으로 엔드포인트를 고정한다. `AWS_ENDPOINT_URL`(LocalStack)이 설정돼 있어도 인증 트래픽은 실제 Cognito로 간다.

### User Pool · 앱 클라이언트 전제

배포 환경에서 이 구현의 동작과 맞는지 확인하고 맞출 User Pool · 앱 클라이언트 설정이다. 특정 환경의 현재 값은 이 문서가 보증하지 않으며, 배포 환경마다 확인한다.

- Essentials, `MfaConfiguration=ON`, Software token MFA만 활성, 관리자만 사용자 생성, 사용자 이름은 email(대소문자 구분 없음), 복구는 `verified_email`
- 비밀번호 정책은 최소 12자 · 대문자 · 소문자 · 숫자 · 기호 필수. 관리자가 만든 임시 비밀번호는 Pool의 `TemporaryPasswordValidityDays` 기간이 지나면 만료되며, 기간이 지난 초대는 재발송으로 새 임시 비밀번호를 보낸다
- 앱 클라이언트: client secret 있음, `ExplicitAuthFlows`에 `ALLOW_ADMIN_USER_PASSWORD_AUTH`, 토큰 회수 켜짐, `AuthSessionValidity=3`(분), 존재 노출 방지 켜짐
- 기기 추적(`DeviceConfiguration`) 꺼짐
- 실행 주체에 `cognito-idp:AdminInitiateAuth` · `cognito-idp:AdminRespondToAuthChallenge`, PR 3부터 `cognito-idp:AdminCreateUser`(초대 · 재발송) · `cognito-idp:AdminGetUser`(기존 계정 연결 · 재발송 · 재설정 확정)(모두 해당 User Pool 한정). 그 밖의 호출(`AssociateSoftwareToken` · `VerifySoftwareToken` · `GetUser` · `GetUserAttributeVerificationCode` · `VerifyUserAttribute` · `RevokeToken`)은 세션 · 토큰으로 승인돼 IAM 권한이 필요 없다. `ForgotPassword` · `ConfirmForgotPassword`는 IAM이 아니라 client secret으로 계산한 `SECRET_HASH`로 호출한다. 배포 역할에 이 권한을 주는 것은 이 모듈 밖의 배포 설정 몫이며, 특정 환경의 반영 여부는 이 문서가 보증하지 않는다
- SDK: 잠금된 boto3 · botocore 1.43.70에 PR 3이 쓰는 호출이 모두 있어 의존성을 바꾸지 않았다

## 플랫폼 계정 CLI (#492)

```bash
cd apps/core-api
# 직원 — 담당 고객이 없는 TENANT_MANAGER(로그인은 되지만 고객 데이터를 보지 못한다)
uv run python -m auth.bootstrap_admin --platform --email ops@example.com                                         # 생성 미리보기(변경 없음)
uv run python -m auth.bootstrap_admin --platform --email ops@example.com --yes                                   # Cognito 사용자 생성(초대 메일) + 등록
uv run python -m auth.bootstrap_admin --platform --existing-cognito-user --email ops@example.com --yes           # 이미 있는 Cognito 사용자를 명시 연결
uv run python -m auth.bootstrap_admin --designate-unassigned-user-id <user_id>                                   # 지정 미리보기
uv run python -m auth.bootstrap_admin --designate-unassigned-user-id <user_id> --yes                             # UNASSIGNED → PLATFORM TENANT_MANAGER(배정 없음) 지정
# 첫 중앙 관리자(CENTRAL_ADMIN) — ACTIVE 중앙 관리자가 0명일 때만
uv run python -m auth.bootstrap_admin --platform --central-admin --email ops@example.com                         # 생성 미리보기
uv run python -m auth.bootstrap_admin --platform --central-admin --email ops@example.com --yes                   # 새 계정을 중앙 관리자로 생성
uv run python -m auth.bootstrap_admin --bootstrap-central-admin-user-id <user_id> --email ops@example.com        # 기존 PLATFORM 계정 지정 미리보기
uv run python -m auth.bootstrap_admin --bootstrap-central-admin-user-id <user_id> --email ops@example.com --yes  # 기존 PLATFORM 계정(TENANT_MANAGER) → CENTRAL_ADMIN
```

- 기본은 미리보기이고 `--yes`일 때만 바꾼다. 미리보기는 Cognito를 부르지 않으며, 지정 · 중앙 관리자 지정의 미리보기는 DB로 확인할 수 있는 거절 사유(대상 · ACTIVE 중앙 관리자 존재)를 먼저 알린다. 종료 코드는 0 성공 · 1 실패 · 2 거절이다.
- **생성**: `--platform`과 `--email`이 필요하다(ADMIN이라고 플랫폼으로 추론하지 않는다). `app_users`에 행이 있어도 추가한다. 만든 계정은 `account_type=PLATFORM` · `platform_role=TENANT_MANAGER`(담당 고객 없음) · `role=ADMIN`(호환 값) · `tenant_id` NULL이다. 로그인은 되지만 중앙 관리자가 담당 고객을 배정하기 전에는 고객 데이터 · 조치 · 고객 사용자 관리가 없다. Cognito 설정 4키가 필요하다(미리보기 포함). 새 사용자는 `AdminCreateUser`, `--existing-cognito-user`는 `AdminGetUser`를 부른다.
- **첫 중앙 관리자 생성**: `--central-admin`을 더하면 만드는 계정이 `CENTRAL_ADMIN`이다. ACTIVE 중앙 관리자가 있으면 Cognito 호출 전에 거절하고(2), 삽입 직전 잠금 안에서 다시 센다. Cognito 사용자를 만든 뒤 그 사이 중앙 관리자가 생겨 거절되면 Cognito 사용자는 남는다 — 배정 없는 TENANT_MANAGER로 연결하려면 `--existing-cognito-user`로 다시 실행한다.
- **기존 계정 지정**: `--bootstrap-central-admin-user-id`는 `--email` · `--yes`만 함께 쓴다(`--platform` · `--existing-cognito-user` · `--central-admin`과 함께 쓰면 거절 — 계정을 만들지 않는다). 대상은 아직 CENTRAL_ADMIN이 아닌 ACTIVE PLATFORM 계정(TENANT_MANAGER)뿐이고, TENANT · UNASSIGNED · 비활성 계정은 거절한다(계정 종류 · 소속을 바꾸지 않는다). 계정의 issuer가 설정된 User Pool issuer와 같아야 하고, `--yes`일 때 `AdminGetUser(--email)`가 돌려준 `sub`가 계정의 `cognito_sub`와 같아야 한다. Cognito 호출은 DB 트랜잭션 밖이고, 잠금 안에서 상태 · 신원 · ACTIVE 중앙 관리자 0명을 다시 확인한 뒤 `platform_role`을 바꾸고 그 계정의 담당 고객 배정을 같은 commit에서 비운다(나중에 담당자로 내려도 예전 배정이 되살아나지 않는다). 세션은 회수하지 않는다 — 그 계정의 기존 세션은 다음 판정부터 새 등급으로 판정된다. 대상 `user_id`는 그 계정의 `GET /api/v1/auth/me` 응답에 있다. Cognito 설정 4키가 필요하다.
- 중앙 관리자 지정은 ACTIVE 중앙 관리자가 0명일 때만 된다(첫 설치, 기존 PLATFORM이 없던 DB, 또는 전원 비활성 뒤 복구). 마이그레이션이 기존 ACTIVE PLATFORM 계정을 CENTRAL_ADMIN으로 옮긴 DB에서는 필요 없고 거절된다. 추가 중앙 관리자 지정 · 강등 · 비활성화와 담당 고객 배정은 중앙 관리자가 `/api/v1/staff`에서 한다(§직원 관리). CLI에는 배정 · 강등 · 비활성화 · 삭제 경로가 없다.
- 같은 신원 `(issuer, sub)`이 이미 `app_users`에 있으면 생성을 거절하고 기존 계정을 바꾸지 않는다 — TENANT 소속을 옮기거나 플랫폼으로 바꾸지 않는다. 확인과 삽입은 같은 테이블 잠금 안에서 한다.
- 기존 Cognito 사용자 연결은 운영자가 `--existing-cognito-user`로 명시할 때만 한다(`AdminGetUser`의 `sub`로 연결).
- **지정**: `--designate-unassigned-user-id`는 생성 · 중앙 관리자 지정 플래그(`--email` · `--platform` · `--existing-cognito-user` · `--central-admin` · `--bootstrap-central-admin-user-id`)와 함께 쓸 수 없다(지정 등급은 배정 없는 TENANT_MANAGER뿐이다). 대상은 ACTIVE이고 `tenant_id` NULL인 UNASSIGNED 계정뿐이며, TENANT · 비활성 계정은 거절한다(복구하지 않는다). Cognito를 부르지 않아 Cognito 클라이언트 · 설정이 없어도 실행된다. 테이블 잠금 안에서 다시 확인한 뒤 `role=ADMIN` · `account_type=PLATFORM` · `platform_role=TENANT_MANAGER`(배정 없음) 변경과 기존 세션의 `ADMIN_REVOKED` 회수를 한 commit으로 한다. 대상은 새로 로그인해야 한다.
- 생성 · 지정 · 중앙 관리자 지정 모두 Cognito의 비밀번호 · TOTP · MFA 설정을 바꾸지 않는다. TOTP 분실 복구는 여전히 [#515](https://github.com/ProjectVigilantis/vigilantis/issues/515) 몫이다.
- PLATFORM 계정의 등급 · 상태 · 담당 고객 변경은 §직원 관리에서만 하고, 삭제 경로는 없다. 사용자 관리 API는 PLATFORM 계정을 대상으로 받지 않는다(404).
- 고객사 사용자(TENANT)는 CENTRAL_ADMIN, 그 고객이 배정된 TENANT_MANAGER, 또는 그 테넌트의 ADMIN이 `POST /api/v1/users`로 초대한다(§사용자 관리).
- 임시 비밀번호는 Cognito가 초대 메일로만 보낸다. CLI는 비밀값 · 토큰을 출력하지 않는다. 미리보기 · 결과에는 User Pool ID · 이메일 · `user_id` · 등급 · Cognito 사용자 상태 · 회수한 세션 수만 싣고, Cognito 실패는 오류 코드만 출력한다.
- Cognito 사용자 생성 뒤 DB 등록이 실패하면 Cognito 사용자는 남는다. 이때는 `--existing-cognito-user`로 다시 연결한다.
- PLATFORM 계정을 만드는 다른 경로(공개 등록 API · 화면 · 직원 관리 API)는 없다.

## 테스트

| 구분 | 파일 | 보장 범위 |
| --- | --- | --- |
| 계약 | `packages/schemas/tests/test_api_auth.py` · `test_api_errors.py` | 단계 · 역할 · 오류 코드 집합(계약 원문 리터럴과 정확히 일치 — PR 2의 `AUTH_FORBIDDEN`, PR 3의 `USER_NOT_FOUND` · `USER_STATE_CONFLICT` 포함), 비밀값 비노출, 입력 경계. (PR 3) 재설정 요청이 새 비밀번호 길이 · 코드 계약을 공유하고 두 비밀값을 숨김 |
| 계약(PR 3) | `packages/schemas/tests/test_api_users.py` | 기존 계정 연결은 `recover_existing`을 명시할 때만, 잘못된 역할 · 상태 값 거절, 이력 상태에 비밀값 필드를 받을 수 없고 닫힌 타입 값만 받음 |
| 순수 함수(PR 3) | `apps/core-api/tests/test_auth_password_policy.py` | 기호 목록이 정확히 ASCII 문장부호 32자, 길이 경계, 문자 종류 하나라도 없으면 거절, 각 기호의 기호 인정, 위치와 관계없는 공백 거절(trim 없음), 비 ASCII · 제어 문자 거절, 한글이 문자 종류를 대신하지 못함 |
| 대역(botocore Stubber) | `apps/core-api/tests/test_auth_cognito.py` · `test_auth_login_flow.py` | 요청 파라미터(`USER_ID_FOR_SRP` · `SECRET_HASH`), 세션 발급 조건, 예상 밖 토큰 · challenge 거절, 시도 상한, 흐름 만료, refresh token 회수, 로그 · 예외에 비밀값 없음 |
| 실제 PostgreSQL | `apps/core-api/tests/test_auth_sessions.py` · `test_auth_api.py` · `test_auth_bootstrap_admin.py` · `db/tests/test_auth_*.py` | 해시만 저장, **유휴 30분 · 최대 8시간 만료, `GET /me` 반복이 유휴를 늘리지 않음**, 활동이 최대 수명을 넘지 않음, 회수, `(issuer, sub)` 유일, 쿠키 속성, Origin 거절, 보안 헤더, 미구성 시 503, CLI |
| 실제 PostgreSQL(PR 2) | `apps/core-api/tests/test_api_access.py` | 경로 전수(공개 목록을 실제 경로와 정확히 대조, 그 밖은 미인증 401 · WebSocket 1008), (#492 · #533 갱신분은 아래 #492 · 플랫폼 등급 행), 무효 세션 5종(유휴 · 최대 수명 · `LOGOUT` · `SUPERSEDED` · 비활성) 401, 없는 · 외부 · `null` Origin이 세션 조회 전 403, 보호 API 응답(200 · 401 · 403 · 404 · 422 · 500)의 보안 헤더 3종, 보호 요청이 `last_activity_at`을 바꾸지 않음, API 문서 토글, 접근 로그의 `user_id` · `role`과 쿠키 · 토큰 · 이메일 부재 |
| 실제 PostgreSQL · commit 세션(PR 2) | `apps/core-api/tests/test_ws_access.py` · `test_ws_connect.py` · `test_ws_publish.py` | Handshake 1008(세션 없음 · 모르는 토큰 · 회수 · 비활성 · 외부 Origin, `AUTH_ALLOWED_ORIGINS`가 더 좁을 때 CORS에만 있는 Origin — HTTP 403과 같은 목록), 연결 중 `LOGOUT` · `SUPERSEDED` · 비활성 · 유휴 만료 → 4401 `"AUTH_REQUIRED"`, 주기보다 이른 만료 시각의 재확인, 연결 · 재확인이 `last_activity_at`을 바꾸지 않음, 재확인 실패 1011(로그에 오류 종류만) |
| 대역(PR 3) | `apps/core-api/tests/test_auth_cognito.py` · `test_auth_login_flow.py` | `AdminGetUser`는 `UserNotFoundException`만 "없음"(그 밖의 실패 · 네트워크 오류 · 비정상 응답은 장애), `AdminCreateUser` 생성 · RESEND 파라미터와 오류 매핑, `ForgotPassword` 파라미터 · 발송 정보 폐기 · 존재/자격 오류 정규화 · 그 밖의 오류 노출, `ConfirmForgotPassword` 파라미터와 오류 매핑, 최초 비밀번호 변경의 앱 정책 위반이 Cognito 호출 전에 거절되고 흐름 유지 |
| 실제 PostgreSQL + 대역(PR 3) | `apps/core-api/tests/test_auth_password_reset.py` | 요청: 정확한 파라미터 · 빈 204, 없음 · 부적격 · 미인증이 같은 응답, 제한 · 발송 · 장애 오류 노출. 두 경로 모두 Origin 거절 시 Cognito 미호출. 확정: 스키마 422 · 앱 정책 422가 Cognito 전, 성공 시 그 사용자 세션만 `PASSWORD_RESET` 회수 · 쿠키 없음, 앱 행 없는 `sub`는 204, `sub`를 모르는 성공은 500, 실패 매핑과 세션 유지, 조회 실패는 변경 전 중단, 변경 뒤 회수 실패 500(이전 세션 남음), 변경 전 DB 불가 500(Cognito 미호출), 로그 · 응답에 코드 · 비밀번호 없음 |
| 실제 PostgreSQL(PR 3) | `apps/core-api/tests/test_user_admin_api.py` | 비ADMIN은 모든 경로(잘못된 본문 포함) 403 · DB 불변 · Cognito 미호출, 미인증 401, 목록의 `last_login_at`, 이력 `limit` 경계 · 없는 대상, 초대 · 중복 409(자동 연결 없음) · 제공자 실패 · DB 실패 500(앱 행 없음, 제공자 사용자 유지), `recover_existing` 연결 · 404 · 409, 재발송과 상태 불일치 409, 역할 · 상태 변경과 본인 403 · 같은 값 409 · 마지막 활성 ADMIN 409, 비활성화의 세션 회수와 재활성화 뒤 미복구, 세션 회수 개수 · 본인 회수, Cognito 미설정 시 403 → 503 순서와 그 밖의 경로 동작, 행위자 토큰 repr 비노출. 독립 commit 세션: 잠금 대기 중 회수 · 비활성 · 강등된 행위자는 아무것도 쓰지 않음, SDK 호출 중 회수된 행위자(제공자 결과만 남음), 두 ADMIN의 동시 상호 강등 · 비활성화에서 한쪽만 성공 |
| 실제 PostgreSQL + 대역(#492) | `apps/core-api/tests/test_user_admin_api.py` | 관리 범위: 대상 경로 목록이 사용자 단위 경로 전부와 일치(`test_target_ops_cover_every_user_scoped_route`), 범위 밖 대상(PLATFORM 계정 — PLATFORM 행위자 본인 포함 · UNASSIGNED, TENANT ADMIN에게는 다른 테넌트 · 정지된 다른 테넌트의 사용자)이 없는 대상과 같은 404 `USER_NOT_FOUND` · 같은 메시지이고 Cognito 미호출 · DB 무변화, 목록은 PLATFORM이 모든 테넌트 · TENANT ADMIN이 자기 테넌트만, PLATFORM이 어느 테넌트의 사용자든 관리(정지 테넌트 사용자는 재발송 밖 전부), TENANT OPERATOR · VIEWER는 테넌트 상태와 관계없이 모든 경로 403. 초대 · 재발송: PLATFORM 초대는 테넌트 명시 필수, 계정 종류 지정 불가, 없는 테넌트 404 `TENANT_NOT_FOUND` · ACTIVE 아닌 테넌트 409 `TENANT_STATE_CONFLICT`가 SDK 호출 전, TENANT ADMIN 초대는 자기 테넌트에 묶임, TENANT ADMIN의 `recover_existing`은 403 `AUTH_FORBIDDEN` · SDK 0회 · DB 무변화(소속 정지면 409 `TENANT_SUSPENDED`가 먼저)이고 일반 초대는 허용, `recover_existing` 409에서 기존 소속 불변, SDK 호출 뒤 테넌트 재확인 거절 시 아무것도 쓰지 않음. Cognito 미설정이어도 사전 판정(422 · 404 · 409)이 503보다 먼저. 정지 테넌트 ADMIN: `/me` · 목록 · 닫힌 필드만 담긴 이력 조회 · 활동은 되고 모든 변경은 409 `TENANT_SUSPENDED` · Cognito 미호출 · DB 무변화. 테넌트별 마지막 활성 ADMIN. 독립 commit 세션: 같은 테넌트의 두 TENANT ADMIN, 또는 두 PLATFORM 행위자가 마지막 두 ADMIN을 동시에 제거하면 한쪽만 성공, 재확인이 테넌트 행을 commit까지 잡아 그 사이 정지를 막음(`test_tenant_recheck_holds_the_tenant_row_until_commit`) |
| 실제 PostgreSQL(PR 3) | `apps/core-api/tests/test_auth_login_single_session.py` | 다른 브라우저의 두 번째 로그인이 첫 세션 교체, 다른 사용자의 이전 쿠키도 교체, 비활성 · 미등록 거절 시 변경 없음, 사용자 단위 일괄 회수, 동시 로그인 뒤 살아 있는 세션 정확히 1개 |
| 실제 PostgreSQL(PR 3) | `apps/core-api/db/tests/test_user_admin.py` · `test_user_admin_migration.py` | 회수 사유 enum, 마지막 로그인 = 회수 세션 포함 최신 세션, 활성 ADMIN 집계, 외부 변경 뒤 최신 값 읽기, 만료 포함 일괄 회수, 이력 최신순 · 대상별, 허용 목록 밖 상태 거절, UPDATE · DELETE 거절 · INSERT 허용, 이력이 참조하는 사용자 삭제 불가, 단일 head · 왕복 마이그레이션 |
| 실제 PostgreSQL(PR 3 갱신) | `apps/core-api/tests/test_api_access.py` · `test_auth_api.py` | 공개 목록에 재설정 2종 추가, 사용자 관리 경로도 경로 전수 검사 대상(미인증 401). Cognito 미설정 시 로그인 · 재설정만 503이고 `/logout` · `/activity` · `/me` · 보호 API는 동작 |
| 실제 PostgreSQL(#492) | `apps/core-api/db/tests/test_user_account_type_migration.py` · `test_auth_models.py` · `test_user_admin.py` | 부모 revision(`e492a3b5c701` → `f492c4a8d2e1`)과 그래프의 head 1개, head 스키마와 ORM 일치 · 기본값 없음, 기존 행 분류(승격 없음)와 왕복, `account_type` 없는 INSERT 거절, CHECK 2개의 거절 · 허용 형태, 목록은 TENANT 계정만 · 테넌트 범위, 테넌트별 활성 ADMIN 집계 |
| 실제 PostgreSQL(#492 · #533) | `apps/core-api/tests/test_api_access.py` · `test_ws_access.py` | TENANT 계정은 자기 범위의 업무 기록만 읽고 메트릭은 관리 등급 전용, 범위 경로 밖의 업무 라우트, 플랫폼 판정이 역할이 아니라 `account_type`을 봄, 관리 판정이 명시 등급을 봄, 고객 범위가 명시적이고 닫힌 쪽으로 실패하며 계정 종류로 전체 범위를 주지 않음, 인시던트 조회가 범위를 따름, TENANT_MANAGER의 변경이 배정된 기록에만 닿고 배정 회수가 다음 요청에 반영됨, PLATFORM의 조회 · 실행 · 종료, PLATFORM이 아닌 계정의 Handshake가 accept 전 거절 |
| 실제 PostgreSQL(#492 · #533) | `apps/core-api/tests/test_ws_event_scope.py` | TENANT_MANAGER는 배정 고객 인시던트의 이벤트만 · CENTRAL_ADMIN은 전부 받음, 큐에 넣은 뒤 회수된 권한으로는 보내지 않음, 판정 오류는 보내지 않는 쪽으로 실패하고 상세를 남기지 않음, 저장된 인시던트 고객으로 거르고 배정 변경을 따름 |
| 실제 PostgreSQL + 대역(#492 · #533) | `apps/core-api/tests/test_metrics_api.py` | 여섯 축이 범위 안 소유만 집계하고 그 소유 고객의 자격으로만 CloudWatch를 부름, 담당 변경이 다음 요청부터 반영, 같은 리전 · 인스턴스 ID라도 소유별로 따로 부르고 결과를 섞지 않음, 쓸 수 없는 고객 자격은 AWS 호출 없이 UNAVAILABLE, 역할 ARN · ExternalId 변경 시 새 자격으로 다시 부름 |
| 실제 PostgreSQL(#533) | `apps/core-api/tests/test_incident_intake.py` | 새 인시던트가 그 계정에 등록된 고객을 소속으로 받음, 등록 고객이 없는 계정은 소속 없음, 중복 접수가 기존 인시던트의 소속을 바꾸지 않음 |
| 실제 PostgreSQL(#492) | `apps/core-api/tests/test_auth_api.py` · `test_auth_login_single_session.py` · `test_auth_password_reset.py` | PLATFORM `/me`의 `tenant_id` null, UNASSIGNED 로그인 완료 403 · 발급 없음과 기존 세션 401, 정지 테넌트 사용자의 로그인 · `/me` · 활동 · 로그아웃 · 재설정, 테넌트 정지의 재로그인 없는 반영, 정지 테넌트의 DISABLED 사용자 |
| 실제 PostgreSQL + 대역(#492) | `apps/core-api/tests/test_auth_bootstrap_admin.py` | `--platform` · `--email` 없는 생성 거절(SDK 호출 0회), 사용자가 있어도 추가 플랫폼 계정, 기존 신원 이전 없음, 지정 미리보기 무변경, 지정의 변경 · 세션 회수(SDK 호출 0회), 부적격 대상 거절, 생성 플래그 혼용 거절, 잠금 안 재확인, 같은 신원 동시 생성에서 1건만 |
| 계약(#492) | `packages/schemas/tests/test_api_auth.py` · `test_api_users.py` | `AccountScope` 짝 규칙, 초대의 `tenant_id`는 플랫폼 권한 부여가 아님, 관리 요청으로 소속을 바꿀 수 없음 |
| 웹 유닛(#492) | `apps/web/src/lib/home-path.test.ts` · `account-summary.test.ts` · `user-admin.test.ts` | 계정 종류별 첫 화면 · 메뉴, 상단바 계정 정보 항목(TENANT만 고객 ID), 초대 본문(PLATFORM은 고객 ID 필수 · TENANT ADMIN은 생략하고 기존 계정 연결은 언제나 false), 범위 밖 · 고객 상태 · 결과 불명 오류 문구 |
| 실제 PostgreSQL(#492) | `apps/core-api/db/tests/test_account_scope_merge_migration.py` | 병합 revision `c492d6e8f4a2`의 부모 2개(`f492c4a8d2e1` · `f514a6c3d201`) · head 1개와 두 가지가 `e492a3b5c701`에서 갈라지는 부모 관계, 새 DB의 base → head, #492 가지 끝(`f492c4a8d2e1`) · dev 가지 끝(`f514a6c3d201`)에서 head로 앞으로만 올린 DB가 두 열(`app_users.account_type` · `tenants.auto_action_resumed_at`)을 모두 갖고 그 revision 모양으로 넣은 테넌트 · 사용자 행을 보존하며 기록 revision이 병합 revision을 지난 단일 head가 되고 ORM과 일치, dev 가지의 소속 사용자는 `TENANT`로 분류(승격 없음). 테스트마다 일회용 DB를 만들고 지운다 |
| 실제 PostgreSQL(플랫폼 등급) | `apps/core-api/db/tests/test_user_platform_role_migration.py` · `test_user_admin.py` · `test_user_account_type_migration.py` | 부모가 `c492d6e8f4a2`이고 그래프의 head는 1개, head 스키마와 ORM 일치 · 서버 기본값 없음, 기존 PLATFORM → CENTRAL_ADMIN(전역 범위 유지) · TENANT · UNASSIGNED NULL, downgrade가 TENANT_MANAGER를 UNASSIGNED로 내려 배정 범위를 전역으로 넓히지 않음, 등급 CHECK의 거절 · 허용 형태. 저장소: 등급 없이 만든 PLATFORM은 배정 없는 TENANT_MANAGER, `set_platform_role`은 PLATFORM만 바꿈, ACTIVE 중앙 관리자 집계, 배정 전체 교체 · 모르는 대상과 중복 거절 · 고객 삭제 뒤 남는 배정 없음, PLATFORM 목록 · 마지막 로그인, 등급 · 배정 변경 이력 값 |
| 실제 PostgreSQL(플랫폼 등급) | `apps/core-api/db/tests/test_platform_audit_merge_migration.py` | 병합 revision `d492a544e001`의 부모 2개(`b492e7d1a605` · `b544a7d1e2f3`, 둘 다 부모 `c492d6e8f4a2` 그대로)와 head 1개, 새 DB의 base → head, `b492e7d1a605`까지 올린 DB가 head에서 등급 · 배정을 보존하고 감사 기록 테이블 · 트리거를 얻음, `b544a7d1e2f3`까지 올린 DB가 head에서 기존 PLATFORM을 CENTRAL_ADMIN으로 채우고 배정을 만들지 않으며, 그 전에 남은 감사 기록의 저장 값이 그대로이고 append-only 보호도 유지됨, head 스키마와 ORM 일치. 테스트마다 일회용 DB를 만들고 지운다 |
| 실제 PostgreSQL(플랫폼 등급) | `apps/core-api/tests/test_staff_admin_api.py` | 중앙 관리자가 아닌 PLATFORM(배정이 있는 TENANT_MANAGER 포함) · TENANT ADMIN은 모든 경로(잘못된 본문 포함) 403 · DB 불변, 미인증 401, 목록은 PLATFORM만 · 닫힌 필드, 등급 변경과 `PLATFORM_ROLE_CHANGED` 이력, `STAFF` 값 거절, 같은 값 409, 본인 변경 403, 다른 중앙 관리자의 강등 · 비활성화, 비활성화의 세션 회수와 관리 권한 즉시 상실, 배정 조회 · 교체 · 회수와 이력, 이력 실패 시 교체도 되돌림, 같은 집합 409(이력 없음), 중복 · 모르는 · 형식 오류 ID가 아무것도 바꾸지 않음, 정지 고객 배정 허용 · 계정 불변, 중앙 관리자 · 비PLATFORM 대상 배정 거절, 등급 변경이 배정을 비우고 되살리지 않음, 강등된 행위자의 배정 요청이 아무것도 쓰지 않음, TENANT · 없는 · 형식 오류 대상 404, `/api/v1/users`가 PLATFORM 대상을 받지 않음, 요청 · 응답 스키마가 닫힘, 생성 경로 없음 |
| 실제 PostgreSQL(플랫폼 등급) | `apps/core-api/tests/test_tenants_api.py` | 배정 없는 TENANT_MANAGER는 어떤 고객도 보거나 바꾸지 못함, TENANT_MANAGER는 배정 고객으로 한정, CENTRAL_ADMIN은 전체 고객과 전체 계약 사용, 의존성 판정 뒤 강등된 중앙 관리자는 전역 쓰기를 잃고 배정이 회수된 담당자는 아무것도 쓰지 않음 |
| 실제 PostgreSQL + 대역(플랫폼 등급) | `apps/core-api/tests/test_user_admin_api.py` · `test_api_access.py` · `test_auth_api.py` | 배정 없는 TENANT_MANAGER는 고객 데이터 없음, TENANT_MANAGER는 배정 고객의 사용자로 한정되고 배정 교체를 따름, CENTRAL_ADMIN은 배정 없이 모든 고객에 닿음, TENANT_MANAGER의 배정 고객 `recover_existing`, 초대 · 재발송이 SDK 호출 뒤 배정을 다시 확인하고 아무것도 쓰지 않음, 두 중앙 관리자의 동시 상호 강등 · 비활성화에서 한쪽만 성공(잠금 대기 확인), 관리 판정이 `platform_role`을 보고 `role=ADMIN` · 등급 없는 PLATFORM · 다른 계정 종류의 등급 값으로 열리지 않음, `/me`의 `platform_role` · `assigned_tenant_ids`가 재로그인 없이 DB 값을 따름 |
| 실제 PostgreSQL + 대역(플랫폼 등급) | `apps/core-api/tests/test_auth_bootstrap_admin.py` | 생성 · 지정 결과가 배정 없는 TENANT_MANAGER(지정은 세션 회수 · SDK 호출 0회), `--central-admin`은 ACTIVE 중앙 관리자가 없을 때만이고 있으면 SDK 호출 0회로 거절, 기존 TENANT_MANAGER의 중앙 관리자 지정이 배정을 비움, 부적격 대상 · 플래그 혼용 · `sub` 불일치 거절, Cognito 오류 시 변경 없음 · 오류 코드만 출력, Cognito 호출 중 DB 트랜잭션 없음, 잠금 안 재확인(그 사이 중앙 관리자가 생기면 거절) |
| 계약 · 웹 유닛(플랫폼 등급) | `packages/schemas/tests/test_api_auth.py` · `test_api_users.py` · `apps/web/src/lib/roles.test.ts` · `account-summary.test.ts` · `user-admin.test.ts` | 등급 2종이 계약과 정확히 일치, `/me`의 `assigned_tenant_ids` 직렬화와 TENANT_MANAGER 밖의 배정 거절, 사용자 목록 항목에 배정이 없음, 이력 상태의 `platform_role` · `tenant_ids`. 화면: 중앙 관리자는 빈 배정이어도 전체 범위, 담당자는 배정 고객만 · 배정 없으면 범위 없음, 고객 추가는 중앙 관리자만, 사용자 관리 · 조치 자격이 등급 · 배정을 봄, 막는 이유 문구, 계정 정보의 고객 범위, 담당자의 초대 본문이 담당 고객으로 한정 |
| 실제 Cognito | 자동 테스트 없음 | 없음. CI는 실제 Cognito를 호출하지 않는다(아래 주의) |

통합 픽스처(`apps/core-api/tests/conftest.py`): `client_pg`는 테스트 DB에 만든 PLATFORM 실세션(`platform_session` — `role=ADMIN` 호환 값, 등급 `CENTRAL_ADMIN`) 쿠키와 허용 Origin을 기본 헤더로 싣는다(세션 쿠키는 `Secure`라 httpx 쿠키 저장소가 http로 보내지 않아 `Cookie` 헤더로 직접 싣는다). 업무 허용 테스트는 이 전체 범위 세션을 쓴다. 세션 없는 클라이언트는 `client_pg_anon`, 다른 계정은 `login_as(role, tenant_id=..., account_type=..., platform_role=...)`, 임의 시드는 `make_auth_session(role, tenant_id=, account_type=, platform_role=, assigned_tenant_ids=, status=, now=)`다. PLATFORM 시드의 기본 등급은 `CENTRAL_ADMIN`이고, TENANT_MANAGER는 `platform_role`과 `assigned_tenant_ids`(실제 테넌트 행)로 명시해 배정한다 — 생략하면 배정 없음이다. 운영 코드의 기본값(`create_user`)은 배정 없는 `TENANT_MANAGER`이므로 시드 기본값을 운영 기본값으로 읽지 않는다. 다른 계정 종류에 등급을 주면 시드가 거절한다. `tenant_id`를 주면 TENANT, 아니면 `account_type`(기본 PLATFORM, `UNASSIGNED`도 고를 수 있다)이며 계정 종류를 추론하지 않는다 — `tenant_id` 없이 ADMIN 밖의 역할을 주면 DB CHECK가 시드를 거절한다. TENANT 계정은 실제 테넌트 행에 묶는다: 테스트 트랜잭션은 `make_tenant(status)`, 임의 세션 · 격리 DB는 `seed_tenant`, commit 세션은 `committed_auth.make_tenant`. WebSocket은 다른 스레드가 자기 DB 세션으로 판정하므로 `ws_client` · `committed_auth`가 실제로 commit하고 끝나면 지운다.

#492 테스트 원칙: 제약 · 마이그레이션 · 잠금과 동시성은 실제 PostgreSQL로 확인하고, Cognito는 대역만 쓴다.

전체 실행은 CI(`.github/workflows/ci.yml`)와 같은 명령이다. PostgreSQL · LocalStack이 필요하다(준비 순서는 루트 `CLAUDE.md` §푸시 전 로컬 통합 테스트).

```bash
uv run pytest tests apps/core-api/services/tests apps/core-api/ai/tests packages/schemas/tests apps/core-api/db/tests apps/core-api/tests apps/core-api/security/tests -q
cd apps/web && npm run lint && npm run build && npm run test --if-present
```

**대역 테스트 통과는 실제 Cognito 동작의 보장이 아니다.** 대역 테스트는 정해 둔 Cognito 응답을 앱이 어떻게 처리하는지만 고정한다. 실제 User Pool의 challenge 순서 · 오류 · 메일 발송과 §User Pool · 앱 클라이언트 전제는 자동 테스트가 확인하지 않는다. HTTPS 배포 환경의 쿠키 동작과 실제 브라우저 · OS IME · 자동 완성 · 비밀번호 관리자 동작도 자동 테스트 범위가 아니다(통합 픽스처는 `Cookie` 헤더를 직접 싣는다). 실제 Cognito · HTTPS · 이전한 배포 환경에서의 동작은 그 환경에서 따로 검증해야 하며, 이 문서의 어떤 내용도 그 검증을 대신하지 않는다.

## 운영 주의점

- **단일 프로세스 전제.** 진행 중인 로그인과 사용자별 코드 실패 횟수는 프로세스 메모리에 있다(uvicorn worker 1개 기동 전제). 재시작하면 진행 중인 로그인은 처음부터 다시 하고 실패 횟수는 0이 된다. 복제본을 늘리면 다른 프로세스의 로그인 흐름을 찾지 못해 로그인이 실패한다(우회가 아니라 거절 쪽). 세션은 DB에 있어 재시작과 무관하다.
- **세션 만료는 관제를 멈추지 않는다.** 수집 · 탐지 · 자동 대응은 사용자 세션과 독립적으로 돈다.
- **SDK 로그는 WARNING으로 고정된다.** Admin 인증 요청 본문에 비밀번호가, 응답에 토큰이 실리므로 `setup_logging`이 `LOG_LEVEL`과 관계없이 `botocore` · `boto3` · `urllib3`를 WARNING으로 둔다. 이 고정을 풀지 않는다.
- **Cognito 자격증명.** `COGNITO_AWS_PROFILE`이 비면 `AWS_PROFILE`(운영 이미지의 관제 대상 접근 역할 프로필)을 무시하고 기본 체인(태스크 역할)을 쓴다. Cognito 권한을 관제 대상 접근 역할에 주지 않는다.
- **로컬 개발.** 웹(`http://localhost:3000`) → API(`http://localhost:8000`)는 같은 사이트라 `SameSite=Lax` 쿠키가 실린다. FE 요청은 `credentials: "include"`다. Chrome · Firefox는 `http://localhost`를 보안 컨텍스트로 보아 `Secure` · `__Host-` 쿠키를 받는다. `.env`에 LocalStack 더미 AWS 키가 있으면 `COGNITO_AWS_PROFILE`로 실제 자격증명을 지정한다.
- **배포 도메인.** 세션 쿠키는 host-only라 기본 도메인(`https://vigilantis.dev`)과 `/api/*` 경로 라우팅을 전제한다. CloudFront 기본 도메인으로 접속하면 API 호스트와 달라 쿠키가 실리지 않는다.
- **초대 메일.** 기본 초대 메일 문구는 임시 비밀번호 뒤에 문장부호가 붙는다. 복사 때 함께 들어가지 않게 안내하거나 문구를 바꾼다.
- **비활성 사용자.** `DISABLED` 사용자의 세션은 401로 막히고, 열린 WebSocket은 재확인 주기 안에 4401로 닫힌다. PR 3의 비활성화는 그 사용자의 세션 행도 `ACCOUNT_DISABLED`로 회수하고, 재활성화해도 되살리지 않는다. 앱 DB만 바꾸며 Cognito 사용자는 그대로다(§사용자 관리).
- **TOTP 분실(PR 3 제외).** 비밀번호 재설정은 비밀번호만 바꾸고 로그인에는 여전히 TOTP가 필요하므로, 인증 앱을 잃은 사용자는 재설정으로 복구할 수 없다. 관리자용 TOTP 재설정 UI · API · CLI는 PR 3 게시 뒤의 후속 작업이다([#515](https://github.com/ProjectVigilantis/vigilantis/issues/515)). 필요한 `AdminDeleteSoftwareToken`은 botocore 1.43.82부터 있어(현재 잠금 1.43.70에는 없다) 후속에서 의존성 갱신이 필요하다. 마지막 ADMIN이 TOTP를 잃으면 다시 로그인하지 못할 수 있다. 중앙 관리자도 같다 — CLI는 ACTIVE 중앙 관리자가 0명일 때만 중앙 관리자를 지정하므로, ACTIVE로 남은 중앙 관리자가 로그인하지 못하는 상황을 CLI가 풀지 않는다. 다른 ACTIVE 중앙 관리자가 `/api/v1/staff`로 바꿀 수 있는 것은 그 계정의 등급 · 상태뿐이며, TOTP를 재설정하거나 로그인을 복구하지 않는다. 다만 이미 열린 세션은 만료 · 회수 전까지 유효하므로 즉시 운영이 멈추지는 않는다.
- **세션 확인 부하.** 보호 API 요청마다 `auth_sessions` · `app_users` 조회가 2번 붙는다(요청 세션 안). 열린 WebSocket은 연결마다 `WS_SESSION_CHECK_SECONDS`에 한 번 같은 조회를 한다.
- **요청 본문 크기.** 본문 크기 제한은 인증 전에 강제되지 않는다(FastAPI 기존 동작 — 본문을 의존성보다 먼저 읽는다). 본문을 받는 경로는 미인증 요청도 본문 전체를 읽은 뒤에 거절한다. 인증 전 크기 제약은 배포 계층(WAF 등)에 두는 전제이며, 특정 환경의 적용 여부는 이 문서가 보증하지 않는다.
- **고객 범위의 한계(#492 · #533).** TENANT 계정은 업무 API에서 자기 고객의 기록을 조회 · 조치할 수 있지만, 메트릭 · 실시간 연결과 고객용 업무 화면은 아직 없다(T-4 · T-5). 수집기가 자산 · 수집 회차에 고객 소속을 채우지 않아, 실제 수집 자산은 CENTRAL_ADMIN에게만 보인다 — 담당자에게 고객을 배정해도 그 고객의 수집 자산이 담당자 화면에 나타난다고 안내하지 않는다(§접근 보호). 이 상태를 테넌트 데이터 격리 완료로 안내하지 않는다.
- **담당 고객 배정(#492).** 새로 만들거나 지정한 직원은 담당 고객이 없어 로그인 뒤 아무 고객 데이터도 보지 못한다. 중앙 관리자가 `PUT /api/v1/staff/{user_id}/tenants`로 전체 목록을 보내 배정한다(화면 없음). 새로 만든 고객은 누구에게도 자동 배정되지 않는다. 배정 회수 · 강등은 그 계정의 다음 판정부터 반영되며, 이미 예약 · 실행 중인 작업과 이미 시작한 전송은 취소하지 않는다(§직원 관리).
- **고객 ID 입력(#492).** 사용자 관리 화면에 고객 선택 목록이 없어(백엔드의 테넌트 목록 API를 이 화면은 쓰지 않는다) 플랫폼 관리 권한자는 초대할 고객의 UUID를 직접 확인해 입력해야 한다. 없는 값과 TENANT_MANAGER에게 배정되지 않은 고객은 404 `TENANT_NOT_FOUND`다.
- **마이그레이션 적용(#492).** head는 플랫폼 등급 가지(`b492e7d1a605`)와 감사 기록 가지(`b544a7d1e2f3`)를 잇는 빈 병합 revision `d492a544e001`이며, dev 가지 끝 · #492 가지 끝 · `c492d6e8f4a2` · `b492e7d1a605` · `b544a7d1e2f3` 어느 쪽의 DB도 `alembic upgrade head`로 앞으로만 올린다(§마이그레이션 그래프). `b492e7d1a605`도 이전 앱 · CLI와 함께 쓸 수 없다(§마이그레이션 `b492e7d1a605`). 등록 링크 스키마(`platform_enrollments`)가 있는 DB와 `b492e7d1a605`의 등급 3종 초안을 적용한 DB는 revision 기록과 관계없이 이 경로의 대상이 아니다 — 연결 · `stamp` 없이 보존하고, 따로 승인된 절차로만 다룬다(§마이그레이션 그래프 · 병합 `c492d6e8f4a2` · §마이그레이션 `b492e7d1a605`). `f492c4a8d2e1`은 이전 앱 · CLI의 사용자 생성과 함께 쓸 수 없으므로 마이그레이션 · 새 앱 · 새 CLI를 함께 올린다. downgrade는 PLATFORM · UNASSIGNED 구분을 잃는다(§마이그레이션 `f492c4a8d2e1`). 소속 없는 기존 계정은 UNASSIGNED로 남아 로그인할 수 없으므로, 필요하면 CLI로 지정한다.
- **플랫폼 등급 전환(#492).** upgrade 뒤 기존 PLATFORM 계정은 모두 CENTRAL_ADMIN이라 지금까지의 전역 운영을 그대로 이어 가고, 직원 관리 · 담당 고객 배정 권한도 갖는다. 범위를 좁혀야 하는 계정은 다른 중앙 관리자가 `/api/v1/staff`에서 TENANT_MANAGER로 내리고 담당 고객을 배정한다(내리면 배정 없음 상태에서 시작한다). ACTIVE 중앙 관리자가 하나도 없는 DB에서만 CLI로 첫 중앙 관리자를 지정한다(`--bootstrap-central-admin-user-id` 또는 `--platform --central-admin`). 등급 · 배정 변경은 재로그인 없이 그 계정의 다음 판정부터 반영된다(§직원 관리). downgrade는 TENANT_MANAGER를 UNASSIGNED로 내리고 배정을 버리며, 다시 upgrade해도 되살리지 않는다(§마이그레이션 `b492e7d1a605`). 실제 배포 환경에서의 적용은 검증되지 않았다.
- **초대 · 재발송의 결과 불명(#492).** SDK 호출 뒤 재확인이 거절하면 메일은 나갔을 수 있다. 오류 코드만으로 미발송을 판단하지 말고 목록 · Cognito 상태를 확인한 뒤 다시 수행한다(§부분 실패).
- **SSOT.** 팀 공통 현황 · API 계약의 기준은 `docs/PROJECT_STATUS.md`다. 이 모듈의 세부 계약은 이 문서와 `packages/schemas/`에 있으며, 여기 적힌 경로 · 오류 코드 · 응답 칸이 SSOT에 반영됐는지는 SSOT에서 확인한다.

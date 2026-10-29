# auth — 로그인 · 서버 세션 · 계정 종류 권한 (Post-MVP T-1, #492)

Cognito Admin 인증 API로 로그인을 진행하고, **이번 로그인에서 TOTP 단계 성공 응답을 서버가 직접 받은 뒤에만** 앱 서버 세션을 발급한다. 공개 계약은 `packages/schemas/api/auth.py`, 오류 코드는 `packages/schemas/api/errors.py`다.

이 문서는 PR 1(로그인 기반), PR 2(API · WebSocket 접근 보호 · 역할 검사), PR 3(ADMIN 사용자 관리 · 사용자당 세션 1개 · 비밀번호 재설정 · 새 비밀번호 정책 · 사용자 관리 이력)과 계정 종류(`account_type`)에 따른 테넌트 · 플랫폼 권한(§계정 종류와 테넌트 소속)의 백엔드 기준이다. 웹 화면(SSR) 쪽 처리는 `apps/web` 범위다 — PR 3 화면(사용자 관리 · 비밀번호 재설정 · 새 비밀번호 조건 · 한글 입력 경고 · 세션 종료 안내)은 최소 구현이고 시각 다듬기는 포함하지 않으며, `apps/web/src/lib/session-expiry.ts`의 시계 보정은 PR 3 범위 밖이다. 계정 종류에 따른 화면 기능(`apps/web/src/lib/home-path.ts` · `components/gnb.tsx` 포함)은 §화면이 다룬다. 분실한 TOTP의 재설정은 PR 3에서 제외했다(§운영 주의점). 실제 Cognito · HTTPS 배포 환경 · 환경 이전에서의 검증과 환경별 설정 · 배포 권한 반영은 이 문서가 보증하지 않는 배포 전제다(§User Pool · 앱 클라이언트 전제 · §테스트).

근거 표시: **[표준]** 외부 표준 · 공식 문서, **[팀 결정]** 이슈 #492 확정 정책, **[판단]** 이 구현의 선택. 동작의 근거는 현재 코드와 자동 테스트(대역 · 실제 PostgreSQL)이며, 자동 테스트가 보장하지 않는 범위(실제 Cognito · HTTPS 배포 환경)는 §테스트에 적었다.

## 구조

| 파일 | 책임 |
| --- | --- |
| `cognito.py` | boto3 `cognito-idp` 어댑터. `SECRET_HASH` 계산, Cognito 오류를 `AUTH_*` 코드와 1:1인 도메인 예외로 분류. (PR 3) 관리 조회 · 초대(`AdminGetUser` · `AdminCreateUser`)와 재설정(`ForgotPassword` · `ConfirmForgotPassword`), 호출 종류별 오류 분류 |
| `login_flow.py` | 로그인 상태 머신. 세션 발급 조건 판정, 코드 시도 상한(흐름별 · 사용자별), refresh token 회수. (PR 3) 최초 비밀번호 변경은 Cognito 호출 전에 `password_policy`로 확인 |
| `flow_store.py` | 진행 중인 로그인과 사용자별 코드 실패 횟수의 프로세스 메모리 보관(TTL · 슬라이딩 윈도). DB · 로그에 남기지 않는다 |
| `sessions.py` | 서버 세션 발급 · 조회 · 활동 갱신 · 회수. 설정을 읽지 않고 commit하지 않는다. (PR 3) 사용자 단위 일괄 회수(`revoke_user_sessions`) |
| `service.py` | 트랜잭션 소유자. 로그인 완료(사용자 조회 · 세션 발급), 현재 사용자, 활동, 로그아웃. (PR 3) 로그인 완료를 `app_users` 잠금으로 직렬화하고 그 사용자의 이전 세션을 전부 회수. (#492) `CurrentUser`에 `account_type` · `tenant_id`와 내부 전용 `tenant_status`. UNASSIGNED는 로그인 완료 403 · 세션 판정 401 |
| `password_policy.py` | (PR 3) 새 비밀번호 정책 순수 함수 `check_password` — 최초 비밀번호 변경 · 재설정 확정 공용 |
| `password_reset.py` | (PR 3) 비밀번호 재설정 요청 · 확정, 확정 성공 뒤 앱 세션 회수 |
| `admin_service.py` | (PR 3) ADMIN 사용자 관리 트랜잭션 소유자 — 초대 · 재발송 · 역할 · 상태 · 세션 회수 · 이력 조회. 잠금 · 행위자 재판정 · 마지막 활성 ADMIN 보호. (#492) 계정 종류에 따른 관리 범위, 초대 대상 테넌트 결정 · ACTIVE 확인, 정지 테넌트 행위자의 쓰기 거절, 테넌트별 마지막 활성 ADMIN, 기존 계정 연결(`recover_existing`)의 PLATFORM 한정 |
| `http.py` | 쿠키 · Origin 검사(HTTP 상태 변경 · WebSocket 공용 목록 `allowed_origins`) · 보안 헤더(`/api/v1/*`) · `AuthError` → 오류 봉투 매핑. (PR 3) 관리 · 재설정 경로용 Cognito 어댑터 조립(`get_cognito_adapter`, 설정이 비면 503) |
| `access.py` | (PR 2) 보호 API 의존성 — 세션(`require_user`) → 역할(`require_action_role`), 신원을 `request.state.current_user`에 둔다. WebSocket 세션 판정(`resolve_session_user`, 판정마다 짧은 DB 세션). (PR 3) 사용자 관리용 `require_admin`. (#492) 플랫폼 판정 `is_platform_account` · `require_platform_account`(`/api/v1/tenants` 라우터 의존성), 메트릭 플랫폼 관문 `require_business_access`. (#533) 업무 기록 범위 `tenant_scope_of` · `require_tenant_scope`(범위 값은 `tenancy.TenantScope`). `require_action_role`은 역할만 보고 테넌트 정지는 보지 않는다 |
| `provider.py` | Cognito 클라이언트 · `LoginFlow` · (PR 3) 단독 어댑터(`build_adapter`) 조립(엔드포인트 고정) |
| `bootstrap_admin.py` | (#492) 플랫폼 계정 생성 · UNASSIGNED 계정의 플랫폼 지정 운영 CLI(§플랫폼 계정 CLI) |
| `routers/auth.py` | `/api/v1/auth` 라우터(얇게). (PR 3) `/password-reset` · `/password-reset/confirm`. (#492) `/me`에 `account_type` · `tenant_id` |
| `routers/users.py` | (PR 3) `/api/v1/users` 라우터(얇게). 라우터 의존성 `require_admin`. (#492) 관리 범위는 서비스가 판정한다. 초대 · 재발송의 Cognito 어댑터는 사전 판정 뒤에 조립한다(`get_cognito_adapter_factory`) |
| `main.py` | (PR 2) 포함 시점 보호 배선(Origin → 세션), 접근 로그의 `user_id` · `role`, API 문서 토글. (PR 3) 사용자 관리 라우터도 같은 배선으로 포함. (#492) 메트릭 라우터에는 `require_business_access`를 더한다. (#533) 조치 · 자산 · 인시던트 라우터는 Origin → 세션 배선만 쓰고, 테넌트 범위는 각 라우트가 받는다. 테넌트 라우터(`routers/tenants.py`)는 Origin → 세션 배선으로만 포함하고, 플랫폼 판정은 그 라우터의 의존성이다 |
| `routers/ws.py` | (PR 2) WebSocket 연결 시 세션 확인 · 연결 중 재확인(4401). 닫기 코드 계약은 `packages/schemas/api/ws.py`. (#492) PLATFORM 계정만 받는다 |
| `routers/actions.py` · `routers/incidents.py` | (PR 2) 조치 실행 · 종료가 역할 검사 결과(`CurrentUser`)를 받는다 |
| `db/models.py` · `db/repositories/auth.py` | `app_users` · `auth_sessions`. (PR 3) `user_admin_events`(`UserAdminEvent`), 세션 회수 사유 추가, 마지막 로그인 · 활성 ADMIN 집계. (#492) `AppUser.account_type`과 CHECK 2개, 목록 · 활성 ADMIN 집계의 테넌트 범위 |
| `db/migrations/versions/20261020_e492a3b5c701_user_admin_events.py` | (PR 3) 회수 사유 `ADMIN_REVOKED` · `ACCOUNT_DISABLED` · `PASSWORD_RESET` 추가, `user_admin_events` 생성과 append-only 트리거 |
| `db/migrations/versions/20261022_f492c4a8d2e1_user_account_type.py` | (#492) `app_users.account_type` 추가 · 기존 행 분류 · CHECK 2개(§마이그레이션 `f492c4a8d2e1`) |
| `db/migrations/versions/20261026_c492d6e8f4a2_merge_account_type_auto_action.py` | (#492) 빈 병합 revision — #492 가지와 dev 가지를 head 하나로 잇는다(§마이그레이션 그래프 · 병합 `c492d6e8f4a2`) |

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
| 플랫폼 계정 | 운영자가 CLI로 명시 생성 · 지정할 때만 생긴다(§플랫폼 계정 CLI). 공개 API · 화면의 등록 경로는 없다. 첫 계정과 추가 계정이 같다. 첫 방문자를 승격하지 않고, ADMIN 역할이나 `tenant_id` NULL에서 플랫폼 권한을 추론하지 않는다 | [팀 결정: CLI 명시 생성 · 지정, 첫 방문자 승격 · 권한 추론 없음] |

## 계정 종류와 테넌트 소속 (#492)

권한 범위의 원천은 `app_users.account_type` 하나다. `role`은 계정 안의 역할이고, `tenant_id`가 NULL인 것은 소속이 없다는 뜻일 뿐 권한이 아니다.

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 계정 종류 | `PLATFORM` · `TENANT` · `UNASSIGNED`(enum `app_user_account_type`), NOT NULL. ORM 기본값과 서버 기본값이 없어 모든 쓰기가 값을 명시해야 하고, 빠뜨린 쓰기는 DB가 거절한다 | [판단] |
| 소속 | `TENANT`이면 `tenant_id`가 반드시 있고(테넌트 FK), `PLATFORM` · `UNASSIGNED`이면 NULL이다. CHECK `ck_app_users_tenant_binding_by_account_type`이 강제한다 | [판단] |
| 플랫폼 역할 | `PLATFORM`의 `role`은 호환 값 `ADMIN`으로 고정한다(CHECK `ck_app_users_platform_role_is_admin`). 플랫폼 계정끼리 권한 차이가 없고, 플랫폼 권한은 `account_type`에서만 나온다 | [팀 결정: 플랫폼 계정 권한 동일 · 명시적 플랫폼 권한] [판단: `role=ADMIN` 호환 값] |
| UNASSIGNED | 접근 권한이 없는 계정이다. 로그인은 완료 단계에서 403 `ACCOUNT_NOT_PROVISIONED`(세션 미발급), 이미 있던 세션은 다음 판정부터 401 `AUTH_REQUIRED`다. 사용자 목록과 공개 응답에 나오지 않는다 | [판단] |
| 공개 계약 | `CurrentUserResponse`(`GET /me`) · `UserListItem`에 `account_type`(`PLATFORM` \| `TENANT`)과 짝 `tenant_id`(UUID \| `null`)를 싣는다. 짝 규칙은 `AccountScope`(`packages/schemas/api/auth.py`)가 검증하며, 내부 `CurrentUser`와 TS 타입(`apps/web/src/types/api.ts`의 `AccountScope`)도 같은 규칙이다. 테넌트 상태(`tenant_status`)는 내부 판정용이라 공개하지 않는다 | [판단] |
| 플랫폼 전용 라우트 | `auth.access.require_platform_account` — 테넌트 관리 라우터 `/api/v1/tenants`(`routers/tenants.py`, 경로 7개: 목록 · 생성 · 상세 · 수정 · 삭제 · 온보딩 조회 · 정지)의 라우터 의존성이다. `main.py`가 이 라우터를 Origin → 세션 배선으로 포함하고, 이 판정은 본문 필드 검증 전에 돈다. TENANT 계정은 역할 · 소속 테넌트 상태와 관계없이 403 `AUTH_FORBIDDEN`이고 PLATFORM 계정만 통과한다. 테넌트 API의 계약 · 동작은 `packages/schemas/api/tenants.py`와 그 라우터가 맡고, 이 문서는 접근 판정만 다룬다 | [판단] |

### 마이그레이션 `f492c4a8d2e1`

`e492a3b5c701` 다음 revision이다(`db/migrations/versions/20261022_f492c4a8d2e1_user_account_type.py`).

- **upgrade**: enum과 열을 만들고 기존 행을 `tenant_id` NULL → `UNASSIGNED`, 값 있음 → `TENANT`로 채운 뒤 NOT NULL과 CHECK 2개를 건다. `PLATFORM`은 만들지 않는다(기존 계정 승격 없음). 기존 행이 있다고 전제하지 않으며, 행 · 신원(`issuer` · `cognito_sub`) · 세션 행은 그대로 둔다.
- 소속 없는 기존 계정이 있었다면 업그레이드 뒤 `UNASSIGNED`가 되어 로그인 403 · 기존 세션 401이 된다. 그 계정을 플랫폼 계정으로 쓰려면 §플랫폼 계정 CLI의 지정을 실행한다.
- **downgrade**: CHECK 2개 · 열 · enum을 지운다. `PLATFORM` · `UNASSIGNED` 구분은 복원되지 않는다 — 다시 upgrade하면 소속 NULL인 계정은 이전에 `PLATFORM`이었어도 모두 `UNASSIGNED`가 되어 다시 지정해야 한다.
- **호환성**: 기본값 없는 NOT NULL 열이라, upgrade 뒤에는 `account_type`을 모르는 이전 앱 · CLI의 `app_users` INSERT(초대 · 관리자 생성)가 거절된다. 환경에 적용할 때는 마이그레이션 · 새 앱 · 새 CLI를 함께 맞춰 올린다. 실제 환경 적용과 그 검증은 이 문서가 보증하지 않는다(§테스트).

### 마이그레이션 그래프 · 병합 `c492d6e8f4a2`

`e492a3b5c701`에서 가지 둘로 갈렸다가 빈 병합 revision에서 다시 합쳐진다. 최종 head는 `c492d6e8f4a2` 하나다.

```
e492a3b5c701 ─┬─ f492c4a8d2e1 ─┬─ c492d6e8f4a2 (head)
              └─ f514a6c3d201 ─┘
```

- **가지**: #492 가지는 `f492c4a8d2e1`(`app_users.account_type`) 하나이고, dev 가지는 `f514a6c3d201`(`tenants.auto_action_resumed_at`, 이 모듈 밖)이다. 두 revision 모두 부모가 `e492a3b5c701`이다.
- **병합**: `c492d6e8f4a2`(`db/migrations/versions/20261026_c492d6e8f4a2_merge_account_type_auto_action.py`)의 부모는 `f492c4a8d2e1` · `f514a6c3d201` 둘이고, upgrade · downgrade가 모두 비어 있다. 두 가지가 서로 다른 객체를 바꾸므로 스키마 · 데이터 변경이 필요 없다.
- **기존 DB**: dev 가지 끝(`f514a6c3d201`)의 DB와 #492 가지 끝(`f492c4a8d2e1`)의 DB 모두 `alembic upgrade head`가 아직 적용하지 않은 쪽 가지와 병합 revision만 적용해 head에 이른다. downgrade나 `stamp` 없이 앞으로만 올린다. dev 가지 DB에는 이때 `f492c4a8d2e1`이 처음 적용되므로, 그 upgrade 규칙(소속 NULL → `UNASSIGNED`, 값 있음 → `TENANT`, 승격 없음)과 호환성 주의(`account_type`을 모르는 이전 앱 · CLI의 `app_users` INSERT 거절)가 그대로 적용된다(§마이그레이션 `f492c4a8d2e1`). UNASSIGNED 계정의 동작도 위 표 그대로다.
- **새 DB**: base → head가 같은 그래프의 두 가지와 병합 revision을 모두 거친다.
- **downgrade**: 병합 revision의 downgrade는 아무것도 바꾸지 않는다. 그 아래 revision의 downgrade 주의점은 §마이그레이션 `f492c4a8d2e1`에 적힌 그대로다.
- **등록 링크 스키마가 있는 DB**: 이 그래프에는 `platform_enrollments`(enum `platform_enrollment_status` 포함)가 없다. 등록 링크를 제거하기 전의 #527 그래프로 올린 DB — 기록 revision이 `b492e5d7c3a1`인 DB, 또는 기록 revision은 같은 `c492d6e8f4a2`이지만 `platform_enrollments`가 있는 DB — 는 이 그래프의 자동 upgrade 경로가 지원하지 않는다. revision ID만 보고 head라고 판단하지 않는다. 이런 DB에는 이 그래프로 upgrade · downgrade · `stamp`를 실행하지 않고 이 코드를 연결하지 않는다. 그 스키마를 만든 이전 코드와 짝지어 보존하거나, 따로 승인된 전환 절차로만 다룬다.
- 실제 배포 환경 DB의 현재 revision과 그 환경에서의 적용 결과는 이 문서가 보증하지 않는다(§테스트).

## 접근 보호 (PR 2)

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 공개 경로 | `GET /health`, `POST /api/v1/auth/login*`(6단계), (PR 3) `POST /api/v1/auth/password-reset` · `/password-reset/confirm`, `POST /api/v1/auth/logout`(멱등 — 세션이 없으면 지울 것이 없어 204). `GET /me` · `POST /activity`는 세션이 없으면 401이다. PR 1 · 2에서는 Cognito 미구성이면 503이었고, PR 3부터 Cognito 설정과 무관하게 동작한다(§설정). 그 밖의 `/api/v1/*`와 WebSocket은 전부 세션이 필요하다. 공개 목록은 `test_api_access.py`의 `PUBLIC_ROUTES`가 실제 앱 경로와 대조한다 — 새 경로는 목록에 넣지 않는 한 401이어야 통과한다 | [팀 결정] |
| 세션 판정 | `auth.service.resolve_current_user(touch=False)` 하나. 보호 API · WebSocket은 유휴 시간을 늘리지 않는다. Cognito 설정을 읽지 않는다 — Cognito가 비어도 보호 API는 세션만으로 열린다 | [팀 결정] [판단] |
| 배선 | `main.py`가 조치 · 자산 · 인시던트 · 메트릭 라우터를 `include_router(..., dependencies=[Origin, 세션])`로 포함한다. 역할은 조치 실행 · 종료 라우트의 의존성이다. 검사 순서: Origin(상태 변경만, DB 전) → 세션 → 역할 → 본문 검증 → 라우터. (#492) 메트릭 라우터는 세션 다음에 `require_business_access`(아래 메트릭 · WebSocket 플랫폼 전용)를 거친다. (#533) 조치 · 자산 · 인시던트는 계정 종류 관문 없이 요청자의 테넌트 범위(아래 테넌트 범위)로 제한한다 | [판단] |
| 메트릭 · WebSocket 플랫폼 전용(#492) | 메트릭 조회(`GET /api/v1/metrics/timeseries`)와 전역 WebSocket은 테넌트 범위로 걸러지지 않으므로 PLATFORM 계정만 받는다. 메트릭은 `require_business_access`(플랫폼 판정만 한다), WebSocket은 연결 수립 · 재확인 때 계정 종류를 직접 본다. 켜고 끄는 설정이 없다. TENANT 계정은 역할 · 소속 테넌트 상태와 관계없이 메트릭 403 `AUTH_FORBIDDEN`, WebSocket 1008 · 4401이다 | [판단: 범위 없는 데이터의 임시 제한] |
| 테넌트 범위(#533) | 자산 · 인시던트 · 조치는 요청자의 테넌트 범위로 제한한다. 범위는 `auth.access.tenant_scope_of`가 `account_type`으로 정한다 — PLATFORM은 전체, TENANT는 자기 `tenant_id`만이다(`tenancy.TenantScope`). 소속 없는(`tenant_id` NULL) 기록은 PLATFORM 범위에서만 보인다. 조회 라우트는 `require_tenant_scope` 의존성으로 범위를 받고, 실행 · 종료는 역할 검사가 돌려준 신원으로 같은 범위를 적용한다. 목록(`GET /api/v1/assets` · `/incidents`)은 범위 안 기록만 돌려주고, 범위 밖 인시던트의 상세 · 실행 · 종료는 없는 인시던트와 같은 404 `INCIDENT_NOT_FOUND`다(존재를 알리지 않는다). 소속 테넌트가 정지돼도 TENANT 계정은 자기 범위의 자산 · 인시던트를 조회한다 | [판단] [ADR-0012: 정지 테넌트 사용자의 자기 기록 조회] |
| 실행 · 종료 판정 순서(#533) | `POST /api/v1/actions/execute`: 역할(403 `AUTH_FORBIDDEN`, 대상을 읽기 전) → 범위(404 `INCIDENT_NOT_FOUND`) → 같은 Idempotency Key의 같은 요청 재생(200, 요청이 다르면 409 `IDEMPOTENCY_KEY_CONFLICT`) → 대상 인시던트 테넌트의 정지(409 `TENANT_SUSPENDED`) → 업무 상태(409). 정지는 요청자 소속이 아니라 대상 인시던트의 테넌트로 판정하며, 정지 테넌트 대상의 새 실행은 PLATFORM도 막는다. 기존 실행의 재생은 정지 판정보다 먼저라 대상이 정지돼도 200이다. `POST /api/v1/incidents/{id}/resolve`: 역할 → 범위(404) → 대상 테넌트 정지 → 이미 종료된 건의 재요청(200) · 업무 상태(409) 순이다. 정지 테넌트 대상의 종료는 TENANT 계정이면 이미 종료된 건이어도 409 `TENANT_SUSPENDED`이고, PLATFORM은 일반 상태 규칙대로 종료한다. 범위 · 정지 · 멱등 판정은 `workflows.reserve_execution` · `workflows.resolve_incident`가 하고, `require_action_role`은 역할만 본다 | [판단] |
| 역할 | 아래 표. 역할 · Origin 거절은 `workflows.reserve_execution` · `workflows.resolve_incident` 전이다 — 실행 행(디스패처가 보는 비종결 `action_executions`) · 후보 전이 · AWS 호출이 생기지 않는다 | [팀 결정] |
| 401 · 403 | 세션 없음 · 만료(유휴 · 최대 수명) · 회수(`LOGOUT` · `SUPERSEDED`, PR 3의 `ADMIN_REVOKED` · `ACCOUNT_DISABLED` · `PASSWORD_RESET`) · 비활성 사용자 · (#492) UNASSIGNED 계정 → 401 `AUTH_REQUIRED`(사유를 구분하지 않는다). 역할 · 계정 종류 불허 → 403 `AUTH_FORBIDDEN`. (#533) 범위 밖 인시던트 → 404 `INCIDENT_NOT_FOUND`(없는 인시던트와 같다). 대상 인시던트의 테넌트가 정지된 새 실행 · TENANT 계정의 종료 → 409 `TENANT_SUSPENDED`. 허용 밖 Origin → 403 `AUTH_ORIGIN_REJECTED` | [팀 결정] [판단: #492 UNASSIGNED · 플랫폼 전용 관문, #533 범위 밖 404 · 대상 테넌트 정지 409] |
| Origin 범위 | 쿠키로 승인되는 모든 상태 변경 요청(GET · HEAD · OPTIONS 외). `AUTH_ALLOWED_ORIGINS`와 정확히 일치(비면 `CORS_ALLOW_ORIGINS`), 세션 조회 전에 거절. 조회(GET)는 Origin을 보지 않는다. WebSocket Handshake도 같은 목록(`auth.http.allowed_origins`)을 쓴다 | [표준] [OWASP CSRF: Verifying Origin](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) |
| 깨진 JSON 본문 | 잠금된 FastAPI 0.141.1은 JSON 본문을 의존성보다 먼저 파싱한다(`fastapi/routing.py` 요청 처리기). 그래서 파싱할 수 없는 본문의 POST는 세션 · Origin과 관계없이 422 `REQUEST_VALIDATION_FAILED`다. 이때 Origin · 세션 · DB · Workflow는 하나도 돌지 않는다. 파싱되는 본문의 필드 오류는 세션 · 역할 뒤(미인증이면 401)다. 이대로 둔다 — 상태 변경 · DB 접근 · 입력 반향이 없고, 바꾸려면 본문 파싱 앞에 같은 관문을 한 번 더 두어야 한다(중복) | [팀 결정: 유지] |
| WebSocket 연결 | Handshake Origin은 HTTP 상태 변경 검사와 같은 목록(`AUTH_ALLOWED_ORIGINS`, 비면 `CORS_ALLOW_ORIGINS`)과 정확히 일치해야 한다 — `AUTH_ALLOWED_ORIGINS`가 더 좁으면 CORS에만 있는 Origin은 WebSocket도 1008. Origin 없는 비브라우저 연결은 #73처럼 통과하지만 세션 쿠키는 필요하다. 세션 쿠키가 없거나 무효면 accept 전에 close 1008 — 브라우저에는 HTTP 403으로 보인다. 사유는 구분하지 않는다. (#492) PLATFORM이 아닌 계정도 무효 세션과 같이 다룬다 — 연결 때 1008, 연결 중 재확인에서는 4401 | [팀 결정] [판단: #492 플랫폼 전용] |
| WebSocket 재확인 | 연결마다 감시 task가 `min(지금 + WS_SESSION_CHECK_SECONDS, 유휴 만료, 최대 수명)`에 다시 판정하고, 무효면 close 4401(`WS_CLOSE_AUTH_REQUIRED`) reason `"AUTH_REQUIRED"`. 반영 지연 상한: 로그아웃 · 재로그인 회수 · 사용자 비활성은 주기 1회(기본 30초) + 확인 조회 시간, 유휴 · 최대 수명 만료는 그 시각. 확인 자체가 실패하면(DB 오류) 1011로 닫고 오류 종류만 로그에 남긴다(세션 무효가 아니므로 FE는 재연결). 판정마다 짧은 DB 세션을 스레드풀에서 연다 — 연결 수명 동안 DB 세션을 쥐지 않는다. close reason에 세션 · 사용자 정보를 싣지 않는다 | [팀 결정] [판단: 주기 30초] |
| 신원 전달 | 통과한 `CurrentUser`(user_id · role)를 `request.state.current_user`에 둔다. 접근 로그 `http_request`에 `user_id` · `role`을 싣는다(세션을 통과한 요청만). 이메일 · 쿠키 · 토큰은 싣지 않는다. 조치 실행 · 종료 라우터는 역할 검사 결과로 같은 신원을 받는다 — 감사 기록은 T-2 | [판단] |
| API 문서 | `API_DOCS_ENABLED`(기본 false)일 때만 `/docs` · `/redoc` · `/openapi.json`을 둔다(끄면 경로가 없어 404). `.env.example`은 로컬 개발용으로 true. 배포 스크립트(`scripts/provision_platform_aws.py`)는 태스크 환경변수를 명시 목록으로 넘기므로 꺼진 채로 뜬다 | [판단] |

| 동작 | PLATFORM(`role=ADMIN`) | TENANT ADMIN | TENANT OPERATOR | TENANT VIEWER |
| --- | --- | --- | --- | --- |
| 로그인 · `/me` · `/activity` · `/logout` · 비밀번호 재설정 | 허용 | 허용(테넌트 정지와 무관) | 허용(테넌트 정지와 무관) | 허용(테넌트 정지와 무관) |
| 자산 · 인시던트 조회(`GET /api/v1/assets` · `/incidents` · `/incidents/{id}`) | 전체 | 자기 테넌트(정지여도 허용) | 자기 테넌트(정지여도 허용) | 자기 테넌트(정지여도 허용) |
| 메트릭 `GET /api/v1/metrics/timeseries` | 허용 | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |
| WebSocket | 허용 | 1008 · 4401 | 1008 · 4401 | 1008 · 4401 |
| 조치 실행 `POST /api/v1/actions/execute` · 인시던트 종료 `POST /api/v1/incidents/{id}/resolve` | 모든 테넌트 대상. 대상 테넌트가 정지면 실행은 새 실행 409 `TENANT_SUSPENDED`(같은 Key 재생은 200), 종료는 정지와 무관하게 상태 규칙대로 허용 | 자기 테넌트 대상(범위 밖 404). 소속 테넌트가 정지면 새 실행 · 종료 모두 409 `TENANT_SUSPENDED`(실행의 같은 Key 재생은 200) | 자기 테넌트 대상(범위 밖 404). 소속 테넌트가 정지면 새 실행 · 종료 모두 409 `TENANT_SUSPENDED`(실행의 같은 Key 재생은 200) | 403 `AUTH_FORBIDDEN`(역할) |
| 사용자 관리 `/api/v1/users*` | 모든 TENANT 계정 | 자기 테넌트의 TENANT 계정(소속 정지면 조회만) | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |
| 테넌트 관리 `/api/v1/tenants*` | 허용 | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` | 403 `AUTH_FORBIDDEN` |

소속 테넌트가 정지된 TENANT 계정도 자기 테넌트의 자산 · 인시던트를 조회한다([ADR-0012](../../../docs/adr/0012-saas-transition-account-structure-and-tenant-model.md), #533). 메트릭과 WebSocket은 테넌트 범위가 없어 PLATFORM 계정만 쓴다. `auth.access`는 역할과 요청자 테넌트 범위만 정하고, 대상 인시던트 테넌트의 정지 판정(PLATFORM의 새 실행 거절 포함)은 Workflow가 범위 · 멱등 재생 판정 뒤에 한다. 이 범위 · 정지 판정은 HTTP 요청에만 걸리며 수집기 · 자동 대응 같은 백그라운드 처리에는 적용되지 않는다.

## 사용자당 세션 1개 (PR 3)

- 로그인 완료(`service.complete_login`)는 `app_users` 잠금 안에서 사용자 상태 · 이메일 확인 기록을 새로 읽는다. 이어서 요청 쿠키의 이전 세션(다른 사용자의 것이어도)과 그 사용자의 살아 있는 세션 전부를 `SUPERSEDED`로 회수한 뒤 새 세션 하나를 발급한다. 동시 로그인도 같은 잠금으로 직렬화되어 살아 있는 세션은 1개만 남는다 [판단].
- 회수 시점은 다음 로그인의 **완료**다. 비밀번호 · TOTP 단계에서 멈춘 로그인은 기존 세션을 건드리지 않는다. 이 변경 전에 이미 있던 사용자당 여러 세션은 일괄 회수하지 않는다(마이그레이션은 세션 행을 바꾸지 않는다). 그 세션들은 그 사용자의 다음 로그인 완료 · ADMIN 회수 · 만료 때 정리된다.
- 교체된 브라우저는 다음 요청에서 일반 401 `AUTH_REQUIRED`를 받는다. 열린 WebSocket은 재확인 주기(기본 30초) 안에 4401로 닫힌다. 응답 · close reason에 회수 사유나 새 로그인의 기기 정보를 싣지 않는다.
- 관제 화면을 상황판(wallboard)으로 상시 띄우려면 그 화면 전용 VIEWER 계정을 따로 둔다. 같은 계정으로 다른 곳에서 로그인하면 상황판 세션이 교체된다.

## 사용자 관리 (PR 3)

계약은 `packages/schemas/api/users.py`, 판정 · 잠금 · 트랜잭션은 `admin_service.py`다. 모든 경로에 Origin(상태 변경만) → 세션 → ADMIN 검사(`require_admin`)가 걸린다. 미인증은 401, ADMIN이 아니면 본문 필드 검증과 Cognito 설정 확인보다 먼저 403 `AUTH_FORBIDDEN`이다(DB 변경 · Cognito 호출 없음). (#492) 그다음 서비스가 행위자의 `account_type`으로 관리 범위를 정한다. 범위 · 대상 · 테넌트 판정은 모두 Cognito 호출 전이다.

| 행위자 | 관리 범위 (#492) |
| --- | --- |
| PLATFORM | 모든 테넌트의 TENANT 계정. 정지 테넌트 사용자의 역할 · 상태 · 세션 회수도 관리한다(초대 · 재발송은 ACTIVE 테넌트만) |
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

없는 `user_id`(UUID 형식이 아닌 값 포함), (#492) 관리 범위 밖의 대상, PLATFORM · UNASSIGNED 대상은 모두 같은 404 `USER_NOT_FOUND`이며 Cognito를 부르지 않는다. 목록 · 초대 · 재발송 · 역할 · 상태 · 세션 회수 · 이력 전부에 같은 범위가 걸린다.

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 본인 변경 | 자기 역할 · 상태 변경은 403 `AUTH_FORBIDDEN`. 자기 세션 회수는 허용한다(그 뒤 요청은 401). PLATFORM 행위자 자신은 관리 대상이 아니므로 404다 | [판단] |
| 같은 값 | 현재와 같은 역할 · 상태로 바꾸는 요청은 409 `USER_STATE_CONFLICT` | [판단] |
| 마지막 활성 ADMIN | 활성 ADMIN을 강등 · 비활성화해 그 테넌트에 활성 TENANT ADMIN이 남지 않게 되면 409 `USER_STATE_CONFLICT`. (#492) 테넌트별로 세며 PLATFORM 계정은 세지 않는다. 행위자가 PLATFORM이어도 같다. 집계는 아래 테이블 잠금 안에서 한다 | [판단] |
| 동시성 | 모든 쓰기는 `app_users` 테이블 잠금(`LOCK TABLE app_users IN EXCLUSIVE MODE`) 안에서 한다. 로그인 완료와 재설정 뒤 세션 회수도 같은 잠금을 잡는다. 잠금을 잡은 직후 세션 캐시를 비우고 행위자 세션을 다시 판정한다(`touch=False`). 그 사이 회수 · 비활성 · 강등된 행위자는 401 · 403으로 끝나고 아무것도 쓰지 않는다. 활성 ADMIN 수도 잠금 안에서 세므로, 두 ADMIN이 동시에 서로를 강등 · 비활성화하면 한쪽만 성공한다 | [판단] |
| 비활성화 | 앱 DB의 `status`만 바꾸고 그 사용자의 세션 전부를 `ACCOUNT_DISABLED`로 회수한다. 재활성화는 회수된 세션을 되살리지 않는다(다시 로그인). Cognito 사용자는 비활성화 · 삭제하지 않는다 — Cognito 인증은 계속 통과하지만 로그인 완료가 403 `ACCOUNT_DISABLED`다 | [판단] |
| 세션 회수 | 대상의 미회수 세션 전부(만료된 행 포함)를 `ADMIN_REVOKED`로 회수하고, 회수한 개수를 이력에 남긴다 | [판단] |
| 초대 | 기본은 새 Cognito 사용자 생성(`AdminCreateUser`)이다. 초대 메일은 Cognito가 이메일로 보내며, 앱은 임시 비밀번호를 요청하지도 읽지도 않는다. Cognito에 이미 있으면 409 `USER_STATE_CONFLICT`이고 자동 연결하지 않는다. Cognito 생성이 성공한 뒤에 앱 사용자(`ACTIVE`, `email_verified_at` 비어 있음)와 `USER_INVITED` 이력을 한 commit으로 만든다. 첫 로그인은 최초 비밀번호 변경 · TOTP 등록 · 이메일 코드를 거친다 | [판단] |
| 기존 계정 연결 | `recover_existing=true`는 PLATFORM 행위자만 고를 수 있는 명시적 선택이다. (#492) PLATFORM이 아닌 행위자(TENANT ADMIN)가 보내면 403 `AUTH_FORBIDDEN`이고, 어댑터 조립 · SDK 호출 · DB 변경이 없다. 이 판정은 쓰기 행위자 판정(401 · 403 · 소속 정지 409 `TENANT_SUSPENDED`) 다음, 초대 대상 테넌트 판정 전이다 — 소속 테넌트가 정지된 TENANT ADMIN은 409 `TENANT_SUSPENDED`가 먼저다. `recover_existing=false`인 일반 초대는 TENANT ADMIN도 자기 테넌트에 그대로 할 수 있다. PLATFORM의 요청이면 `AdminGetUser`로 기존 Cognito 사용자를 찾아(없으면 404 `USER_NOT_FOUND`) 앱에 아직 없는 신원이면 연결한다(이미 있으면 409). 직전 초대가 실패했는지는 확인하지 않고 Cognito 사용자 상태와 관계없이 연결한다. 이메일 소유 증명이 아니므로 첫 로그인에서 이메일 코드를 요구한다 | [판단] |
| 재발송 | 앱 사용자의 issuer가 설정된 User Pool issuer와 같아야 한다. 앱 사용자의 이메일로 `AdminGetUser`를 호출해, 돌아온 Cognito 사용자의 `sub`가 앱 사용자의 `sub`와 같고 Cognito 상태가 `FORCE_CHANGE_PASSWORD`일 때만 `AdminCreateUser(MessageAction=RESEND)`를 보낸다. Cognito 쪽 이메일 값은 비교하지 않는다. AWS 문서상 RESEND는 새 임시 비밀번호를 보내고 임시 비밀번호 유효 기간을 다시 시작한다. 조건이 다르면 409(재발송 없음), 대상이 없으면 404(Cognito 미호출). SDK 호출 뒤 앱 DB의 대상 신원(issuer · `sub` · 이메일)을 다시 읽어 처음 읽은 값과 다르면 409다(재발송은 이미 나갔고 이력은 남지 않는다) | [표준] [AWS: AdminCreateUser `MessageAction`](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_AdminCreateUser.html) |
| Cognito 오류 | 초대 · 재발송: `UsernameExistsException` · `UnsupportedUserStateException` → 409 `USER_STATE_CONFLICT`, 발송 실패(`CodeDeliveryFailureException` · `InvalidEmailRoleAccessPolicyException`) → 503 `AUTH_EMAIL_DELIVERY_FAILED`, 요청 제한 → 429 `AUTH_RATE_LIMITED`, 그 밖 → 503 `AUTH_PROVIDER_UNAVAILABLE`. `AdminGetUser`는 `UserNotFoundException`일 때만 "없음"이고, 그 밖의 실패 · 비정상 응답은 429 · 503이다 | [판단] |
| Cognito 미설정 | 초대 · 재발송만 503 `AUTH_PROVIDER_UNAVAILABLE`. 목록 · 역할 · 상태 · 세션 회수 · 이력은 동작한다. (#492) 어댑터는 지연 조립(`routers.users.get_cognito_adapter_factory`)이라, 서비스가 행위자 · 대상 · 테넌트 사전 판정(422 · 404 · 409)을 마친 요청만 503에 이른다 — 설정 누락이 그 거절을 가리지 않는다 | [판단] |
| 계정 종류 부여(#492) | 사용자 관리 API로 만드는 계정은 항상 TENANT다. 이 API로 PLATFORM을 주거나 UNASSIGNED를 만들거나 소속(`tenant_id`)을 옮기는 경로가 없다(역할 · 상태 변경 요청에는 소속 칸이 없다). PLATFORM은 운영 CLI(§플랫폼 계정 CLI)로만 생긴다 | [팀 결정: 명시적 플랫폼 권한 · 고객사 범위] [판단: UNASSIGNED] |
| 초대 대상 테넌트(#492) | `tenant_id`는 스키마상 선택이고 최종 값은 서버가 정한다. PLATFORM은 반드시 보낸다(없으면 422 `REQUEST_VALIDATION_FAILED`, 없는 테넌트면 404 `TENANT_NOT_FOUND`). TENANT ADMIN은 생략하거나 자기 테넌트만 보낼 수 있고, 다른 값은 404 `TENANT_NOT_FOUND`다 | [팀 결정: 고객사 범위] [판단: 요청 형태 · 오류 코드] |
| 테넌트 상태(#492) | 초대 · 재발송은 대상 테넌트가 ACTIVE일 때만 받는다(그 밖은 409 `TENANT_STATE_CONFLICT`). Cognito 호출 전에 확인하고, DB 쓰기 직전에 잠금 안에서 다시 확인한다(테넌트 행 `FOR SHARE` — commit까지 상태 전이를 막는다). 역할 · 상태 · 세션 회수 · 조회는 대상 테넌트 상태를 보지 않는다 | [판단] |
| 정지 테넌트의 행위자(#492) | 소속 테넌트가 SUSPENDED인 TENANT 행위자의 쓰기(초대 · 재발송 · 역할 · 상태 · 세션 회수)는 409 `TENANT_SUSPENDED`이고, 잠금 안에서 다시 판정한다. 조회는 허용한다. 로그인 · 로그아웃 · 활동 · 비밀번호 재설정은 테넌트 정지와 관계없이 동작한다. 사용자 `DISABLED`는 이와 별개 상태다(로그인 403 `ACCOUNT_DISABLED` · 세션 401) | [판단] |

### 부분 실패

Cognito 호출은 DB 트랜잭션 밖에서 DB 쓰기보다 먼저 한다(짧은 읽기 트랜잭션으로 행위자를 확인하고 닫은 뒤 호출). 제공자 쪽이 성공한 뒤 DB 쓰기가 실패하면 500 `INTERNAL_ERROR`(오류 상세 비노출)이고, 제공자 쪽 결과는 되돌리지 않는다. 승인된 한계다 [판단].

SDK 호출 뒤 잠금 안의 재확인(행위자 · 대상 신원 · 대상 테넌트 ACTIVE)은 메일이 이미 나간 뒤에 요청을 거절할 수 있다. 따라서 오류 코드만으로 메일이 나가지 않았다고 판단하지 않는다. 앱은 Cognito 사용자를 자동으로 지우지 않고, 부작용이 있는 호출을 자동으로 재시도하지 않는다 [판단].

| 경우 | 남는 상태 | 복구 |
| --- | --- | --- |
| 초대: Cognito 생성(초대 메일 발송) 뒤 DB 실패 | Cognito 사용자가 남는다. 앱 사용자 · 이력은 없다 | PLATFORM이 `recover_existing=true`로 다시 초대해 연결 |
| 재발송: RESEND 뒤 DB 실패 | 새 임시 비밀번호 메일은 나갔고 `INVITE_RESENT` 이력은 없다 | 다시 재발송하면 이력이 남는다(새 임시 비밀번호가 다시 나간다) |
| 초대 · 재발송: SDK 호출 중 행위자 세션이 회수 · 비활성 · 강등됨 | 401 · 403. 제공자 결과는 남고 DB는 쓰지 않는다 | 유효한 ADMIN이 다시 수행(초대의 기존 계정 연결은 PLATFORM이 `recover_existing=true`로) |
| (#492) 초대 · 재발송: SDK 호출 뒤 대상 테넌트가 ACTIVE가 아니게 됨 · 행위자 소속 테넌트가 정지됨 | 409(`TENANT_STATE_CONFLICT` · `TENANT_SUSPENDED`). 메일은 나갔을 수 있고 DB는 쓰지 않는다 | 테넌트가 ACTIVE로 돌아온 뒤 유효한 행위자가 다시 수행(초대의 기존 계정 연결은 PLATFORM이 `recover_existing=true`로) |
| 재설정 확정: 비밀번호 변경 뒤 세션 회수 실패 | 비밀번호는 바뀌었고 이전 세션이 남을 수 있다 | §비밀번호 재설정 |

### 이력 (`user_admin_events`)

- 동작 5종: `USER_INVITED` · `INVITE_RESENT` · `ROLE_CHANGED` · `STATUS_CHANGED` · `SESSIONS_REVOKED`. 행위자 · 대상 · 전후 상태 · 시각을 상태 변경과 같은 commit에 넣는다.
- 전후 상태는 `UserEventState`의 닫힌 목록(`role` · `status` · `revoked_session_count` · `recover_existing` · (#492) 초대 대상 `tenant_id`)만 저장하고 모르는 키는 거절한다. 이메일 · 비밀번호 · 코드 · 토큰 · Cognito 원본 응답은 싣지 않는다.
- append-only: `BEFORE UPDATE OR DELETE` 트리거가 수정 · 삭제를 거절한다(INSERT만 허용). 이력이 참조하는 사용자는 FK `RESTRICT`로 지울 수 없다. 테이블 소유자(DB owner)는 트리거를 끄고 우회할 수 있으므로 소유자에 대한 방어는 아니다.
- 비밀번호 재설정 · 로그인 교체 · 로그아웃은 이력 대상이 아니다(세션 행의 `revoke_reason`으로만 남는다). 조치 실행 감사 기록(T-2)과는 별개다.
- (#492) CLI의 플랫폼 계정 생성 · 지정도 이 이력에 남지 않는다(지정의 세션 회수는 `ADMIN_REVOKED`로만 남는다).

### OWASP ASVS 4.0.3 V4와의 관계 (#492)

원문: [V4 Access Control](https://raw.githubusercontent.com/OWASP/ASVS/v4.0.3/4.0/en/0x12-V4-Access-Control.md). 아래는 사용자 관리 API와 계정 종류 권한(업무 기록 테넌트 범위 · 메트릭 · WebSocket 플랫폼 전용 포함) 범위에서 정책이 따르는 항목이다. 이 README는 모듈 범위의 정책만 적고 앱 전체의 ASVS 준수를 주장하지 않는다. 표에 없는 V4 항목은 이 문서가 다루지 않는다.

| 항목 | 이 범위의 정책 |
| --- | --- |
| 4.1.1 신뢰할 수 있는 서버 계층에서 접근 제어 강제 | 계정 종류 · 역할 · 관리 범위 판정은 서버(`auth.access` · `admin_service`)가 한다. 화면의 메뉴 숨김 · 첫 화면 분기는 편의이며 차단 근거가 아니다 |
| 4.1.2 접근 제어 속성을 최종 사용자가 조작할 수 없음 | `account_type` · `tenant_id`는 API 입력으로 바꿀 수 없다. 초대의 `tenant_id`는 PLATFORM만 고르고, TENANT ADMIN은 서버가 행위자 소속으로 정한다. 기존 Cognito 계정 연결(`recover_existing`)도 PLATFORM만 고른다. PLATFORM 생성 · 지정은 운영 CLI 직접 실행뿐이다 |
| 4.1.3 최소 권한 | TENANT ADMIN은 자기 테넌트만, OPERATOR · VIEWER는 사용자 관리 없음. 자산 · 인시던트 · 조치는 요청자 테넌트 범위만(#533), 테넌트 범위가 없는 메트릭 · WebSocket은 PLATFORM만 |
| 4.1.5 안전한 실패 | UNASSIGNED와 그 밖의 종류는 권한 없음으로 처리한다(세션 판정 401, 관리 범위 403). 소속 NULL을 「전체」로 읽지 않는다 |
| 4.2.1 IDOR | `user_id` 경로 대상은 관리 범위 안의 TENANT 계정만이다. 범위 밖 · PLATFORM · UNASSIGNED · 없는 대상은 같은 404 `USER_NOT_FOUND`이고 Cognito를 부르지 않는다 |

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

화면은 `/me`의 `account_type` · `role`만으로 가른다(`apps/web/src/lib/roles.ts` · `home-path.ts`). 역할이 주는 권한(`roles.ts`)과 테넌트 격리 전 지금 열린 기능(`home-path.ts`의 `businessOpen`)은 따로 판정한다. 분기와 숨김은 편의이고 차단은 서버다.

| 계정 | 첫 화면 | 메뉴 | 업무 조회 · 실시간 |
| --- | --- | --- | --- |
| PLATFORM | `/tenants`(고객) | 대시보드 · 자산 · 보안 · 고객 · 사용자 | 사용 |
| TENANT ADMIN | `/users`(사용자) | 사용자 | 시작하지 않는다 — 자산 주기 조회와 WebSocket 연결을 열지 않는다 |
| TENANT OPERATOR · VIEWER | `/`(업무 화면 이용 제한) | 없음(사이드바를 그리지 않는다) | 위와 같음 |
| 계정 확인 중 · 미로그인 · `/me` 실패 | — | 없음(사이드바를 그리지 않는다) | 위와 같음 |

- 첫 화면은 로그인 뒤 돌아갈 곳(`?next=`)이 없을 때의 이동처다(`home-path.ts`의 `homePathFor`). 로그인 직후 `/me`를 읽지 못하면 `/`로 보내 거기서 다시 확인한다.
- `/`(`apps/web/src/app/page.tsx`)는 업무 조회 전에 `/me`로 계정을 확인한다. PLATFORM에게는 기존 대시보드를, TENANT 계정에게는 업무 API를 부르지 않고 짧은 `업무 화면 이용 제한` 안내를 보인다. 이 안내는 서버 403 오류 화면이 아니며, 고객용 업무 대시보드를 여는 것도 아니다. `/me` 실패는 오류 화면으로 알리며(401은 로그인 이동) 이용 제한 안내나 빈 대시보드로 바꾸지 않는다.
- 셸의 자산 주기 조회(수집 · 연결 인디케이터)와 전역 WebSocket(`components/gnb.tsx` · `realtime-provider.tsx`)은 `/me`가 PLATFORM으로 확인된 뒤에만 시작한다. 확인 중 · 미로그인 · 확인 실패 · TENANT 계정에서는 업무 요청을 보내지 않는다.
- 상단바 오른쪽 이메일(`components/auth/user-menu.tsx`)을 누르면 `계정 정보` 대화상자가 열린다(별도 화면은 없다). SessionProvider가 이미 가진 `/me` 값으로 이메일 · 역할(사용자 관리와 같은 이름) · 계정 종류를 보이고, TENANT 계정이면 자기 고객 ID를 더한다. PLATFORM 계정에는 고객 ID 항목이 없다(`apps/web/src/lib/account-summary.ts`). 고객 이름은 `/me` 계약에 없어 보이지 않으며, 이를 위해 새 조회 API · PLATFORM 전용 테넌트 API를 부르거나 공개 계약을 넓히지 않는다.
- 지금 쓸 수 있는 것은 TENANT ADMIN의 자기 테넌트 사용자 관리다. 자산 · 보안 조회와 조치 실행 · 인시던트 종료는 역할상 권한이 있어도 화면에서 열리지 않는다(`businessOpen`은 PLATFORM만). 서버의 자산 · 인시던트 · 조치 API는 요청자 테넌트 범위로 열려 있고, 메트릭 · WebSocket은 PLATFORM 전용이다(§접근 보호).
- 사용자 변경 이력은 기존 ADMIN 관리 범위의 `user_admin_events` 조회(§이력)이며, 로그인 기록이나 조치 실행 감사 기록(T-2)을 포함한 전체 감사가 아니다.
- 사용자 관리 화면은 고객 ID(`tenant_id`) 열을 보인다. PLATFORM은 초대할 고객 ID(UUID)를 직접 입력하고, TENANT ADMIN은 자기 소속을 읽기 전용으로 보며 다른 고객을 고를 수 없다. 초대의 기존 계정 연결(복구) 선택은 PLATFORM에게만 보이고, TENANT ADMIN의 초대 본문은 언제나 `recover_existing=false`다(`apps/web/src/lib/user-admin.ts`의 `inviteRequestFor`).
- 오류는 서버 판정대로 알린다 — 범위 밖 사용자는 대상 없음과 같은 문구, 고객 상태 거절은 고객 사유, 초대 · 재발송의 500 · 네트워크 실패는 메일이 나갔을 수 있다는 안내다(`apps/web/src/lib/user-admin.ts`).
- 백엔드에는 테넌트 목록 · 상세 API(`GET /api/v1/tenants` · `/api/v1/tenants/{id}`, PLATFORM 전용)가 있지만, 사용자 관리 화면은 이를 쓰지 않아 고객 선택 목록이 없다. 초대할 고객 ID는 위처럼 직접 입력한다. `/tenants` 화면과 테넌트 API 자체는 이 모듈의 범위가 아니다.

## 설정

`apps/core-api/config.py`의 `AuthSettings`(PR 2의 두 키는 `Settings`). 환경변수와 루트 `.env`를 읽는다. 키 목록과 설명은 루트 `.env.example`에 있다. #492는 설정 키를 추가하지 않으며, 메트릭 · WebSocket 플랫폼 전용과 업무 기록 테넌트 범위(#533)에도 켜고 끄는 설정이 없다.

| 키 | 기본값 | 비고 |
| --- | --- | --- |
| `COGNITO_REGION` · `COGNITO_USER_POOL_ID` · `COGNITO_CLIENT_ID` · `COGNITO_CLIENT_SECRET` | 없음 | 하나라도 비면 앱은 뜨고, Cognito를 부르는 경로 — 로그인 6단계 · 비밀번호 재설정 2종 · 초대(`POST /api/v1/users`) · 재발송 — 만 503 `AUTH_PROVIDER_UNAVAILABLE`이다. `/me` · `/activity` · `/logout`, 그 밖의 사용자 관리 경로와 보호 API는 미설정이어도 동작한다(PR 2까지는 `/api/v1/auth/*` 전체가 503). 비ADMIN의 초대 · 재발송은 설정과 관계없이 403이 먼저이고, (#492) 범위 · 테넌트 사전 판정(422 · 404 · 409)과 TENANT ADMIN의 기존 계정 연결 거절(403)도 503보다 먼저다(§사용자 관리). 기동 로그에 누락 키 이름만 남긴다 |
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
uv run python -m auth.bootstrap_admin --platform --email ops@example.com                                # 생성 미리보기(변경 없음)
uv run python -m auth.bootstrap_admin --platform --email ops@example.com --yes                          # Cognito 사용자 생성(초대 메일) + PLATFORM 등록
uv run python -m auth.bootstrap_admin --platform --existing-cognito-user --email ops@example.com --yes  # 이미 있는 Cognito 사용자를 명시 연결
uv run python -m auth.bootstrap_admin --designate-unassigned-user-id <user_id>                          # 지정 미리보기
uv run python -m auth.bootstrap_admin --designate-unassigned-user-id <user_id> --yes                    # UNASSIGNED → PLATFORM 지정
```

- 기본은 미리보기이고 `--yes`일 때만 바꾼다. 종료 코드는 0 성공 · 1 실패 · 2 거절이다.
- **생성**: `--platform`과 `--email`이 필요하다(ADMIN이라고 플랫폼으로 추론하지 않는다). 첫 계정과 추가 계정의 동작 · 권한이 같다 — `app_users`에 행이 있어도 추가한다. 만든 계정은 `account_type=PLATFORM` · `role=ADMIN` · `tenant_id` NULL이다. Cognito 설정 4키가 필요하다.
- 같은 신원 `(issuer, sub)`이 이미 `app_users`에 있으면 생성을 거절하고 기존 계정을 바꾸지 않는다 — TENANT 소속을 옮기거나 플랫폼으로 바꾸지 않는다. 확인과 삽입은 같은 테이블 잠금 안에서 한다.
- 기존 Cognito 사용자 연결은 운영자가 `--existing-cognito-user`로 명시할 때만 한다(`AdminGetUser`의 `sub`로 연결).
- **지정**: `--designate-unassigned-user-id`는 생성 플래그(`--email` · `--platform` · `--existing-cognito-user`)와 함께 쓸 수 없다. 대상은 ACTIVE이고 `tenant_id` NULL인 UNASSIGNED 계정뿐이며, TENANT · 비활성 계정은 거절한다(복구하지 않는다). Cognito를 부르지 않아 Cognito 클라이언트 · 설정이 없어도 실행된다. 테이블 잠금 안에서 다시 확인한 뒤 `role=ADMIN` · `account_type=PLATFORM` 변경과 기존 세션의 `ADMIN_REVOKED` 회수를 한 commit으로 한다. 대상은 새로 로그인해야 한다.
- 생성 · 지정 모두 Cognito의 비밀번호 · TOTP · MFA 설정을 바꾸지 않는다. TOTP 분실 복구는 여전히 [#515](https://github.com/ProjectVigilantis/vigilantis/issues/515) 몫이다.
- 플랫폼 계정의 비활성화 · 강등 · 삭제 경로와 플랫폼 역할 옵션은 없다. 사용자 관리 API도 PLATFORM 계정을 대상으로 받지 않는다(404).
- 고객사 사용자(TENANT)는 PLATFORM 또는 그 테넌트의 ADMIN이 `POST /api/v1/users`로 초대한다(§사용자 관리).
- 임시 비밀번호는 Cognito가 초대 메일로만 보낸다. CLI는 비밀값을 출력하지 않고 `user_id` · Cognito 사용자 상태 · 회수한 세션 수만 출력한다.
- Cognito 사용자 생성 뒤 DB 등록이 실패하면 Cognito 사용자는 남는다. 이때는 `--existing-cognito-user`로 다시 연결한다.
- PLATFORM 계정을 만드는 다른 경로(공개 등록 API · 화면)는 없다.

## 테스트

| 구분 | 파일 | 보장 범위 |
| --- | --- | --- |
| 계약 | `packages/schemas/tests/test_api_auth.py` · `test_api_errors.py` | 단계 · 역할 · 오류 코드 집합(계약 원문 리터럴과 정확히 일치 — PR 2의 `AUTH_FORBIDDEN`, PR 3의 `USER_NOT_FOUND` · `USER_STATE_CONFLICT` 포함), 비밀값 비노출, 입력 경계. (PR 3) 재설정 요청이 새 비밀번호 길이 · 코드 계약을 공유하고 두 비밀값을 숨김 |
| 계약(PR 3) | `packages/schemas/tests/test_api_users.py` | 기존 계정 연결은 `recover_existing`을 명시할 때만, 잘못된 역할 · 상태 값 거절, 이력 상태에 비밀값 필드를 받을 수 없고 닫힌 타입 값만 받음 |
| 순수 함수(PR 3) | `apps/core-api/tests/test_auth_password_policy.py` | 기호 목록이 정확히 ASCII 문장부호 32자, 길이 경계, 문자 종류 하나라도 없으면 거절, 각 기호의 기호 인정, 위치와 관계없는 공백 거절(trim 없음), 비 ASCII · 제어 문자 거절, 한글이 문자 종류를 대신하지 못함 |
| 대역(botocore Stubber) | `apps/core-api/tests/test_auth_cognito.py` · `test_auth_login_flow.py` | 요청 파라미터(`USER_ID_FOR_SRP` · `SECRET_HASH`), 세션 발급 조건, 예상 밖 토큰 · challenge 거절, 시도 상한, 흐름 만료, refresh token 회수, 로그 · 예외에 비밀값 없음 |
| 실제 PostgreSQL | `apps/core-api/tests/test_auth_sessions.py` · `test_auth_api.py` · `test_auth_bootstrap_admin.py` · `db/tests/test_auth_*.py` | 해시만 저장, **유휴 30분 · 최대 8시간 만료, `GET /me` 반복이 유휴를 늘리지 않음**, 활동이 최대 수명을 넘지 않음, 회수, `(issuer, sub)` 유일, 쿠키 속성, Origin 거절, 보안 헤더, 미구성 시 503, CLI |
| 실제 PostgreSQL(PR 2) | `apps/core-api/tests/test_api_access.py` | 경로 전수(공개 목록을 실제 경로와 정확히 대조, 그 밖은 미인증 401 · WebSocket 1008), (#533 갱신) 메트릭은 PLATFORM만 허용하고 TENANT 계정은 역할 · 테넌트 상태와 관계없이 403, 자산 · 인시던트 · 조치는 TENANT 계정도 세션 · 역할 검사를 통과해 자기 테넌트 범위로 판정됨, 실행 · 종료의 역할 거절(VIEWER 403)은 Workflow 미호출 · DB 무변화, 판정 함수 단위로 역할 검사가 테넌트 정지를 보지 않고 플랫폼 · 범위 판정이 `account_type`을 봄, 무효 세션 5종(유휴 · 최대 수명 · `LOGOUT` · `SUPERSEDED` · 비활성) 401, 없는 · 외부 · `null` Origin이 세션 조회 전 403, 보호 API 응답(200 · 401 · 403 · 404 · 422 · 500)의 보안 헤더 3종, 보호 요청이 `last_activity_at`을 바꾸지 않음, API 문서 토글, 접근 로그의 `user_id` · `role`과 쿠키 · 토큰 · 이메일 부재 |
| 실제 PostgreSQL · commit 세션(PR 2) | `apps/core-api/tests/test_ws_access.py` · `test_ws_connect.py` · `test_ws_publish.py` | Handshake 1008(세션 없음 · 모르는 토큰 · 회수 · 비활성 · 외부 Origin, `AUTH_ALLOWED_ORIGINS`가 더 좁을 때 CORS에만 있는 Origin — HTTP 403과 같은 목록), 연결 중 `LOGOUT` · `SUPERSEDED` · 비활성 · 유휴 만료 → 4401 `"AUTH_REQUIRED"`, 주기보다 이른 만료 시각의 재확인, 연결 · 재확인이 `last_activity_at`을 바꾸지 않음, 재확인 실패 1011(로그에 오류 종류만) |
| 대역(PR 3) | `apps/core-api/tests/test_auth_cognito.py` · `test_auth_login_flow.py` | `AdminGetUser`는 `UserNotFoundException`만 "없음"(그 밖의 실패 · 네트워크 오류 · 비정상 응답은 장애), `AdminCreateUser` 생성 · RESEND 파라미터와 오류 매핑, `ForgotPassword` 파라미터 · 발송 정보 폐기 · 존재/자격 오류 정규화 · 그 밖의 오류 노출, `ConfirmForgotPassword` 파라미터와 오류 매핑, 최초 비밀번호 변경의 앱 정책 위반이 Cognito 호출 전에 거절되고 흐름 유지 |
| 실제 PostgreSQL + 대역(PR 3) | `apps/core-api/tests/test_auth_password_reset.py` | 요청: 정확한 파라미터 · 빈 204, 없음 · 부적격 · 미인증이 같은 응답, 제한 · 발송 · 장애 오류 노출. 두 경로 모두 Origin 거절 시 Cognito 미호출. 확정: 스키마 422 · 앱 정책 422가 Cognito 전, 성공 시 그 사용자 세션만 `PASSWORD_RESET` 회수 · 쿠키 없음, 앱 행 없는 `sub`는 204, `sub`를 모르는 성공은 500, 실패 매핑과 세션 유지, 조회 실패는 변경 전 중단, 변경 뒤 회수 실패 500(이전 세션 남음), 변경 전 DB 불가 500(Cognito 미호출), 로그 · 응답에 코드 · 비밀번호 없음 |
| 실제 PostgreSQL(PR 3) | `apps/core-api/tests/test_user_admin_api.py` | 비ADMIN은 모든 경로(잘못된 본문 포함) 403 · DB 불변 · Cognito 미호출, 미인증 401, 목록의 `last_login_at`, 이력 `limit` 경계 · 없는 대상, 초대 · 중복 409(자동 연결 없음) · 제공자 실패 · DB 실패 500(앱 행 없음, 제공자 사용자 유지), `recover_existing` 연결 · 404 · 409, 재발송과 상태 불일치 409, 역할 · 상태 변경과 본인 403 · 같은 값 409 · 마지막 활성 ADMIN 409, 비활성화의 세션 회수와 재활성화 뒤 미복구, 세션 회수 개수 · 본인 회수, Cognito 미설정 시 403 → 503 순서와 그 밖의 경로 동작, 행위자 토큰 repr 비노출. 독립 commit 세션: 잠금 대기 중 회수 · 비활성 · 강등된 행위자는 아무것도 쓰지 않음, SDK 호출 중 회수된 행위자(제공자 결과만 남음), 두 ADMIN의 동시 상호 강등 · 비활성화에서 한쪽만 성공 |
| 실제 PostgreSQL + 대역(#492) | `apps/core-api/tests/test_user_admin_api.py` | 관리 범위: 대상 경로 목록이 사용자 단위 경로 전부와 일치(`test_target_ops_cover_every_user_scoped_route`), 범위 밖 대상(PLATFORM 계정 — PLATFORM 행위자 본인 포함 · UNASSIGNED, TENANT ADMIN에게는 다른 테넌트 · 정지된 다른 테넌트의 사용자)이 없는 대상과 같은 404 `USER_NOT_FOUND` · 같은 메시지이고 Cognito 미호출 · DB 무변화, 목록은 PLATFORM이 모든 테넌트 · TENANT ADMIN이 자기 테넌트만, PLATFORM이 어느 테넌트의 사용자든 관리(정지 테넌트 사용자는 재발송 밖 전부), TENANT OPERATOR · VIEWER는 테넌트 상태와 관계없이 모든 경로 403. 초대 · 재발송: PLATFORM 초대는 테넌트 명시 필수, 계정 종류 지정 불가, 없는 테넌트 404 `TENANT_NOT_FOUND` · ACTIVE 아닌 테넌트 409 `TENANT_STATE_CONFLICT`가 SDK 호출 전, TENANT ADMIN 초대는 자기 테넌트에 묶임, TENANT ADMIN의 `recover_existing`은 403 `AUTH_FORBIDDEN` · SDK 0회 · DB 무변화(소속 정지면 409 `TENANT_SUSPENDED`가 먼저)이고 일반 초대는 허용, `recover_existing` 409에서 기존 소속 불변, SDK 호출 뒤 테넌트 재확인 거절 시 아무것도 쓰지 않음. Cognito 미설정이어도 사전 판정(422 · 404 · 409)이 503보다 먼저. 정지 테넌트 ADMIN: `/me` · 목록 · 닫힌 필드만 담긴 이력 조회 · 활동은 되고 모든 변경은 409 `TENANT_SUSPENDED` · Cognito 미호출 · DB 무변화. 테넌트별 마지막 활성 ADMIN. 독립 commit 세션: 같은 테넌트의 두 TENANT ADMIN, 또는 두 PLATFORM 행위자가 마지막 두 ADMIN을 동시에 제거하면 한쪽만 성공, 재확인이 테넌트 행을 commit까지 잡아 그 사이 정지를 막음(`test_tenant_recheck_holds_the_tenant_row_until_commit`) |
| 실제 PostgreSQL(PR 3) | `apps/core-api/tests/test_auth_login_single_session.py` | 다른 브라우저의 두 번째 로그인이 첫 세션 교체, 다른 사용자의 이전 쿠키도 교체, 비활성 · 미등록 거절 시 변경 없음, 사용자 단위 일괄 회수, 동시 로그인 뒤 살아 있는 세션 정확히 1개 |
| 실제 PostgreSQL(PR 3) | `apps/core-api/db/tests/test_user_admin.py` · `test_user_admin_migration.py` | 회수 사유 enum, 마지막 로그인 = 회수 세션 포함 최신 세션, 활성 ADMIN 집계, 외부 변경 뒤 최신 값 읽기, 만료 포함 일괄 회수, 이력 최신순 · 대상별, 허용 목록 밖 상태 거절, UPDATE · DELETE 거절 · INSERT 허용, 이력이 참조하는 사용자 삭제 불가, 단일 head · 왕복 마이그레이션 |
| 실제 PostgreSQL(PR 3 갱신) | `apps/core-api/tests/test_api_access.py` · `test_auth_api.py` | 공개 목록에 재설정 2종 추가, 사용자 관리 경로도 경로 전수 검사 대상(미인증 401). Cognito 미설정 시 로그인 · 재설정만 503이고 `/logout` · `/activity` · `/me` · 보호 API는 동작 |
| 실제 PostgreSQL(#492) | `apps/core-api/db/tests/test_user_account_type_migration.py` · `test_auth_models.py` · `test_user_admin.py` | 부모 revision(`e492a3b5c701` → `f492c4a8d2e1`)과 그래프의 head 1개, head 스키마와 ORM 일치 · 기본값 없음, 기존 행 분류(승격 없음)와 왕복, `account_type` 없는 INSERT 거절, CHECK 2개의 거절 · 허용 형태, 목록은 TENANT 계정만 · 테넌트 범위, 테넌트별 활성 ADMIN 집계 |
| 실제 PostgreSQL(#492 · #533) | `apps/core-api/tests/test_api_access.py` · `test_ws_access.py` | 메트릭 라우트는 PLATFORM 전용이고 TENANT 계정의 조회는 거절 · DB 무변화, 자산 · 인시던트 · 조치 라우트는 TENANT 계정을 계정 종류로 거절하지 않고 그 소속 테넌트 범위로 판정, 플랫폼 · 범위 판정이 역할이 아니라 `account_type`을 봄, PLATFORM의 조회 · 실행 · 종료, PLATFORM이 아닌 계정의 Handshake가 accept 전 거절 |
| 실제 PostgreSQL(#492) | `apps/core-api/tests/test_auth_api.py` · `test_auth_login_single_session.py` · `test_auth_password_reset.py` | PLATFORM `/me`의 `tenant_id` null, UNASSIGNED 로그인 완료 403 · 발급 없음과 기존 세션 401, 정지 테넌트 사용자의 로그인 · `/me` · 활동 · 로그아웃 · 재설정, 테넌트 정지의 재로그인 없는 반영, 정지 테넌트의 DISABLED 사용자 |
| 실제 PostgreSQL + 대역(#492) | `apps/core-api/tests/test_auth_bootstrap_admin.py` | `--platform` · `--email` 없는 생성 거절(SDK 호출 0회), 사용자가 있어도 추가 플랫폼 계정, 기존 신원 이전 없음, 지정 미리보기 무변경, 지정의 변경 · 세션 회수(SDK 호출 0회), 부적격 대상 거절, 생성 플래그 혼용 거절, 잠금 안 재확인, 같은 신원 동시 생성에서 1건만 |
| 계약(#492) | `packages/schemas/tests/test_api_auth.py` · `test_api_users.py` | `AccountScope` 짝 규칙, 초대의 `tenant_id`는 플랫폼 권한 부여가 아님, 관리 요청으로 소속을 바꿀 수 없음 |
| 웹 유닛(#492) | `apps/web/src/lib/home-path.test.ts` · `account-summary.test.ts` · `user-admin.test.ts` | 계정 종류별 첫 화면 · 메뉴, 상단바 계정 정보 항목(TENANT만 고객 ID), 초대 본문(PLATFORM은 고객 ID 필수 · TENANT ADMIN은 생략하고 기존 계정 연결은 언제나 false), 범위 밖 · 고객 상태 · 결과 불명 오류 문구 |
| 실제 PostgreSQL(#492) | `apps/core-api/db/tests/test_account_scope_merge_migration.py` | 병합 revision `c492d6e8f4a2`의 부모 2개(`f492c4a8d2e1` · `f514a6c3d201`) · head 1개와 두 가지가 `e492a3b5c701`에서 갈라지는 부모 관계, 새 DB의 base → head, #492 가지 끝(`f492c4a8d2e1`) · dev 가지 끝(`f514a6c3d201`)에서 head로 앞으로만 올린 DB가 두 열(`app_users.account_type` · `tenants.auto_action_resumed_at`)을 모두 갖고 그 revision 모양으로 넣은 테넌트 · 사용자 행을 보존하며 기록 revision이 병합 revision 하나가 되고 ORM과 일치, dev 가지의 소속 사용자는 `TENANT`로 분류(승격 없음). 테스트마다 일회용 DB를 만들고 지운다 |
| 실제 Cognito | 자동 테스트 없음 | 없음. CI는 실제 Cognito를 호출하지 않는다(아래 주의) |

통합 픽스처(`apps/core-api/tests/conftest.py`): `client_pg`는 테스트 DB에 만든 PLATFORM ADMIN 실세션(`platform_session`) 쿠키와 허용 Origin을 기본 헤더로 싣는다(세션 쿠키는 `Secure`라 httpx 쿠키 저장소가 http로 보내지 않아 `Cookie` 헤더로 직접 싣는다). 기본 세션은 PLATFORM이라 모든 테넌트 범위의 기록과 플랫폼 전용 메트릭에 접근하므로, 계정 종류 · 테넌트 범위를 따로 확인하지 않는 테스트는 이 기본 세션을 쓴다. 세션 없는 클라이언트는 `client_pg_anon`, 다른 계정은 `login_as(role, tenant_id=..., account_type=...)`, 임의 시드는 `make_auth_session(role, tenant_id=, account_type=, status=, now=)`다. `tenant_id`를 주면 TENANT, 아니면 `account_type`(기본 PLATFORM, `UNASSIGNED`도 고를 수 있다)이며 계정 종류를 추론하지 않는다 — `tenant_id` 없이 ADMIN 밖의 역할을 주면 DB CHECK가 시드를 거절한다. TENANT 계정은 실제 테넌트 행에 묶는다: 테스트 트랜잭션은 `make_tenant(status)`, 임의 세션 · 격리 DB는 `seed_tenant`, commit 세션은 `committed_auth.make_tenant`. WebSocket은 다른 스레드가 자기 DB 세션으로 판정하므로 `ws_client` · `committed_auth`가 실제로 commit하고 끝나면 지운다.

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
- **TOTP 분실(PR 3 제외).** 비밀번호 재설정은 비밀번호만 바꾸고 로그인에는 여전히 TOTP가 필요하므로, 인증 앱을 잃은 사용자는 재설정으로 복구할 수 없다. 관리자용 TOTP 재설정 UI · API · CLI는 PR 3 게시 뒤의 후속 작업이다([#515](https://github.com/ProjectVigilantis/vigilantis/issues/515)). 필요한 `AdminDeleteSoftwareToken`은 botocore 1.43.82부터 있어(현재 잠금 1.43.70에는 없다) 후속에서 의존성 갱신이 필요하다. 마지막 ADMIN이 TOTP를 잃으면 다시 로그인하지 못할 수 있다. 다만 이미 열린 세션은 만료 · 회수 전까지 유효하므로 즉시 운영이 멈추지는 않는다.
- **세션 확인 부하.** 보호 API 요청마다 `auth_sessions` · `app_users` 조회가 2번 붙는다(요청 세션 안). 열린 WebSocket은 연결마다 `WS_SESSION_CHECK_SECONDS`에 한 번 같은 조회를 한다.
- **요청 본문 크기.** 본문 크기 제한은 인증 전에 강제되지 않는다(FastAPI 기존 동작 — 본문을 의존성보다 먼저 읽는다). 본문을 받는 경로는 미인증 요청도 본문 전체를 읽은 뒤에 거절한다. 인증 전 크기 제약은 배포 계층(WAF 등)에 두는 전제이며, 특정 환경의 적용 여부는 이 문서가 보증하지 않는다.
- **메트릭 · WebSocket 플랫폼 전용(#492 · #533).** 고객사(TENANT) 계정은 자기 테넌트 범위의 자산 · 인시던트 API와 조치 실행 · 인시던트 종료 API를 쓰고, 메트릭과 실시간 연결은 쓰지 못한다. 끄는 설정이 없다. 테넌트 범위는 자산 · 인시던트 · 조치 HTTP API에만 걸린다 — 메트릭 · WebSocket은 PLATFORM 전용으로 막고, 백그라운드 처리에는 범위가 적용되지 않는다.
- **고객 ID 입력(#492).** 사용자 관리 화면에 고객 선택 목록이 없어(백엔드의 테넌트 목록 API를 이 화면은 쓰지 않는다) PLATFORM 운영자는 초대할 고객의 UUID를 직접 확인해 입력해야 한다. 틀린 값은 404 `TENANT_NOT_FOUND`다.
- **마이그레이션 적용(#492).** head는 병합 revision `c492d6e8f4a2`이며, dev 가지 끝과 #492 가지 끝 어느 쪽의 DB도 `alembic upgrade head`로 앞으로만 올린다. 등록 링크 스키마(`platform_enrollments`)가 있는 DB는 revision과 관계없이 이 경로의 대상이 아니다(§마이그레이션 그래프 · 병합 `c492d6e8f4a2`). `f492c4a8d2e1`은 이전 앱 · CLI의 사용자 생성과 함께 쓸 수 없으므로 마이그레이션 · 새 앱 · 새 CLI를 함께 올린다. downgrade는 PLATFORM · UNASSIGNED 구분을 잃는다(§마이그레이션 `f492c4a8d2e1`). 소속 없는 기존 계정은 UNASSIGNED로 남아 로그인할 수 없으므로, 필요하면 CLI로 지정한다.
- **초대 · 재발송의 결과 불명(#492).** SDK 호출 뒤 재확인이 거절하면 메일은 나갔을 수 있다. 오류 코드만으로 미발송을 판단하지 말고 목록 · Cognito 상태를 확인한 뒤 다시 수행한다(§부분 실패).
- **SSOT.** 팀 공통 현황 · API 계약의 기준은 `docs/PROJECT_STATUS.md`다. 이 모듈의 세부 계약은 이 문서와 `packages/schemas/`에 있으며, 여기 적힌 경로 · 오류 코드 · 응답 칸이 SSOT에 반영됐는지는 SSOT에서 확인한다.

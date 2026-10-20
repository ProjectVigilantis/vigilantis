# auth — 로그인 · 서버 세션 (Post-MVP T-1, #492)

Cognito Admin 인증 API로 로그인을 진행하고, **이번 로그인에서 TOTP 단계 성공 응답을 서버가 직접 받은 뒤에만** 앱 서버 세션을 발급한다. 공개 계약은 `packages/schemas/api/auth.py`, 오류 코드는 `packages/schemas/api/errors.py`다.

이 문서는 PR 1(로그인 기반)의 상태다. API · SSR · WebSocket 접근 보호와 역할 검사는 PR 2, 사용자 관리 · 세션 회수 · 복구 · 권한 이력은 PR 3, HTTPS · 환경별 설정 · 배포 권한은 PR 4 범위다.

근거 표시: **[표준]** 외부 표준 · 공식 문서, **[팀 결정]** 이슈 #492 확정 정책, **[실측]** 2026-10-19 실제 User Pool 확인, **[판단]** 이 구현의 선택.

## 구조

| 파일 | 책임 |
| --- | --- |
| `cognito.py` | boto3 `cognito-idp` 어댑터. `SECRET_HASH` 계산, Cognito 오류를 `AUTH_*` 코드와 1:1인 도메인 예외로 분류 |
| `login_flow.py` | 로그인 상태 머신. 세션 발급 조건 판정, 코드 시도 상한(흐름별 · 사용자별), refresh token 회수 |
| `flow_store.py` | 진행 중인 로그인과 사용자별 코드 실패 횟수의 프로세스 메모리 보관(TTL · 슬라이딩 윈도). DB · 로그에 남기지 않는다 |
| `sessions.py` | 서버 세션 발급 · 조회 · 활동 갱신 · 회수. 설정을 읽지 않고 commit하지 않는다 |
| `service.py` | 트랜잭션 소유자. 로그인 완료(사용자 조회 · 세션 발급), 현재 사용자, 활동, 로그아웃 |
| `http.py` | 쿠키 · Origin 검사 · 보안 헤더 · `AuthError` → 오류 봉투 매핑 |
| `provider.py` | Cognito 클라이언트 · `LoginFlow` 조립(엔드포인트 고정) |
| `bootstrap_admin.py` | 최초 ADMIN 생성 운영 CLI |
| `routers/auth.py` | `/api/v1/auth` 라우터(얇게) |
| `db/models.py` · `db/repositories/auth.py` | `app_users` · `auth_sessions` |

## 로그인 흐름

```
POST /login                  비밀번호 → NEW_PASSWORD_REQUIRED | TOTP_SETUP_REQUIRED | TOTP_REQUIRED
POST /login/new-password     초대 사용자 최초 비밀번호 변경 → TOTP_SETUP_REQUIRED
POST /login/totp-setup       등록 비밀키 · otpauth URI (no-store)
POST /login/totp-setup/verify  VerifySoftwareToken SUCCESS + MFA_SETUP 응답 → (EMAIL_VERIFICATION_REQUIRED | COMPLETED)
POST /login/totp             SOFTWARE_TOKEN_MFA 응답 → (EMAIL_VERIFICATION_REQUIRED | COMPLETED)
POST /login/email-verification  이메일 코드 → COMPLETED
POST /logout · POST /activity · GET /me
```

**세션을 발급하는 경우는 둘뿐이다** [팀 결정]:

1. 이번 로그인에서 `AdminRespondToAuthChallenge(SOFTWARE_TOKEN_MFA)`가 `AuthenticationResult`를 돌려줌
2. 이번 로그인에서 `VerifySoftwareToken`이 `Status=SUCCESS`를 돌려주고 이어서 `AdminRespondToAuthChallenge(MFA_SETUP)`가 `AuthenticationResult`를 돌려줌

비밀번호 단계나 최초 비밀번호 변경 직후에 토큰이 나오거나, 그 밖의 challenge(`SELECT_MFA_TYPE` · `EMAIL_OTP` · `SMS_MFA` 등)가 나오면 세션 없이 거절한다(`AUTH_FAILED`, 받은 refresh token 즉시 회수). `acr` · `auth_time` · 사용자 MFA 설정 상태는 근거로 쓰지 않는다 — 관리형 로그인 PoC에서 `acr`가 없었고 기존 로그인 상태 재사용에도 새 `auth_time`이 발급됐으며 [실측], API 방식에서도 TOTP 등록 뒤 `PreferredMfaSetting` · `UserMFASettingList`가 비어 있었다 [실측].

최초 TOTP 등록(`MFA_SETUP`)으로 토큰을 받으면 먼저 `SetUserMFAPreference`로 TOTP를 선호 MFA로 지정한다. 이어서 `GetUser`로 `sub` · `email_verified`를 확인하고, 아래 중 하나라도 해당하면 이메일 검증 코드를 보내 확인한 뒤에야 완료한다.

- 최초 TOTP 등록 경로다.
- Cognito의 `email_verified`가 `true`가 아니다.
- 이 앱이 그 사용자의 이메일 코드 확인을 기록한 적이 없다(`app_users.email_verified_at`이 비어 있음).

마지막 조건이 필요한 이유: Cognito는 `VerifySoftwareToken` 성공 시점에 인증 앱을 계정에 묶는데, 이메일 코드를 보내는 API는 그 뒤에 받는 access token이 있어야 쓸 수 있다. 등록 전에 이메일을 확인시킬 수 없으므로, 비밀번호만 가진 사람이 자기 인증 앱을 등록한 뒤 이메일 단계를 버리고 다시 로그인하는 경로를 앱 DB의 확인 기록으로 막는다 [판단]. `email_verified_at`은 이 앱의 로그인 흐름에서 `VerifyUserAttribute`가 성공했을 때만 기록한다.

정상 완료 · 실패 · 요청 중 만료가 확인된 흐름은 그 자리에서 refresh token을 `RevokeToken`으로 회수한다. 사용자가 중간에 떠난 흐름은 다음 인증 요청의 정리(sweep) 때 회수되며, 그 전에 프로세스가 재시작되면 회수되지 않는다(토큰은 프로세스 메모리에만 있었다). 토큰은 응답 · DB · 로그 어디에도 두지 않는다.

완료된 신원 `(issuer, sub)`로 `app_users`를 찾는다. 없으면 403 `ACCOUNT_NOT_PROVISIONED`, 비활성이면 403 `ACCOUNT_DISABLED`다. 이메일로 계정을 찾거나 연결하지 않는다 [팀 결정].

## 정책과 근거

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| MFA | 전 역할 TOTP 필수. User Pool `MfaConfiguration=ON`, TOTP만 사용 | [팀 결정] |
| 인증 흐름 | `ADMIN_USER_PASSWORD_AUTH`(`AdminInitiateAuth` · `AdminRespondToAuthChallenge`). AWS가 AWS 자격증명을 가진 서버 측 기밀 클라이언트의 경로로 안내하며, 로그인 호출을 client secret에 더해 IAM 서명으로 보호한다. Admin 흐름은 기억된 기기를 지원하지 않아 기기 기억으로 MFA를 건너뛸 수 없다 | [표준] [AWS: Server-side and client-side authentication](https://docs.aws.amazon.com/cognito/latest/developerguide/authentication-flows-public-server-side.html) |
| SRP 미채택 | 브라우저가 이미 HTTPS로 서버에 비밀번호를 보내므로 SRP는 서버–Cognito 구간만 더 보호한다. 그 대가로 SDK 밖 암호 코드가 로그인 경로에 들어간다 | [판단] |
| 세션 최대 수명 | 발급 시점 + 8시간(`AUTH_SESSION_MAX_SECONDS=28800`). 활동으로 늘지 않는다 | [팀 결정] [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html#session-expiration) |
| 유휴 만료 | 마지막 사용자 활동 + 30분(`AUTH_SESSION_IDLE_SECONDS=1800`). 최대 수명을 넘지 않는다. 두 제한 중 먼저 도달한 쪽에서 서버가 만료한다 | [팀 결정] |
| 유휴 연장 | `POST /activity`만 연장한다(FE는 실제 사용자 입력이 있을 때만, 1분에 1번까지 호출). `GET /me` · 자동 조회 · WebSocket은 연장하지 않는다 | [팀 결정] |
| 세션 저장 | 서버 세션. 토큰 `secrets.token_urlsafe(32)`, DB에는 SHA-256 hex만. JWT를 브라우저에 두지 않고 Cognito refresh token을 저장하지 않으며 Remember Me가 없다 | [팀 결정] [판단: 고엔트로피 무작위 토큰이라 솔트 없는 SHA-256] |
| 세션 쿠키 | `__Host-vg_session`: `Secure; HttpOnly; SameSite=Lax; Path=/`, `Domain` 없음(host-only), `Max-Age=최대 수명` | [팀 결정] [표준: `__Host-` 접두 요건] |
| 로그인 진행 쿠키 | `__Host-vg_login`, 속성 동일, `Max-Age=300`. 완료 · `AUTH_FLOW_EXPIRED` 때 삭제 | [판단] |
| 세션 고정 방지 | 로그인 완료 때 요청에 실린 이전 세션은 `SUPERSEDED`로 회수 | [판단] |
| CSRF | 모든 상태 변경(POST) 요청은 `Origin`이 `AUTH_ALLOWED_ORIGINS`와 정확히 같아야 한다. 없거나 다르면 403 `AUTH_ORIGIN_REJECTED` — Cognito · 흐름 저장소 · DB에 닿기 전에 거절. 쿠키는 `SameSite=Lax` | [표준] [OWASP CSRF: Verifying Origin](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) |
| CORS | 자격증명 허용(`allow_credentials=True`). 그래서 `CORS_ALLOW_ORIGINS` · `AUTH_ALLOWED_ORIGINS`에 `*`를 받지 않는다(Starlette는 `*` + 자격증명에서 요청 Origin을 되돌린다) | [판단] |
| 보안 헤더 | `/api/v1/auth/*`의 모든 응답에 `Cache-Control: no-store`, `Referrer-Policy: strict-origin`, `X-Content-Type-Options: nosniff` | [판단, PoC 차용] |
| 코드 시도 상한(흐름별) | 한 로그인에서 TOTP · 이메일 코드 제출은 3번까지(`AUTH_TOTP_MAX_ATTEMPTS=3`). 3번째 실패에서 로그인을 폐기한다. Cognito는 TOTP를 틀려도 challenge 세션을 무효화하지 않았다 | [실측] [판단] |
| 코드 시도 상한(사용자별) | 로그인을 다시 시작해 흐름별 상한을 초기화하지 못하도록, Cognito 사용자 단위로 15분 동안 코드 실패 10회(`AUTH_CODE_FAILURE_LIMIT` · `AUTH_CODE_FAILURE_WINDOW_SECONDS`)에 이르면 429 `AUTH_RATE_LIMITED`. 코드 단계는 Cognito를 부르지 않고 거절하고, 비밀번호 단계는 사용자를 식별한 뒤 흐름을 만들지 않고 거절한다. 코드 실패는 비밀번호를 통과해야만 쌓이므로 비밀번호를 모르는 사람이 계정을 잠글 수 없다. 이 때문에 한도에 이른 사용자의 올바른 비밀번호에는 429, 틀린 비밀번호에는 401이 간다 — 비밀번호를 이미 아는 사람에게만 보이는 차이라 열거 수단이 아니며, 401로 바꾸면 정상 사용자가 잠금 사실을 알 수 없다. IP 단위 제한(비밀번호 대입 방어)은 PR 4(WAF · 클라이언트 IP 신뢰 설정) | [판단] |
| 이메일 검증 | 첫 세션 전 필수. 초대 사용자는 비밀번호 변경 · TOTP 등록 뒤에도 `email_verified`가 없었고, 계정 복구는 검증된 이메일만 쓴다. 최초 TOTP 등록 경로와 앱 DB에 확인 기록이 없는 사용자는 Cognito 값과 관계없이 이메일 코드를 요구한다 | [실측] [팀 결정] [판단] |
| 비밀번호 정책 | User Pool 정책(12자 이상, 대소문자 · 숫자 · 기호)을 유지한다. OWASP ASVS 2.1.7(유출 비밀번호 확인) · 2.1.9(문자 종류 규칙 금지)는 적용하지 않는다. 유출 비밀번호 확인은 Cognito Essentials 요금제에 없고 Plus 요금제에 있으며, 그 확인 없이 문자 종류 규칙을 없애지 않는다. TOTP 필수와 코드 시도 제한으로 비밀번호만으로 계정이 열리는 위험은 제한된다 | [팀 결정(#492)] [표준: 미적용 항목 명시] |
| MFA 선호 | 최초 등록 직후 TOTP를 선호 MFA로 지정한다. User Pool이 실수로 `OPTIONAL`이 되면 비밀번호 단계에서 토큰이 나오는데, 이 경우 앱은 세션 없이 거절한다(fail closed) | [판단] |
| 401 · 403 | 미인증(세션 없음 · 만료 · 회수 · 비활성 사용자의 세션)은 401 `AUTH_REQUIRED`, 인증 후 허용되지 않음은 403 | [팀 결정] |
| 비밀값 | 비밀번호 · 코드는 `SecretStr`, 검증 오류에 입력값을 싣지 않는다(`hide_input_in_errors`). 로그에는 AWS 오류 코드와 호출 이름만 | [팀 결정] |
| 최초 관리자 | 운영자가 CLI로 명시 생성. 첫 방문자를 승격하지 않는다. 단일 bootstrap 소속으로 시작(테넌트 연결은 T-4) | [팀 결정] |

## 설정

`apps/core-api/config.py`의 `AuthSettings`. 환경변수와 루트 `.env`를 읽는다. 키 목록과 설명은 루트 `.env.example`에 있다.

| 키 | 기본값 | 비고 |
| --- | --- | --- |
| `COGNITO_REGION` · `COGNITO_USER_POOL_ID` · `COGNITO_CLIENT_ID` · `COGNITO_CLIENT_SECRET` | 없음 | 하나라도 비면 앱은 뜨고 `/api/v1/auth/*`만 503 `AUTH_PROVIDER_UNAVAILABLE`. 기동 로그에 누락 키 이름만 남긴다 |
| `COGNITO_AWS_PROFILE` | 없음 | 로컬 개발에서 이름 있는 AWS 프로파일을 쓸 때만. 배포는 비운다(태스크 역할) |
| `AUTH_SESSION_MAX_SECONDS` | 28800 | |
| `AUTH_SESSION_IDLE_SECONDS` | 1800 | |
| `AUTH_TOTP_MAX_ATTEMPTS` | 3 | 한 로그인 흐름의 코드 제출 횟수 |
| `AUTH_CODE_FAILURE_LIMIT` | 10 | 사용자별 코드 실패 한도 |
| `AUTH_CODE_FAILURE_WINDOW_SECONDS` | 900 | 사용자별 한도의 슬라이딩 윈도(초) |
| `AUTH_ALLOWED_ORIGINS` | `CORS_ALLOW_ORIGINS`와 같음 | 콤마 구분, 정확히 일치, `*` 금지 |

Cognito 클라이언트는 `https://cognito-idp.{region}.amazonaws.com`으로 엔드포인트를 고정한다. `AWS_ENDPOINT_URL`(LocalStack)이 설정돼 있어도 인증 트래픽은 실제 Cognito로 간다.

### User Pool · 앱 클라이언트 전제

- Essentials, `MfaConfiguration=ON`, Software token MFA만 활성, 관리자만 사용자 생성, 사용자 이름은 email, 복구는 `verified_email`
- 앱 클라이언트: client secret 있음, `ExplicitAuthFlows`에 `ALLOW_ADMIN_USER_PASSWORD_AUTH`, 토큰 회수 켜짐, `AuthSessionValidity=3`(분), 존재 노출 방지 켜짐
- 기기 추적(`DeviceConfiguration`) 꺼짐
- 실행 주체에 `cognito-idp:AdminInitiateAuth` · `cognito-idp:AdminRespondToAuthChallenge`(해당 User Pool 한정). 그 밖의 호출(`AssociateSoftwareToken` · `VerifySoftwareToken` · `GetUser` · `GetUserAttributeVerificationCode` · `VerifyUserAttribute` · `RevokeToken`)은 세션 · 토큰으로 승인돼 IAM 권한이 필요 없다. 배포 역할 반영은 PR 4(플랫폼 배포 스크립트)

## 최초 관리자 생성

```bash
cd apps/core-api
uv run python -m auth.bootstrap_admin --email <관리자 이메일>                         # 미리보기(변경 없음)
uv run python -m auth.bootstrap_admin --email <관리자 이메일> --yes                   # Cognito 초대 + ADMIN 등록
uv run python -m auth.bootstrap_admin --existing-cognito-user --email <이메일> --yes  # 이미 있는 Cognito 사용자 연결
```

- `app_users`에 행이 하나라도 있으면 거절한다. 두 번째 사용자부터는 PR 3(사용자 관리) 몫이다.
- 기존 Cognito 사용자 연결은 운영자가 플래그로 명시할 때만 한다(`AdminGetUser`의 `sub`로 연결).
- 임시 비밀번호는 Cognito가 초대 메일로만 보낸다. CLI는 비밀값을 출력하지 않는다.
- Cognito 사용자 생성 뒤 DB 등록이 실패하면 Cognito 사용자는 남는다. 이때는 `--existing-cognito-user`로 다시 연결한다.

## 테스트

| 구분 | 파일 | 보장 범위 |
| --- | --- | --- |
| 계약 | `packages/schemas/tests/test_api_auth.py` · `test_api_errors.py` | 단계 · 역할 · 오류 코드 집합, 비밀값 비노출, 입력 경계 |
| 대역(botocore Stubber) | `apps/core-api/tests/test_auth_cognito.py` · `test_auth_login_flow.py` | 요청 파라미터(`USER_ID_FOR_SRP` · `SECRET_HASH`), 세션 발급 조건, 예상 밖 토큰 · challenge 거절, 시도 상한, 흐름 만료, refresh token 회수, 로그 · 예외에 비밀값 없음 |
| 실제 PostgreSQL | `apps/core-api/tests/test_auth_sessions.py` · `test_auth_api.py` · `test_auth_bootstrap_admin.py` · `db/tests/test_auth_*.py` | 해시만 저장, **유휴 30분 · 최대 8시간 만료, `GET /me` 반복이 유휴를 늘리지 않음**, 활동이 최대 수명을 넘지 않음, 회수, `(issuer, sub)` 유일, 쿠키 속성, Origin 거절, 보안 헤더, 미구성 시 503, CLI |
| 실제 Cognito | 자동 테스트 없음 | 아래 확인 결과. CI는 실제 Cognito를 호출하지 않는다 |

**대역 테스트 통과는 실제 Cognito의 MFA 동작 보장이 아니다.** Cognito 쪽 동작의 근거는 아래 실측이다.

### 실제 Cognito 확인 (2026-10-19, 개인 개발 계정 · 테스트 사용자)

- 비밀번호 단계는 토큰 없이 `SOFTWARE_TOKEN_MFA`만 반환했다. 올바른 TOTP 뒤에만 토큰이 나왔다.
- 틀린 TOTP는 `CodeMismatchException`. 같은 challenge 세션은 그 뒤에도 올바른 코드를 받았다(→ 서버 시도 상한).
- 성공에 쓴 challenge 세션 재사용은 `NotAuthorizedException`(1회만 사용), 3분이 지난 challenge는 만료로 거절됐다.
- 기기 추적은 꺼져 있고 `NewDeviceMetadata`가 오지 않았다.
- 초대 사용자는 `NEW_PASSWORD_REQUIRED` → `MFA_SETUP` → (`VerifySoftwareToken` SUCCESS) → 토큰. 다음 로그인부터 `SOFTWARE_TOKEN_MFA`.
- 앱을 통한 확인(로컬 core-api + 웹, Chrome): 등록 사용자 로그인 → 틀린 TOTP 401 → 올바른 TOTP → 이메일 인증 코드 메일 수신 · 입력 → 세션 발급(`SOFTWARE_TOKEN_MFA`, 최대 수명 28800초, DB에 64자 해시) → `/me` → 로그아웃(`LOGOUT` 회수). Cognito `email_verified`가 비어 있다가 `true`가 됐다. 서버 로그에 토큰 · 쿠키 · 비밀번호 · client secret이 없었다. 최초 관리자 CLI의 기존 사용자 연결(`--existing-cognito-user`)도 실제 Cognito로 확인했다.
- 앱 DB에 이메일 확인 기록이 없는 사용자는 Cognito `email_verified=true`여도 코드 메일이 다시 발송되고(이미 검증된 속성에 대한 `GetUserAttributeVerificationCode` 동작 확인), 코드 입력 뒤 완료됐다. 기록이 생긴 뒤의 로그인은 이메일 단계 없이 완료됐고, 로그인 상태에서 다시 로그인하면 이전 세션이 `SUPERSEDED`로 회수됐다.
- 미검증: 최초 TOTP 등록 중 틀린 코드의 동작, 초대 메일 임시 비밀번호로 시작하는 전체 경로(기본 초대 메일에서 복사한 값이 반복 거절됨 — 끝 문장부호 혼입 추정, 미확인), 최초 등록 직후 `SetUserMFAPreference`(앱 경로로 새 사용자 등록을 하지 않았다), HTTPS 배포 환경의 쿠키 동작.

## 운영 주의점

- **단일 프로세스 전제.** 진행 중인 로그인과 사용자별 코드 실패 횟수는 프로세스 메모리에 있다(uvicorn worker 1개 기동 전제). 재시작하면 진행 중인 로그인은 처음부터 다시 하고 실패 횟수는 0이 된다. 복제본을 늘리면 다른 프로세스의 로그인 흐름을 찾지 못해 로그인이 실패한다(우회가 아니라 거절 쪽). 세션은 DB에 있어 재시작과 무관하다.
- **세션 만료는 관제를 멈추지 않는다.** 수집 · 탐지 · 자동 대응은 사용자 세션과 독립적으로 돈다.
- **SDK 로그는 WARNING으로 고정된다.** Admin 인증 요청 본문에 비밀번호가, 응답에 토큰이 실리므로 `setup_logging`이 `LOG_LEVEL`과 관계없이 `botocore` · `boto3` · `urllib3`를 WARNING으로 둔다. 이 고정을 풀지 않는다.
- **Cognito 자격증명.** `COGNITO_AWS_PROFILE`이 비면 `AWS_PROFILE`(운영 이미지의 관제 대상 접근 역할 프로필)을 무시하고 기본 체인(태스크 역할)을 쓴다. Cognito 권한을 관제 대상 접근 역할에 주지 않는다.
- **로컬 개발.** 웹(`http://localhost:3000`) → API(`http://localhost:8000`)는 같은 사이트라 `SameSite=Lax` 쿠키가 실린다. FE 요청은 `credentials: "include"`다. Chrome · Firefox는 `http://localhost`를 보안 컨텍스트로 보아 `Secure` · `__Host-` 쿠키를 받는다(Safari는 확인하지 않았다). `.env`에 LocalStack 더미 AWS 키가 있으면 `COGNITO_AWS_PROFILE`로 실제 자격증명을 지정한다.
- **배포 도메인.** 세션 쿠키는 host-only라 기본 도메인(`https://vigilantis.dev`)과 `/api/*` 경로 라우팅을 전제한다. CloudFront 기본 도메인으로 접속하면 API 호스트와 달라 쿠키가 실리지 않는다(PR 4 환경 정리).
- **초대 메일.** 기본 초대 메일 문구는 임시 비밀번호 뒤에 문장부호가 붙는다. 복사 때 함께 들어가지 않게 안내하거나 문구를 바꾼다(PR 4).
- **비활성 사용자.** `DISABLED` 사용자의 세션은 401로 막힌다. 세션 행 회수와 사용자 관리 화면은 PR 3이다.
- **SSOT.** `docs/PROJECT_STATUS.md` §API 계약의 오류 코드 목록은 PM 갱신 때 반영한다.

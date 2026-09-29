# 개발 스택 한 번에 띄우기

DB · LocalStack · API · FE를 명령 하나로 띄우고 내린다. 루트 README §로컬 실행의 수동 절차(`docker compose up` → 시드 → `npm run dev`)를 그대로 묶은 것이며, 새로 하는 일은 없다.

- `up.ps1`: Docker 확인 → db · localstack 기동 → LocalStack 시드 → api 기동(migrate 선행) → FE dev 서버를 새 창으로 띄운다.
- `down.ps1`: FE 창을 닫고 컨테이너를 멈춘다(`stop`). **DB 볼륨은 지우지 않는다.**
- `compose.no-ai.yml`: `up.ps1 -NoAi`가 얹는 override. AI 과금 경로를 끈다(아래 §과금).

Windows 전용(PowerShell 5.1 · 7 모두 동작)이다.

## 준비

1. Docker Desktop 설치. 꺼져 있으면 `up.ps1`이 켜고 기다린다.
2. 저장소 루트에 `.env`(없으면 `.env.example`을 복사해 값을 채운다).
3. 저장소 루트에서 `uv sync --all-packages` — 시드 스크립트가 쓴다(신규 클론 때 한 번).
4. `apps/web/node_modules`가 없으면 `up.ps1`이 `npm ci`를 한 번 돌린다.

## 명령

저장소 루트(또는 워크트리 루트)에서 실행한다.

| 명령 | 하는 일 |
| --- | --- |
| `scripts\devstack\up.ps1` | 전부 띄운다. 이미 떠 있는 것은 그대로 쓴다 — 여러 번 실행해도 된다 |
| `scripts\devstack\up.ps1 -NoAi` | api의 스캔 · 분석 · 실행 타이머를 끄고 띄운다. **OpenAI 과금 없음** |
| `scripts\devstack\up.ps1 -NoWeb` | FE 없이 BE(db · localstack · api)만 |
| `scripts\devstack\down.ps1` | FE 창을 닫고 api · localstack · db를 멈춘다 |
| `scripts\devstack\down.ps1 -KeepInfra` | FE · api만 내리고 db · localstack은 남긴다(pytest를 계속 돌릴 때) |

**`up.ps1` 옵션**

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `-NoAi` | 꺼짐 | `compose.no-ai.yml`을 얹어 `SCAN_ENABLED`(수집→판정 스캔)와 `DISPATCH_ENABLED`(AI 분석 · 조치 실행 디스패치)를 `false`로 띄운다 |
| `-NoWeb` | 꺼짐 | FE를 띄우지 않는다 |
| `-NoSeed` | 꺼짐 | LocalStack 시드를 건너뛴다. 시드는 멱등이라 보통은 끌 이유가 없다 |
| `-WebPort` | `3000` | FE 포트. 바꾸면 `.env`의 `CORS_ALLOW_ORIGINS`에도 넣어야 한다(아래 §문제 해결) |
| `-TimeoutSeconds` | `180` | Docker 기동 · api `/health` · FE 포트 대기의 제한 시간 |

`down.ps1`의 `-WebPort`는 `up.ps1` 밖에서 띄운 FE를 찾을 때만 쓴다.

**띄운 뒤 주소**

- FE: `http://localhost:3000`
- API 문서: `http://localhost:8000/docs`
- DB 웹 UI(선택): `docker compose --profile tools up -d adminer` → `http://localhost:8080`

## 과금 — OpenAI 키는 api 컨테이너가 쓴다

- **FE는 OpenAI 키를 쓰지 않는다.** 과금은 api 컨테이너에서만 일어난다.
- api는 compose의 `env_file: .env`로 **`up.ps1`을 실행한 저장소(워크트리) 루트의 `.env`** 를 읽고, 그 안의 `OPENAI_API_KEY`로 호출한다.
- 기본 실행(`-NoAi` 없음)은 `docker compose up`과 같다. 스캔이 사건을 만들면 AI 분석이 자동으로 돌아 **과금된다.** 키가 채워져 있으면 `up.ps1`이 경고를 띄운다.
- **셸에서 `$env:SCAN_ENABLED='false'`를 줘도 컨테이너에 반영되지 않는다** — `env_file` 값은 셸 변수로 덮이지 않는다. 그래서 `-NoAi`는 override 파일(`environment:`가 `env_file`보다 우선)로 끈다.
- `-NoAi`는 조치 실행 디스패치도 함께 멈춘다(AI 분석과 같은 `DISPATCH_ENABLED`를 공유한다). **FE에서 [실행]을 눌러도 실행이 진행되지 않는다** — 실행 흐름까지 보려면 `-NoAi` 없이 띄우거나 단계 진행 CLI([`scripts/stepper/README.md`](../stepper/README.md))를 쓴다.
- `-NoAi` 유무를 바꿔 다시 실행하면 compose가 설정 변경을 감지해 api만 다시 만든다.

## 워크트리 · 전용 스택

- 스크립트는 **자기가 놓인 저장소**를 기준으로 돈다. 워크트리에서 실행하면 그 워크트리의 코드 · `.env` · `apps/web`을 쓴다.
- 포트와 스택 이름은 그 루트 `.env`의 `APP_PORT` · `LOCALSTACK_PORT` · `COMPOSE_PROJECT_NAME`을 따른다. 시드의 `AWS_ENDPOINT_URL`과 FE의 `NEXT_PUBLIC_API_BASE_URL`(`apps/web/.env.local`보다 우선)도 그 값에 맞춰 스크립트가 넣는다.
- `.env`에 전용 포트가 없으면 기본 스택(`vigilantis`, 5432 · 4566 · 8000)을 쓴다. **`api`는 `./apps/core-api`를 마운트하므로 마지막으로 `up`한 폴더의 코드가 돈다.**

## 문제 해결

| 증상 | 원인 · 조치 |
| --- | --- |
| `db · localstack 기동 실패` | 같은 포트를 다른 스택이 쓰고 있다. `docker ps`로 확인 — 시연 스택(`vigilantis-demo`) · 테스트 서버(`vigilantis-test`)와는 동시에 띄울 수 없다 |
| `시드 실패` | `uv sync --all-packages`를 안 했거나 LocalStack이 준비되지 않았다 |
| `api 기동 실패` · `/health 응답 없음` | 스크립트가 migrate · api 로그 끝 40줄을 출력한다. 첫 실행은 이미지 빌드로 수 분 걸린다 |
| FE 화면이 전부 조회 오류 | api가 안 떠 있거나 CORS다. `-WebPort`를 바꿨다면 `.env`에 `CORS_ALLOW_ORIGINS=http://localhost:<포트>`를 넣고 `up.ps1`을 다시 실행한다 |
| `포트 3000 이미 사용 중 — 건너뜀` | FE가 이미 떠 있다. 다른 프로그램이 3000을 쓰고 있다면 `-WebPort`로 바꾼다 |
| LocalStack 자산이 비어 있다 | LocalStack을 재시작하면 비워진다. `up.ps1`을 다시 실행하면 시드한다 |

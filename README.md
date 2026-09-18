# Vigilantis 🛡️

> **24/7 클라우드 자산·보안 상시 관제 및 4단계 AI 가드레일 기반 원클릭 자율 조치·자동 롤백(Auto-Rollback) FinSecOps 파이프라인**

---

## 📌 Project Overview

* **팀명**: 딸깍 인프라
* **개발 배경**: Multi-Account/Region 환경 확산에 따른 인프라 파편화와 초단위 보안 위협에 대응하고, AI 자동화 도입 시 발생하는 환각(Hallucination) 및 과도한 권한 실행(Excessive Agency) 위험을 해결하기 위해 구축되었습니다.
* **MVP 범위**: **AWS 단일 계정 / 1–2개 리전 / EC2·Security Group 중심**(런북 조치 대상: NACL·EBS·ASG·Launch Template·ALB Target Group 포함). CloudWatch(CPU/Network) 기반 Idle EC2 판별, OpenIP·SSH 브루트포스 **모의 위협** 대응, `gpt-5.6-luna` 4단계 가드레일 + **런북 10종(본편 7 + 롤백 3) Action Whitelist**, 양방향 회복 엔진, Next.js 대시보드까지가 중간 발표 시연 대상이다.
* **현황·결정 기준(SSOT)**: [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) — 확정 범위·결정 로그·역할·미해결 이슈의 **단일 기준**. 본 README는 포트폴리오·소개용 문서이며, 충돌 시 항상 `PROJECT_STATUS.md`가 이긴다.
* **Post-MVP (로드맵)**: RDS·S3 확장, Multi-Account/Region, OpenTelemetry 전 구간 트레이싱, Step Functions/ECS Fargate/Lambda, Terraform Drift 감지·GitOps PR, 모바일 푸시(FCM), GCP·Azure. (아래 Tech Stack 참고)

### 일정 고정점

| 날짜 | 무엇 |
| :--- | :--- |
| 2026-09-04(금) ✅ | 기획 발표 — 기획·문서 단계까지, 구현 시연 없음 |
| 2026-09-30(수) | **E2E 시연물 완성 마감** (내부 마감 — 다음 날 시연할 경로가 이날 전부 서 있어야 한다) |
| 2026-10-01(목)–10/02(금) | **Connect Day 2일** — **1일차 세션이 중간 발표 · MVP 시연**(LocalStack 기반). 제출물로 프로젝트 기획서 1부 |
| 2026-10-15(목) | **MVP 마감 판정** — 실 AWS 스모크 + P2 3종 첫 검증까지 포함한 내부 완료 판정일 |
| 2026-12-11(금) | **최종 발표** — Post-MVP 범위 배포·시연 |

> 날짜의 원천은 SSOT [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) §현재 위치다. 주차 구간·주차 종료 판정 기준은 그 문서에만 둔다.

---

## 👥 Team & Roles

| 이름 | 역할 | 담당 DOMAIN | 담당 영역 (주요 경로) |
| :--- | :--- | :--- | :--- |
| **김세혁 (팀장)** | PM · **Infra & DevSecOps · Frontend** | `BE` · `INFRA` · `FE` | Boto3 EC2/SG 제어·스펙 JSON 백업/자동 원복 엔진(`services/aws`), (모의) 위협 차단(`security`), Docker·CI/배포, Git 브랜치·코드리뷰, **Next.js 대시보드 전부**(`apps/web`) |
| **안성일** | **AI/Guardrail · Architect** | `AI` · FastAPI 아키텍처 · DB | 전체 아키텍처·DB 스키마(`db`), FastAPI 메인·라우터(`main.py`,`routers`), `gpt-5.6-luna` + 4단계 가드레일 + LangGraph 2그래프(`ai`) |
| **김승철** | **Data & Rule Engine · QA/Scenario** | `DATA` · QA 시나리오 | CloudWatch 수집·정형화(`services/collector`), Idle EC2·미사용 SG 판별 및 Skip 사유 코드(`services/rule_engine`), **Golden Dataset·E2E 시연 시나리오·pytest**(`datasets`,`tests`) |
| **박지현** | **Technical Writer** | `DOCS` | 프로젝트 기획서 전담, 저장소 문서화(`docs`) |

> 역할·담당 경계의 유일한 기준은 SSOT [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) §팀 & 역할이다. 위 표는 그 표에서 옮긴 소개용 서술이다.
> **FE↔BE 접점은 김세혁이 양쪽을 들고 있으므로 그 접점의 PR은 안성일이 리뷰한다** — "최소 1명 승인" 요건은 면제되지 않는다.

---

## 🛠 Tech Stack

**MVP (실사용)**

* **Frontend**: Next.js 16 (App Router), React 19, TypeScript, Shadcn UI(radix-ui), Tailwind CSS v4, WebSocket, `node --test` 유닛 테스트 (차트 라이브러리는 미선정)
* **Backend**: FastAPI (Python 3.11+), Boto3, PostgreSQL 16, SQLAlchemy 2 · Alembic, APScheduler, pydantic-settings, uvicorn
* **AI & Safety**: OpenAI `gpt-5.6-luna`(`reasoning_effort=low`), LangGraph (FinOps·SecOps 두 그래프), Pydantic v2 (Structured Output), Golden Dataset 기반 pytest 회귀 + 요약 품질 평가 하네스(`ai/evaluation`)
* **Infra/Dev**: uv workspace 모노레포, Docker Compose (FastAPI + PostgreSQL + LocalStack + Alembic migrate), GitHub Actions CI **3잡** — `test` · `web` · `ai-signature`

**Post-MVP (로드맵)**: OpenTelemetry(W3C Trace Context) · AWS Step Functions/ECS Fargate/Lambda/EventBridge/GuardDuty · Terraform(Drift·GitOps) · Redis(ElastiCache) · LangGraph **Multi-Agent 확장**(MVP는 도메인별 단일 그래프) · OIDC SSO·MFA·RBAC · 모바일 푸시(FCM) · GCP/Azure

---

## ✨ Key Features (MVP)

1. **자산 관제 & Idle 판별**: EC2·SG·NACL·EBS·ASG·Launch Template·ALB Target Group **7종** 인벤토리와 CloudWatch(CPU/Network)를 APScheduler로 주기 수집하고 자산 간 **연결관계 6종**(`SECURED_BY`·`ATTACHED_TO`·`MEMBER_OF`·`USES`·`REGISTERED_IN`·`PROTECTED_BY`)을 산출한다. Rule Engine이 Idle EC2·미사용 SG를 판별하고, 정상 자산은 **Skip 사유 코드 6종**(`SKIP_LOW_UTIL`·`SKIP_PROD_PROTECTED` 등)으로 적재해 LLM 호출을 절감한다. 수집에서 사라진 자산은 소멸 표시로 조치 대상에서 빠진다.
2. **보안 위협 대응 (모의)**: OpenIP(0.0.0.0/0)·SSH 브루트포스 모의 관측을 정형화하고 Risk Evaluator가 초기 위험도를 판정한다. 위협 토폴로지에 붉은 노드로 시각화하고, 선제 차단 → 관제자 **[원클릭 해제]** 로 되돌린다. 접수 시점의 자산·SG/NACL 관계 사본과 로그 근거를 함께 보존해 AI 분석에 넘긴다.
3. **Capability-Restricted AI & 4단계 Guardrail**: LLM 권한을 사전 등록된 **Action Whitelist 런북 10종** 중 **AI 추천 가능한 본편 7종** 추천으로 제한하고(롤백 3종은 `ai_recommendable: false` — 시스템/관제자만 트리거), `SCHEMA_CHECK ➔ ACTION_WHITELIST ➔ ARN_MATCH ➔ AWS_DRY_RUN` **고정 순서 4단계** 출력 검증으로 RCE를 차단한다. 거절 사유 코드는 단계 접두(`SCHEMA_`·`WHITELIST_`·`ARN_`·`PRECHECK_`)로 어느 단계에서 막혔는지 역산된다.
4. **One-Click & 양방향 회복 엔진**: Idempotency Key로 중복 실행을 막는다. 다운사이징 전 **스펙 JSON 백업** → `get_waiter` **2/2 Status Check** 감시 → 기동 실패 시 이전 스펙으로 **자동 원복(Auto-Rollback)**. AWS 조회 실패로 결과를 확정하지 못하면 자동 원복 대신 `UNVERIFIED`(결과 확인 불가)로 닫고 관제자 복구를 연다.
5. **레콘(Reckon) — Incident 상태 축**: 가드레일(*실행해도 되는가*) 다음의 두 물음, *실제로 무엇이 일어났는가* → *그 결과를 어떻게 수습·종료하는가*를 다루는 Incident 상태와 그 관리 과정. 남은 제안이 있으면 종료할 수 없고, 종료 판단은 관제자 모달을 거친다.
6. **조치별 절감 예상**: 다운사이징 등 자산 조치를 실행하면 얼마를 아낄 수 있는지를 **금액과 산출 근거를 함께 AI가 내고** 구조화 필드로 저장해 재사용한다. **추정값이며 실제 청구액이 아니다** — 서버 파생 사실값과 필드 이름·화면 표기로 구분한다.
7. **실시간 대시보드**: Next.js + Shadcn 기반 자산/위협 실시간 뷰(WebSocket), 자산 토폴로지 그래프, AI CoT 3줄 요약 카드, 원클릭 조치 UX.

---

## 🧯 Action Whitelist — 런북 10종

여기 없는 Runbook ID는 실행 경로에 진입할 수 없다(가드레일 ② Action Whitelist).

| 분류 | Runbook ID | 하는 일 |
| :--- | :--- | :--- |
| SecOps | `RUNBOOK_EC2_ISOLATE` | ALB 타겟 그룹 이탈 + 격리 SG 교체 |
| SecOps | `RUNBOOK_NACL_ADD_DENY` | 위협 출발지 대역 NACL Deny 규칙 추가 |
| SecOps | `RUNBOOK_NACL_RESTORE` | 우리가 넣은 Deny 규칙만 확인하고 삭제 (원클릭 해제) |
| SecOps | `RUNBOOK_SG_DELETE_ISOLATED` | 미사용·고립 SG 삭제 |
| FinOps | `RUNBOOK_EC2_RIGHTSIZING` | Idle EC2 다운사이징 (자동 원복 시연 대상) |
| FinOps | `RUNBOOK_EC2_ENABLE_AUTOSCALING` | stateless 한정 ASG 구조 전환 |
| FinOps | `RUNBOOK_EBS_DELETE_UNATTACHED` | 미부착 EBS 볼륨 삭제 |
| 롤백 | `RUNBOOK_EC2_UNISOLATE` | 격리 해제 (원클릭 해제) |
| 롤백 | `RUNBOOK_SG_RECREATE` | 삭제한 SG 재생성 |
| 롤백 | `RUNBOOK_EC2_REVERT_SIZE` | 이전 인스턴스 타입 복원 (Status Check 실패 시 시스템 자동 발동) |

* **10종 전부 실구현이 원칙이다** — mock·영상 대체를 전제한 컷라인은 채택하지 않는다. `P0`(`RIGHTSIZING`+`REVERT_SIZE` · `NACL_ADD_DENY`+`NACL_RESTORE`) → `P1`(`SG_DELETE_ISOLATED`+`SG_RECREATE` · `EBS_DELETE_UNATTACHED`) → `P2`(`EC2_ISOLATE`+`UNISOLATE` · `ENABLE_AUTOSCALING`)는 **범위 축소선이 아니라 구현 착수 순서**이며, 각 런북의 현재 진행 상태는 SSOT가 갖는다.
* **확정본은 SSOT [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) §Action Whitelist 표다** — 위험도·승인 방식·AI 추천 가능 여부·롤백 짝은 그 표가 갖는다. 코드 소재는 [`packages/schemas/runbooks.py`](packages/schemas/runbooks.py)·[`runbook_parameters.py`](packages/schemas/runbook_parameters.py)이며, 표와 코드가 어긋나면 표를 기준으로 코드를 고친다.
* **롤백 3종 공통 정책**: Whitelist 정식 등록(우회 경로 없음) · `ai_recommendable: false` · 원복 파라미터는 DB 백업 레코드에서만 로드 · 가드레일 거절 시 자동 재시도 없이 CRITICAL 알림 + 수동 개입. ([ADR-0004](docs/adr/0004-rollback-runbook-whitelist-registration.md))
* **실행 축 어휘**: `trigger_source`(실행별 기록) = `USER_APPROVAL`·`PRE_MITIGATION_0_5S`·`TIMEOUT_ISOLATION_1M`·`AUTO_ON_FAILURE` / `approval_mode`(런북별 정책) = `HUMAN_ONLY`·`SYSTEM_OR_HUMAN`.

---

## 🔌 API 계약 (FE↔BE 공개 계약)

코드 원천은 [`packages/schemas/api/`](packages/schemas/api)이며, 계약의 확정 서술은 SSOT §API 계약이다.

| 엔드포인트 | 내용 |
| :--- | :--- |
| `GET /api/v1/assets` | EC2/SG 상태·스펙·연결관계·헬스 스코어(0–100 정수)·Skip 사유 코드 |
| `GET /api/v1/incidents` | 목록(상세의 부분집합) — `status`·`category` 필터, `created_at` 내림차순 |
| `GET /api/v1/incidents/{id}` | AI CoT 3줄 요약, Evidence ID, 추천 Runbook(본편 7종), 실행 요약(복구 조치는 롤백 3종) |
| `POST /api/v1/actions/execute` | Request `{ incident_id, runbook_id, idempotency_key }` — 신규 접수 `202`, 멱등 재요청 `200` |
| `WS /api/v1/ws` | `INCIDENT_CREATED` · `INCIDENT_UPDATED` · `EXECUTION_UPDATED` (DB commit 이후 전송) |

* **실행 상태 7종**: `IN_PROGRESS` · `SUCCESS` · `FAILED` · `ROLLBACK_INITIATED` · `ROLLED_BACK` · `ROLLBACK_FAILED` · `UNVERIFIED`
* **REST 공통 오류 봉투**: `{"error": {code, message, request_id}}` — 코드 5종(404 · 409×2 · 422 · 500)

---

## 🏗 Directory & Monorepo Structure

uv workspace 모노레포. **MVP는 단일 FastAPI 백엔드(`apps/core-api`)** 로 통합 운영하며, 서비스 물리 분리(Lambda/Step Functions)는 Post-MVP로 미룬다([ADR-0001](docs/adr/0001-mvp-monorepo-structure.md)).

```text
vigilantis/
├── docker-compose.yml           # 로컬 개발: db + migrate + api + localstack (+ adminer: --profile tools)
├── apps/
│   ├── web/                     # [김세혁·FE] Next.js 16 + React 19 + Tailwind v4 + Shadcn 대시보드
│   │   └── src/
│   │       ├── app/             #   App Router — 대시보드 / 자산 / 보안 인시던트 / 자산 인시던트 / 상세
│   │       ├── components/      #   assets · dashboard · incidents · ui(shadcn)
│   │       ├── lib/             #   조회 클라이언트(api/client.ts) · 필터·정렬 · 실시간 이벤트 (+ node --test)
│   │       └── types/api.ts     #   packages/schemas API DTO의 TypeScript 대응
│   └── core-api/                # [안성일·BE/AI · 김세혁·Infra] 단일 FastAPI 백엔드
│       ├── main.py              #   create_app() — 오류 봉투·구조화 로그·스케줄러 3종 lifespan
│       ├── routers/             #   [안성일] 공개 API (assets · incidents · actions · ws)
│       ├── db/                  #   [안성일] SQLAlchemy 모델 · Repository · Alembic migrations
│       ├── services/
│       │   ├── aws/             #     [김세혁] Boto3 실행(executor) · 스펙 JSON 백업(backup) · 원복(rollback)
│       │   ├── collector.py     #     [김승철] 자산 7종 인벤토리 + CloudWatch 메트릭 수집
│       │   ├── rule_engine.py   #     [김승철] Idle EC2·미사용 SG 판정, Skip 사유 코드 산출
│       │   └── scheduler.py     #     [김승철] APScheduler 수집→판정 스캔
│       ├── ai/                  #   [안성일] LangGraph 2그래프(agent.py) · 4단계 가드레일 · Whitelist
│       │   └── evaluation/      #     AI 요약 품질 계측·판정 하네스 (기준선: docs/AI_SUMMARY_BASELINE.md)
│       ├── security/            #   [김세혁] 위협 정형화 · Risk Evaluator · SOAR 차단/해제
│       ├── incident_intake.py   #   판정·위협 → Incident 1건 생성
│       ├── agent_dispatcher.py  #   AI 분석 대기 Incident → LangGraph 호출
│       ├── dispatcher.py        #   승인된 조치 → AWS 실행 디스패치 · 비종료 실행 회수
│       ├── workflows.py         #   업무 흐름 계층 — 상태 전이·트랜잭션 경계 소유(commit은 여기서만)
│       └── realtime.py          #   WebSocket 연결 관리·발행 진입점
├── packages/
│   ├── schemas/                 # [공통] Pydantic DTO — api/(FE↔BE 공개 계약) + 내부 계약
│   │                            #   (assets · events · guardrails · runbooks · runbook_parameters ·
│   │                            #    precheck · backups · candidates · savings · rightsizing_policy 등)
│   ├── telemetry/               # (Post-MVP 자리표시자) OpenTelemetry 셋업
│   └── iac/                     # (Post-MVP 자리표시자) Terraform Core
├── scripts/                     # 시드·주입·평가·스모크 도구 (아래 표)
├── datasets/
│   ├── golden/                  # [김승철] Golden Dataset — FinOps 자산 32건 + SecOps 이벤트 16건
│   └── secops-log-corpus/       # [김승철] SSH 모의 로그 9사례 — 근거 저장·전달 검증용
├── tests/                       # [김승철] 시연 전제 대조 · 가드레일 · 골든 · 롤백 · 실행 하네스 회귀
└── docs/                        # PROJECT_STATUS.md (SSOT) · E2E_DEMO_SCENARIOS.md ·
                                 # AI_SUMMARY_BASELINE.md · adr/ (의사결정 기록 9건)
```

### scripts/

| 스크립트 | 하는 일 |
| :--- | :--- |
| `seed_localstack.py` | LocalStack 더미 AWS 자산 주입 — **시드 단일 원천**(멱등 · 실 AWS 실행 거부) |
| `load_golden_assets.py` | Golden FinOps 자산을 DB에 적재하고 판정까지 돌려 `GET /assets`가 골든을 서빙하게 한다 |
| `inject_mock_threat.py` | 모의 위협 주입 — 골든 SecOps 입력 정형화 + 초기 위험 판정 |
| `inject_status_check_failure.py` | 시연용 Status Check 실패 주입 — 자동 원복 컷을 LocalStack에서 일으킨다 |
| `probe_dryrun.py` | 런북이 쓰는 AWS 작업 전수에 DryRun을 걸어 지원 여부를 실측(ADR-0007 §6) |
| `provision_smoke_aws.py` | 실 AWS 스모크 환경 구성·정리(up / status / down) — ADR-0009 |
| `secops_log_corpus.py` | SSH 모의 로그 코퍼스 build/check + 모의 위협 inbox 준비 |
| `finops_eval.py` · `finops_judge.py` | AI 요약 품질 계측·판정 CLI (구현은 `ai/evaluation/summary`) |
| `smoke_finops_graph.py` · `smoke_secops_graph.py` | LangGraph 실제 모델 왕복 스모크 (⚠️ 과금 호출) |
| `extract_golden_schema.py` | 골든 입력 JSON Schema를 `packages/schemas`에서 재추출(`--check`로 드리프트 대조) |
| `check_ai_signature.py` | 커밋 메시지·PR 본문의 AI 서명 검사 — CI `ai-signature` 잡이 호출 |

---

## ▶️ 로컬 실행

```bash
cp .env.example .env                 # 값 채우기 (OPENAI_API_KEY, AWS_* 등)
docker compose up                    # db(:${POSTGRES_PORT:-5432}) + migrate 1회 + api(:8000) + localstack(:4566)
docker compose --profile tools up    # ↑ + adminer(:${ADMINER_PORT:-8080}) — 선택 DB 웹 UI

uv sync --all-packages               # (호스트 개발 시) 워크스페이스 의존성 동기화
AWS_ENDPOINT_URL=http://localhost:4566 uv run python scripts/seed_localstack.py   # LocalStack 시드

cd apps/web && npm ci && npm run dev  # 대시보드(:3000)
```

> * **대시보드는 `api`(:8000)가 떠 있어야 화면이 선다.** FE에 mock 계층이 없어 `apps/web`만 띄우면 모든 화면이 조회 오류다. BE 주소를 바꿨으면 `apps/web/.env.local`의 `NEXT_PUBLIC_API_BASE_URL`을 맞춘다([`apps/web/.env.example`](apps/web/.env.example)).
> * **LocalStack은 재시작하면 비워진다** — 다시 띄웠으면 시드를 다시 돌린다. 호스트에서 실행할 땐 `AWS_ENDPOINT_URL`이 `http://localhost:4566`이어야 한다(`.env`의 `localstack:4566`은 compose 네트워크 안의 이름이다).
> * **개발 표준 환경은 LocalStack이다**([ADR-0006](docs/adr/0006-localstack-team-standard-env.md)). 실 AWS 전환은 `AWS_ENDPOINT_URL` 줄을 지우고 자격증명을 교체하는 것 하나로 끝난다 — 코드에 환경 감지 분기를 두지 않는다.
> * **`elbv2`·`autoscaling`은 LocalStack Community에 없다** — `EC2_ISOLATE`·`UNISOLATE`·`ENABLE_AUTOSCALING` 3종은 로컬 검증 경로가 없고 실 AWS 스모크가 유일한 검증 자리다.

---

## 🧪 테스트 & CI

```bash
uv run pytest tests apps/core-api/services/tests apps/core-api/ai/tests packages/schemas/tests \
              apps/core-api/db/tests apps/core-api/tests apps/core-api/security/tests -q
cd apps/web && npm run lint && npm run build && npm run test
```

GitHub Actions CI는 **`dev`·`main` 대상 PR·push에서 3잡**이 돈다.

| 잡 | 내용 |
| :--- | :--- |
| `test` | LocalStack·PostgreSQL service container → `uv sync` → LocalStack 시드 → **DB 접속 확인** → pytest |
| `web` | `apps/web` ESLint · `next build`(tsc 타입 체크 포함) · `node --test` 유닛 |
| `ai-signature` | 커밋 메시지·PR 본문의 AI 서명 검사 (PR 본문 수정 시에도 재실행) |

> * `test` 잡이 DB 접속을 **먼저 확인**하는 것은, 컨테이너가 없으면 통합 테스트가 조용히 skip되고 CI가 초록불이 되기 때문이다.
> * **푸시 전 로컬 통합 테스트**: PR이 추가·수정한 테스트 파일에 (조건 없는 `@pytest.mark.skip`이 아닌) skip이 하나라도 나오면 `docker compose up --wait db localstack` → 시드 → `pytest -rs`로 다시 돌린다.
> * **새 테스트 디렉터리를 만들면 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)의 pytest 경로에 함께 추가한다.** 빠지면 CI가 그 디렉터리를 돌지 않는다.

---

## 📚 문서 지도 (신뢰 우선순위 — 충돌 시 위가 이김)

1. **[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)** — **SSOT.** 범위·확정 결정·역할·현황·Action Whitelist
2. [`docs/adr/`](docs/adr) — 결정 배경(왜 그렇게 했나)
3. [`packages/schemas/`](packages/schemas) — 계약의 코드 소재
4. [`docs/E2E_DEMO_SCENARIOS.md`](docs/E2E_DEMO_SCENARIOS.md) — 시연 대본의 원천이자 E2E 회귀 테스트의 명세
5. [`docs/AI_SUMMARY_BASELINE.md`](docs/AI_SUMMARY_BASELINE.md) — AI 요약 3줄의 기준선과 재통과 절차
6. `README.md` (이 문서) — 포트폴리오·소개용. **현황·결정의 기준이 아니다**

### ADR

| # | 결정 |
| :--- | :--- |
| [0001](docs/adr/0001-mvp-monorepo-structure.md) | MVP는 단일 FastAPI 백엔드(`core-api`)로 통합한다 |
| [0002](docs/adr/0002-runbook-whitelist-mvp-scope.md) | Action Whitelist는 레지스트리 7종으로 확정하고 전부 MVP 범위로 한다 |
| [0003](docs/adr/0003-fe-stack-nextjs-16.md) | FE 스택을 Next.js 16(React 19·Tailwind v4·shadcn 4)으로 상향한다 |
| [0004](docs/adr/0004-rollback-runbook-whitelist-registration.md) | 롤백 런북 3종을 Whitelist에 정식 등록한다 (7종 → 10종) |
| [0005](docs/adr/0005-langgraph-stateless-domain-graphs.md) | LangGraph를 상태를 보관하지 않는 도메인별 두 그래프로 구성한다 |
| [0006](docs/adr/0006-localstack-team-standard-env.md) | LocalStack 팀 표준 개발 환경과 실 AWS 전환 전략 |
| [0007](docs/adr/0007-guardrail-dryrun-executor-precheck-contract.md) | 가드레일 4단계 AWS Dry-Run은 executor의 단일 `precheck()` 호출로 판정한다 |
| [0008](docs/adr/0008-backup-record-lifecycle-recovery-integrity.md) | 백업 레코드는 조치 직전 1회 캡처·불변 보존하고, 원복 재개는 상태 대조로 판단한다 |
| [0009](docs/adr/0009-real-aws-smoke-environment.md) | 실 AWS 스모크 환경 — 계정·자격증명·비용 통제와 시연 인프라 |

---

## 🔀 Modified Git-Flow

```
main (Production / Stable · 시연 축 — 릴리스 컷 + 태그)
  ▲  PR & CI 3잡 통과 + 1인 이상 Code Review 승인
dev (Integration · 개발 축 — 계속 흐른다)
  ├── feat/BE-<n>-<desc>      core-api 라우터/DB·Boto3 실행
  ├── feat/AI-<n>-<desc>      4단계 가드레일·LangGraph
  ├── feat/DATA-<n>-<desc>    수집/Rule Engine·Golden·QA 시나리오
  ├── feat/SEC-<n>-<desc>     security/차단·해제
  ├── feat/FE-<n>-<desc>      web 대시보드
  ├── chore/INFRA-<n>-<desc>  Docker/CI/IaC
  └── docs/DOCS-<n>-<desc>    문서·ADR
```

---

## 📝 Branch / Commit / PR Convention

**도메인 코드**: `FE`(web) · `BE`(core-api) · `AI`(ai) · `DATA`(수집/rule) · `SEC`(security) · `SCHEMA`(schemas) · `INFRA`(docker/CI) · `DOCS`(문서)

* **브랜치명**: `<type>/<DOMAIN>-<이슈번호>-<english-kebab-summary>` (이모지 미사용)
  * 예: `feat/BE-7-assets-list-api`, `chore/INFRA-4-docker-compose-setup`
* **커밋·PR 제목**: `<gitmoji> [TYPE] #이슈번호 - 한 줄 설명`
  * 예: `✨ [FEAT] #7 - EC2/SG 자산 조회 API 구현`, `🥅 [FIX] #45 - 롤백 타임아웃 예외 처리`
  * `TYPE ∈ [FEAT] [FIX] [REFACTOR] [CHORE] [DOCS]`, gitmoji는 https://gitmoji.dev 참고
  * 한 줄 설명은 한국어, 코드 식별자·파일명은 원문 유지. 이슈 없으면 번호 생략.
* **테스트·데이터셋·QA 산출물의 DOMAIN**은 파일이 놓인 디렉터리가 아니라 **그 작업이 검증·대상으로 삼는 영역**을 따른다(`tests/test_guardrails.py` → `AI`, `datasets/golden/` → `DATA`). `QA`는 DOMAIN 값이 아니다.

### Pull Request & Code Review

1. `feat/*` 등 작업 브랜치에서 **`dev`로 PR** 제출 (`main` 직접 PR 금지). 본문은 [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)가 자동으로 채운다.
2. 최소 1명 이상(특히 백엔드↔AI↔프론트 API 접점 담당자)의 Approve 후 Merge.
3. GitHub Actions CI **3잡 전부** 통과 필수 — `test` · `web` · `ai-signature`.
4. **리뷰 코멘트는 리뷰 상태와 함께 올린다** — 수정이 필요하면 `Request changes`, 머지해도 되면 `Approve`. 상태 없는 `Comment`만 남기지 않는다(머지를 막는지 알 수 없다).
5. **이슈 자동 CLOSE 금지**: 커밋 푸터·PR 본문에 `Closes`·`Fixes`·`Resolves`를 쓰지 않고 **`Refs #N`으로만** 연결한다. 기본 브랜치가 `dev`라 그 키워드는 머지 즉시 이슈를 닫아 잔여 작업을 가린다 — **CLOSE는 머지 책임자가 머지 후 직접 판단해 수동으로** 한다.
6. **AI 서명 금지**: 커밋 메시지·PR 본문에 `Co-Authored-By: <AI>`·`Claude-Session:` 트레일러, 세션 링크, `Generated with …` 문구를 넣지 않는다. CI `ai-signature` 잡이 검사해 걸리면 실패한다.
7. **이미 올린 글이 틀렸으면 새 글이 아니라 그 글을 고친다** — 정정본을 새 코멘트로 올리면 무엇이 최종 요청인지 흐려진다. 수정본 맨 위에 무엇을 왜 정정했는지 한 줄을 남긴다.

> 규약 원문은 [`CLAUDE.md`](CLAUDE.md)에 있다. 위는 요약이며 충돌 시 `CLAUDE.md`를 따른다.

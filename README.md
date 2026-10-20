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

* **Frontend**: Next.js 16 (App Router), React 19, TypeScript, Shadcn UI(radix-ui), Tailwind CSS v4, WebSocket, `node --test` 유닛 테스트, recharts(대시보드 추이·구성 차트)
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
6. **조치별 절감 예상**: 다운사이징 등 자산 조치를 실행하면 얼마를 아낄 수 있는지를 보인다. **AI는 현재·목표 인스턴스 타입의 단가만 추정하고, 월 절감액·적용 조건·설명 문장은 서버가 만든다**(2026-09-17 개정). 구조화 필드로 저장해 재사용한다. **추정값이며 실제 청구액이 아니다** — 서버 파생 사실값과 필드 이름·화면 표기로 구분한다.
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
| `GET /api/v1/metrics/timeseries` | 대시보드 추이 차트의 원천 — 축 6개(EC2 CPU · 네트워크 In/Out · 인터넷 개방 보안 그룹 · `asset_status` · `asset_inventory` · `threat_events`)를 한 응답에 싣고 축마다 상태를 따로 매긴다 · 쿼리 `hours`(기본 72) |
| `GET /api/v1/incidents` | 목록(상세의 부분집합) — `status`·`category` 필터, `created_at` 내림차순 |
| `GET /api/v1/incidents/{id}` | AI CoT 3줄 요약, Evidence ID, 추천 Runbook(본편 7종), 실행 요약(복구 조치는 롤백 3종) |
| `POST /api/v1/incidents/{id}/resolve` | 관제자 종료 판단 기록 — **Idempotency Key를 받지 않는다**(AWS를 바꾸지 않고 Incident 상태 하나만 옮기므로 재요청도 `200`) |
| `POST /api/v1/actions/execute` | Request `{ incident_id, runbook_id, idempotency_key }` — 신규 접수 `202`, 멱등 재요청 `200` |
| `WS /api/v1/ws` | `INCIDENT_CREATED` · `INCIDENT_UPDATED` · `EXECUTION_UPDATED` (DB commit 이후 전송) |

* **실행 상태 7종**: `IN_PROGRESS` · `SUCCESS` · `FAILED` · `ROLLBACK_INITIATED` · `ROLLED_BACK` · `ROLLBACK_FAILED` · `UNVERIFIED`
* **REST 공통 오류 봉투**: `{"error": {code, message, request_id}}` — 코드 6종(404 · 409×3 · 422 · 500). 409 셋은 `IDEMPOTENCY_KEY_CONFLICT`·`PROPOSAL_NOT_EXECUTABLE`·`INCIDENT_NOT_RESOLVABLE`(종료 처리할 수 있는 상태가 아닌 Incident에 종료를 요청했을 때)이다.

---

## 🏗 Directory & Monorepo Structure

uv workspace 모노레포. **MVP는 단일 FastAPI 백엔드(`apps/core-api`)** 로 통합 운영하며, 서비스 물리 분리(Lambda/Step Functions)는 Post-MVP로 미룬다([ADR-0001](docs/adr/0001-mvp-monorepo-structure.md)).

```text
vigilantis/
├── docker-compose.yml           # 로컬 개발: db + migrate + api + localstack (+ adminer: --profile tools)
├── apps/
│   ├── web/                     # [김세혁·FE] Next.js 16 + React 19 + Tailwind v4 + Shadcn 대시보드
│   │   └── src/
│   │       ├── app/             #   App Router — 대시보드(`/`) / 자산 관제(`/assets`) / 보안 관제(`/security`) / 인시던트 상세(`/incidents/[id]`) · 옛 목록 경로 `/incidents`·`/asset-incidents`는 각 화면의 인시던트 탭으로 redirect(2026-09-28)
│   │       ├── components/      #   assets · dashboard · incidents · ui(shadcn)
│   │       ├── lib/             #   조회 클라이언트(api/client.ts) · 필터·정렬 · 실시간 이벤트 (+ node --test)
│   │       └── types/api.ts     #   packages/schemas API DTO의 TypeScript 대응
│   └── core-api/                # [안성일·BE/AI · 김세혁·Infra] 단일 FastAPI 백엔드
│       ├── main.py              #   create_app() — 오류 봉투·구조화 로그·스케줄러 3종 lifespan
│       ├── routers/             #   [안성일] 공개 API (assets · incidents · actions · metrics · ws)
│       ├── db/                  #   [안성일] SQLAlchemy 모델 · Repository · Alembic migrations
│       ├── services/
│       │   ├── aws/             #     [김세혁] Boto3 실행(executor) · 스펙 JSON 백업(backup) · 원복(rollback)
│       │   ├── collector.py     #     [김승철] 자산 7종 인벤토리 + CloudWatch 메트릭 수집
│       │   ├── rule_engine.py   #     [김승철] Idle EC2·미사용 SG 판정, Skip 사유 코드 산출
│       │   └── scheduler.py     #     [김승철] APScheduler 수집→판정 스캔
│       ├── ai/                  #   [안성일] LangGraph 2그래프(agent.py) · 4단계 가드레일 · Whitelist
│       │   └── evaluation/      #     AI 요약 품질 계측·판정 하네스 (기준선: evaluation/summary/baseline.md)
│       ├── security/            #   [디렉터리 오너 김세혁 · 정형화/판정 김승철] 위협 정형화 · Risk Evaluator · SOAR 차단/해제
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
└── docs/                        # PROJECT_STATUS.md (SSOT) · E2E_DEMO_SCENARIOS.md · AWS_SMOKE_RESULT.md ·
                                 # MVP_CLOSEOUT_CHECKLIST.md · adr/ (의사결정 기록 10건)
```

### scripts/

| 스크립트 | 하는 일 |
| :--- | :--- |
| `stepper/` | 시연 · FE 관찰용 테스트 서버 — 타이머 없이 명령 한 번에 한 칸씩 진행(기동 · 자산 연결 · 위협 주입 · 초기화, [사용법](scripts/stepper/README.md)) |
| `seed_localstack.py` | LocalStack 더미 AWS 자산 주입 — **시드 단일 원천**(멱등 · 실 AWS 실행 거부) |
| `load_golden_assets.py` | Golden FinOps 자산을 DB에 적재하고 판정까지 돌려 `GET /assets`가 골든을 서빙하게 한다 |
| `inject_mock_threat.py` | 모의 위협 주입 — 골든 SecOps 입력 정형화 + 초기 위험 판정 |
| `inject_status_check_failure.py` | 시연용 Status Check 실패 주입 — 자동 원복 컷을 LocalStack에서 일으킨다 |
| `probe_dryrun.py` | 런북이 쓰는 AWS 작업 전수에 DryRun을 걸어 지원 여부를 실측(ADR-0007 §6) |
| `provision_smoke_aws.py` | 실 AWS 스모크 환경 구성·정리(up / status / down) — ADR-0009 |
| `provision_platform_aws.py` | 플랫폼 배포 환경 구성·배포·정리 — CloudFront + WAF · 공개 ALB · ECS Fargate(api·web) · RDS, 장기 키 없이 역할 전환으로 관제 대상에 닿는다 — ADR-0010 |
| `qa_scan_compare.py` | 실 AWS 스모크의 수집·판정 대조(A3·A9) 조회를 `docs/AWS_SMOKE_RESULT.md` §2 순서로 출력 — **읽기 전용** |
| `secops_log_corpus.py` | SSH 모의 로그 코퍼스 build/check + 모의 위협 inbox 준비 |
| `finops_eval.py` · `finops_judge.py` | AI 요약 품질 계측·판정 CLI (구현은 `ai/evaluation/summary`) |
| `secops_eval.py` | SecOps 오프라인 평가 진입점 (구현은 `ai/evaluation/secops`) |
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

시연은 위 절차 대신 단계 진행 테스트 서버로 한다 — `uv run python scripts/stepper/cli.py up`이 DB · LocalStack · API · FE를 한 번에 띄운다([scripts/stepper/README.md](scripts/stepper/README.md)).

> * **대시보드는 `api`(:8000)가 떠 있어야 화면이 선다.** FE에 mock 계층이 없어 `apps/web`만 띄우면 모든 화면이 조회 오류다. BE 주소를 바꿨으면 `apps/web/.env.local`의 `NEXT_PUBLIC_API_BASE_URL`을 맞춘다([`apps/web/.env.example`](apps/web/.env.example)).
> * **LocalStack은 재시작하면 비워진다** — 다시 띄웠으면 시드를 다시 돌린다. 호스트에서 실행할 땐 `AWS_ENDPOINT_URL`이 `http://localhost:4566`이어야 한다(`.env`의 `localstack:4566`은 compose 네트워크 안의 이름이다).
> * **개발 표준 환경은 LocalStack이다**([ADR-0006](docs/adr/0006-localstack-team-standard-env.md)). 실 AWS 전환은 `AWS_ENDPOINT_URL` 줄을 지우고 자격증명을 교체하는 것 하나로 끝난다 — 코드에 환경 감지 분기를 두지 않는다.
> * **`elbv2`·`autoscaling`은 LocalStack Community에 없다** — `EC2_ISOLATE`·`UNISOLATE`·`ENABLE_AUTOSCALING` 3종은 로컬 검증 경로가 없고 실 AWS 스모크가 유일한 검증 자리다.

### 🔔 외부 알림 운영 (#493)

기본값은 꺼짐(`NOTIFICATION_ENABLED=false`)이다. 켜면 업무 트랜잭션이 남긴 사건(`notification_events`)을 앱 안의 워커가 (사건, 채널)마다 `notification_deliveries` 행으로 만들고, Slack webhook이나 SNS Publish로 보낸 뒤 결과를 기록한다. 설정 키·기본값·허용 범위는 [`.env.example`](.env.example) §외부 알림과 `apps/core-api/config.py`에 있다.

> **실 AWS 변경과 실제 발송 전 확인** — 대상 환경은 확정되지 않았다. 계정·리전·역할·비밀 ARN·토픽·수신처·발송 건수·비용·롤백 방법을 사용자가 승인하기 전에는 `--yes`를 붙이지 않고 실제로 보내지도 않는다. `--yes`가 없는 미리보기도 실 AWS를 읽는다. 실제 발송 확인(사건 → outbox → 워커 → receipt)은 AWS 변경 승인과 따로 승인받는다. 이때 사건을 만드는 방법·수신처·건수·정리 방법을 함께 정한다. 확인에는 승인된 활성화 **뒤에 새로 만든** 사건을 쓴다. 활성화 전 사건은 `NOT_ACTIVATED`로 막힌다.

**설정 주입** — webhook 원문은 Compose에서는 커밋하지 않는 로컬 `.env`에만, ECS에서는 Secrets Manager 비밀에만 둔다. routes 파일·`--notification-config` 입력·`NOTIFICATION_ROUTES_JSON`·소스·이미지·로그·명령줄 인자에는 평문으로 두지 않는다. 값을 출력하거나 이슈·채팅에 붙여 넣지 않는다.

| | Compose 개발 스택 | ECS 플랫폼 배포 |
| :--- | :--- | :--- |
| routes | 기존 `apps/core-api` 바인드 마운트 아래에 파일을 두고 `NOTIFICATION_ROUTES_FILE=/app/apps/core-api/<파일>`로 지정한다(`serve.py`를 거치지 않는다) | `--notification-config`의 `routes`가 `NOTIFICATION_ROUTES_JSON`으로 들어간다. `serve.py`가 검증한 뒤 `$HOME/.vigilantis/notification-routes.json`에 쓴다. JSON과 `NOTIFICATION_ROUTES_FILE`을 함께 주면 기동이 멈춘다 |
| Slack | route의 `slack_webhook_env` 이름의 변수를 `.env`(커밋 금지)에 둔다 | 이미 있는 Secrets Manager 비밀을 쓴다. 이 계정·리전의 정확한 전체 ARN이어야 하고, SecretString은 webhook URL 원문이다. 실행 역할이 ECS `secrets`로 주입하며, 스크립트는 비밀을 만들거나 값을 쓰지 않는다 |
| SNS | 검증 경로가 없다. ECS 컨테이너 자격증명만 쓰므로 `sns_credentials_unavailable`(PERMANENT)로 실패한다 | 플랫폼 계정이 소유한 토픽만 지정할 수 있다. 토픽·구독·KMS·교차 계정 정책은 만들지 않는다 |

`--notification-config` 입력 형식(비밀 아님)은 아래와 같다. `enabled: true`를 명시해야 켜지고, `settings`에는 `.env.example`의 `NOTIFICATION_*` 조정값 8개만 넣을 수 있다. 다음 입력은 AWS를 바꾸기 전에 거부된다. 입력 검증 자체는 AWS를 부르지 않지만, CLI는 그 전에 STS로 계정을 확인한다.

* 입력 파일이 80 KiB를 넘거나, 직렬화한 routes가 런타임 상한인 64 KiB를 넘는 경우
* `settings` 조합이 런타임 규칙에 맞지 않는 경우: max backoff가 backoff보다 작거나, lease가 `2×(connect+read timeout)+10`초보다 짧으면 거부된다
* `slack_secret_arns`의 변수 이름이 기존 설정 이름과 겹치거나 `AWS_`·`VIGILANTIS_`로 시작하는 경우(대소문자 구분 없음)

```json
{"enabled": true, "public_base_url": "https://<승인된 대시보드 origin>",
 "routes": [{"tenant_id": "<고객>", "aws_account_id": "<관제 계정>", "slack_webhook_env": "NOTIFICATION_SLACK_A",
             "sns_topic_arn": "arn:aws:sns:<리전>:<플랫폼 계정>:<토픽>"}],
 "slack_secret_arns": {"NOTIFICATION_SLACK_A": "arn:aws:secretsmanager:<리전>:<플랫폼 계정>:secret:<기존 비밀>"},
 "settings": {"NOTIFICATION_READ_TIMEOUT_SECONDS": 5}}
```

* **권한 범위** — `vigilantis-platform`(태스크 역할)은 지정한 토픽 ARN의 `sns:Publish`(`publish-notification-topics`)만 받는다. `vigilantis-platform-exec`(실행 역할)는 지정한 비밀 ARN의 `GetSecretValue`(`read-notification-secrets`)만 받는다. api 앱(태스크 역할)은 비밀을 읽지 못한다. 관제 대상 접근 역할 `vigilantis-access`는 알림 권한을 하나도 받지 않는다. SNS는 프로필·정적 키로 넘어가지 않고 실패하면 닫힌다.
* **배포 명령 주의** — `deploy`는 IAM을 바꾸지 않는다. 알림을 켠 `deploy`는 미리보기를 포함해 알림 전용 정책 문서가 입력과 맞는지만 읽어서 확인하고, 다르면 멈춘다. 이 확인은 실제로 적용된 권한을 증명하지 않는다. `deploy`는 api만이 아니라 migrate·api·web을 함께 재배포한다. `--enable-dispatch`를 지금 값과 같게 주지 않으면 AI 분석·조치 실행 디스패치(`DISPATCH_ENABLED`)도 바뀐다. api는 이전 태스크를 먼저 내리므로 배포 중 끊김이 생긴다. `배포 실패`이면 서킷 브레이커가 이전 태스크 정의로 되돌렸을 수 있고, 그 정의의 알림 설정이 그대로 돈다.
* **다시 켜기 전 경고** — 같은 수신처로 다시 켜면 `activated_at`과 epoch가 유지된다. 그래서 꺼져 있던 동안 생긴 미발송 사건과 남아 있던 `PENDING` 행이 재기동 뒤 발송된다(`BLOCKED`·`FAILED`·`SUCCEEDED`는 되살아나지 않는다). 켜기 전에 아래 조회로 미발송 건수를 확인하고, 그 건수를 보내도 되는지 승인받는다.
* **발송 규칙**
  * 라우트를 처음 등록하거나 수신처·테넌트를 바꾸면, 다음 기동 때 DB 시각으로 새 `activated_at`을 받는다. `created_at`이나 `occurred_at` 중 하나라도 그보다 이른 사건은 `BLOCKED(NOT_ACTIVATED)`로 끝난다. 받을 사건이 생기기 전에 활성화한다. 과거 사건을 보내려고 이벤트·라우트 행의 시각을 고치지 않는다.
  * 수신처가 바뀐 계정의 기존 대기 행은 보내기 직전 `BLOCKED(ROUTE_CHANGED)`가 된다. 새 수신처로 옮겨 보내지 않는다.
  * 설정 변경은 재기동(ECS는 재배포)해야 적용된다. 실행 중에 바로 적용되는 경로는 없다.
  * `BLOCKED`·`FAILED`(`PERMANENT`·`EXHAUSTED`)·`SUCCEEDED`는 되돌리지 않는다. 재전송·취소·비우기(drain) 기능은 없다. DB를 고쳐 되살리거나 지우지 않는다.
* **중복 가능** — 종료되는 태스크는 새 claim을 멈추고 진행 중인 결과 저장을 기다린다. 그 전에 강제 종료되면 해당 행이 `IN_FLIGHT`로 남는다. 결과를 저장하기 전에 멈춘 시도는 임대(`NOTIFICATION_LEASE_SECONDS`)가 지난 뒤 회수되어 다시 보낸다. 그래서 같은 사건이 최대 `NOTIFICATION_MAX_ATTEMPTS`번 갈 수 있다(정확히 1회 보장 없음). 임대가 `2×(connect+read timeout)+10`초보다 짧으면 기동하지 않는다.
* **수신의 의미** — `receipt_kind`의 `SLACK_POSTED`는 Slack이 게시 요청을 받았다는 뜻이고, `SNS_ACCEPTED`는 SNS가 Publish를 접수했다는 뜻이다. 어느 쪽도 메일함 도착이나 사람이 읽었음을 뜻하지 않는다.
* **기동 경고·실패**(계정·채널·사유 코드만 남는다) — `notification_route_blocked`는 선언됐지만 막힌 라우트로, 예를 들어 `SECRET_MISSING`·`DESTINATION_*`·`OWNERSHIP_CONFLICT`가 있다. `notification_account_ownership_missing`은 선언되지 않은 계정의 사건이며 계정마다 1회 남는다. `notification_delivery_blocked`는 행 단위로 남는다(일부러 뺀 채널은 제외). 설정 오류로 기동이 멈추면 `NOTIFICATION_ROUTES_REQUIRED`·`NOTIFICATION_LEASE_TOO_SHORT`·`NOTIFICATION_CONFIGURATION_FAILED` 같은 고정 코드만 남는다.
* **로그인 링크 차단 사유** — 알림 본문의 `상세:` 링크는 `<public_base_url>/incidents/<id>`이다. API·SSR은 서버 세션으로 보호되며, 로그인 전에 링크를 열면 `/login?next=…`로 이동했다가 로그인 뒤 같은 사이트 경로인 `/incidents/<id>`로 돌아온다(#502). 세션 보호는 로그인 여부를 확인하고, 사건의 테넌트·계정 소유 검사는 T-4에서 붙는다. CloudFront→오리진 구간은 10/13 관측 기준 `http-only`다([ADR-0010](docs/adr/0010-platform-deployment-saas-shape.md) 1차 개정). 실제 오리진 HTTPS를 확인하기 전에는 이 링크로 비밀번호를 입력하거나 세션을 쓰지 않는다.

**켜기·변경·끄기 절차** — IAM 정책을 쓰는 명령은 `notification-iam`과 `notification-revoke` 둘뿐이다. 둘 다 알림이 꺼진 정의로 서비스가 안정돼 있고, 알림이 켜진 api 태스크가 하나도 남아 있지 않을 때만 실행한다. 내려가는 중인 태스크도 남은 것으로 본다. `--yes`이면 스크립트도 이 상태를 확인하고, 맞지 않으면 정책을 바꾸지 않고 멈춘다. 미리보기는 이 상태를 확인하지 않는다. 정책을 준비해도 발송은 켜지지 않는다. 모든 `--yes`는 그 단계의 미리보기를 확인한 뒤 따로 승인받는다.

처음 켤 때:

1. 알림이 꺼진 정의로 서비스가 안정돼 있는지 확인한다.
2. `notification-iam --account <ID> --notification-config <파일>`로 미리보기 → 승인 → `--yes`.
3. 권한을 확인한다. 실제 접근 확인 방법은 대상 환경이 정해지지 않아 아직 고르지 않았다. 이 단계는 켜기 전 미해결 선행 조건이다.
   * 승인된 IAM 정책 시뮬레이션으로 의도한 토픽·비밀은 허용되고, 범위 밖 합성 ARN은 거부되는지 실제 Publish 없이 볼 수 있다. 다만 이것은 정책 근거일 뿐 실제 IAM 실행 결과가 아니다.
   * 비밀 주입과 SNS 실제 동작은 따로 승인된 환경·발송 확인에서만 검증한다. 시간을 기다렸다고 전파가 증명되지 않고, 시뮬레이션 통과가 런타임 동작을 증명하지도 않는다.
   * 권한이 실제로 적용되기 전에 나간 SNS 발송은 `AccessDenied`가 PERMANENT로 분류되어 재시도 없이 `FAILED`로 끝난다.
   * 권한을 확인하려고 승인되지 않은 실제 Publish를 보내지 않는다.
4. 아래 '다시 켜기 전 경고'를 확인한다. 그다음 `deploy --account <ID> --tag <태그> --notification-config <파일>`로 미리보기 → 승인 → `--yes`.

수신처·권한을 바꾸거나 끌 때:

1. `--notification-config` 없이 `deploy --account <ID> --tag <태그>`를 실행한다. `--enable-dispatch`는 지금 값을 유지한다. `배포 완료`가 나왔는지 확인한다.
2. 알림이 꺼진 정의로 안정됐고 이전 태스크가 모두 멈췄는지 확인한다. `배포 실패`이면 여기서 멈추고 원인부터 본다.
3. 바꿀 때는 새 입력으로 `notification-iam`을 실행한다. 없앨 때는 `notification-revoke --account <ID>`로 알림 전용 인라인 정책 둘만 지운다. 이때 다른 정책·비밀·토픽·구독과 태그 없는 역할은 건드리지 않는다.
4. 다시 켤 때만 처음 켤 때의 3–4단계를 밟는다.

이전 정책을 자동으로 되돌리거나 실패한 건을 다시 보내는 기능은 없다.

**대기·실패 조회** — 읽기 전용이다. Compose는 `docker compose exec db psql -U vigilantis -d vigilantis`로 접속한다. 배포 DB 접속 경로는 따로 승인받는다.

```sql
BEGIN TRANSACTION READ ONLY;
-- 상태별 건수: BLOCKED 사유 · FAILED의 PERMANENT(영구 실패)/EXHAUSTED(재시도 소진)
SELECT channel, state, COALESCE(blocked_reason, final_reason) AS reason, count(*)
FROM notification_deliveries GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
-- 최근 종결 실패·차단 50건(수신처·비밀값 컬럼은 없다)
SELECT delivery_id, event_id, channel, state, blocked_reason, final_reason, last_category, last_error_code, attempts, finished_at
FROM notification_deliveries WHERE state IN ('BLOCKED', 'FAILED') ORDER BY finished_at DESC LIMIT 50;
-- 대기 건수 · 가장 오래된 대기
SELECT channel, state, count(*), min(created_at) AS oldest, min(next_attempt_at) AS next_due
FROM notification_deliveries WHERE state IN ('PENDING', 'IN_FLIGHT') GROUP BY 1, 2;
-- 아직 행이 없는 사건(워커가 읽지 않은 outbox) — 채널마다
SELECT c.channel, count(*), min(e.created_at) AS oldest
FROM notification_events e CROSS JOIN (VALUES ('SLACK'), ('SNS')) AS c(channel)
WHERE NOT EXISTS (SELECT 1 FROM notification_deliveries d WHERE d.event_id = e.event_id AND d.channel = c.channel)
GROUP BY 1;
ROLLBACK;
```

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

## 📚 문서 지도

**신뢰 우선순위는 SSOT [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) §문서 지도가 갖는다 — 프로젝트의 우선순위 목록은 그것 하나뿐이다.** 아래는 각 문서가 무엇을 담는지의 안내이며 순위표가 아니다. 문서끼리 어긋나면 순위를 여기서 판단하지 말고 그 목록을 본다.

* **[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)** — **SSOT.** 범위·확정 결정·역할·현황·Action Whitelist
* [`docs/adr/`](docs/adr) — 결정 배경(왜 그렇게 했나)
* [`packages/schemas/`](packages/schemas) — 계약의 코드 소재
* [`docs/E2E_DEMO_SCENARIOS.md`](docs/E2E_DEMO_SCENARIOS.md) — 시연 대본의 원천이자 E2E 회귀 테스트의 명세
* [`docs/AWS_SMOKE_RESULT.md`](docs/AWS_SMOKE_RESULT.md) — 실 AWS 스모크·P2 검증의 실측 기록 양식(A1–A11 번호의 정의). **값은 기록이지 기준이 아니다**
* [`docs/MVP_CLOSEOUT_CHECKLIST.md`](docs/MVP_CLOSEOUT_CHECKLIST.md) — 10/15(목) MVP 마감 판정에서 볼 칸과 근거의 자리. 판정 기준 자체는 SSOT에만 있다
* [`apps/core-api/ai/evaluation/summary/baseline.md`](apps/core-api/ai/evaluation/summary/baseline.md) — AI 요약 3줄의 기준선과 재통과 절차
* `README.md` (이 문서) — 포트폴리오·소개용. **현황·결정의 기준이 아니다**

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
| [0010](docs/adr/0010-platform-deployment-saas-shape.md) | 플랫폼 배포 — 단일 계정이지만 SaaS 모양으로(CloudFront · 역할 전환 · 수집 범위) |

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

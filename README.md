# Vigilantis 🛡️

> **24/7 클라우드 자산·보안 상시 관제 및 4단계 AI 가드레일 기반 원클릭 자율 조치·자동 롤백(Auto-Rollback) FinSecOps 파이프라인**

---

## 📌 Project Overview

* **팀명**: 딸깍 인프라
* **개발 배경**: Multi-Account/Region 환경 확산에 따른 인프라 파편화와 초단위 보안 위협에 대응하고, AI 자동화 도입 시 발생하는 환각(Hallucination) 및 과도한 권한 실행(Excessive Agency) 위험을 해결하기 위해 구축되었습니다.
* **MVP 범위**: **AWS 단일 계정 / 1–2개 리전 / EC2·Security Group 중심**(런북 조치 대상: NACL·EBS·ASG·ALB Target Group 포함). CloudWatch(CPU/Network) 기반 Idle EC2 판별, OpenIP·SSH 브루트포스 **모의 위협** 대응, GPT-4o 4단계 가드레일 + **런북 10종(본편 7 + 롤백 3) Action Whitelist**, 양방향 회복 엔진, Next.js 대시보드까지를 1차 발표 대상으로 한다.
* **현황·결정 기준(SSOT)**: [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) — 확정 범위·결정 로그·미해결 이슈의 단일 기준. 본 README와 충돌 시 PROJECT_STATUS.md가 우선한다.
* **Post-MVP (로드맵)**: RDS·S3 확장, Multi-Account/Region, OpenTelemetry 전 구간 트레이싱, Step Functions/ECS Fargate/Lambda, Terraform Drift 감지·GitOps PR, 모바일 푸시(FCM), GCP·Azure. (아래 Tech Stack 참고)

---

## 👥 Team & Roles

| 이름 | 역할 | 담당 영역 (주요 경로) |
| :--- | :--- | :--- |
| **김세혁 (팀장)** | PM · **Infra & DevSecOps** | Boto3 EC2/SG 제어·자동 원복 엔진(`services/aws`), (모의) 위협 차단(`security`), APScheduler, Docker/배포, Git 브랜치·코드리뷰 |
| **안성일** | **AI/Guardrail · Architect** | 전체 아키텍처·DB 스키마(`db`), FastAPI 메인·라우터(`main.py`,`routers`), GPT-4o + 4단계 가드레일(`ai`) |
| **김승철** | **Data & Rule Engine** | CloudWatch 수집·정형화(`services/collector`), Idle EC2·미사용 SG 판별 및 Skip 사유 코드(`services/rule_engine`) |
| **박지현** | **QA & Scenario / Technical Writer** | Golden Dataset(`datasets/golden`), pytest 회귀·E2E 시나리오(`tests`), 문서·ADR(`docs`) |
| **유건희** | **Frontend Engineer** | Next.js 16 + Shadcn 대시보드, REST/WebSocket 연동, 자산 토폴로지·차트 시각화(`apps/web`) |

---

## 🛠 Tech Stack

**MVP (실사용)**

* **Frontend**: Next.js 16 (App Router), React 19, TypeScript, Shadcn UI(radix-ui), Tailwind CSS v4, WebSocket (차트 라이브러리는 미선정 — Recharts/Tremor 후보)
* **Backend**: FastAPI (Python 3.11+), Boto3, PostgreSQL, SQLAlchemy · Alembic, APScheduler, pydantic-settings
* **AI & Safety**: OpenAI GPT-4o, LangGraph (오케스트레이션), Pydantic v2 (Structured Output), pytest (Golden Dataset Evals)
* **Infra/Dev**: Docker Compose (FastAPI + PostgreSQL + LocalStack), GitHub Actions CI — `test` 잡(pytest + PostgreSQL·LocalStack service container) · `web` 잡(ESLint · `next build` 타입 체크 · `node --test`)

**Post-MVP (로드맵)**: OpenTelemetry(W3C Trace Context) · AWS Step Functions/ECS Fargate/Lambda/EventBridge/GuardDuty · Terraform(Drift·GitOps) · Redis(ElastiCache) · LangGraph **Multi-Agent 확장**(MVP는 단일 그래프 오케스트레이션) · OIDC SSO·MFA·RBAC · 모바일 푸시(FCM) · GCP/Azure

---

## ✨ Key Features (MVP)

1. **자산 관제 & Idle 판별**: EC2·SG·EBS·ASG/Launch Template·ALB Target Group 인벤토리와 CloudWatch(CPU/Network)를 주기 수집(APScheduler)하고 자산 간 연결관계 6종을 산출하며, Rule Engine이 Idle EC2·미사용 SG를 판별. 정상 자산은 Skip 사유 코드(`SKIP_LOW_UTIL` 등)로 적재해 LLM 호출 절감.
2. **보안 위협 대응 (모의)**: OpenIP(0.0.0.0/0)·SSH 브루트포스 모의 위협을 수집·시각화(붉은색 토폴로지 노드)하고, 선제 차단 → 관제자 **[원클릭 해제]** 롤백.
3. **Capability-Restricted AI & 4단계 Guardrail**: LLM 권한을 사전 등록된 **Action Whitelist 런북 10종**([ADR-0002](docs/adr/0002-runbook-whitelist-mvp-scope.md)·[ADR-0004](docs/adr/0004-rollback-runbook-whitelist-registration.md)) 중 **AI 추천 가능한 본편 7종** 추천으로 제한하고(롤백 3종은 `ai_recommendable: false` — 시스템/관제자만 트리거), `Schema ➔ Action Whitelist ➔ ARN Match ➔ AWS Dry-Run` 4단계 출력 검증으로 RCE 차단.
4. **One-Click & 양방향 회복 엔진**: Idempotency Key로 중복 실행 방지. 다운사이징 전 스펙 JSON 백업 → `get_waiter` Status Check 감시 → 기동 실패 시 이전 스펙 **자동 원복(Auto-Rollback)**.
5. **실시간 대시보드**: Next.js + Shadcn 기반 자산/위협 실시간 뷰, AI CoT 3줄 요약 카드, 원클릭 조치 UX.

---

## 🏗 Directory & Monorepo Structure

uv workspace 모노레포. **MVP는 단일 FastAPI 백엔드(`apps/core-api`)** 로 통합 운영하며, 서비스 물리 분리(Lambda/Step Functions)는 Post-MVP로 미룬다.

```text
vigilantis/
├── docker-compose.yml         # 로컬 개발: api + db + migrate + localstack (+ adminer: --profile tools)
├── apps/
│   ├── web/                   # [유건희·FE] Next.js 16 + React 19 + Shadcn 대시보드
│   └── core-api/              # [안성일·BE/AI · 김세혁·Infra] 단일 FastAPI 백엔드
│       ├── db/                #   [안성일] PostgreSQL ORM 모델·세션·migrations
│       ├── routers/           #   [안성일] 3대 API 계약 구현 (assets/incidents/actions)
│       ├── services/          #   [김세혁/김승철] Boto3 AWS 실행·원복(aws) · 수집·Rule Engine·스케줄러
│       ├── ai/                #   [안성일/김세혁] GPT-4o Agent + 4단계 Guardrail + Whitelist 10종
│       └── security/          #   [김세혁] 위협 선제 차단 / 원클릭 해제 (SOAR)
├── packages/
│   ├── schemas/               # [공통] Pydantic DTO — api/(FE↔BE 공개 계약) + 내부 계약
│   │                          #   (assets·events·guardrails·runbooks·runbook_parameters·precheck 등)
│   ├── telemetry/             # (Post-MVP 자리표시자) OpenTelemetry 셋업
│   └── iac/                   # (Post-MVP 자리표시자) Terraform Core
├── scripts/                   # [김세혁] seed_localstack.py(시드 단일 원천) · probe_dryrun.py(DryRun 지원 실측)
├── datasets/golden/           # [박지현] Golden Dataset 테스트 정답지 (*.json)
├── tests/                     # [박지현] pytest 회귀·E2E 시나리오 테스트
└── docs/                      # PROJECT_STATUS.md (SSOT) · E2E_DEMO_SCENARIOS.md · adr/ (의사결정 기록)
```

### 로컬 실행

```bash
cp .env.example .env                 # 값 채우기 (OPENAI_API_KEY, AWS_* 등)
docker compose up                    # api(:8000) + db(:${POSTGRES_PORT:-5432}) + localstack(:4566) [+ migrate 1회]
docker compose --profile tools up    # ↑ + adminer(:${ADMINER_PORT:-8080}) — 선택 DB 웹 UI
uv sync                              # (호스트 개발 시) 워크스페이스 의존성 동기화
```

---

## 🔀 Modified Git-Flow

```
main (Production / Stable)
  ▲  PR & CI Pass + 1인 이상 Code Review 승인
dev (Integration)
  ├── feat/BE-<n>-<desc>      [안성일]  core-api 라우터/DB
  ├── feat/AI-<n>-<desc>      [안성일]  4단계 가드레일
  ├── feat/DATA-<n>-<desc>    [김승철]  수집/Rule Engine
  ├── feat/SEC-<n>-<desc>     [김세혁]  soar/차단
  ├── chore/INFRA-<n>-<desc>  [김세혁]  Docker/CI
  ├── feat/FE-<n>-<desc>      [유건희]  web 대시보드
  └── docs/DOCS-<n>-<desc>    [박지현]  문서/데이터셋
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

### Pull Request & Code Review

1. `feat/*` 등 작업 브랜치에서 **`dev`로 PR** 제출 (`main` 직접 PR 금지).
2. 최소 1명 이상(특히 백엔드↔AI↔프론트 API 접점 담당자)의 Approve 후 Merge.
3. GitHub Actions CI 통과 필수 — `test` 잡(pytest + PostgreSQL·LocalStack service container)과 `web` 잡(ESLint · `next build` · `node --test`) 양쪽.
4. **이슈 자동 CLOSE 금지**: 커밋 푸터·PR 본문에 `Closes`·`Fixes`·`Resolves`를 쓰지 않고 **`Refs #N`으로만** 연결한다. 기본 브랜치가 `dev`라 그 키워드는 머지 즉시 이슈를 닫아 잔여 작업을 가린다 — **CLOSE는 머지 책임자가 머지 후 직접 판단해 수동으로** 한다.

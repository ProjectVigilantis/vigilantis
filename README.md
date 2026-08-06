# Vigilantis 🛡️

> **24/7 클라우드 자산·보안 상시 관제 및 4단계 AI 가드레일 기반 원클릭 자율 조치·자동 롤백(Auto-Rollback) DevSecOps 파이프라인**

---

## 📌 Project Overview

* **팀명**: 서버룸 난방공사
* **개발 배경**: Multi-Account/Region 환경 확산에 따른 인프라 파편화와 초단위 보안 위협에 대응하고, AI 자동화 도입 시 발생하는 환각(Hallucination) 및 과도한 권한 실행(Excessive Agency) 위험을 해결하기 위해 구축되었습니다.
* **핵심 가치**:
  * **Observability**: 24/7 365일 상시 인프라 관제 및 Terraform IaC 기반 Drift 감지
  * **Safety & Resilience**: Runbook ID 기반 실행 제어 및 4단계 Execution Guardrail + 양방향 Auto-Rollback Engine
  * **Actionability**: 대시보드 내 One-Click 실행 및 Dual-Path State Sync (GitOps & Boto3)
  * **Transparency**: Evidence ID 기반 Decision Trace 및 OpenTelemetry 전 구간 Tracing

---

## 👥 Team & Roles

| 이름 | 역할 | 담당 영역 |
| :--- | :--- | :--- |
| **김세혁 (팀장)** | PM / SecOps Specialist | 0.5초 Pre-Mitigation Lambda, GuardDuty/EventBridge 파이프라인 |
| **김승철** | Cloud Architect | Step Functions/ECS Fargate 분산 스캔, Terraform IaC, tfstate 관리 |
| **박지현** | Backend Engineer | FastAPI Core API, Dual-Path (GitOps PR / Boto3) 실행 엔진, State Sync |
| **안성일** | AI System Engineer | LangGraph Multi-Agent, 4단계 Execution Guardrail, Golden Dataset Evals |
| **유건희** | Frontend Engineer | Next.js 14 대시보드, CoT Timeline 시각화, 헬스 스코어 Gauge Bar |

---

## 🛠 Tech Stack

* **Frontend**: Next.js 14 (App Router), TypeScript, Shadcn UI, Tailwind CSS, Recharts
* **Backend**: FastAPI (Python 3.11+), Boto3, PostgreSQL, Redis, OpenTelemetry (W3C Trace Context)
* **AI & Safety**: LangGraph, OpenAI GPT-4o, Pydantic v2, Pytest (Golden Dataset Evals)
* **Infra & Security**: AWS Step Functions, ECS Fargate, Lambda, EventBridge, GuardDuty, Terraform

---

## ✨ Key Features

1. **24/7 자산 관제 & Terraform Drift 감지**: Terraform `plan/show` JSON 파싱을 통해 코드로 정의된 상태(.tfstate)와 실제 AWS 리소스 간의 무단 변경을 100% 식별.
2. **0.5초 초단위 선제 차단 & 3단계 위협 대응**: High Risk 발생 시 Lambda 기반 즉시 차단, Medium Risk 시 Agentic AI 가이드 제공 및 1분 타임아웃 격리.
3. **Capability-Restricted AI & 4단계 Guardrail**: LLM 권한을 사전 등록된 Runbook ID 추천으로 제한하고, `Input Sanitization ➔ Schema ➔ Whitelist ➔ ARN Match ➔ Dry-Run` 다층 필터로 RCE 차단.
4. **Actionable One-Click & 양방향 Auto-Rollback**: 스펙 다운사이징 후 기동 실패(Status Check Fail) 시 이전 스냅샷으로 자동 원복 및 긴급 조치 후 `terraform import/refresh` 상태 맞춤.
5. **Evidence 기반 Decision Trace & OpenTelemetry**: raw CoT 노출을 지양하고 Evidence ID 기반 감사 증거 제공 및 전 구간 `trace_id` 관통.

---

## 🏗 Directory & Monorepo Structure

```text
vigilantis/
├── .github/                 # GitHub Actions (PR 자동검증, CODEOWNERS)
├── docs/
│   └── adr/                 # [공통] Architecture Decision Records (OCSF, Cedar 등 판단 기록)
├── apps/
│   ├── web/                 # [유건희 - FE] Next.js 14, Shadcn, Recharts
│   ├── core-api/            # [박지현 - BE] FastAPI, GitOps, Boto3 Engine
│   ├── ai-engine/           # [안성일 - AI] LangGraph, GPT-4o, Guardrail, Evals
│   ├── scan-worker/         # [김승철 - Infra] Step Functions/Fargate Scanner
│   └── security-soar/       # [김세혁 - SecOps] EventBridge/Lambda 0.5초 차단
├── packages/
│   ├── schemas/             # [공통] Pydantic Models (Guardrail, Runbook Schema)
│   ├── telemetry/           # [공통] OpenTelemetry W3C Trace Context Setup
│   └── iac/                 # [김승철 - Infra] Terraform Core Code & tfstate
├── docker-compose.yml
<<<<<<< HEAD
└── README.md
=======
└── README.md
```

---

## Modified Git-Flow

```
main (Production / Stable)
  ▲
  │  (PR & CI/CD Pass + 1인 이상 Code Review 승인)
dev (Integration Test Branch)
  ▲
  ├── feat/web/dashboard-cot         [유건희 - FE]
  ├── feat/core/gitops-pr-engine     [박지현 - BE]
  ├── feat/ai/4step-guardrail        [안성일 - AI]
  ├── feat/infra/fargate-scanner     [김승철 - Infra]
  └── feat/sec/lambda-pre-mitigation [김세혁 - SecOps]
```

---

## Branch Naming & Commit Convention 규칙

```
[Type] #이슈번호 - 한 줄 설명

예시:
[FEAT] #12 - 4단계 Guardrail 엔진 중 Action Whitelist 필터 구현
[FIX] #45 - EC2 Status Check 실패 시 자동 롤백 타임아웃 예외 처리
```

- [FEAT] : 새로운 기능 추가

- [FIX] : 버그 수정

- [REFACTOR] : 코드 리팩토링 (기능 변경 없음)

- [CHORE] : 빌드 업무, 패키지 매니저, CI/CD 설정 변경

- [DOCS] : 문서 수정 (README 등)

---

## Pull Request (PR) & Code Review 규칙

1. `feat/*` 브랜치에서 작업 후 dev 브랜치로 PR 제출.

2. 최소 1명 이상(특히 백엔드↔AI↔프론트 간 API 접점 담당자)의 Code Review 및 승인(Approve)을 받아야 Merge 가능.

3. CI/CD Pipeline (GitHub Actions)에서 Linting & Pydantic Schema Validation Test가 통과해야 함.
>>>>>>> f4ffbf52b6f7507d29ba991433e8280b59c97bda

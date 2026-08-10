# Vigilantis 🛡️

> **24/7 클라우드 자산·보안 상시 관제 및 4단계 AI 가드레일 기반 Agentic AI 원클릭 자율 대응 FinSecOps 시스템**

---

## 📌 Project Overview

* **팀명**: 딸깍 인프라
* **개발 배경**: Multi-Account/Region 환경 확산에 따른 인프라 파편화와 초단위 보안 위협에 대응하고, AI 자동화 도입 시 발생하는 환각(Hallucination) 및 과도한 권한 실행(Excessive Agency) 위험을 해결하기 위해 구축되었습니다.
* **MVP 범위**: AWS EC2·Security Group 중심. RDS·S3는 Post-MVP 확장 범위이며, GCP·Azure는 Phase 3 로드맵에서 다룹니다.
* **핵심 가치**:
  * **Observability**: 24/7 365일 상시 인프라 관제 및 Terraform IaC 기반 Drift 감지
  * **Safety & Resilience**: Runbook ID 기반 실행 제어, Input Sanitization과 4단계 Execution Guardrail, 자산 자동 원복·보안 원클릭 해제로 구성된 양방향 회복 엔진
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
| **유건희** | Frontend Engineer | Next.js 14 대시보드, Evidence ID 기반 Decision Trace·판단 근거 요약 타임라인, 헬스 스코어 Gauge Bar |

---

## 🛠 Tech Stack

* **Frontend**: Next.js 14 (App Router), TypeScript, Shadcn UI, Tailwind CSS, Recharts
* **Mobile**: Native App, Push Notifications, REST API
* **Backend**: FastAPI (Python 3.11+), Boto3, PostgreSQL, Redis, OpenTelemetry (W3C Trace Context)
* **AI & Safety**: LangGraph, OpenAI GPT-4o, Pydantic v2, Pytest (Golden Dataset Evals)
* **Infra & Security**: AWS Step Functions, ECS Fargate, Lambda, EventBridge, GuardDuty, Terraform
* **Identity & Access**: OIDC SSO, TOTP/FIDO2 MFA, Admin/Approver/Viewer RBAC
* **Audit & Reporting**: HIS-001 Audit Trail, CSV/JSON 내보내기, 조치 결과 PDF 자동 생성·발송

---

## ✨ Key Features

1. **24/7 자산 관제 & Terraform Drift·FinOps 분석**: MVP 범위인 EC2·Security Group을 상시 관제하고 Terraform `plan/show` JSON 파싱으로 코드 상태(.tfstate)와 실제 AWS 리소스 간 Drift를 100% 식별. AWS Price List API를 비용 추정의 주 원천으로, Cost Explorer T-2 확정치를 참고·보정용으로 사용.
2. **0.5초 초단위 선제 차단 & 3단계 위협 대응**: High Risk 발생 시 Lambda 기반 즉시 차단하고, Medium Risk에는 Agentic AI 가이드와 관제자 승인 흐름을 제공하며 1분 미응답 시 자동 격리. CloudTrail S3 로그로 사후 재검증하여 차단 유지 또는 관제자 원클릭 해제로 전환. 보안 이벤트와 상태 변경은 Native App Push로 알림.
3. **Capability-Restricted AI & 4단계 Guardrail**: LLM 권한을 사전 등록된 Runbook ID 추천으로 제한하고, 입력 측 `Input Sanitization` 후 `Schema ➔ Action Whitelist ➔ ARN Matching ➔ AWS Dry-Run`의 4단계 출력 검증으로 RCE 차단.
4. **Actionable One-Click & 양방향 회복 엔진**: 웹 대시보드의 AI 제안과 Native App의 보안 대응 요청에 One-Click 실행 흐름을 적용하고, Idempotency Key로 중복 실행을 방지. Production 자원은 조직·Scope별 정족수(기본 2인)를 적용. 승인 요청은 만료시키지 않으며 실행 직전 스펙 해시를 재검증하고, 자산 Post-Check 실패 시 이전 스냅샷으로 자동 원복. 긴급 Boto3 조치 후 `terraform import/refresh`로 상태 동기화.
5. **Evidence 기반 Decision Trace & OpenTelemetry**: raw CoT 노출을 지양하고 Evidence ID 기반 감사 증거와 전 구간 `trace_id`, LLM 토큰·지연 시간 기록을 제공. HIS-001에서 생애주기 Audit Trail을 조회하고 CSV/JSON으로 내보내며, 조치 결과 PDF를 자동 생성·발송.
6. **Enterprise Identity & Access Control**: OIDC SSO와 TOTP/FIDO2 MFA로 인증하고 Admin/Approver/Viewer RBAC로 조회·승인·관리 권한을 분리.

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
│   │   ├── __init__.py      # 외부 모듈(core-api, ai-engine 등)로 노출할 스키마 Export
│   │   ├── pyproject.toml   # Pydantic v2 라이브러리 의존성 정의
│   │   ├── assets.py        # 자산 메타데이터 & Terraform Drift 스키마
│   │   ├── events.py        # GuardDuty/CloudTrail 위협 이벤트 스키마
│   │   ├── guardrails.py    # 4단계 Guardrail 요청/응답 DTO
│   │   ├── runbooks.py      # Runbook 실행 매개변수 스키마
│   │   └── tests/           # 스키마 직렬화/검증 단위 테스트
│   │       └── test_schemas.py
│   ├── telemetry/           # [공통] OpenTelemetry W3C Trace Context Setup
│   └── iac/                 # [김승철 - Infra] Terraform Core Code & tfstate
├── docker-compose.yml
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

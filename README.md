## Directory & Monorepo 구조

```
vigilantis/
├── .github/                  # GitHub Actions CI/CD (PR 자동검증, Lint)
├── .gitignore
├── apps/
│   ├── web/                  # [유건희] Frontend (Next.js 14, Shadcn, Recharts)
│   ├── core-api/             # [박지현] Core Backend (FastAPI, GitOps, Boto3 Engine)
│   ├── ai-engine/            # [안성일] AI Agent (LangGraph, GPT-4o, 4단계 Guardrail)
│   └── scan-worker/          # [김승철] Ingestion Worker (Step Functions/Fargate)
├── packages/
│   ├── schemas/              # [공통] Pydantic Data Models (CoT, Guardrail Schema)
│   └── iac/                  # [김승철] Terraform Core Code & tfstate Management
├── security-soar/            # [김세혁] GuardDuty/EventBridge/Lambda 0.5초 차단 스크립트
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

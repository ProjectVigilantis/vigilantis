# Vigilantis 프로젝트 현황 (PROJECT STATUS — SSOT)

> **이 문서가 프로젝트 범위·확정 결정·역할의 단일 기준(Single Source of Truth)이다.**
> 다른 문서(README, 기획서, MVP 범위 명세 등)와 충돌하면 **이 문서가 이긴다.**
> 범위·API 계약·역할이 바뀌는 PR은 이 문서 갱신을 포함할 것.
>
> **최종 갱신**: 2026-08-13 (유건희)

---

## 한 줄 요약

24/7 AWS 자산·보안 상시 관제 + 4단계 AI 가드레일 기반 원클릭 자율 조치 + 양방향 회복(자동 원복/원클릭 해제)을 제공하는 FinSecOps 플랫폼. **1차 발표(10/15) MVP 시연**이 목표다.

## 현재 위치 (2026-08-12 기준)

- **마일스톤**: 1~2주차(8/11~8/23) — 시스템 설계 & 개발 환경 구축 단계.
- **완료**: 모노레포 단일 백엔드 재편([ADR-0001](adr/0001-mvp-monorepo-structure.md)), docker-compose, **런북 명세서(Action Whitelist) 확정**([ADR-0002](adr/0002-runbook-whitelist-mvp-scope.md)), 시스템 흐름도 MVP 기준 갱신.
- **진행 중**: DB 스키마 설계(안성일), API 계약 확정, Golden Dataset 1차(박지현), FE 와이어프레임(유건희).

## MVP 확정 범위

- **관제**: AWS 단일 계정 / 1~2개 리전. **EC2·SG 중심** + 런북 조치 대상 리소스(NACL, EBS, ASG·Launch Template, ALB Target Group).
- **위협**: OpenIP(0.0.0.0/0)·SSH 브루트포스 — Golden Dataset 기반 **모의(Mock) 주입** (실환경 GuardDuty 연동은 Post-MVP).
- **AI**: OpenAI GPT-4o + Pydantic v2 Structured Output. CoT 3줄 요약 + Runbook ID 추천. (LangGraph는 MVP 미사용)
- **4단계 가드레일(순서 고정)**: ① Schema Check ➔ ② Action Whitelist ➔ ③ ARN Match ➔ ④ AWS Dry-Run.
- **양방향 회복**: 자산 = 스펙 JSON 백업 ➔ `get_waiter` Status Check(2/2) ➔ 자동 원복 / 보안 = 선제 차단 ➔ 관제자 [원클릭 해제].
- **3단계 위험 대응**: High `PRE_MITIGATION_0_5S`(0.5초 선차단 시뮬레이션) / Medium·Low `AGENT_WAIT`(승인 대기) / **1분 미응답 `TIMEOUT_ISOLATION_1M`(자동 격리)**.
- **FE**: Next.js 16 + Shadcn UI, 위협 토폴로지 맵(붉은색 노드), One-Click + Idempotency Key.
- **아키텍처**: 단일 FastAPI 백엔드(`apps/core-api`) + PostgreSQL + APScheduler.

### Action Whitelist — 런북 7종 (전부 MVP, 확정본: `vigilantis-docs/런북 명세서.md`)

| 분류 | Runbook ID | 위험도 / 승인 |
| --- | --- | --- |
| SecOps | `RUNBOOK_EC2_ISOLATE` | High / 0.5초 선차단·1분 타임아웃 |
| SecOps | `RUNBOOK_NACL_ADD_DENY` | Medium / 관제자 승인 |
| SecOps | `RUNBOOK_NACL_RESTORE` | Low / 관제자 승인 (원클릭 해제) |
| SecOps | `RUNBOOK_SG_DELETE_ISOLATED` | Medium / 관제자 승인 |
| FinOps | `RUNBOOK_EC2_RIGHTSIZING` | Medium / 관제자 승인 (자동 원복 시연 대상) |
| FinOps | `RUNBOOK_EC2_ENABLE_AUTOSCALING` | Medium / 관제자 승인 (stateless 한정 구조 전환) |
| FinOps | `RUNBOOK_EBS_DELETE_UNATTACHED` | Low / 관제자 승인 |

## 확정 결정 로그

| 날짜 | 결정 | 기록 |
| --- | --- | --- |
| 2026-08-11 | 단일 FastAPI 백엔드(`core-api`)로 모노레포 통합 | [ADR-0001](adr/0001-mvp-monorepo-structure.md) |
| 2026-08-12 | **런북 명세서 7종 전부 MVP 확정** — 런북 명세서.md = Action Whitelist 확정본 | [ADR-0002](adr/0002-runbook-whitelist-mvp-scope.md) |
| 2026-08-12 | 다운사이징 백업 = **스펙 JSON**(`SAVE_INSTANCE_SPEC_JSON`) 주 방식, EBS 스냅샷은 선택 보조 | 런북 명세서.md |
| 2026-08-12 | 관제자 미응답 타임아웃 **1분**(`TIMEOUT_ISOLATION_1M`)으로 통일 (3분 폐기) | 런북 명세서.md |
| 2026-08-12 | 구 Whitelist 예시(`RUNBOOK_EC2_DOWNSIZE`, `RUNBOOK_IP_BLOCK`) 폐기 | 런북 명세서.md |
| 2026-08-13 | **FE 스택 Next.js 14 → 16 상향** — 14 라인은 14.2.35에서 동결, 로컬 Node 26 지원 범위 밖, shadcn CLI 4.x가 Tailwind v4/Next 15+ 기준. `apps/web` = Next 16.3.0 + React 19 + Tailwind v4 + shadcn(radix-nova) | [ADR-0003](adr/0003-fe-stack-nextjs-16.md) |

## API 계약 (최우선 확정 대상 — FE↔BE Mock 병렬 개발 기준)

- `GET /api/v1/assets` — EC2/SG 상태·스펙·연결관계·헬스 스코어·Skip 사유 코드
- `GET /api/v1/incidents/{id}` — AI CoT 3줄 요약, Evidence ID, 추천 Runbook ID
- `POST /api/v1/actions/execute`
  - Request: `{ incident_id, runbook_id, idempotency_key }`
  - Response status: `SUCCESS | FAILED | IN_PROGRESS | ROLLBACK_INITIATED`

## 팀 & 역할 (최신 — README·기획서의 역할 표는 구버전)

| 이름 | 역할 | 담당 |
| --- | --- | --- |
| **김세혁** (팀장/PM) | Infra & DevSecOps | Boto3 EC2/SG 제어, 스펙 JSON 백업/자동 원복 엔진, Docker·배포, Code Review·브랜치 관리 |
| **안성일** | AI / Guardrail · Architect | 아키텍처·DB 스키마, FastAPI 메인·PostgreSQL, GPT-4o + 4단계 가드레일 |
| **김승철** | Data & Rule Engine | CloudWatch 수집 파이프라인, Rule Evaluator(Idle EC2·미사용 SG, Skip 사유 적재) |
| **박지현** | QA & Scenario / Technical Writer | Golden Dataset(약 20건), E2E 시연 시나리오, pytest, 문서화 |
| **유건희** | Frontend | Next.js + Shadcn 대시보드, REST/WebSocket 연동, 차트·토폴로지 시각화 |

## 미해결 이슈 / 할 일 (블로커 순)

1. **롤백 런북 3종 미등록** — `RUNBOOK_EC2_UNISOLATE`·`RUNBOOK_SG_RECREATE`·`RUNBOOK_EC2_REVERT_SIZE`가 `rollback_runbook_id`로 참조만 되고 Whitelist에 없음 → 이대로 구현하면 **자동 원복이 가드레일에 차단됨**. 명세 추가 또는 "롤백은 Whitelist 검증 우회" 정책 명문화 필요(김세혁·안성일).
2. **API 계약 3종 스키마 확정** (안성일·유건희) — FE 병렬 개발의 전제.
3. **코드 스텁 주석 갱신** — `apps/core-api/ai/whitelist.py`·`services/aws/executor.py`의 구버전 런북 2종 주석 → 확정 7종으로.
4. **RIGHTSIZING 트리거 문구** — "CPU/메모리"에서 메모리는 기본 CloudWatch 미수집(Agent 필요). CPU(/Network) 기준으로 정리.
5. **팀명 확정** — 기획서 "딸깍 인프라" vs README "서버룸 난방공사".
6. README·기획서 역할 표 및 `apps/*` 오너 주석 최신화.

## 일정 리스크 & 권고 컷라인 (제안 — 팀 합의 필요)

런북이 2종 → 7종(+롤백 3종)으로 늘었는데 기간(9주)은 그대로. 전부 실환경 완성도는 위험 → 우선순위 컷라인 제안:

- **P0 (시연 필수, 3~5주차 내)**: `RIGHTSIZING`+`REVERT_SIZE`(자산 자동 원복), `NACL_ADD_DENY`+`NACL_RESTORE`(차단→원클릭 해제). 이 4개로 "양방향 회복" 스토리 완성.
- **P1**: `SG_DELETE_ISOLATED`, `EBS_DELETE_UNATTACHED` — 난도 낮음, P0 후 순차.
- **P2 (mock/영상 대체 허용)**: `EC2_ISOLATE`(ALB 시연 인프라 필요), `ENABLE_AUTOSCALING`(구현량 최대). **9/13까지 P0 미완이면 P2 자동 컷.**

## 문서 지도 (신뢰 우선순위 — 충돌 시 위가 이김)

1. **`docs/PROJECT_STATUS.md`** (이 문서) — 범위·결정·역할·현황
2. `vigilantis-docs/런북 명세서.md` — Action Whitelist 확정 규격 (7종)
3. `vigilantis-docs/시스템 흐름도.md` — MVP 아키텍처·파이프라인
4. `docs/adr/` — 결정 배경(왜 그렇게 했나)
5. `vigilantis-docs/1차 발표까지의 마일스톤 및 MVP 범위 명세.md` — 주차별 마일스톤 (※ 범위 서술 일부 구버전: EC2·SG 한정, 런북 2종 예시)
6. `README.md` — 프로젝트 소개용 (※ 역할 표·오너 주석 구버전)
7. `vigilantis-docs/기획서/*.docx` — **풀비전 비전 문서(동결)**. 구현 기준 아님.

> ※ `vigilantis-docs/`는 현재 저장소 밖 폴더. 팀 공유가 필요하면 repo `docs/`로 이전 검토.

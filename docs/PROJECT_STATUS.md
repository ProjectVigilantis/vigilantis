# Vigilantis 프로젝트 현황 (PROJECT STATUS — SSOT)

> **이 문서가 프로젝트 범위·확정 결정·역할의 단일 기준(Single Source of Truth)이다.**
> 다른 문서(README, 기획서, MVP 범위 명세 등)와 충돌하면 **이 문서가 이긴다.**
> 범위·API 계약·역할이 바뀌는 PR은 이 문서 갱신을 포함할 것.
>
> **최종 갱신**: 2026-08-13 (김세혁)

---

## 한 줄 요약

24/7 AWS 자산·보안 상시 관제 + 4단계 AI 가드레일 기반 원클릭 자율 조치 + 양방향 회복(자동 원복/원클릭 해제)을 제공하는 FinSecOps 플랫폼. **1차 발표(10/15) MVP 시연**이 목표다.

## 현재 위치 (2026-08-13 기준)

- **마일스톤**: 1~2주차(8/11~8/23) — 시스템 설계 & 개발 환경 구축 단계.
- **완료**: 모노레포 단일 백엔드 재편([ADR-0001](adr/0001-mvp-monorepo-structure.md)), docker-compose, **런북 명세서(Action Whitelist) 10종 확정**([ADR-0002](adr/0002-runbook-whitelist-mvp-scope.md) + 롤백 3종), 시스템 흐름도 MVP 기준 갱신, 자산 수집·Rule Engine 1차(PR #22), CI 가동(GitHub Actions pytest, PR #28), EC2/SG raw 수집 테스트(PR #29), FE Next.js 16 스캐폴딩(PR #30, [ADR-0003](adr/0003-fe-stack-nextjs-16.md)).
- **진행 중**: DB 스키마 설계(안성일), API 계약 확정(최우선), Golden Dataset 1차(박지현), LocalStack 팀 표준 환경 전략(김세혁).

## MVP 확정 범위

- **관제**: AWS 단일 계정 / 1~2개 리전. **EC2·SG 중심** + 런북 조치 대상 리소스(NACL, EBS, ASG·Launch Template, ALB Target Group).
- **위협**: OpenIP(0.0.0.0/0)·SSH 브루트포스 — Golden Dataset 기반 **모의(Mock) 주입** (실환경 GuardDuty 연동은 Post-MVP).
- **AI**: OpenAI GPT-4o + Pydantic v2 Structured Output + **LangGraph 오케스트레이션**. CoT 3줄 요약 + Runbook ID 추천. LangGraph는 프로젝트 정체성으로 MVP 구현 확정(2026-08-13) — 출력 계약(Pydantic 스키마)은 동일하게 유지.
- **4단계 가드레일(순서 고정)**: ① Schema Check ➔ ② Action Whitelist ➔ ③ ARN Match ➔ ④ AWS Dry-Run.
- **양방향 회복**: 자산 = 스펙 JSON 백업 ➔ `get_waiter` Status Check(2/2) ➔ 자동 원복 / 보안 = 선제 차단 ➔ 관제자 [원클릭 해제].
- **3단계 위험 대응**: High `PRE_MITIGATION_0_5S`(0.5초 선차단 시뮬레이션) / Medium·Low `AGENT_WAIT`(승인 대기) / **1분 미응답 `TIMEOUT_ISOLATION_1M`(자동 격리)**.
- **FE**: Next.js 16 + Shadcn UI, 위협 토폴로지 맵(붉은색 노드), One-Click + Idempotency Key.
- **아키텍처**: 단일 FastAPI 백엔드(`apps/core-api`) + PostgreSQL + APScheduler.

### Action Whitelist — 런북 10종 = 본편 7 + 롤백 3 (전부 MVP, 확정본: `vigilantis-docs/런북 명세서.md`)

| 분류 | Runbook ID | 위험도 / 승인 |
| --- | --- | --- |
| SecOps | `RUNBOOK_EC2_ISOLATE` | High / 0.5초 선차단·1분 타임아웃 |
| SecOps | `RUNBOOK_NACL_ADD_DENY` | Medium / 관제자 승인 |
| SecOps | `RUNBOOK_NACL_RESTORE` | Low / 관제자 승인 (원클릭 해제) |
| SecOps | `RUNBOOK_SG_DELETE_ISOLATED` | Medium / 관제자 승인 |
| FinOps | `RUNBOOK_EC2_RIGHTSIZING` | Medium / 관제자 승인 (자동 원복 시연 대상) |
| FinOps | `RUNBOOK_EC2_ENABLE_AUTOSCALING` | Medium / 관제자 승인 (stateless 한정 구조 전환) |
| FinOps | `RUNBOOK_EBS_DELETE_UNATTACHED` | Low / 관제자 승인 |
| SecOps (롤백) | `RUNBOOK_EC2_UNISOLATE` | Medium / 관제자 승인 (원클릭 해제) · AI 추천 불가 |
| SecOps (롤백) | `RUNBOOK_SG_RECREATE` | Low / 관제자 승인 · AI 추천 불가 |
| FinOps (롤백) | `RUNBOOK_EC2_REVERT_SIZE` | High / 시스템 자동 발동 (Status Check 실패 시) · AI 추천 불가 |

**롤백 런북 공통 정책 (2026-08-13 확정)**: ① Whitelist 정식 등록 — 가드레일 우회 경로 없음. ② `ai_recommendable: false` — AI 추천 목록에서 제외, 트리거는 시스템/관제자만. ③ 원복 파라미터는 DB 백업 레코드(`backup_record_id`)에서만 로드. ④ 롤백이 가드레일에서 거절되면 자동 재시도 없이 CRITICAL 알림 + 수동 개입.

## 확정 결정 로그

| 날짜 | 결정 | 기록 |
| --- | --- | --- |
| 2026-08-11 | 단일 FastAPI 백엔드(`core-api`)로 모노레포 통합 | [ADR-0001](adr/0001-mvp-monorepo-structure.md) |
| 2026-08-12 | **런북 명세서 7종 전부 MVP 확정** — 런북 명세서.md = Action Whitelist 확정본 | [ADR-0002](adr/0002-runbook-whitelist-mvp-scope.md) |
| 2026-08-12 | 다운사이징 백업 = **스펙 JSON**(`SAVE_INSTANCE_SPEC_JSON`) 주 방식, EBS 스냅샷은 선택 보조 | 런북 명세서.md |
| 2026-08-12 | 관제자 미응답 타임아웃 **1분**(`TIMEOUT_ISOLATION_1M`)으로 통일 (3분 폐기) | 런북 명세서.md |
| 2026-08-12 | 구 Whitelist 예시(`RUNBOOK_EC2_DOWNSIZE`, `RUNBOOK_IP_BLOCK`) 폐기 | 런북 명세서.md |
| 2026-08-13 | **FE 스택 Next.js 14 → 16 상향** — 14 라인은 14.2.35에서 동결, 로컬 Node 26 지원 범위 밖, shadcn CLI 4.x가 Tailwind v4/Next 15+ 기준. `apps/web` = Next 16.3.0 + React 19 + Tailwind v4 + shadcn(radix-nova) | [ADR-0003](adr/0003-fe-stack-nextjs-16.md) |
| 2026-08-13 | **개발 환경 = LocalStack, 발표 직전 실 AWS 전환** — `AWS_ENDPOINT_URL` 유무로 전환. 팀 표준 환경(compose·시드·env) 구성은 전략 수립 후 진행 | (전략 문서 예정) |
| 2026-08-13 | **롤백 런북 3종 Whitelist 정식 등록(7→10종)** — 우회 정책 기각, `ai_recommendable: false`·백업 레코드 기반 복원·가드레일 실패 시 수동 개입 정책 채택 (미해결 #1 해소) | [ADR-0004](adr/0004-rollback-runbook-whitelist-registration.md) |
| 2026-08-13 | **팀명 = "딸깍 인프라" 확정** — README의 "서버룸 난방공사" 표기는 구버전(갱신 필요) | — |
| 2026-08-13 | **런북 10종 전부 실구현 방침** — mock/영상 대체 컷라인 기각(팀장 결정). P0/P1/P2는 착수 순서로만 운용, 9/13은 중간 점검 시점 | 본 문서 §일정 리스크 |
| 2026-08-13 | **LangGraph MVP 도입 확정** — 프로젝트 정체성 사유(팀장 결정, "미확정" 상태 종료). AI 파이프라인을 LangGraph 그래프로 구현하되 GPT-4o + Pydantic Structured Output 출력 계약은 불변. 그래프 설계는 안성일 주관(ADR 후보) | 본 문서 §MVP 확정 범위 |

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

1. **API 계약 3종 스키마 확정** (안성일·유건희) — FE 병렬 개발의 전제.
2. **LocalStack 팀 표준 환경** (김세혁) — docker-compose `localstack` 서비스 + EC2/SG 시드 스크립트 + `.env.example`. 현재는 개인 로컬 환경에만 존재해 수집 테스트를 타 팀원이 재현 불가. 6~7주차 실 AWS 스모크 테스트 일정 포함해 전략 수립 중.
3. **PR #29 후속 보완** (김승철) — 머지된 raw 수집 테스트가 `collector.py`를 호출하지 않고 자체 boto3 로직 사용(`_open_to_world` 중복 구현), LocalStack 시드 없으면 빈 결과로 통과. 실제 collector 경로를 검증하도록 재작성 필요.
4. **코드 스텁 주석 갱신** — `apps/core-api/ai/whitelist.py`·`services/aws/executor.py`의 구버전 런북 2종 주석 → 확정 10종으로.
5. README·기획서의 역할 표, 팀명("서버룸 난방공사" → **"딸깍 인프라"**) 및 `apps/*` 오너 주석 최신화.

## 일정 리스크 & 구현 우선순위 (2026-08-13 방침 확정)

**런북 10종 전부 실구현이 원칙이다** — mock/영상 대체를 전제한 컷라인("P2 자동 컷")은 채택하지 않는다(팀장 결정). P0/P1/P2는 범위 축소선이 아니라 **구현 착수 순서**로만 사용한다.

- **P0 (최우선 착수, 3~5주차)**: `RIGHTSIZING`+`REVERT_SIZE`(자산 자동 원복), `NACL_ADD_DENY`+`NACL_RESTORE`(차단→원클릭 해제) — "양방향 회복" 스토리의 골격.
- **P1 (P0 후 순차)**: `SG_DELETE_ISOLATED`(+`SG_RECREATE`), `EBS_DELETE_UNATTACHED` — 난도 낮음.
- **P2 (조기 준비 병행)**: `EC2_ISOLATE`(+`UNISOLATE`)는 ALB·다중 EC2 시연 인프라가 선행 조건 → 인프라 준비를 앞당긴다. `ENABLE_AUTOSCALING`은 구현량 최대 → 설계 선행.
- **9/13 중간 점검**: P0 4종 실동작 여부 점검. 미달 시 범위 축소가 아니라 **인력 재배치·범위 외 작업 중단**으로 대응한다.

## 문서 지도 (신뢰 우선순위 — 충돌 시 위가 이김)

1. **`docs/PROJECT_STATUS.md`** (이 문서) — 범위·결정·역할·현황
2. `vigilantis-docs/런북 명세서.md` — Action Whitelist 확정 규격 (10종: 본편 7 + 롤백 3)
3. `vigilantis-docs/시스템 흐름도.md` — MVP 아키텍처·파이프라인
4. `docs/adr/` — 결정 배경(왜 그렇게 했나)
5. `vigilantis-docs/1차 발표까지의 마일스톤 및 MVP 범위 명세.md` — 주차별 마일스톤 (※ 범위 서술 일부 구버전: EC2·SG 한정, 런북 2종 예시)
6. `README.md` — 프로젝트 소개용 (※ 역할 표·오너 주석 구버전)
7. `vigilantis-docs/기획서/*.docx` — **풀비전 비전 문서(동결)**. 구현 기준 아님.

> ※ `vigilantis-docs/`는 작업 폴더 내 **로컬 전용 문서**(`.gitignore` 등록, GitHub 미공유). 팀이 참조할 확정 결정은 `docs/PROJECT_STATUS.md`와 `docs/adr/`에 기록한다.

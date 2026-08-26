# Vigilantis 프로젝트 현황 (PROJECT STATUS — SSOT)

> **이 문서가 프로젝트 범위·확정 결정·역할의 단일 기준(Single Source of Truth)이다.**
> 다른 문서(README, 기획서, MVP 범위 명세 등)와 충돌하면 **이 문서가 이긴다.**
> 범위·API 계약·역할이 바뀌는 PR은 이 문서 갱신을 포함할 것.
>
> **최종 갱신**: 2026-08-26 (김세혁)

---

## 한 줄 요약

24/7 AWS 자산·보안 상시 관제 + 4단계 AI 가드레일 기반 원클릭 자율 조치 + 양방향 회복(자동 원복/원클릭 해제)을 제공하는 FinSecOps 플랫폼. **1차 발표(10/15) MVP 시연**이 목표다.

## 현재 위치 (2026-08-26 기준)

> 이 섹션은 ⓐ 주차 전환 ⓑ 범위·API 계약·역할·확정 결정 변경 ⓒ 미해결 이슈 상태 변경 시에만 갱신한다.
> **개별 머지 이력은 이 문서에 복제하지 않는다** — 상세 원천은 [dev 머지 PR 목록](https://github.com/ProjectVigilantis/vigilantis/pulls?q=is%3Apr+is%3Amerged+base%3Adev), 결정 배경은 아래 §확정 결정 변경 로그와 `docs/adr/`다. 일반 기능 PR은 이 문서를 갱신하지 않는다.

- **마일스톤**: 3주차(8/24–8/30) — 실행 기반 착수: 실행 인터페이스 확정(executor ↔ 가드레일 ④ Dry-Run), 판정 규칙 잔여 결정 종결, CI 검증 공백 해소.
- **완료 (구간 요약)**
  - **1–2주차(8/11–8/23) · 설계와 환경 확정** — 모노레포 단일 백엔드 재편, 런북 10종 Action Whitelist 확정·코드화(AI 추천 분리), FE↔BE API 계약 DTO 확정과 내부 공통 계약 코드화, LangGraph 그래프 구조 확정, LocalStack 팀 표준 환경(compose·시드 스크립트·`.env.example`), FE Next.js 16 스캐폴딩·계약 타입·mock 계층·공통 레이아웃, 자산 수집·Rule Engine 1차와 CI(pytest) 가동. 결정 근거는 [ADR-0001](adr/0001-mvp-monorepo-structure.md)–[ADR-0006](adr/0006-localstack-team-standard-env.md).
  - **2주차(8/18–8/23) · 실행 기반 구축** — DB 저장 계층(ORM 13종·Alembic baseline·Repository)과 collector·rule_engine 재연결, Core API 앱 골격·조회 API 3종·로깅, WebSocket 실시간 상태 전송(`/api/v1/ws`), Golden Dataset 20건(`Verdict` 4종·`SkipReasonCode` 5종 전량 커버, 회귀 21건), CI LocalStack service container, FE shadcn 프리미티브·다크 모드 고정.
  - **3주차(8/24–8/30, 진행 중) · 실행 기반 착수** — 현재까지: CI PostgreSQL service container(#92)로 DB 통합 테스트 28건 상시 skip 해소(미해결 5번 종결), PR 본문 템플릿·리뷰 요청 규칙 코드화(`.github/PULL_REQUEST_TEMPLATE.md`), `_is_prod` 인식 태그 키·값 집합 확정(#95 → PR #97, 미해결 4번 핵심 해소), **executor ↔ 가드레일 ④ Dry-Run 호출 규약 확정([ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) — #113 종결, 판정 기준 ⓐ 충족)**.
- **다음 단계 (담당별 1줄)**
  - **김세혁**: ~~Dry-Run 호출 시그니처를 안성일과 문서로 합의(8/26 목표)~~ ✅ 확정(2026-08-24, ADR-0007 / #113 — 기한 내). ~~Boto3 클라이언트 팩토리·AWS 예외 공통 래퍼~~ ✅ 완료(2026-08-25, #128 / PR #131). ~~executor 런북 디스패치 테이블·`precheck()` 확정 10종(#129)~~ ✅ 완료(2026-08-25, PR #147 — [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) 1차 개정(#133 / PR #157) 선행 머지 후 승인). ~~`scripts/probe_dryrun.py` 편입(#130)~~ ✅ 완료(2026-08-26, #130 — ADR-0007 §6이 머지 조건으로 못 박은 실측 절차. 확정 10종 `target_api` 14개 전수가 §Context 표를 재현하고, 표↔코드 정합은 회귀 테스트가 CI에서 상시 확인). 다음: 스펙 JSON 백업 모듈 → `execute` 본체. 후속으로 런북별 typed 파라미터 계약(#154 — 안성일). CI `apps/web` lint·build 잡 추가(#91), compose 정리(#94·#111).
  - **안성일**: `AIModelClient` 경계(ADR-0005 — 외부 전송 페이로드 마스킹 포함, #115), 가드레일 ①Schema Check ②Action Whitelist(#114), `POST /api/v1/actions/execute` 라우터 골격·Idempotency Key 멱등 처리(#116, 김세혁 공동).
  - **김승철**: ~~`_is_prod` 인식 태그 키·값 집합 확정(#95)~~ ✅ 확정(2026-08-24, PR #97 — 기한 8/27 내 본인 결정, PM 대행 불발동). `evaluate_ec2` `name` 인자 정리(#96 — `PROD_HINTS`는 #97에서 제거돼 잔여 범위 축소), rule_engine update 경로 SKIP→비SKIP 전이 회귀 테스트(#109 — #99 잔여 재발행). ~~자산 연결관계(토폴로지) 산출(#101)~~ ✅ NACL 축 완료(2026-08-25, PR #101 — subnet 연관 기반 EC2→NACL `PROTECTED_BY`); 자산 4종·관계 4종 확장(#149)은 축별 PR로 완료 — EBS·`ATTACHED_TO`(#156), ASG·Launch Template·`MEMBER_OF`·`USES`(#161), ALB TG·`REGISTERED_IN`(PR3)으로 `RelationType` 6종이 코드상 전부 채워졌다. 단 `autoscaling`·`elbv2`(ASG·ALB TG)는 LocalStack Community 미포함(ADR-0006 §4)이라 collector 가 호출 실패를 흡수해 degrade 하고(수집 시 `PARTIAL` 표면화), `MEMBER_OF`·`USES`·`REGISTERED_IN` 실검증은 실 AWS 스모크(6–7주차)로 이월한다.
  - **박지현**: ~~`_is_prod` 확정 기준 반영 Golden 경계 케이스 추가~~ ✅ 완료(2026-08-25, #124 — 미해결 4번 완전 종결). ~~회귀 CI 상시 실행 검증~~ ✅ 확인(dev CI `419 passed, 5 skipped` — 골든 회귀 21→26건 상시 실행, DB 통합 28건 skip 0건으로 판정 기준 ⓒ 충족. 남은 skip 5건은 전부 미구현 대기 중인 placeholder다). ~~E2E 시연 시나리오 설계서 1차(FinOps·SecOps 2트랙)~~ ✅ 완료(2026-08-25, #132 — `docs/E2E_DEMO_SCENARIOS.md`). 실행 계열 테스트 하네스·픽스처 선구축(#136). **가드레일 회귀 테스트 skip 2건 해제**(#114 / PR #123 머지로 선행 조건 해소). SecOps 정답은 Risk Evaluator 대기(미해결 6번).
  - **유건희**: ~~다크 고정 마감(#89)·`--font-sans` 순환 참조 수정(#90)~~ ✅ 완료(2026-08-24, PR #103·#102). 자산 목록·상세 화면 mock 연동 마감(#106 — 판정 기준 ⓔ), global-error 셸 다크 고정(#112 — #103 잔여 재발행).
- **주차 종료 판정 기준**: ⓐ ~~executor ↔ 가드레일 ④ Dry-Run 인터페이스 문서 합의~~ ✅ 충족(2026-08-24, ADR-0007 / #113) ⓑ ~~`_is_prod` 정책 결정 종결(또는 PM 대행)~~ ✅ 충족(2026-08-24, #95) ⓒ ~~CI에서 DB 통합 테스트 28건 실행(skip 0건)~~ ✅ 충족(2026-08-25, #92 — dev CI `419 passed, 5 skipped`. 남은 skip 5건은 `test_e2e_scenario` 2 + `test_guardrails` 2 + `test_rollback` 1 로 전부 미구현 대기 QA placeholder이므로 DB 통합 28건은 skip 0건으로 실행 중) ⓓ `POST /actions/execute` 멱등 처리 동작(실행 스텁 허용) ⓔ FE 자산 화면 mock 100% 렌더 ⓕ LLM 외부 전송 페이로드 마스킹 적용.

## MVP 확정 범위

- **관제**: AWS 단일 계정 / 1–2개 리전. **EC2·SG 중심** + 런북 조치 대상 리소스(NACL, EBS, ASG·Launch Template, ALB Target Group).
- **위협**: OpenIP(0.0.0.0/0)·SSH 브루트포스 — Golden Dataset 기반 **모의(Mock) 주입** (실환경 GuardDuty 연동은 Post-MVP).
- **AI**: OpenAI GPT-4o + Pydantic v2 Structured Output + **LangGraph 오케스트레이션**. CoT 3줄 요약 + Runbook ID 추천. LangGraph는 프로젝트 정체성으로 MVP 구현 확정(2026-08-13) — 출력 계약(Pydantic 스키마)은 동일하게 유지.
- **4단계 가드레일(순서 고정)**: ① Schema Check ➔ ② Action Whitelist ➔ ③ ARN Match ➔ ④ AWS Dry-Run.
- **양방향 회복**: 자산 = 스펙 JSON 백업 ➔ `get_waiter` Status Check(2/2) ➔ 자동 원복 / 보안 = 선제 차단 ➔ 관제자 [원클릭 해제].
- **3단계 위험 대응**: High `PRE_MITIGATION_0_5S`(0.5초 선차단 시뮬레이션) / Medium·Low `AGENT_WAIT`(승인 대기) / **Medium 1분 미응답 `TIMEOUT_ISOLATION_1M`(자동 격리) — Low는 제외**(2026-08-25 확정, 아래 결정 로그).
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
| FinOps (롤백) | `RUNBOOK_EC2_REVERT_SIZE` | High / 시스템 자동 발동 (Status Check 실패 시) 또는 관제자 수동 요청 · AI 추천 불가 |

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
| 2026-08-13 | **개발 환경 = LocalStack, 발표 직전 실 AWS 전환** — `AWS_ENDPOINT_URL` 유무로 전환. 팀 표준 환경(compose·시드·env) 구성은 전략 수립 후 진행 | [ADR-0006](adr/0006-localstack-team-standard-env.md) |
| 2026-08-13 | **롤백 런북 3종 Whitelist 정식 등록(7→10종)** — 우회 정책 기각, `ai_recommendable: false`·백업 레코드 기반 복원·가드레일 실패 시 수동 개입 정책 채택 (미해결 #1 해소) | [ADR-0004](adr/0004-rollback-runbook-whitelist-registration.md) |
| 2026-08-13 | **팀명 = "딸깍 인프라" 확정** — README의 "서버룸 난방공사" 표기는 구버전(갱신 필요) | — |
| 2026-08-13 | **런북 10종 전부 실구현 방침** — mock/영상 대체 컷라인 기각(팀장 결정). P0/P1/P2는 착수 순서로만 운용, 9/13은 중간 점검 시점 | 본 문서 §일정 리스크 |
| 2026-08-13 | **LangGraph MVP 도입 확정** — 프로젝트 정체성 사유(팀장 결정, "미확정" 상태 종료). AI 파이프라인을 LangGraph 그래프로 구현하되 GPT-4o + Pydantic Structured Output 출력 계약은 불변. 그래프 설계는 안성일 주관(ADR 후보) | 본 문서 §MVP 확정 범위 |
| 2026-08-14 | **API 계약 확정** — Incident·Execute·WebSocket·오류 봉투 DTO 코드화(`packages/schemas/api/`), 실행 상태 4→6종(`ROLLED_BACK`·`ROLLBACK_FAILED` = 복구 최종 결과 추가), health_score 0–100 **정수** 확정 | 이슈 #32 |
| 2026-08-18 | **실행 축 어휘 교체(ADR-0004 1차 개정)** — 확정본 런북 명세서의 `approval_mode`·`trigger_source` 두 축을 의도적으로 교체. `trigger_source`(실행별 기록) = `USER_APPROVAL`·`PRE_MITIGATION_0_5S`·`TIMEOUT_ISOLATION_1M`·`AUTO_ON_FAILURE`, `approval_mode`(런북별 정책) = `HUMAN_ONLY`·`SYSTEM_OR_HUMAN`. 런타임 의미 무변경이라 supersede 없이 1차 개정으로 종결 | [ADR-0004](adr/0004-rollback-runbook-whitelist-registration.md) |
| 2026-08-18 | **LangGraph 그래프 구조 확정** — FinOps·SecOps 두 그래프로 분리, Checkpointer 미사용(업무 상태는 PostgreSQL 단일 원천·그래프 내 승인 중단점 없음), Guardrail·DB 저장·AWS 실행·승인은 그래프 밖, 모델 호출은 `AIModelClient` 경계 경유. 판단 근거는 구조화 필드로만 보존(Prompt 전문·내부 추론 텍스트 미보존) | [ADR-0005](adr/0005-langgraph-stateless-domain-graphs.md) |
| 2026-08-19 | **LocalStack 팀 표준 환경 전략 확정** — 단일 compose(Community 전용·버전 고정), 시드 = Boto3 스크립트 단일 원천(멱등·rule_engine 임계값 결합·실 AWS 실행 거부), `AWS_ENDPOINT_URL` 스위치 규약 전 모듈 승격(환경 감지 분기 금지), 검증 한계 4경로(Dry-Run·Status Check·CloudWatch·ALB TG/ASG)는 6–7주차 실 AWS 스모크로 이월 | [ADR-0006](adr/0006-localstack-team-standard-env.md) |
| 2026-08-24 | **가드레일 ④ AWS Dry-Run = executor `precheck()` 단일 호출로 확정** — 동기·예외 미전파, 반환은 `PrecheckOutcome`(`passed`·`reason_code`·`verification_summary`). `DryRunOperation` **예외 발생만 PASS**(정상 반환은 플래그 미적용으로 보고 FAIL). `DryRun` 미지원 5종(전면 2 = `NACL_ADD_DENY`·`NACL_RESTORE`, 부분 3 = `EC2_ISOLATE`·`EC2_UNISOLATE`의 elbv2 호출·`ENABLE_AUTOSCALING`의 asg 호출)은 환경 무관 **조회(describe) 대체 검증**. 실측 결과 `elbv2`·`autoscaling`은 LocalStack Community 미포함(Pro 전용)이라 P2 3종은 로컬 검증 경로 자체가 없음 → ADR-0006 §4 검증 한계 표에 확정 편입 | [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) / 이슈 #113 / PR #117 |
| 2026-08-25 | **ADR-0007 1차 개정 — `precheck()` 구현 실측 반영** — 판정 구조·사유 코드·대체 검증 5종은 **불변**. ① `DryRun` 통과는 대상 자원 존재를 증명하지 않음(부재 자원에도 `DryRunOperation` 반환) → §3 요약 문구 정정, DryRun 전면 6종에 존재 확인 describe **미추가**로 확정 ② `EC2_UNISOLATE` 조회를 `describe_target_health` → **`describe_target_groups`**(전자의 응답에 `VpcId`가 없어 통과 조건 ③을 확인할 수 없음) ③ §1 시그니처에 키워드 전용 **`backup_loader`** 명시, "예외를 던지지 않는다"의 예외를 **배선 오류 1건**으로 한정 ④ `EC2_ISOLATE` 등록 판별 = `Target.NotRegistered` 기준, `NACL_ADD_DENY` 중복 검사 = 인바운드(`egress=False`) 기준, `NACL_RESTORE` 백업 조회 = `(rule_number, egress)`로 특정 ⑤ **리전 규약 신설** — AWS 클라이언트와 ARN 파라미터 모두 `target_arn`의 리전 기준(기본 리전 고정 시 2번째 리전 자산 오판정) | [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) / 이슈 #133 / 구현 PR #147 |
| 2026-08-24 | **`_is_prod` 운영 자산 인식 기준 확정** — 규칙 소유자(김승철) 결정. **키**(대소문자 무시) = `environment`·`env`·`stage`·`tier`, **값**(소문자 정확일치·부분일치 금지) = `prod`·`production`·`prd`. 부분 문자열 매칭은 영구 금지(`product-service`류 오탐 방지, #81). 접미 변형(`prod-us-east` 등) 미탐은 의도된 결과이며 Golden 경계 케이스로 고정 | 이슈 #95 / PR #97 |
| 2026-08-25 | **`TIMEOUT_ISOLATION_1M` 자동 격리에서 Low 제외 확정** — `AGENT_WAIT`(Medium·Low) 중 **Medium만** 1분 미응답 시 자동 격리한다. `TIMEOUT_ISOLATION_1M`이 부르는 조치가 `RUNBOOK_EC2_ISOLATE`(ALB 타겟 그룹 이탈 + 격리 SG 교체)라, Low 판정 건을 사람 확인 없이 격리하면 오탐 비용이 이득보다 크다. **기준 등급 = `initial_risk_level`**(+ `response_mode = AGENT_WAIT`). `reviewed_risk_level`(AI 정밀 평가)은 초기 판정을 덮어쓰지 않는 관제자 참고값이라 자동 행동을 가르지 않는다 — `response_mode` 자체가 초기 판정에서만 파생되기 때문이다(`packages/schemas/events.py` `_EXPECTED_MODE_BY_RISK`). 정밀 평가에 자동 행동 변경 권한을 주려면 상태 전이 계약(`MEDIUM→LOW` 대기 취소 · `LOW→MEDIUM` 대기 시작 시점 · `→HIGH` 즉시 격리 · `HIGH→하향` 시 이미 수행된 격리 처리)을 먼저 정의해야 하며, 그 전까지는 이 규칙을 확장하지 않는다(2026-08-25 PR #163 리뷰, 안성일). 본 문서 §MVP 확정 범위와 `시스템 흐름도.md`의 3분기 서술이 Low를 빼지 않아 화면설계서 v1.5 §4.5와 갈려 있던 것을 §4.5 쪽으로 확정한다. 판정 규칙의 코드 확정은 Risk Evaluator(§미해결 6번) 구현과 함께 남긴다 | PM 결정(김세혁) / PR #162 리뷰 |

## API 계약 (확정 — FE↔BE 공개 계약, 코드 원천: `packages/schemas/api/`)

- `GET /api/v1/assets` — EC2/SG 상태·스펙·연결관계·헬스 스코어(**0–100 정수**)·Skip 사유 코드
- `GET /api/v1/incidents` — 목록(상세의 부분집합 10필드 + nullable `title`). `status`·`category` 필터, `created_at` 내림차순 전체 반환(페이지네이션 Post-MVP)
- `GET /api/v1/incidents/{id}` — nullable `title`, AI CoT 3줄 요약, Evidence ID, 추천 Runbook(본편 7종만)·실행 요약(관제자 복구 조치는 롤백 3종만)
- `POST /api/v1/actions/execute`
  - Request: `{ incident_id, runbook_id, idempotency_key }` — 추가 필드 거부, Target ARN·AWS 파라미터는 받지 않음
  - Response status: `IN_PROGRESS | SUCCESS | FAILED | ROLLBACK_INITIATED | ROLLED_BACK | ROLLBACK_FAILED` (마지막 2종 = 복구 최종 결과, 원본 Execution에만 기록)
- WebSocket `/api/v1/ws` — `INCIDENT_CREATED | INCIDENT_UPDATED | EXECUTION_UPDATED` 이벤트 봉투 (DB commit 이후 전송, 상태 원본 아님)
- REST 공통 오류 봉투 `{"error": {code, message, request_id}}` — 코드 5종(404·409×2·422·500)

## 팀 & 역할 (최신 — README·기획서의 역할 표는 구버전)

| 이름 | 역할 | 담당 |
| --- | --- | --- |
| **김세혁** (팀장/PM) | Infra & DevSecOps | Boto3 EC2/SG 제어, 스펙 JSON 백업/자동 원복 엔진, Docker·배포, Code Review·브랜치 관리 |
| **안성일** | AI / Guardrail · Architect | 아키텍처·DB 스키마, FastAPI 메인·PostgreSQL, GPT-4o + 4단계 가드레일 |
| **김승철** | Data & Rule Engine | CloudWatch 수집 파이프라인, Rule Evaluator(Idle EC2·미사용 SG, Skip 사유 적재) |
| **박지현** | QA & Scenario / Technical Writer | Golden Dataset(약 20건), E2E 시연 시나리오, pytest, 문서화 |
| **유건희** | Frontend | Next.js + Shadcn 대시보드, REST/WebSocket 연동, 차트·토폴로지 시각화 |

## 미해결 이슈 / 할 일 (블로커 순)

1. ~~LocalStack 팀 표준 환경~~ ✅ 해소(#62) — [ADR-0006](adr/0006-localstack-team-standard-env.md) 전략 + compose `localstack` 서비스 + `scripts/seed_localstack.py` + `.env.example` 스위치 활성화. 잔여 후속 중 **CI LocalStack service container는 완료**(#65). 남은 것은 6–7주차 실 AWS 스모크 테스트(ADR-0006 §4). **범위 확대(2026-08-24, ADR-0007 실측)**: `elbv2`·`autoscaling`이 LocalStack Community에 없어(Pro 전용) `EC2_ISOLATE`·`UNISOLATE`·`ENABLE_AUTOSCALING` **P2 3종은 실행뿐 아니라 Dry-Run 대체 조회조차 로컬에서 돌지 않는다.** 이 3종의 유일한 검증 경로가 실 AWS 스모크이므로, §일정 리스크의 "P2 인프라 조기 준비"가 선택이 아니라 전제 조건이 됐다.
2. ~~PR #29 후속 보완~~ ✅ 종결(2026-08-19, PM 대행 판단 — 김승철 부재, 기록: 이슈 #67 댓글) — 당초 우려("자체 boto3 로직·시드 없으면 빈 결과 통과")는 현행 `test_collector_raw.py`에서 해소 확인(`collect_region()` 직접 호출, 시드 없으면 assert 실패). dev 머지본(#64) 기준 작성자 외 로컬에서 표준 절차(compose→시드→pytest) 통합 테스트 3건 통과 재현. 잔여였던 **CI LocalStack service container**는 #65로 완료. 김승철 복귀 후 이견 시 재오픈.
3. ~~README 최신화~~ ✅ 해소(팀명·역할 표·런북 10종·LangGraph 반영). 기획서 docx는 동결 방침이라 갱신 대상 아님.
4. ~~**`_is_prod` 판정 규칙**~~ ✅ **완전 종결**(2026-08-25) — 박지현 제기 #81 → 규칙 소유자 김승철 확정(#95 / PR #97). 인식 태그 키·값 집합을 확정·구현했다. **키**(대소문자 무시 정확일치): `environment`·`env`·`stage`·`tier`. **값**(`strip` 후 소문자 정확일치, 부분일치 영구 금지): `prod`·`production`·`prd`. 잔여 2건도 닫혔다 — ① `evaluate_ec2`의 `name` 인자 정리 = #96 / PR #110(인자 자체 제거) ② 확정 기준 반영 Golden 경계 케이스 = #124 / 본 PR(`asset_inventory_003.json` A11–A16, 뮤테이션 4종 방어 확인). 접미 변형(`prod-us-east`)·부정 접두(`non-prod`)·부분 포함(`product-service`) 미탐이 **의도된 결과임이 정답지로 고정**됐으므로, 앞으로 이를 버그로 보고 부분일치를 되살리는 변경은 골든 회귀에서 막힌다.
5. ~~CI에 PostgreSQL service container 없음~~ ✅ 해소(#92, 박지현 제기 2026-08-20) — `ci.yml` `test` 잡에 `postgres:16-alpine` service container를 추가했다(이미지·계정·DB명은 compose `db` 서비스와 동일 — 로컬 = CI 동형). 그동안 CI에서 항상 skip되던 **DB 통합 테스트 28건**(`apps/core-api/db/tests` 21 + `apps/core-api/tests` 7)이 실행되며, `health_score` 0–100 정수 변환 등 DB 경유 계약과 Alembic `upgrade head`가 매 PR에서 검증된다. 서비스 장애로 다시 조용히 skip되는 것을 막기 위해 pytest 앞에 접속 확인 스텝을 둔다. **원 제기 문구 정정**: `test_persistence_pipeline`은 저장소에 존재하지 않는 테스트 이름이며(실제 대상은 위 28건), "`ci.yml` 주석에도 명시"는 #65(PR #84)에서 헤더 주석이 교체되며 사라진 서술이다.
6. **Golden Dataset SecOps 정답 보류** (박지현) — 위협 입력 10건은 작성 완료했으나 `initial_risk_level`·`response_mode`·`reason_codes` 판정 규칙이 미확정이라 정답을 채우면 추측이 된다. Risk Evaluator 구현과 `RiskReasonCode` 값 목록 확정 시 별도 PR. 각 케이스가 강제하는 판정 논점은 `datasets/golden/secops/expected/README.md`에 기록.

## 일정 리스크 & 구현 우선순위 (2026-08-13 방침 확정)

**런북 10종 전부 실구현이 원칙이다** — mock/영상 대체를 전제한 컷라인("P2 자동 컷")은 채택하지 않는다(팀장 결정). P0/P1/P2는 범위 축소선이 아니라 **구현 착수 순서**로만 사용한다.

- **P0 (최우선 착수, 3–5주차)**: `RIGHTSIZING`+`REVERT_SIZE`(자산 자동 원복), `NACL_ADD_DENY`+`NACL_RESTORE`(차단→원클릭 해제) — "양방향 회복" 스토리의 골격.
- **P1 (P0 후 순차)**: `SG_DELETE_ISOLATED`(+`SG_RECREATE`), `EBS_DELETE_UNATTACHED` — 난도 낮음.
- **P2 (조기 준비 병행 — 인프라는 선택이 아니라 전제 조건)**: `EC2_ISOLATE`(+`UNISOLATE`)는 ALB·다중 EC2 시연 인프라가 선행 조건 → 인프라 준비를 앞당긴다. `ENABLE_AUTOSCALING`은 구현량 최대 → 설계 선행. **`elbv2`·`autoscaling`이 LocalStack Community에 없어 이 3종은 Dry-Run 대체 조회조차 로컬에서 돌지 않는다**(ADR-0007 실측, §미해결 1번) → **실 AWS 스모크 환경 확보가 P2 착수의 전제 조건**이며, 지연 시 6–7주차 스모크가 아니라 P2 구현 자체가 막힌다.
- **9/13 중간 점검**: P0 4종 실동작 여부 점검. 미달 시 범위 축소가 아니라 **인력 재배치·범위 외 작업 중단**으로 대응한다.

## 문서 지도 (신뢰 우선순위 — 충돌 시 위가 이김)

1. **`docs/PROJECT_STATUS.md`** (이 문서) — 범위·결정·역할·현황
2. `vigilantis-docs/런북 명세서.md` — Action Whitelist 확정 규격 (10종: 본편 7 + 롤백 3)
3. `vigilantis-docs/시스템 흐름도.md` — MVP 아키텍처·파이프라인
4. `docs/adr/` — 결정 배경(왜 그렇게 했나)
5. [`docs/E2E_DEMO_SCENARIOS.md`](E2E_DEMO_SCENARIOS.md) — **1차 발표 시연 대본의 원천**이자 `tests/test_e2e_scenario.py`의 명세. 위 1–4를 원천으로 삼는 파생 문서라 충돌하면 위가 이긴다. 확정본 대조가 필요한 항목은 문서 내 §대조 필요 목록에 모아둔다
6. `vigilantis-docs/마일스톤/` — **주차별 실행 계획(현행)**. 담당별 카드·DoD·주차 종료 판정 기준. 현재 주차: `W03_0824-0830.md`
7. `vigilantis-docs/1차 발표까지의 마일스톤 및 MVP 범위 명세.md` — 초기 주차 계획 (※ 범위 서술 일부 구버전: EC2·SG 한정, 런북 2종 예시)
8. `README.md` — 프로젝트 소개용 (※ 역할 표·오너 주석 구버전)
9. `vigilantis-docs/기획서/*.docx` — **풀비전 비전 문서(동결)**. 구현 기준 아님.

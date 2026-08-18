# ADR-0004: 롤백 런북 3종을 Whitelist에 정식 등록한다 (7종 → 10종)

- **Status**: Accepted
- **Date**: 2026-08-13
- **Amended**: 2026-08-18 — 실행 축 어휘 교체(하단 "개정 이력" 참조, 핵심 결정 불변)
- **Deciders**: 김세혁(PM/Infra) 확정, 안성일(AI/Guardrail) 공유 대상

## Context (배경)

런북 명세서 7종([ADR-0002](0002-runbook-whitelist-mvp-scope.md))의 각 상세 명세는 `safety_and_rollback.rollback_runbook_id`로 롤백 런북을 참조하지만, 참조 대상 3종(`RUNBOOK_EC2_UNISOLATE`, `RUNBOOK_SG_RECREATE`, `RUNBOOK_EC2_REVERT_SIZE`)은 레지스트리·상세 명세·Whitelist 어디에도 정의되어 있지 않았다(ADR-0002 미해결 항목).

가드레일 Step 2는 "Whitelist에 없는 런북은 실행 차단"이 원칙이고 **롤백 실행도 런북 실행**이므로, 이대로 구현하면:

- 다운사이징 실패 → 자동 원복(`REVERT_SIZE`) 시도 → Whitelist 미등록 → **가드레일이 자체 자동 원복을 차단** (Auto-Rollback 셀링포인트 무력화)
- 보안 [원클릭 해제](`UNISOLATE`)도 `POST /api/v1/actions/execute` 경로에서 Step 2 거절

두 가지 대안을 비교했다:

| | ⓐ 정식 등록 (10종) | ⓑ 롤백은 Whitelist 검증 우회 |
| --- | --- | --- |
| 가드레일 원칙 | 예외 없음 | "롤백 주장 시 무검증" 뒷문 발생 |
| API 계약 | `actions/execute` 그대로 | 롤백 전용 엔드포인트 신설 필요(FE 영향) |
| 감사 로그 | 단일 경로 | 이원화 |
| 작업량 | 명세 3종 + Literal 확장 | 문서 한 단락 (단, 우회 경로의 안전성 증명 비용 별도) |
| 긴급 원복 | 가드레일 실패 시 예외 처리 필요 | 항상 실행 |

## Decision (결정)

**ⓐ 채택 — 롤백 런북 3종을 Whitelist에 정식 등록한다. Action Whitelist는 총 10종(본편 7 + 롤백 3)이 된다.**

| 분류 | Runbook ID | 위험도 | trigger_source (시작 사유) | approval_mode (승인 정책) |
| --- | --- | --- | --- | --- |
| SecOps (롤백) | `RUNBOOK_EC2_UNISOLATE` | Medium | `USER_APPROVAL` — 관제자 [원클릭 해제] | `HUMAN_ONLY` |
| SecOps (롤백) | `RUNBOOK_SG_RECREATE` | Low | `USER_APPROVAL` — 관제자 원복 요청 | `HUMAN_ONLY` |
| FinOps (롤백) | `RUNBOOK_EC2_REVERT_SIZE` | High | `AUTO_ON_FAILURE` — Status Check(2/2) 실패 시 자동 원복 엔진 · `USER_APPROVAL` — 관제자 수동 요청 | `SYSTEM_OR_HUMAN` |

**롤백 런북 공통 정책 4항**:

1. **정식 등록, 우회 없음** — 롤백도 4단계 가드레일(Schema → Whitelist → ARN Match → Dry-Run)을 본편과 동일하게 전부 통과한다.
2. **AI 추천 금지** — 롤백 3종은 `ai_recommendable: false`. AI 추천 목록(7종)과 실행 Whitelist(10종)를 분리한다. 트리거는 시스템(자동 원복) 또는 관제자(원클릭 해제/수동 원복)뿐이다. (공격자가 AI를 유도해 "격리 해제"를 추천시키는 경로 차단)
3. **백업 레코드 기반 복원** — 원복 파라미터(원본 SG 규칙·인스턴스 스펙 등)는 요청 페이로드가 아니라 실행 시점에 DB 백업 레코드(`backup_record_id`)에서만 로드한다.
4. **가드레일 거절 시** — 자동 재시도 없이 CRITICAL 알림 후 수동 개입으로 전환한다. (긴급 원복이 차단된 채 방치되는 것을 방지)

상세 명세(파라미터 스키마·target_api·`trigger_source: AUTO_ON_FAILURE` 신설 포함)는 `vigilantis-docs/런북 명세서.md` [SecOps-05]·[SecOps-06]·[FinOps-04]에 작성 완료.

## Consequences (결과·트레이드오프)

**장점**
- "AWS를 건드리는 모든 실행은 가드레일을 통과한다"가 예외 없이 성립 — 발표·심사 방어 논리 단순화
- 보안 원클릭 해제가 기존 `POST /api/v1/actions/execute` 계약을 그대로 사용 (FE 추가 작업 없음)
- 실행·감사 로그가 단일 경로로 통일

**비용/유의**
- Whitelist 구현(코드 enum·Pydantic Literal)은 10종 기준이어야 한다 — **7종 기준 구현·테스트는 이 ADR로 구버전이 됨** (예: `RUNBOOK_EC2_UNISOLATE` 차단을 기대하는 테스트는 반전 필요)
- AI 추천 검증용 7종 목록(`ai_recommendable` 분리)이 별도로 필요 — 실행 Whitelist와 원천을 공유하되 파생 목록으로 관리
- 자동 원복(`REVERT_SIZE`)이 가드레일에서 거절되는 경우의 CRITICAL 알림 경로 구현 필요
- 신규 규격 필드(`trigger_source` — 신규 값 `AUTO_ON_FAILURE` 포함, `approval_mode`, `ai_recommendable`, `backup_record_id`)의 스키마 반영 필요

## Related

- 확정 규격: `vigilantis-docs/런북 명세서.md` (10종)
- 현황 기준: `docs/PROJECT_STATUS.md`
- 선행 결정: [ADR-0002](0002-runbook-whitelist-mvp-scope.md) — 미해결로 남겼던 항목의 해소
- 영향 범위: `apps/core-api/ai/whitelist.py`(PR #35 재작업), `packages/schemas/runbooks.py`, API 계약(`action_type=ROLLBACK_EXECUTION` 논의)

## 개정 이력

- **2026-08-18 (1차 개정)** — 실행 축 어휘 교체. 확정본 런북 명세서가 두 축의 이름을
  서로 바꿔 쓰고 있었고, 구 어휘에 세 결함이 있었다. ① `approval_mode`가 ResponseMode
  값(`PRE_MITIGATION_0_5S`·`AGENT_WAIT`)을 재사용해 한 어휘가 두 개념에 걸침
  ② `RUNBOOK_EC2_REVERT_SIZE`의 "엔진 자동 시작 vs 관제자 수동 요청"을 실행 기록에
  남길 자리가 없음 ③ 가드레일 2단계의 "시작 주체 ∈ 허용 정책" 대조에 두 축 분리가 전제.
  두 축을 의도적으로 교체(swap)한다.

  | 구 (명세서 원문) | 신 |
  | --- | --- |
  | `approval_mode: PRE_MITIGATION_0_5S \| AGENT_WAIT \| AUTO_ON_FAILURE` | `trigger_source`로 이동 (`AGENT_WAIT`→`USER_APPROVAL`, `TIMEOUT_ISOLATION_1M` 추가) |
  | `trigger_source: HUMAN_ONLY \| SYSTEM_OR_HUMAN` | `approval_mode`로 이동 (값 동일) |

  보완 결정 2건:
  - `RUNBOOK_EC2_ISOLATE`(본편)의 `trigger_source`에 `USER_APPROVAL` 포함 — AI 추천
    가능 7종이라 관제자 승인 실행 경로가 존재. 본 ADR 결정 표는 롤백 3종 범위이므로
    표에는 반영하지 않는다.
  - `RUNBOOK_EC2_REVERT_SIZE`의 `trigger_source`는 `AUTO_ON_FAILURE`·`USER_APPROVAL` —
    명세서 원문 "자동 원복 엔진 또는 관제자 수동 요청".

  런타임 의미가 변하지 않아 supersede 없이 1차 개정으로 종결한다. 핵심 결정(롤백 3종
  Whitelist 등록·공통 정책 4항)은 불변.

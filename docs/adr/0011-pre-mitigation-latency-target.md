# ADR-0011: High 선차단 목표 시간 — 0.5초에서 3초로 (실 AWS 실측 기준)

- **Status**: Accepted (2026-10-15 — 김세혁 결정. `SEC` 복수 담당인 안성일은 이날 부재라 사후 확인 대상이며, 이견이 있으면 이 ADR을 개정한다)
- **Date**: 2026-10-15
- **Deciders**: 김세혁(PM · `SEC` 복수 담당 — 차단 실행 축)
- **Refs**: #447 / PR #469(발동 경로 · 개인 계정 30회 측정) · [`docs/AWS_SMOKE_RESULT.md`](../AWS_SMOKE_RESULT.md) §8-2(팀 실 AWS 측정) · [ADR-0004](0004-rollback-runbook-whitelist-registration.md)(실행 축 어휘) · [ADR-0001](0001-mvp-monorepo-structure.md)

## Context (배경)

3단계 위험 대응의 High 경로는 기획 단계부터 **"0.5초 선차단"** 으로 적혀 왔다(SSOT §MVP 확정 범위 · §Action Whitelist `RUNBOOK_EC2_ISOLATE` 행). 이 값은 실측에서 나온 것이 아니라 목표로 정한 것이고, 발동 경로(B5 · #447)가 없던 동안에는 잴 대상도 없었다.

2026-10-13(화) B5가 머지되고(PR #469) 실 AWS에서 처음 쟀다. 결과는 다음과 같다.

| 측정 | 환경 | 회수 | 결과 |
| --- | --- | --- | --- |
| 팀 실 AWS | 배포된 플랫폼(ECS · 접근 역할 전환) · 스모크 VPC `web-1` | High 1회 계측(2회 실행 중) · Medium 1회 | High `end_to_end_ms` **2,932.7ms** · Medium 기한 후 확인까지 **2,991.6ms** |
| 개인 AWS(참고) | 안성일 개인 계정 · 코드 변경 없음 | High 30회 | 최소 1.648초 · 중앙값 **1.730초** · p95 **1.961초** · 최대 2.055초 · 0.5초 초과 **30/30** |

팀 측정의 구간별 내역(`isolation_latency` 로그 phase)이 원인을 보여 준다.

- **가드레일 ④ 1.38초** — `describe_instances` · ENI `DryRun` · `describe_security_groups` · `describe_target_health` 4회 직렬 호출. 실제 변경 전에 반드시 거치는 검증이라 생략할 수 없다(ADR-0007).
- **격리 실행·확인 1.09초** — `DeregisterTargets` → `ModifyNetworkInterfaceAttribute` → 확인 조회.
- 그 밖(인시던트 저장·예약·백업 커밋)은 합쳐 0.5초 미만이다.

즉 **AWS API 왕복이 직렬로 8회 안팎 들어가는 구조라, 한 번에 100–300ms인 왕복만으로 0.5초를 넘는다.** 코드 최적화로 닿을 수 있는 값이 아니다. ADR-0001이 적은 "0.5초 실환경 선제 차단 필요 시 Lambda/EventBridge" 대안도 실행 위치만 바꿀 뿐 같은 AWS 호출을 지우지 못한다.

## Decision (결정)

**High 선차단의 목표 시간을 0.5초에서 3초로 바꾼다.** 식별자 `PRE_MITIGATION_0_5S`는 그대로 둔다.

### 1. 목표값과 재는 구간

| 경로 | 목표 | 재는 구간(`vigilantis.isolation_timing` 로거의 `isolation_latency` 로그) |
| --- | --- | --- |
| High 선차단(`PRE_MITIGATION_0_5S`) | **3초 이내** | `end_to_end_ms` — 위협 접수부터 격리 확인(`isolation_confirmed`)까지 |
| Medium 1분 자동 격리(`TIMEOUT_ISOLATION_1M`) | **응답 기한(60초) 뒤 3초 이내** | `deadline_to_confirmation_ms` — 저장된 응답 기한부터 격리 확인까지(1초 감시 주기 포함) |

- **3초를 고른 근거**: 팀 실 AWS 측정 2.93초 · 2.99초가 모두 들어오고, 개인 계정 30회 최대 2.06초에 1초의 여유가 있다. 2초(안성일 10/13 제안)는 팀 환경 측정이 이미 넘으므로 지금 구조에서 지킬 수 없는 값이다.
- **성격은 목표(관측 기준)이며 런타임 제한 시간이 아니다.** 코드에 0.5초를 강제하는 상수는 원래 없었고, 3초도 코드로 강제하지 않는다. 넘어도 격리는 끝까지 진행하고, 넘은 사실은 로그에 남는다.

### 2. 식별자는 바꾸지 않는다

`PRE_MITIGATION_0_5S`는 `trigger_source`·`response_mode`의 값으로 DB enum · 공개 API(`packages/schemas/api/incidents.py`) · FE 타입과 필터(`apps/web/src/types/api.ts` · `incident-filter.ts`)에 걸쳐 있다(ADR-0004 실행 축 어휘). 이름을 바꾸면 API 계약 변경과 DB 마이그레이션이 함께 따라온다. 그래서 **이 이름은 "High 즉시 선차단"을 가리키는 고유명으로 읽고, 숫자는 목표값이 아니다.** 화면 표기는 이미 숫자 없이 `선제 차단됨`이다(`apps/web/src/lib/enum-labels.ts`).

### 3. 반영 위치

- SSOT §MVP 확정 범위의 3단계 위험 대응 · §Action Whitelist `RUNBOOK_EC2_ISOLATE` 행의 "0.5초"를 "3초 목표"로 바꾼다(2026-10-15 SSOT 갱신).
- 측정 기록의 원천은 `docs/AWS_SMOKE_RESULT.md` §8-2다.

## Consequences (결과·트레이드오프)

**장점**

- 목표가 실측과 맞아 **판정에 쓸 수 있는 값**이 된다. 0.5초는 30/30 초과라 기준으로서 아무것도 가르지 못했다.
- 가드레일 ④를 건너뛰는 식의 "숫자 맞추기"를 하지 않는다 — 사람 승인 없이 서버가 시작하는 조치일수록 검증을 빼면 안 된다.

**비용/유의**

- **팀 환경 표본이 작다** — High 계측 1회 · Medium 1회다. 3초가 팀 환경의 p95를 덮는지는 아직 모른다. Post-MVP 첫 주에 배포 플랫폼에서 반복 측정(30회 이상)해 p95를 확인하고, 3초를 넘으면 이 ADR을 개정한다.
- 개인 계정 측정(중앙값 1.73초)과 팀 측정(2.93초) 사이 약 1.2초 차이의 원인을 아직 가르지 않았다 — 접근 역할 전환(AssumeRole) 경로 · ECS 네트워크 · 회차 조건 중 무엇인지는 반복 측정과 함께 본다.
- 더 줄이려면 구조를 바꿔야 한다(예: 가드레일 ④ 조회 병렬화 · 위협 접수 전 사전 검증). 이는 MVP 범위 밖이며 Post-MVP 후보로 남긴다.
- 이름(`0_5S`)과 목표(3초)가 어긋나 보인다. 처음 보는 사람이 오해하지 않도록 이 ADR과 SSOT 본문이 그 사실을 적는다.

## Related

- 선행 결정: [ADR-0004](0004-rollback-runbook-whitelist-registration.md)(실행 축 어휘 `trigger_source`) · [ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md)(가드레일 ④ 호출 규약 — 가장 큰 구간)
- 측정: `docs/AWS_SMOKE_RESULT.md` §8-2 · PR #469 본문(개인 계정 30회)
- 영향 범위: `docs/PROJECT_STATUS.md`(§MVP 확정 범위 · §Action Whitelist) — 코드 변경 없음

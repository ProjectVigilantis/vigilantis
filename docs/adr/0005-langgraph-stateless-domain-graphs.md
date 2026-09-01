# ADR-0005: LangGraph를 상태를 보관하지 않는 도메인별 두 그래프로 구성한다

- **Status**: Accepted
- **Date**: 2026-08-18
- **Deciders**: 안성일(AI/Guardrail) 제안, 김세혁(PM/Infra) 승인

## Context (배경)

LangGraph 도입 여부는 [ADR-0001](0001-mvp-monorepo-structure.md)·[ADR-0002](0002-runbook-whitelist-mvp-scope.md)가 후속 결정 후보로 남겼던 항목이며, 2026-08-13 팀장 결정으로 도입이 확정됐다(`docs/PROJECT_STATUS.md` 결정 로그). 그래프 설계는 안성일 주관. 입출력 계약은 `packages/schemas/agents.py`에 정의 완료(#49) — 도메인별 입력, 공통 출력, 불변식 9항. **그래프 내부는 미정.**

구현 전 결정 대상:

- FinOps·SecOps 그래프 분리 여부
- 상태 원천 — 그래프 Checkpointer vs 외부 DB
- Guardrail·DB 저장·AWS 실행의 그래프 내외 배치와 LLM 호출 경계
- 판단 근거의 보존 형태 — AWS를 실제로 변경하므로 사후 설명·검증이 가능해야 함

## Decision (결정)

**FinOps·SecOps 두 그래프로 나누고, 상태 보관·승인 대기·AWS 실행·Guardrail 검증을 모두 그래프 밖에 둔다. 판단 근거는 검증 가능한 구조화 필드로만 남긴다.**

```text
FinOps : summarize_evidence → propose_candidates → validate_output_contract
SecOps : summarize_evidence → reassess_risk → propose_candidates → validate_output_contract
```

`validate_output_contract`는 출력 스키마와 #49 불변식만 검사한다 — 4단계 Guardrail 검증이 아니며 그래프 밖에서 별도로 수행된다.

**설계 원칙 4항**:

1. **도메인별 분리** — 단일 그래프에 조건 Edge를 두면 합집합 State가 필요해 한쪽 전용 필드가 nullable로 혼입된다. 두 그래프로 나눠 출력 불변식을 그래프 단위로 강제한다. (`domain`은 Workflow가 설정하며 그래프 노드가 이를 바꿀 수 없다 — #49 불변식 2) SecOps 전용 `reassess_risk`가 `reviewed_risk_level`을 산출하며 초기 위험도는 덮어쓰지 않는다.
2. **상태 비보관** — Checkpointer를 두지 않고 업무 상태는 PostgreSQL 단일 원천으로 둔다. 승인 중단점도 없다 — 그래프는 한 번 호출되면 Terminal 결과 반환 후 종료한다. 중복 호출은 Incident의 AI 호출 상태를 PostgreSQL 트랜잭션 안에서 원자적으로 선점(Claim)해 막는다 — 프로세스 수와 무관하게 성립한다. (상태 이원화와 서버 타이머·그래프 수명의 결합 회피)
3. **책임 한정** — 그래프는 분석·후보 생성까지만 담당하고 Guardrail·DB 저장·AWS 실행·승인은 밖에 둔다. 모델 호출은 이 ADR에서 도입하는 `AIModelClient` 경계를 통해서만 수행해 SDK 타입·자격증명·예외가 `AIModelClient` 구현 밖의 그래프·도메인 코드로 새지 않게 한다. (테스트 주입용 경계이며 Provider 교체·배포 방식의 확정이 아님)
4. **근거는 구조화 필드로** — 판단 근거는 `Incident`의 요약 3줄·`initial_risk_reason_codes`·`reviewed_risk_level`, `RunbookCandidate.evidence_ids`, `GuardrailEvaluation` 4단계 결과에 남긴다. 호출 메타는 구조화 로그로 출력하되 업무·감사 데이터의 기준은 PostgreSQL이며 운영 로그는 감사 근거가 아니다. `evidence_ids`가 가리키는 Evidence는 생성 후 변경하지 않는다 — 근거가 바뀌면 판단 설명이 성립하지 않는다. (`RiskReasonCode` 값 목록은 2026-08-31 확정 — PR #206, `packages/schemas/events.py`)

미보존 대상:

| 항목 | 이유 |
| --- | --- |
| Prompt 전문·원본 응답 | 재호출마다 변동하는 텍스트는 감사 근거로 부적합 — 로그 레벨과 무관하게 저장·출력하지 않는다 |
| 모델의 내부 추론 텍스트 | 검증·재현 불가 — 후보 정당성은 `evidence_ids` 참조 검증으로 대체. 팀 문서가 "CoT 3줄 요약"이라 부르는 최종 요약(`Incident.summary_lines`)은 저장·노출 대상이며 여기서 말하는 미보존 대상이 아니다 |
| 단가·계산된 비용 | 토큰 수는 사용량 집계용이며 청구 비용의 근거가 아니다(캐싱·할인·요금제 차이) |

노드 분할과 호출 수명주기 상세는 구현 단계에서 정한다.

## Consequences (결과·트레이드오프)

**장점**

- 도메인별 출력 불변식을 그래프 단위로 강제 → 계약 위반 조기 노출
- 업무 상태 원천이 PostgreSQL 단일 → 재시작 복구 경로 단일화
- `FakeAIModelClient` 주입으로 실제 API 없이 두 경로 검증 가능
- 후보가 참조한 근거를 계약으로 검증 가능 → 판단 경위 설명 가능

**비용/유의**

- 호출 중 프로세스 종료 시 남은 Claim을 회수한 뒤 처음부터 재호출 — 부분 재개 미지원. 회수·재시도 규칙은 호출 수명주기 결정에서 정한다
- Prompt 미보존으로 과거 판단을 그대로 재현할 수 없음 — 입력 snapshot 재호출 시 결과 상이 가능
- 공통 노드 미추출 시 두 그래프에 중복 발생
- 그래프 내부 State를 `packages/schemas` 계약과 분리해 별도 정의 필요
- 그래프 호출 중복은 DB Claim으로 막지만 Scheduler·Dispatcher의 인메모리 상태는 별개 문제 — 다중 worker·replica 실행 토폴로지는 **별도 결정 대상**
- LLM 운영비 지표는 **MVP 제품 기능 제외** — 필요 시 로그의 토큰 사용량을 집계해 오프라인으로 추정
- 노드명과 세부 분할은 비규범적이라 개정 대상이 아님. 다만 도메인 분리·영속성·부수효과(노드의 DB·AWS 접근)·감사 경계를 바꾸는 변경은 **이 ADR의 개정 대상**

## Related

- 확정 계약: `packages/schemas/agents.py` — LangGraph 입출력 (#49)
- 현황 기준: `docs/PROJECT_STATUS.md`
- 선행 결정: [ADR-0001](0001-mvp-monorepo-structure.md) — 단일 `apps/core-api` 구조
- 선행 결정: [ADR-0002](0002-runbook-whitelist-mvp-scope.md) — AI 추천 7종
- 선행 결정: [ADR-0004](0004-rollback-runbook-whitelist-registration.md) — 롤백 3종 AI 추천 금지
- 영향 범위: `apps/core-api/ai/**`(agent·graph·model_client·openai_client), AI 호출을 Claim하는 Workflow와 Incident 저장·조회 계층(`apps/core-api/db/**`), AI 호출 상태 계약(`packages/schemas/incidents.py`)

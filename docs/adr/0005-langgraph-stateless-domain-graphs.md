# ADR-0005: LangGraph를 상태를 보관하지 않는 도메인별 두 그래프로 구성한다

- **Status**: Accepted
- **Date**: 2026-08-18
- **Amended**: 2026-11-05(1차 — 설계 원칙 5항 "근거 선행" 신설, SecOps 그래프 순서 복귀·위험 재평가 노드 제거, 실행 계획 단계의 대체 불가). 하단 "개정 이력" 참조, **핵심 결정 불변**
- **Deciders**: 안성일(AI/Guardrail) 제안, 김세혁(PM/Infra) 승인 / 1차 개정 — 안성일 제안·김세혁 승인(PR #584)

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
FinOps : summarize_evidence(근거 3줄 + decision) → propose_candidates(실행 계획 전사) → validate_output_contract
SecOps : summarize_evidence(근거 3줄 + decision) → (실행 계획: 코드 조립) → validate_output_contract
```

`validate_output_contract`는 출력 스키마와 #49 불변식만 검사한다 — 4단계 Guardrail 검증이 아니며 그래프 밖에서 별도로 수행된다.

- **SecOps `reassess_risk`는 두지 않는다.** 이 노드는 서버가 이미 집계한 입력에 서버 자신의 산술 규칙을 다시 적용해 등급만 내는 호출이었다. 2026-11-05 모델 비교에서 이 노드가 서버의 `initial_risk_level`과 다른 값을 낸 것은 167회 중 6회이며, 6회 모두 규칙 위반이었다([#583 코멘트](https://github.com/ProjectVigilantis/vigilantis/issues/583#issuecomment-6654327195)).
- **SecOps 출력의 `reviewed_risk_level`은 null로 둔다.** 서버 값을 복사하지 않으며("대안과 기각 이유" 1), 값이 실려 있으면 Workflow 검사가 `REVIEWED_RISK_MISMATCH`로 거절한다. 이 필드가 다시 의미를 갖는 것은 자기 입력과 평가셋을 가진 맥락 보정 노드가 생길 때이며, 그 도입은 후속 결정이다. 자동 선제 차단은 `initial_risk_level`이 구동하므로 영향이 없다.
- **두 그래프 모두 "분석 먼저" 순서로 돌아간다.** 이 ADR의 원래 순서이며, 요약↔후보 일치는 요약을 후보 뒤에 두는 대신(#324) `decision` 필드와 코드의 decision↔후보 대응으로 맞춘다(설계 원칙 5).
- **SecOps 실행 계획은 코드가 조립한다.** 이 단계에 모델 호출은 없다.
- **FinOps는 AI 소관 파라미터를 위해 실행 계획 단계의 모델 호출을 유지한다.**
- **호출 수는 비규범이다.** 초기 구현은 SecOps 1회, FinOps 2회다. 어떤 값을 모델이 채우고 어떤 값을 코드가 정하는지는 규칙이 있느냐로 정하며, 호출 수는 그 결과이지 목표가 아니다.

**설계 원칙 5항**:

1. **도메인별 분리** — 단일 그래프에 조건 Edge를 두면 합집합 State가 필요해 한쪽 전용 필드가 nullable로 혼입된다. 두 그래프로 나눠 출력 불변식을 그래프 단위로 강제한다. (`domain`은 Workflow가 설정하며 그래프 노드가 이를 바꿀 수 없다 — #49 불변식 2) SecOps 전용 `reviewed_risk_level`은 초기 위험도를 덮어쓰지 않는다 — 1차 개정 이후 SecOps 그래프는 맥락 보정 노드가 생기기 전까지 이 값을 null로 낸다(위 결정 참조).
2. **상태 비보관** — Checkpointer를 두지 않고 업무 상태는 PostgreSQL 단일 원천으로 둔다. 승인 중단점도 없다 — 그래프는 한 번 호출되면 Terminal 결과 반환 후 종료한다. 중복 호출은 Incident의 AI 호출 상태를 PostgreSQL 트랜잭션 안에서 원자적으로 선점(Claim)해 막는다 — 프로세스 수와 무관하게 성립한다. (상태 이원화와 서버 타이머·그래프 수명의 결합 회피)
3. **책임 한정** — 그래프는 분석·후보 생성까지만 담당하고 Guardrail·DB 저장·AWS 실행·승인은 밖에 둔다. 모델 호출은 이 ADR에서 도입하는 `AIModelClient` 경계를 통해서만 수행해 SDK 타입·자격증명·예외가 `AIModelClient` 구현 밖의 그래프·도메인 코드로 새지 않게 한다. (테스트 주입용 경계이며 Provider 교체·배포 방식의 확정이 아님)
4. **근거는 구조화 필드로** — 판단 근거는 `Incident`의 요약 3줄·`initial_risk_reason_codes`·`reviewed_risk_level`(1차 개정 전 행에 한함 — 이후 SecOps 그래프는 null), `RunbookCandidate.evidence_ids`, `GuardrailEvaluation` 4단계 결과에 남긴다. 호출 메타는 구조화 로그로 출력하되 업무·감사 데이터의 기준은 PostgreSQL이며 운영 로그는 감사 근거가 아니다. `evidence_ids`가 가리키는 Evidence는 생성 후 변경하지 않는다 — 근거가 바뀌면 판단 설명이 성립하지 않는다. (`RiskReasonCode` 값 목록은 2026-08-31 확정 — PR #206, `packages/schemas/events.py`)
5. **근거 선행** — 모델이 결정을 내는 호출에서는 근거가 결정에 선행한다. (1차 개정 신설)
   - **"결정"의 범위** — 이 원칙에서 결정은 조치/무조치의 선택, 곧 사람에게 제시할 선택지의 선택이다. 실행 계획 단계에서 파라미터를 채우는 것은 결정이 아니라 전사다.
   - **`decision`** — 근거 3줄 뒤에 오는 구조화 필드로, 사람에게 제시할 조치의 집합을 뜻한다.
   - **구조와 평가의 구분** — 구조가 보장하는 것(근거 3줄이 `decision`보다 먼저 오는 순서, `decision`과 후보의 대응)과 평가로 재는 것(근거가 결정을 뒷받침하는가)을 가른다.
   - **대체 불가** — 실행 계획 단계는 `decision`을 전사할 뿐 다른 조치로 대체할 수 없다.
   - **근거와 설명의 구분** — 결정 뒤에 생성된 텍스트는 설명이며 근거라 부르지 않는다.
   - **이유** — 결정보다 먼저 생성된 근거는 결정의 조건이 될 수 있지만, 결정 뒤에 생성된 텍스트는 이미 내려진 결정을 설명할 뿐이다. 2026-11-05 모델 비교에서 SecOps 요약 호출은 등급 호출이 낸 틀린 등급을 바른 사실로 충실히 설명했다([#583 코멘트](https://github.com/ProjectVigilantis/vigilantis/issues/583#issuecomment-6654327195)). 그래서 이 개정은 등급을 모델 호출에서 빼고 결정을 근거 뒤에 둔다.

### 실행 계획 단계

실행 계획 단계(FinOps는 모델 호출, SecOps는 코드)는 근거 3줄과 `decision`, capabilities와 원래의 타입 입력을 받아 `decision`을 후보로 전사하며, 다른 조치로 대체할 수 없다. "실현 불가"는 모델이 쓰는 판정이 아니라 코드의 결과이며, 그 출력은 FAILED가 된다. 가드레일·승인·실행은 그래프 밖에 남고, 가드레일 4단계와 그 실패 처리는 바뀌지 않는다(원칙 3 불변).

미보존 대상:

| 항목 | 이유 |
| --- | --- |
| Prompt 전문·원본 응답 | 재호출마다 변동하는 텍스트는 감사 근거로 부적합 — 로그 레벨과 무관하게 저장·출력하지 않는다 |
| 모델의 내부 추론 텍스트 | 검증·재현 불가 — 후보 정당성은 `evidence_ids` 참조 검증으로 대체. 팀 문서가 "CoT 3줄 요약"이라 부르는 최종 요약(`Incident.summary_lines`)은 저장·노출 대상이며 여기서 말하는 미보존 대상이 아니다 |
| 단가·계산된 비용 | 토큰 수는 사용량 집계용이며 청구 비용의 근거가 아니다(캐싱·할인·요금제 차이) |

노드 분할과 호출 수명주기 상세는 구현 단계에서 정한다.

## 대안과 기각 이유 (1차 개정)

| # | 대안 | 기각 이유 | 근거 |
| --- | --- | --- | --- |
| 1 | `reviewed_risk_level`에 서버 `initial_risk_level` 값을 복사 | 서버 값을 AI "정밀 평가"로 보이게 해 사실과 다르다 | 출력 계약은 null을 허용한다(`AgentGraphOutput`). 웹 표기는 함께 바꾼다. |
| 2 | SecOps 요약을 후보 뒤에 두는 현행 순서 유지 | 요약이 실제 후보를 설명하긴 하지만 결정의 조건이 되지 못한다. 요약↔후보 일치라는 같은 목표는 `decision` 필드와 코드 대응으로 달성된다 | 2026-11-05 비교에서 설명 호출이 틀린 등급을 바른 사실로 충실히 설명했다(설계 원칙 5 "이유") |
| 3 | `decision`을 정확히 하나로 제한 | 대상이 서로 다르거나 함께 실행해야 하는 조치를 표현할 수 없다 | #243의 후보 2건 35/60은 Runbook 이름 노출의 결과이지 2건이 결함이라는 근거가 아니다. 현 FinOps 후보 프롬프트가 그 경우를 허용한다 |
| 4 | 모델이 "실현 불가"를 출력 | 검증할 수 없는 도피구가 된다 — #243이 막은 NO_PROPOSAL 도피와 같은 종류다 | 코드가 기존 사유 코드로 판정할 수 있다("실행 계획 단계") |

## Consequences (결과·트레이드오프)

**장점**

- 도메인별 출력 불변식을 그래프 단위로 강제 → 계약 위반 조기 노출
- 업무 상태 원천이 PostgreSQL 단일 → 재시작 복구 경로 단일화
- `FakeAIModelClient` 주입으로 실제 API 없이 두 경로 검증 가능
- 후보가 참조한 근거를 계약으로 검증 가능 → 판단 경위 설명 가능
- (1차 개정) 세 확인 차원(근거와 `decision`의 순서, decision↔후보 대응, 근거의 뒷받침)이 분리된다
- (1차 개정) SecOps 위험 등급이 모델 호출에서 나오지 않는다 — 2026-11-05 비교의 규칙 위반 6건이 생긴 경로가 없어진다

**바뀌는 것(1차 개정)**

- SecOps 정상 경로 모델 호출은 3회에서 1회가 된다. FinOps는 2회다. 호출 수는 비규범이다(Decision 참조)
- SecOps 출력의 `reviewed_risk_level`은 null이 된다. 과거 행의 AI 재평가 값은 그대로 남는다

**비용/유의**

- 호출 중 프로세스 종료 시 남은 Claim을 회수한 뒤 처음부터 재호출 — 부분 재개 미지원. 회수·재시도 규칙은 호출 수명주기 결정에서 정한다
- Prompt 미보존으로 과거 판단을 그대로 재현할 수 없음 — 입력 snapshot 재호출 시 결과 상이 가능
- 공통 노드 미추출 시 두 그래프에 중복 발생
- 그래프 내부 State를 `packages/schemas` 계약과 분리해 별도 정의 필요
- 그래프 호출 중복은 DB Claim으로 막지만 Scheduler·Dispatcher의 인메모리 상태는 별개 문제 — 다중 worker·replica 실행 토폴로지는 **별도 결정 대상**
- LLM 운영비 지표는 **MVP 제품 기능 제외** — 필요 시 로그의 토큰 사용량을 집계해 오프라인으로 추정
- 노드명과 세부 분할은 비규범적이라 개정 대상이 아님. 다만 도메인 분리·영속성·부수효과(노드의 DB·AWS 접근)·감사 경계를 바꾸는 변경은 **이 ADR의 개정 대상**
- (1차 개정) `decision` 미보존에 따른 감사 공백과, 실현 불가·대응 불일치 FAILED에서의 근거 3줄 손실을 받아들인다 — 둘 다 해소하려면 계약 변경이 필요한 후속 결정이다
- (1차 개정) 요약 출력 스키마가 두 도메인에 공유되므로 FinOps·SecOps 모두 기준선을 다시 잡아야 하며, 과거 품질 수치는 이력으로만 남고 이어지지 않는다
- (1차 개정) 웹 앱의 `reviewed_risk_level` 표기와, 이 값이 null이 아니어야 통과하는 평가·테스트는 함께 바뀐다

## Related

- 확정 계약: `packages/schemas/agents.py` — LangGraph 입출력 (#49)
- 현황 기준: `docs/PROJECT_STATUS.md`
- 선행 결정: [ADR-0001](0001-mvp-monorepo-structure.md) — 단일 `apps/core-api` 구조
- 선행 결정: [ADR-0002](0002-runbook-whitelist-mvp-scope.md) — AI 추천 7종
- 선행 결정: [ADR-0004](0004-rollback-runbook-whitelist-registration.md) — 롤백 3종 AI 추천 금지
- 영향 범위: `apps/core-api/ai/**`(agent·graph·model_client·openai_client), AI 호출을 Claim하는 Workflow와 Incident 저장·조회 계층(`apps/core-api/db/**`), AI 호출 상태 계약(`packages/schemas/incidents.py`)
- 1차 개정 관련: #434(AI 호출 경계 후속), #324(SecOps 순서 변경), #243(메뉴 실측), #237(파라미터 흔들림), #251(규칙 도출 파라미터), #570(서버 rule number)
- 1차 개정 관련 결정: [ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md) — 가드레일 ④ 사전 검사 계약
- 1차 개정 구현 계획: #583
- 1차 개정 근거: [#583 코멘트](https://github.com/ProjectVigilantis/vigilantis/issues/583#issuecomment-6654327195) — 2026-11-05 모델 비교 요약
- 결정 로그: `docs/PROJECT_STATUS.md` §확정 결정 로그 (1차 개정 행은 주간 SSOT 갱신에서 추가)

## 개정 이력

- **2026-11-05 (1차 개정)** — 설계 원칙 5항 "근거 선행"을 신설하고 SecOps 그래프를 근거 우선 순서로 되돌린다. 안성일 제안 · 김세혁 승인(PR #584).
  - **이유**: 2026-11-05 모델 비교에서 `reassess_risk`가 서버의 `initial_risk_level`과 다른 값을 낸 6회(167회 중)는 모두 규칙 위반이었고, 뒤의 요약 호출은 그 틀린 등급을 바른 사실로 충실히 설명했다([#583 코멘트](https://github.com/ProjectVigilantis/vigilantis/issues/583#issuecomment-6654327195)). #324(2026-09-21, commit `dd22cda`)가 SecOps 요약을 후보 뒤로 옮기면서 요약은 결정에 앞서는 근거가 아니라 사후 설명이 됐다.
  - **내용**: 설계 원칙 5항 신설, SecOps 그래프 형태 교체, 대안과 기각 이유 신설, 설계 원칙 1·4의 `reviewed_risk_level` 문장 수정. 확인 방법·입력·판정 표 등 구현 틀은 #583으로 옮겼고 16주차 상세 ADR에서 구현 결과로 다시 쓴다.
  - **불변**: 핵심 결정 4항 — 도메인 분리·상태 비보관·책임 한정·근거 구조화. 공개 출력 계약 `AgentGraphOutput`, 가드레일 4단계와 그 실패 처리, 승인·실행이 그래프 밖에 있다는 것(원칙 3), 자동 선제 차단의 `initial_risk_level` 구동, 미보존 대상, 노드명 비규범.

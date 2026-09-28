# AI 산출 정답지 (`expected_ai/`) — 고정 세트 6건

**계약**: `packages/schemas/agents.py :: AgentGraphOutput`
**대상**: `finops/input/`의 자산 중 `verdict == COST_CANDIDATE`인 **6건**(A1·A7·A11·A12·A14·A16)
**변환기**: `apps/core-api/ai/evaluation/cases.py :: finops_cases` — #237의 것을 **그대로 쓴다**(사본을 만들지 않는다)
**회귀**: `tests/test_golden_ai_dataset.py`
**카드**: [#234](https://github.com/ProjectVigilantis/vigilantis/issues/234) — 판정 기준 ⓒ를 같은 입력으로 반복 확인 가능하게 만든다

## 왜 필요했나

판정 기준 ⓒ는 *"AI가 Golden Dataset FinOps에서 CoT 3줄 + Runbook 추천 산출"* 인데,
**그 산출을 대조할 정답지가 없었다.** 그래프를 검증하는 자리 둘이 모두 합성 입력을 썼다.

| 자리 | 입력 | 골든 사용 |
| --- | --- | --- |
| `apps/core-api/ai/tests/test_finops_graph.py` | `make_input()` 손으로 만든 상수 | ❌ |
| `scripts/smoke_finops_graph.py` | 합성 인시던트 1건 | ❌ |
| **`tests/test_golden_ai_dataset.py`** | **골든 6건** | ✅ |

골든 FinOps는 규칙 엔진 축(`expected/`)에만 쓰이고 있었다. 같은 입력이 AI 그래프로도 흐르는데
그 축의 정답지가 없어, **골든 입력이 그래프 계약을 통과하는지조차 확인된 적이 없었다.**

## 대상이 16건이 아니라 6건인 이유

| golden 판정 | 포함 | 근거 |
| --- | --- | --- |
| `COST_CANDIDATE` (EC2 6건) | ✅ | `services/rule_engine.py`가 AI로 넘긴다고 적은 대상 |
| `THREAT`(SG) · `UNUSED`(SG) | ❌ | SG를 대상으로 삼는 런북이 전부 SecOps 도메인(`schemas/runbooks.py`) |
| `SKIP` | ❌ | 판정 단계가 LLM 호출을 아끼려고 이미 거른 자산 |

건수를 손으로 세지 않는다 — `test_expected_ai_covers_the_whole_fixed_set`이 변환기 산출과
`case_id` 집합으로 대조한다. 골든이 늘어 `COST_CANDIDATE`가 생기면 **그 테스트가 먼저 실패한다.**

## 정답을 값이 아니라 **명세**로 적는다

산출이 LLM이라 축마다 못박을 수 있는 정도가 다르다. **규칙에서 도출되는 만큼만 적는다.**

| 축 | 어떻게 담나 | 근거 |
| --- | --- | --- |
| `invocation_status` | **정확한 값** `SUCCEEDED` | `AgentGraphOutput._enforce_contract`가 상태별 모양을 강제 |
| `summary_line_count` | **정확한 값** `3` | 독립 축이 아니라 `invocation_status`에 딸린 값 — `SUCCEEDED`·`NO_PROPOSAL`이 3, **`FAILED`가 0** |
| `reviewed_risk_level` | **정확한 값** `null` | FINOPS는 계약상 항상 `null` |
| `candidates[].target_arn` | **정확한 값**(그 케이스의 EC2) | 아래 §target_arn |
| `candidates[].runbook_id` | **멤버십** (⊆ capabilities) | 아래 §runbook_id |
| `candidates[].evidence_ids` | **부분집합** (⊆ 입력 evidences, 후보당 1개 이상) | 아래 §evidence_ids |

**담지 않는 것**: `summary_lines`의 문장 내용 · `candidates[].parameters` · 후보 순서 · 토큰/시간.
문장 품질 판정은 **#237의 결함 체크리스트·사실 정합성 검사** 몫이다 — 여기서 중복해 재지 않는다.

### §target_arn — 왜 정확한 값으로 못박아도 되나

그래프는 `target_arn`을 `_allowed_target_arns()` 안으로 강제한다(`ai/agent.py`).
**그런데 그 목록에는 인시던트 EC2와 관계 SG가 함께 있어 항목이 2개다** — 목록만으로는 답이 하나로 좁혀지지 않는다.

EC2로 못박는 근거는 **capabilities**다.

1. `build_capabilities()`(`ai/capabilities.py`)가 **대상 자산 유형이 subject와 같은 런북만** 메뉴에 올린다
2. 각 `RunbookCapability`가 `allowed_target_asset_types=[EC2]`를 싣는다
3. 메뉴에 오른 두 런북의 파라미터 계약이 `instance_id: InstanceId`(패턴 `i-…`)를 요구한다 — SG로는 만들 수 없다

**즉 EC2라는 답은 규칙에서 도출되며 산출 관찰이 아니다.** 그리고 **이 축이 케이스 6건을 서로 구분한다** —
나머지 축은 6건에서 값이 같아, 이 축이 없으면 정답 파일 6개가 서로 구별되지 않는다.

### §runbook_id — 왜 `RIGHTSIZING`으로 못박지 않나

메뉴에 `RUNBOOK_EC2_RIGHTSIZING`과 `RUNBOOK_EC2_ENABLE_AUTOSCALING` **둘이 오르고 그래프는 둘 다 받는다.**
실측상 산출이 `RIGHTSIZING`으로 모이더라도 그것은 **관찰**이지 규칙이 정한 답이 아니다 —
값으로 못박으면 정답지가 **현재 구현을 기록**하게 된다(#223 원칙).

어느 런북이 더 나은 추천인가는 이 정답지가 재는 축이 아니다. 판정 기준 ⓒ의 문장은
*"추천을 산출한다"* 이지 *"가장 좋은 추천을 고른다"* 가 아니다.

### §evidence_ids — 왜 부분집합인가

입력 Evidence 집합은 변환기가 결정적으로 만들지만, **그중 몇 개를 인용할지는 모델이 고른다.**
그리고 그 부분집합 관계를 **그래프는 검사하지 않는다** — `ai/agent.py` 헤더가 *"계약이 Workflow 몫으로
못 박았다"* 고 적고 있다. 정답지가 이 관계를 담는 이유가 그것이다: **아무도 안 보는 축이었다.**

## 회귀가 재는 것 — 그리고 재지 않는 것

`tests/test_golden_ai_dataset.py`는 **모델을 부르지 않는다**(`FakeAIModelClient`).
실제 호출은 과금이라 CI 인자에 넣지 않는다. 그래서 재는 것은 셋이다.

| | 재는 것 |
| --- | --- |
| ① | **골든 입력이 그래프 계약을 통과하는가** — `_to_draft` → `RunbookCandidateDraft` → `AgentGraphOutput` 조립 |
| ② | **그래프의 불변식이 골든 입력에서 실제로 작동하는가** — 음성 케이스 2건(남의 자산 ARN · 메뉴 밖 런북 → 둘 다 `FAILED`) |
| ③ | **명세가 계약·변환기 산출과 어긋나지 않는가** — 골든이나 변환기가 움직이면 여기서 먼저 걸린다 |

**재지 않는 것**: 산출 문장의 품질. 스텁이 문장을 주므로 이 파일은 그것을 판정할 수 없다.

### 가드가 헛돌지 않는 것을 확인한 방법

초록불은 아무것도 안 하는 테스트도 통과시킨다. 뮤테이션으로 재 봤다.

| 오구현 재현 | 잡은 테스트 |
| --- | --- |
| `target_arn` 불변식 제거(`ai/agent.py`) | `test_graph_rejects_a_target_arn_outside_the_input_assets` |
| `capabilities` 불변식 제거(`ai/agent.py`) | `test_graph_rejects_a_runbook_outside_the_offered_capabilities` |
| 정답지의 `evidence_ids` 인용을 틀리게 | `test_expected_ai_input_facts_match_the_converter[A11]` · `test_golden_input_satisfies_the_expected_ai_spec[A11]` |
| 정답지에서 케이스 1건 누락 | `test_expected_ai_covers_the_whole_fixed_set` |

넷 다 의도한 테스트가 잡았고, 변형은 사본을 떠 두고 원복했다.

## 입력을 복제하지 않는다

`expected_ai/`는 **`finops/input/`을 그대로 재사용한다.** 파일명도 입력과 짝을 맞춘다
(`asset_inventory_001.json` ↔ `asset_inventory_001.json`).
복제하면 규칙 엔진 축과 AI 축이 서로 다른 자산을 보게 되고, **그 어긋남은 아무 테스트도 잡지 못한다.**

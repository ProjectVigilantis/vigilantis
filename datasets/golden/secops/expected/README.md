# SecOps 정답(expected) — 10건 작성 완료

위협 입력 10건(`secops/input/`)에 **1:1로 대응하는 정답 파일 10개**가 이 폴더에 있다.
판정 규칙은 2026-08-31 확정됐고(PR #206 — Risk Evaluator: `RiskReasonCode` 값 목록·위험도 임계),
그 규칙대로 정답을 채웠다(J3, 박지현). 보류 사유(규칙 미확정)는 해소됐다.

**정답은 `evaluate_threat`의 산출을 베낀 것이 아니다.** 확정 규칙에서 손으로 도출한 값을 적고,
`tests/test_golden_dataset.py`가 실제 산출과 대조한다. 산출을 베끼면 구현이 틀려도 정답지가
함께 틀려 **회귀를 못 잡는다.** 불일치가 나면 규칙 해석 오류이거나 구현 버그 둘 중 하나다.
각 파일의 `derivation` 필드에 그 도출 과정이 한 줄로 적혀 있다.

## 정답 3값 · 계약 위치

위협 이벤트의 "판단/대응 결과"는 다음 세 값을 뜻한다.

| 값 | 계약 위치 |
| --- | --- |
| `initial_risk_level` (HIGH/MEDIUM/LOW) | `packages/schemas/events.py :: InitialRiskEvaluationResult` |
| `response_mode` (`PRE_MITIGATION_0_5S` / `AGENT_WAIT`) | 〃 |
| `reason_codes` (`RiskReasonCode`) | 〃 |

셋 다 확정됐다(2026-08-31, PR #206):

- `packages/schemas/events.py` — `RiskReasonCode` Enum 6종 확정, `reason_codes` = list[RiskReasonCode](최소 1개).
- `apps/core-api/security/risk_evaluator.py` — `evaluate_threat()` 구현. 판정 규칙은 이 파일 상단·PR #206 참고.

## 파일 형식

```json
{
  "source": "secops/input/evt_ssh_bruteforce_001.json",
  "contract": "packages/schemas/events.py :: InitialRiskEvaluationResult (부분집합)",
  "excluded_fields": { "threat_event_id": "정규화 단계에서 부여되는 런타임 값 — 정답 대조 대상 아님" },
  "thresholds_at_authoring": { "SSH_HIGH_ATTEMPT_MIN": 10, "SSH_HIGH_RATE_PER_MIN": 20.0, "...": "..." },
  "case_id": "S3",
  "purpose": "120회 / 300초 — 명백한 고강도 공격. 선제 차단 경로가 열리는 기준선",
  "derivation": "rate = 120 × 60 / 300 = 24.0/min. count 120 ≥ 10 AND rate 24.0 ≥ 20.0 → HIGH",
  "initial_risk_level": "HIGH",
  "response_mode": "PRE_MITIGATION_0_5S",
  "reason_codes": ["RISK_SSH_BRUTEFORCE"]
}
```

`thresholds_at_authoring`에는 **그 케이스의 판정이 실제로 의존하는 상수만** 적는다(OPEN_IP는 CIDR·포트 계열,
SSH는 횟수·속도 계열). `test_secops_thresholds_not_drifted`가 이 값을 현재 `risk_evaluator` 상수와 대조하므로,
**임계값을 바꾸는 PR은 정답지 갱신을 포함해야 한다**(ADR-0006 임계값 결합 원칙).

## 판정 결과 요약

| 케이스 | 파일 | 위험도 | 대응 경로 | reason_codes |
| --- | --- | --- | --- | --- |
| S1 | `evt_open_ip_001.json` | MEDIUM | `AGENT_WAIT` | `OPEN_INGRESS_WORLD` · `SENSITIVE_PORT_EXPOSED` |
| S2 | `evt_open_ip_002.json` | MEDIUM | `AGENT_WAIT` | `OPEN_INGRESS_WORLD` · `ALL_PROTOCOL_OPEN` |
| S5 | `evt_open_ip_003.json` | MEDIUM | `AGENT_WAIT` | `OPEN_INGRESS_WORLD` · `ALL_PORTS_EXPOSED` |
| S6 | `evt_open_ip_004.json` | MEDIUM | `AGENT_WAIT` | `OPEN_INGRESS_WORLD` · `SENSITIVE_PORT_EXPOSED` |
| S7 | `evt_open_ip_005.json` | MEDIUM | `AGENT_WAIT` | `OPEN_INGRESS_WORLD` · `SENSITIVE_PORT_EXPOSED` |
| S3 | `evt_ssh_bruteforce_001.json` | **HIGH** | `PRE_MITIGATION_0_5S` | `SSH_BRUTEFORCE` |
| S4 | `evt_ssh_bruteforce_002.json` | LOW | `AGENT_WAIT` | `SSH_LOW_SIGNAL` |
| S8 | `evt_ssh_bruteforce_003.json` | LOW | `AGENT_WAIT` | `SSH_LOW_SIGNAL` |
| S9 | `evt_ssh_bruteforce_004.json` | **HIGH** | `PRE_MITIGATION_0_5S` | `SSH_BRUTEFORCE` |
| S10 | `evt_ssh_bruteforce_005.json` | **HIGH** | `PRE_MITIGATION_0_5S` | `SSH_BRUTEFORCE` |

**S5·S10의 원래 논점(자산 문맥)은 확정 규칙 ②로 답이 났다** — 초기 위험 판정은 위협 정보만 본다.
조치 가능성(S5의 `default` SG)과 운영 자산 여부(S10의 prod EC2)는 가드레일·실행 단계가 판단한다.
그래서 S10은 S3와 **판정이 같아야 하고**, 달라지면 규칙이 깨진 것이다.

## 알려진 커버리지 공백 — SSH `MEDIUM` 밴드

확정 규칙의 SSH 분기는 셋인데(HIGH / MEDIUM / LOW), **입력 10건은 MEDIUM을 하나도 만들지 않는다.**
MEDIUM은 "횟수·속도 중 한쪽만 충족"(예: 60회/600초 = 분당 6)일 때 나온다.

지금은 `apps/core-api/security/tests/test_risk_evaluator.py`가 합성 이벤트로 그 밴드를 덮고 있어
**규칙 자체는 검증된다.** 다만 Golden Dataset 기준(판정 분기 전량 커버 — FinOps는 `Verdict` 4종·
`SkipReasonCode` 5종 전량)으로 보면 입력 1건이 비어 있다. `test_secops_expected_covers_every_risk_level`은
**전체 위험도 3등급**을 보므로 현재는 통과한다(OPEN_IP MEDIUM이 있다).

→ SSH MEDIUM 입력 케이스(S11) 추가는 **별도 판단 사항**이다. 4주차 판정 기준 ⓔ가 "20건"을 기준으로
잡고 있어 임의로 늘리지 않았다.

## 입력 4건과 판정 논점

| 케이스 | 파일 | 입력 요약 | 규칙 확정 시 판단해야 할 논점 |
| --- | --- | --- | --- |
| S1 | `evt_open_ip_001.json` | tcp 22 / `0.0.0.0/0` | 전형적 SSH 전체개방. `finops/input`의 `sg-...00005`와 **같은 ARN** — 자산 문맥 조인 검증용 |
| S2 | `evt_open_ip_002.json` | protocol `-1` / 포트 `null` | 전 프로토콜 개방. 포트가 `null`일 때의 위험도 산정 방식 |
| S3 | `evt_ssh_bruteforce_001.json` | 120회 / 300초 | 명백한 고강도 공격 |
| S4 | `evt_ssh_bruteforce_002.json` | 5회 / 3600초 | **저강도 — 위협인가 사용자 오타인가.** 위험도 하한선을 정하게 만드는 케이스 |

## 입력 6건 추가 (2차) 과 판정 논점

| 케이스 | 파일 | 입력 요약 | 규칙 확정 시 판단해야 할 논점 |
| --- | --- | --- | --- |
| S5 | `evt_open_ip_003.json` | `tcp 0–65535` / `0.0.0.0/0` | 대상이 `finops/input/asset_inventory_002.json`의 **`default` SG(A8)** 다. 위협이지만 삭제·변경이 불가능해 **조치할 수 없는 자산** — Risk 판정이 자산 문맥을 봐야 하나, 아니면 판정과 조치 가능성을 분리하나 |
| S6 | `evt_open_ip_004.json` | `tcp 3389` (RDP) / `0.0.0.0/0` | 포트 종류가 위험도를 바꾸나. SSH 22(S1)와 같은 등급인가 |
| S7 | `evt_open_ip_005.json` | `tcp 22` / `::/0` | **IPv6 전체개방.** `0.0.0.0/0` 문자열만 보면 놓친다. `source_cidr` 판정을 IPv4에 한정하지 않았는지 |
| S8 | `evt_ssh_bruteforce_003.json` | 1회 / 1초 | 계약 최소값(`ge=1`). **위험도 하한선** — LOW인가, 판정 대상 제외인가. S4(5회/3600초)보다 더 아래 |
| S9 | `evt_ssh_bruteforce_004.json` | 1000회 / 60초 | 상한 없음 확인 + `PRE_MITIGATION_0_5S` 발동선이 어디인가. S8과 **같은 EC2**를 대상으로 해 강도만 다른 사다리를 만든다 |
| S10 | `evt_ssh_bruteforce_005.json` | 120회 / 300초 | **S3와 강도·`source_ip` 동일, 대상만 다름**(A6의 prod 보호 EC2). 자산이 prod면 위험도가 올라가나 — 통제된 대조 실험 |
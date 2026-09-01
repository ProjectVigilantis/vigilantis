# SecOps 정답(expected) — 12건 작성 완료

위협 입력 12건(`secops/input/`)에 **1:1로 대응하는 정답 파일 12개**가 이 폴더에 있다.
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
| S11 | `evt_ssh_bruteforce_006.json` | **MEDIUM** | `AGENT_WAIT` | `SSH_BRUTEFORCE` |
| S12 | `evt_ssh_bruteforce_007.json` | **MEDIUM** | `AGENT_WAIT` | `SSH_BRUTEFORCE` |

**S5·S10의 원래 논점(자산 문맥)은 확정 규칙 ②로 답이 났다** — 초기 위험 판정은 위협 정보만 본다.
조치 가능성(S5의 `default` SG)과 운영 자산 여부(S10의 prod EC2)는 가드레일·실행 단계가 판단한다.
그래서 S10은 S3와 **판정이 같아야 하고**, 달라지면 규칙이 깨진 것이다.

## SSH `MEDIUM` 밴드 — 2026-09-01 해소

확정 규칙의 SSH 분기는 넷인데(단발 LOW / HIGH / 저강도 LOW / **한쪽만 충족 MEDIUM**), 초기 입력 10건은
MEDIUM 분기를 하나도 만들지 않았다. **S11·S12 를 추가해 그 밴드의 두 갈래를 각각 덮는다.**

| 갈래 | 케이스 | 입력 | rate | 판정 근거 |
| --- | --- | --- | --- | --- |
| 지속적이나 발동선 미만 | **S11** | 60회 / 600초 | 6.0/분 | count 60 ≥ 10 충족 · rate 6.0 < 20.0 미달 |
| 짧은 버스트 | **S12** | 5회 / 10초 | 30.0/분 | count 5 < 10 미달 · rate 30.0 ≥ 20.0 충족 |

**뮤테이션으로 잡는 힘을 확인했다.** `risk_evaluator.py` 의 HIGH 분기 조건 `and` 를 `or` 로 바꾸면
`tests/test_golden_dataset.py` 55건 중 **S11·S12 의 판정 대조(`test_secops_verdicts_match_expected`)
2건만 실패하고 나머지 53건은 전부 통과한다** — 골든 입력 10건이 이 버그를 하나도 못 잡았다는 뜻이다.
(S11·S12 가 더하는 테스트는 6건이지만 `test_secops_input_validates`·`test_secops_thresholds_not_drifted`
4건은 판정을 보지 않아 이 뮤테이션에 걸리지 않는다.)

**다만 규칙 자체는 이미 유닛이 지키고 있었다.** 같은 뮤테이션에서
`apps/core-api/security/tests/test_risk_evaluator.py` 의 `test_ssh_medium_band_sustained_below_rate` ·
`test_ssh_short_burst_is_medium_not_low` 2건도 함께 깨진다(#210 · `6a850fd`). 특히 앞 것의 합성 이벤트는
**S11 과 같은 60회/600초**다. 그래서 S11·S12 의 값은 "규칙을 처음 검증한다"가 아니라 **팀 공유 정답지에서
판정 분기를 전량 덮는다**에 있다 — FE·AI·BE 가 함께 쓰는 입력은 `datasets/golden/` 쪽이고, 유닛의 합성
이벤트는 `security/` 안에서만 쓰인다.

**S12 는 S4 와 통제된 대조를 이룬다** — 대상 EC2(`...00002`) · 공격 IP(`198.51.100.77`) · 실패 횟수(5회)가
모두 같고 **관측 창만 다르다**(3600초 vs 10초). 속도 조건 하나로 LOW 와 MEDIUM 이 갈리는 것을 보이므로,
rate 계산식이 사라지거나 창을 무시하면 이 짝이 먼저 깨진다.

**S11 은 S8 과 같은 EC2(`...00012`) · 같은 공격 IP(`198.51.100.20`) 를 쓴다** — 1회/1초(LOW) →
60회/600초(MEDIUM) 로 같은 출처가 강도를 올린 사다리가 된다. S9(1000회/60초 · HIGH)도 같은 EC2 를
대상으로 하지만 **공격 IP 는 다르다**(`203.0.113.55`).

> 처음에는 4주차 판정 기준 ⓔ의 "20건"을 흐릴까 봐 보류했으나, **이 2건을 넣기 전에도 골든은 이미 26건**이었다
> (`datasets/golden/README.md` — 추가 후 28건). SSOT 숫자가 그와 별개로 낡아 있어 보류 사유가 성립하지 않았다.
> 건수 기준 갱신은 PM 몫이라 여기서는 사실만 남긴다.

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

## 입력 2건 추가 (3차) 과 판정 논점

| 케이스 | 파일 | 입력 요약 | 이 케이스가 지키는 것 |
| --- | --- | --- | --- |
| S11 | `evt_ssh_bruteforce_006.json` | 60회 / 600초 | **횟수만 충족한 지속형.** HIGH 분기가 `and` 가 아니라 `or` 로 바뀌면 이 케이스가 HIGH 로 넘어가 실패한다 |
| S12 | `evt_ssh_bruteforce_007.json` | 5회 / 10초 | **속도만 충족한 버스트.** S4(5회/3600초)와 횟수가 같아, rate 계산식이 사라지거나 창을 무시하면 두 케이스가 같은 판정으로 붙어 실패한다 |

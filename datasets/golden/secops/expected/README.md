# SecOps 정답(expected) — 작성 대기(규칙 확정됨)

이 폴더는 아직 비어 있다. 위협 입력(`secops/input/`)은 작성 완료했고,
**판정 규칙은 2026-08-31 확정됐다(PR #206 — Risk Evaluator: `RiskReasonCode` 값 목록·위험도 임계).**
남은 것은 그 규칙대로 정답을 채우는 작업(J3, 박지현)이며, 이 문서의 보류 사유(규칙 미확정)는 해소됐다.

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

## 남은 작업 (J3, 박지현)

확정된 규칙대로 10건의 정답을 채운다(형식은 `finops/expected/`와 동일). **여전히 값을 추정해 적지 말 것** —
정답은 `evaluate_threat`의 실제 산출과 대조해 고정하며, 규칙이 바뀌면 정답지와 `risk_evaluator` 상수를 함께 갱신한다.

```json
{
  "source": "secops/input/evt_ssh_bruteforce_001.json",
  "case_id": "S3",
  "initial_risk_level": "...",
  "response_mode": "...",
  "reason_codes": ["..."]
}
```

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
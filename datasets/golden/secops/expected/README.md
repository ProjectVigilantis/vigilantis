# SecOps 정답(expected) — 작성 보류

이 폴더는 의도적으로 비어 있다. 위협 4건의 입력(`secops/input/`)은 작성 완료했으나,
**예상 판단/대응 결과를 정의할 규칙이 아직 확정되지 않았다.**

## 보류 사유

위협 이벤트의 "판단/대응 결과"는 다음 세 값을 뜻한다.

| 값 | 계약 위치 |
| --- | --- |
| `initial_risk_level` (HIGH/MEDIUM/LOW) | `packages/schemas/events.py :: InitialRiskEvaluationResult` |
| `response_mode` (`PRE_MITIGATION_0_5S` / `AGENT_WAIT`) | 〃 |
| `reason_codes` (`RiskReasonCode`) | 〃 |

셋 다 판정 규칙이 미확정이며, 저장소는 **값을 추정하지 말 것**을 명시하고 있다.

- `packages/schemas/events.py` — `reason_codes` Enum은 Risk 판정 규칙 확정 시 교체한다.
  값을 지어내지 않기 위해 우선 문자열로 둔다.
- [ADR-0005](../../../docs/adr/0005-langgraph-stateless-domain-graphs.md) —
  `RiskReasonCode` 값 목록은 Risk 규칙 확정 전까지 미정. 저장 위치만 정한 것이다.
- `apps/core-api/security/` — Risk Evaluator 구현 자체가 아직 없다.

여기서 정답을 채우면 임계값을 추측해 적는 것이 되고, 나중에 실제 규칙이 정해졌을 때
**틀린 정답지가 회귀 테스트를 통과시키는** 더 나쁜 상태가 된다.

## 해소 조건

Risk 판정 규칙(`RiskReasonCode` 값 목록 + 위험도 임계값)이 확정되면 별도 PR로 채운다.
형식은 `finops/expected/`와 동일하게 맞춘다.

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
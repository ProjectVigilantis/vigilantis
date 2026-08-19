# Golden Dataset (담당: 박지현)

MVP 공통 테스트 정답지. 위협/자산 더미 데이터 20여 건을 `*.json`으로 적재.

- 낭비 자원 시나리오 10건 (예: CPU 2% 미만 Idle EC2, Unattached SG)
- 보안 위협 시나리오 10건 (예: 22번 포트 전체 개방 0.0.0.0/0, SSH 브루트포스)

전체 팀(UI/AI/백엔드)이 공유하며 pytest 회귀 테스트(`tests/`)의 입력으로 사용한다.

## 양식 (JSON Schema — `packages/schemas` Pydantic 모델에서 추출)

`schema/` 폴더의 JSON Schema가 데이터 작성 양식이다. VS Code에서 파일에 `"$schema"` 참조를 걸면 자동완성·검증이 된다.

| 파일 | 용도 | 원천 모델 |
| --- | --- | --- |
| `schema/mock_threat_event_input.schema.json` | 보안 위협 시나리오 1건 (`event_type`: `OPEN_IP` \| `SSH_BRUTE_FORCE`) | `schemas.events.MockThreatEventInput` |
| `schema/asset_inventory.schema.json` | 낭비 자원 시나리오 — 한 리전 1회 수집 결과(rule_engine 입력 단위) | `schemas.assets.AssetInventory` |

주의:
- 위협 입력에 `severity`·`response_mode` 넣지 말 것 — Risk Evaluator가 판정하며 `extra=forbid`로 거부됨.
- 자산 입력에 Idle/미사용 판정·`SKIP_*`·`health_score` 넣지 말 것 — rule_engine 산출값.
- 스키마는 추출본이다. `packages/schemas` 모델이 바뀌면 재추출 필요(원천은 항상 Pydantic 모델).

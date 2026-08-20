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
  ⚠️ 자산 입력(`AssetInventory`)은 `extra=forbid`가 **아니다.** 모르는 필드를 넣어도 에러 없이
  조용히 무시되므로 Pydantic 검증만으로는 잡히지 않는다. `tests/test_golden_dataset.py`의
  `test_finops_input_has_no_verdict_fields`가 원문 JSON을 직접 검사해 막는다.
- 스키마는 추출본이다. `packages/schemas` 모델이 바뀌면 재추출 필요(원천은 항상 Pydantic 모델).

## 폴더 구조

입력(input)과 정답(expected)을 분리한다. 입력 스키마에는 정답을 담을 자리가 없고,
자산 입력은 모르는 필드를 조용히 무시해 정답이 낡아도 드러나지 않기 때문이다.

```text
datasets/golden/
├── schema/                     # 입력 양식 (Pydantic 추출본)
├── finops/
│   ├── input/                  # AssetInventory — 한 리전 1회 수집 결과
│   └── expected/               # rule_engine 판정 정답
└── secops/
    ├── input/                  # MockThreatEventInput — 위협 1건 = 1파일
    └── expected/               # (작성 보류 — 사유는 해당 폴더 README 참고)
```

자산은 `AssetInventory`가 "한 리전 1회 수집 결과" 단위이므로 여러 자산을 한 파일에 담는다.
위협은 이벤트 1건이 곧 1단위이므로 파일을 나눈다.

## 정답(expected) 형식

`packages/schemas/rules.py`의 `RuleEvaluationResult` 8필드 중 **4개만** 사용한다.

| 필드 | 포함 | 사유 |
| --- | --- | --- |
| `asset_arn` | 포함 | 입력과의 대조 키 |
| `evaluation_status` | 포함 | 판정 수행 여부 |
| `verdict` | 포함 | 판정 결과 |
| `skip_reason_code` | 포함 | SKIP 사유 |
| `collection_run_id` | 제외 | 실행 시점 생성 런타임 값 |
| `evaluated_at` | 제외 | 실행 시점 생성 런타임 값 |
| `health_score` | 제외 | 계약·DB는 0~100 정수인데 `rule_engine.py:53`이 소수 반환 — 불일치 해소 전까지 제외 |
| `reason` | 제외 | 사람이 읽는 자유 서술 — 문자열 대조 대상 아님 |

`runbook_id`도 넣지 않는다. 판정→런북 매핑 코드가 저장소에 없어 적으면 추측이 된다.

`thresholds_at_authoring` 블록에 작성 시점의 rule_engine 임계값을 기록한다. JSON은 상수를
import할 수 없으므로 `tests/test_golden_dataset.py`가 현재 상수와 대조해 드리프트를 막는다.
([ADR-0006](../../docs/adr/0006-localstack-team-standard-env.md) 임계값 결합 원칙)

## 1차 작성 케이스 (9건)

**자산 5건** — `finops/input/asset_inventory_001.json` (판정 근거: `services/rule_engine.py`)

| ID | 자산 | 입력 | 판정 | 목적 |
| --- | --- | --- | --- | --- |
| A1 | EC2 | `cpu_avg 4.9 / max 10.0 / dp 336` | `COST_CANDIDATE` | 임계값 바로 아래 — 후보 선정 |
| A2 | EC2 | `cpu_avg 5.0 / max 10.0 / dp 336` | `SKIP_ACTIVE` | 임계값 정확히 — `<`를 `<=`로 쓰면 실패 |
| A3 | EC2 | `cpu_avg 2.0 / max 40.0 / dp 336` | `SKIP_LOW_UTIL` | 스파이크 정확히 — `>=`를 `>`로 쓰면 실패 |
| A4 | EC2 | `cpu_avg 1.0 / dp 47` | `SKIP_INSUFFICIENT_DATA` | 관측치 부족이 최우선인지 |
| A5 | SG | `attached true / tcp 22 전체개방` | `THREAT` | 전체개방 탐지 |

**위협 4건** — `secops/input/` (정답은 Risk 규칙 확정 후 별도 PR)

| ID | 파일 | 입력 |
| --- | --- | --- |
| S1 | `evt_open_ip_001.json` | `tcp 22 / 0.0.0.0/0` — A5와 **같은 SG ARN**(자산 문맥 조인 검증) |
| S2 | `evt_open_ip_002.json` | `protocol -1 / 포트 null` — 선택 필드 null 처리 |
| S3 | `evt_ssh_bruteforce_001.json` | 120회 / 300초 — 고강도 |
| S4 | `evt_ssh_bruteforce_002.json` | 5회 / 3600초 — 저강도(오탐 후보) |

2차(11건)로 미룬 분기: `SKIP_PROD_PROTECTED`, SG `UNUSED`, SG `default` 화이트리스트,
SG `SKIP_ACTIVE`, EBS, 복합 조건.
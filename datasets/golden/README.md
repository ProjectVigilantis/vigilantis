# Golden Dataset (담당: 박지현)

MVP 공통 테스트 정답지. 위협/자산 더미 데이터 34건을 `*.json`으로 적재.

- 낭비 자원 시나리오 18건 (예: CPU 2% 미만 Idle EC2, Unattached SG, `_is_prod` 경계, 미부착 EBS)
- 보안 위협 시나리오 16건 (예: 22번 포트 전체 개방 0.0.0.0/0, SSH 브루트포스, `OPEN_IP` 네 번째 분기)

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
| `health_score` | 제외 | 정수 변환은 `run_rule_engine`(`rule_engine.py:122`) 소관 — 골든셋은 `evaluate_*` 직접 호출이라 검증 범위 밖. 변환 자체는 `test_persistence_pipeline`과 스키마 계약(0~100 정수)이 커버 |
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

## 2차 작성 케이스 (11건 — 누적 20건)

**자산 5건** — `finops/input/asset_inventory_002.json`

1차 파일은 수정하지 않는다. `AssetInventory`가 "한 리전 1회 수집" 단위이므로 002는 두 번째
수집 회차로 성립하고, 1차 정답지가 그대로 얼려져 회귀 기준이 유지된다.

| ID | 자산 | 입력 | 판정 | 목적 |
| --- | --- | --- | --- | --- |
| A6 | EC2 | 이름에 `prod` 없음 / `Environment: production` 태그 / `cpu_avg 1.0` · `dp 336` | `SKIP_PROD_PROTECTED` | 태그 경로 검증 + prod 보호가 idle보다 우선 |
| A7 | EC2 | `dp 48` (경계 정확히) / `cpu_avg 4.9` / `cpu_max` **null** | `COST_CANDIDATE` | `<`를 `<=`로 쓰면 실패 + `cpu_max` null 가드 |
| A8 | SG | 이름 `default` / `attached false` / `tcp 22` 전체개방 | `SKIP_WHITELISTED` | 화이트리스트가 `THREAT`·`UNUSED`를 모두 이김 |
| A9 | SG | `attached false` / 개방 없음 | `UNUSED` | 미사용 SG 정리 후보 |
| A10 | SG | `attached true` / 개방 없음 | `SKIP_ACTIVE` | 정상 SG — 오탐 방지 음성 대조군 |

A6은 이름을 `golden-ec2-billing-worker`로 잡았다. `_is_prod`는 인식 키(`Environment`·`env`·
`Stage`·`Tier`, 대소문자 무시)의 값이 `prod`·`production`·`prd`(소문자 정확일치)일 때 prod로
본다(#95). 이름에 `prod`를 넣으면 이름 부분 매칭으로 되돌아가는 회귀가 생겨도 정답이 그대로
나와 태그 경로 검증이 무의미해지므로, 이름에서 `prod`를 뺀다.

**판정 커버리지**: 1차 5건 + 2차 5건으로 `Verdict` 4종·`SkipReasonCode` 5종 전부 커버(미커버 0). 3차는 판정 종류가 아니라 `_is_prod` 입력 경로를 넓힌다.

**위협 6건** — `secops/input/` (정답은 Risk 규칙 확정 후 별도 PR)

| ID | 파일 | 입력 |
| --- | --- | --- |
| S5 | `evt_open_ip_003.json` | `tcp 0–65535` 전 범위 / `0.0.0.0/0` — 대상은 A8의 `default` SG |
| S6 | `evt_open_ip_004.json` | `tcp 3389` (RDP) / `0.0.0.0/0` |
| S7 | `evt_open_ip_005.json` | `tcp 22` / `::/0` — IPv6 전체개방 |
| S8 | `evt_ssh_bruteforce_003.json` | 1회 / 1초 — 계약 최소값(`ge=1`) |
| S9 | `evt_ssh_bruteforce_004.json` | 1000회 / 60초 — 초고강도 |
| S10 | `evt_ssh_bruteforce_005.json` | 120회 / 300초 — 대상은 A6의 prod 보호 EC2 |

각 케이스가 강제하는 판정 논점은 `secops/expected/README.md` 표에 기록했다.

## 3차 작성 케이스 (자산 6건 — 누적 26건)

**`_is_prod` 경계 전용** — `finops/input/asset_inventory_003.json`

`docs/PROJECT_STATUS.md` §미해결 4번의 잔여②(확정 기준 반영 Golden 경계 케이스 추가)에 대응한다.
인식 기준은 #95 / PR #97 확정: **키**(`PROD_TAG_KEYS`, 대소문자 무시 **정확일치**) = `environment`·`env`·
`stage`·`tier` / **값**(`PROD_TAG_VALUES`, `strip` 후 소문자 **정확일치**) = `prod`·`production`·`prd`.
부분 문자열 매칭은 영구 금지(#81).

6건 전부 `cpu_datapoints 336` · `cpu_avg 1.5` · `cpu_max 10.0`으로 **입력이 동일하다.** 관측치 부족도
스파이크도 아니고 저활성은 확실하므로 **prod 판정만이 결과를 가르는 유일한 변수**다. 판정이 틀리면
원인은 반드시 `_is_prod`이며 다른 분기를 의심할 필요가 없다.

| ID | 태그 | 판정 | 막는 회귀 |
| --- | --- | --- | --- |
| A11 | `Environment: prod-us-east` | `COST_CANDIDATE` | 값 접두 부분일치 — SSOT 결정 로그가 지목한 "접미 변형 미탐은 의도된 결과" 그 케이스 |
| A12 | `Environment: non-prod` | `COST_CANDIDATE` | 값 접미 부분일치 (`"prod" in "non-prod"`) |
| A13 | `TIER: "  PRD  "` | `SKIP_PROD_PROTECTED` | 키 `lower()` · 값 `strip().lower()` 누락 — **정탐 방향** 경계 |
| A14 | `environment_name: production` | `COST_CANDIDATE` | 키 부분일치(`any(k in key.lower())`) — 값 쪽 방어의 대칭 |
| A15 | `Environment: staging` + `Stage: prd` | `SKIP_PROD_PROTECTED` | 첫 인식 키에서 조기 `return False` — 태그 순회 완주 검증 |
| A16 | `Environment: product-service` | `COST_CANDIDATE` | 이슈 #81이 제기한 **원 오탐 사례**를 회귀 기준으로 고정 |

**A13·A15가 정탐(prod로 잡아야 하는) 방향이라는 점이 중요하다.** 미탐 케이스만 있으면 "아무것도
prod로 안 잡는" 구현이 전부 통과하기 때문이다.

**뮤테이션 검증**: `_is_prod`를 4가지로 일부러 망가뜨려 각 케이스가 잡는지 확인했다.
① 값 부분일치 → A11·A12·A16 / ② 정규화 제거 → A13 / ③ 키 부분일치 → A14 / ④ 조기 종료 → A15.
**②③④는 각각 단 한 케이스만 잡는다** — 그 케이스를 지우면 해당 회귀가 통과한다.

**판정 커버리지**: 3차는 새 `Verdict`·`SkipReasonCode`를 늘리지 않는다(2차에서 이미 전량 커버).
늘리는 것은 **같은 판정에 도달하는 입력 경로의 커버리지**다.

## 4차 이후로 미룬 분기

| 항목 | 사유 |
| --- | --- |
| ~~EBS~~ ✅ 4차 E1·E2로 편입 | 작성 시점에는 `ebs_volumes`도 판정 분기도 없어 미뤘다. #156으로 collector·schema·rule이 모두 들어와 아래 §EBS 판정 규칙 2행을 정답으로 굳혔다. **남은 분기(전이·비정상 상태 `creating`·`deleting`·`error`·`deleted`, `state` 미상)는 확정 규칙에 답이 없어 여전히 보류** — 정책 확정 후 편입한다(이슈 #264) |
| 화이트리스트 태그(`finops:ignore` 등) | `feat/DATA-27-rule-engine-handoff`가 stale(dev가 47커밋 앞섬). 담당자가 현재 dev로 rebase·재작업 예정이며 **2차는 이를 기다리지 않는다** |
| 미부착+개방 → `THREAT` 우선순위 / `dp` 부족 + prod 우선순위 | 3차 파일은 `_is_prod` 단일 변수 설계(다른 입력 전부 동일)라 우선순위 케이스를 섞으면 그 성질이 깨진다. 4차에서 별도 파일로 작성 |
| ~~`non-prod` 부분 문자열 오탐~~ ✅ 3차 A12로 편입 | 당시 "버그 가능성"이라 정답으로 굳히지 못했으나, #95 / PR #97이 **부분일치 영구 금지**를 확정해 정답이 정해졌다 |
| ~~이름 기반 prod 탐지~~ ❌ 성립 불가 | `evaluate_ec2`의 `name` 인자가 #96 / PR #110으로 제거됐다. 이름 경로 자체가 없어 케이스로 만들 수 없다 |

### EBS 판정 규칙 (도입 확정 — 4차 골든셋 작성 근거)

| 입력 | 판정 |
| --- | --- |
| 미연결 (`attached_instance_ids` 비어있음 / `state: available`) | `UNUSED` → `RUNBOOK_EBS_DELETE_UNATTACHED` 후보 |
| 연결됨 (`state: in-use`) | `SKIP` / `SKIP_ACTIVE` |

새 `Verdict`·`SkipReasonCode` 값은 추가되지 않는다(기존 값 재사용). `health_score`는 EBS에서
`null`이어야 한다 — 계약상 EC2 전용이다. `resource_role`은 `RUNBOOK_SUPPORT`다(`_PRIMARY_TYPES`에
EBS가 없음). 세 항목 모두 `AssetItem` 계약이 위반 시 거부하는 것을 확인했다.

**작성 시 선행 조건**: `AssetInventory`에 `ebs_volumes`가 추가되면
`tests/test_golden_dataset.py`의 `_evaluate_inventory`에 순회를 함께 추가해야 한다.
빠뜨리면 EBS 자산이 판정도 대조도 없이 무시되는데,
`test_finops_expected_covers_every_input_asset`이 이를 감지한다.
→ #156에서 둘 다 들어왔고, 4차 작성 시 `tests/test_guardrails.py`의 `_GOLDEN_ASSET_RUNBOOKS`에
`ebs_volumes` 매핑을 추가하는 것이 **세 번째 선행 조건**이었다. 그 dict가 골든의 자산 종류를
전부 덮는지 검사하므로, 매핑 없이 EBS를 넣으면 가드레일 회귀가 즉시 실패한다.

## 4차 작성 케이스 (자산 2건 — 누적 30건)

**자산 2건** — `finops/input/asset_inventory_004.json` (EBS 전용)

| case | 입력 | 정답 | 막는 것 |
| --- | --- | --- | --- |
| E1 | `state: available` · 부착 없음 | `UNUSED` | 아무것도 `UNUSED`로 만들지 않는 구현 (미탐) |
| E2 | `state: in-use` · 부착 있음 | `SKIP` / `SKIP_ACTIVE` | 사용 중인 볼륨을 삭제 후보로 넘기는 구현 (오탐) |

**두 행은 위 §EBS 판정 규칙 표에서 그대로 도출했다.** 그 표에 없는 입력은 넣지 않았다 —
정답지는 정답을 적는 곳이지 현재 구현을 기록하는 곳이 아니다.

**정답으로 굳히지 않은 입력과 그 사유** (이슈 #264에서 정책 확인 중)

| 입력 | 수집 경로에서 나오나 | 확정 규칙에 답이 있나 | 처리 |
| --- | --- | --- | --- |
| `creating` · `deleting` · `error` · `deleted` + 미부착 | ✅ AWS `VolumeState` 유효값 | ❌ | **정책 질문** — 답이 오면 편입 |
| `state` 없음(null) | ❌ `describe_volumes`가 `State`를 돌려주므로 수집 경로에선 안 나온다. `EbsAsset.state`가 `Optional`이라 형식만 허용 | ❌ | **정책 질문** — fail-safe 판정 승인 필요 |
| `available` + 부착 있음 | ❌ 볼륨 상태와 부착 상태는 별개 필드이며(`VolumeState` vs `VolumeAttachment.status`), 부착이 있는 볼륨은 `in-use`다 | ❌ | **제외** |
| `AVAILABLE`(대문자) | ❌ AWS `VolumeState` 유효값은 전부 소문자 | ❌ | **제외** |

제외 2행은 수집 경로에서 나올 수 없는 입력이라, 정답을 못박아도 막는 것이 **실제 판정 결과를
바꾸지 않는 구현 차이**뿐이다. 실측으로 확인했다 — 부착 조건을 통째로 지우는 변형(`evaluate_ebs`에서
`not attached_instance_ids` 제거)은 골든·단위 테스트 **어느 쪽도 실패시키지 않는다.** AWS가
`state`와 부착을 함께 옮기기 때문이며, 그 조건은 수집단 버그에 대한 방어층이다.
**커버리지가 늘어난다는 것은 정답성의 근거가 아니다.**
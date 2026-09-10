# ADR-0006: LocalStack 팀 표준 개발 환경과 실 AWS 전환 전략

- **Status**: Accepted
- **Date**: 2026-08-19
- **Amended**: 2026-08-24, 2026-09-08, 2026-09-10 — §4 검증 한계 목록 갱신(하단 "개정 이력" 참조, 핵심 결정 불변)
- **Deciders**: 김세혁(PM/Infra) 수립 — 2026-08-13 확정 결정(개발 = LocalStack, 발표 직전 실 AWS 전환)의 구체화

## Context (배경)

2026-08-13 "개발 환경 = LocalStack, 발표 직전 실 AWS 전환"이 확정됐고(`docs/PROJECT_STATUS.md` 결정 로그), 전환 스위치(`AWS_ENDPOINT_URL` 유무)는 이미 코드에 있다 — `apps/core-api/services/collector.py`의 `_runtime_config()`/`_client()`, `.env.example`의 주석 처리된 스위치. **그러나 LocalStack 구성물 자체는 저장소에 0개다**:

- `docker-compose.yml`에 `localstack` 서비스 없음 (db·api·adminer만 존재)
- 시드 스크립트 없음 — `test_collector_raw.py` docstring이 `scripts/seed_localstack`을 전제하지만 `scripts/` 디렉터리 자체가 없음
- 통합 테스트는 LocalStack 미기동 시 전체 skip → CI에서 항상 skip, 로컬에서도 개인 환경(김세혁·김승철 PC)에서만 통과

이 때문에 수집 통합 테스트를 타 팀원이 재현 불가하고(미해결 #1), PR #29 후속 보완(미해결 #2 — collector 실경로 검증 재작성)이 차단돼 있다. 3–5주차 집중 개발에서 팀원 전원이 같은 가짜 AWS를 보게 하려면 1–2주차(8/23까지) 안에 표준 환경이 저장소에 들어가야 한다.

## Decision (결정)

**LocalStack Community를 docker-compose에 포함해 `docker compose up` 한 줄로 팀 전원이 동일한 시드 상태의 가짜 AWS를 얻게 하고, LocalStack이 검증하지 못하는 경로는 명시적 목록으로 관리해 6–7주차 실 AWS 스모크 테스트로 이월한다.**

### 1. 단일 compose — `localstack` 서비스 추가

- `docker-compose.yml`에 `localstack` 서비스 추가(포트 4566). 별도 compose 파일·profile을 만들지 않는다 — 팀 표준 진입로는 `cp .env.example .env && docker compose up` 하나다.
- **이미지 버전 고정**(minor까지 태그 고정). 팀원 간 "내 로컬에선 되는데" 편차의 최대 원인이 이미지 드리프트이므로, 업그레이드는 PR로만 한다.
- **Community(무료) 기능만 사용한다.** Pro 전용 기능에 의존하는 테스트·시드를 금지한다 — 5인 전원 무료 재현이 목적이다.
  - (2026-08-19 구현 중 확인) LocalStack 캘린더 버전(2026.x)부터는 무료 사용에도 `LOCALSTACK_AUTH_TOKEN`(계정 가입) 필수 — 토큰 없이 기동 가능한 마지막 라인인 **4.x(4.14.0)에 고정**한다. 상향은 토큰 정책(계정 배포 부담) 재검토 후에만.

### 2. 시드 = Boto3 스크립트 단일 원천 (`scripts/seed_localstack.py`)

- 시드는 **Boto3 스크립트 하나**로 관리한다. Terraform 시드는 금지 — IaC는 Post-MVP이고(MVP는 Boto3 직접 실행), 시드 원천이 둘이 되면 재현성이 깨진다.
- **멱등(idempotent)**: 식별 태그(`vigilantis:seed`) 기준으로 기존 리소스를 확인하고 재실행 시 중복 생성하지 않는다. 테스트 전제 조건("시드 필요")을 사람 손이 아니라 스크립트가 보장한다.
- 시드 데이터셋은 Rule Engine·위협 시나리오와 정합하게 구성한다:

| 시드 리소스 | 목적 (검증 대상) |
| --- | --- |
| Idle EC2 · non-prod (CPU 평균 < 5.0 메트릭 주입, 대형 타입) | `RUNBOOK_EC2_RIGHTSIZING` 후보 판별 |
| Idle EC2 · `Environment=production` (CPU 평균 < 5.0, 대형 타입) | `SKIP_PROD_PROTECTED` — 저활성 대형이라도 운영 자산은 미조치 |
| 정상 EC2 (CPU 평균 ≥ 5.0) | 오탐 방지 — 후보 미선정 확인 |
| 스파이크 EC2 (평균 < 5.0, 최대 ≥ 40.0) | `SKIP_LOW_UTIL` Skip 경로 |
| OpenIP SG (0.0.0.0/0, 22/tcp) | 위협 탐지(OpenIP)·토폴로지 붉은 노드 |
| 사용 중 SG / 미사용 SG 각 1개 | 미사용 SG 판별 |
| 미연결(available) EBS 볼륨 | `RUNBOOK_EBS_DELETE_UNATTACHED` (P1) |

- **Idle EC2는 prod / non-prod 두 대가 모두 필요하다.** `_is_prod` 검사가 idle 검사보다 앞서므로 idle 인스턴스가 prod 하나뿐이면 시드 전체의 `COST_CANDIDATE`가 0대가 되고 FinOps 경로가 시연 불가가 된다. 판정 분포는 `apps/core-api/services/tests/test_seed_dataset_verdicts.py`가 고정한다(AWS 불필요).
- 임계값(5.0 / 40.0)은 `rule_engine.py`의 `IDLE_CPU_AVG`·`SPIKE_CPU_MAX`와 결합돼 있다 — **임계값을 바꾸는 PR은 시드 스크립트 갱신을 포함해야 한다** (시드 스크립트가 상수를 rule_engine에서 import해 결합을 코드로 강제하는 것을 우선안으로 한다).
- CloudWatch 메트릭은 `put_metric_data`로 `AWS/EC2` 네임스페이스에 직접 주입한다. **이는 LocalStack에서만 가능한 경로다**(실 AWS는 `AWS/` 네임스페이스 커스텀 주입 불가) — 스크립트 주석에 명시하고, 실 AWS 대상 실행을 스크립트 스스로 거부하게 한다(`AWS_ENDPOINT_URL` 미설정 시 즉시 종료).

### 3. 전환 스위치 규약 — `AWS_ENDPOINT_URL` 유무, 코드 분기 금지

- 전환은 **환경변수 하나**로만 한다: 설정 시 LocalStack, 미설정 시 실 AWS (기존 결정 유지).
- collector에 이미 있는 클라이언트 생성 규약(endpoint 주입 헬퍼 경유)을 **전 모듈 공통 규약으로 승격**한다: 이후 작성되는 실행 엔진(`services/aws`)·SOAR(`security/`) 등 모든 boto3 클라이언트 생성은 공용 헬퍼를 경유한다 (`config.get_settings()` 확정 시 그쪽으로 이관 — collector의 기존 TODO와 동일 방향).
- **"LocalStack이면 동작을 바꾸는" 조건 분기를 금지한다.** 환경 차이는 엔드포인트 주입 지점 한 곳에만 존재해야 하며, 비즈니스 로직이 환경을 감지하면 실 AWS 전환 때 검증되지 않은 경로가 생긴다. (유일한 예외: 시드 스크립트의 실 AWS 실행 거부 가드)

### 4. LocalStack 검증 한계 — 명시적 이월 목록

LocalStack 통과를 "검증 완료"로 간주하지 않는 경로를 고정 목록으로 관리한다:

| # | 경로 | 실 AWS와의 격차 |
| --- | --- | --- |
| 1 | 가드레일 4단계 `DryRun=True` | LocalStack은 실제 IAM 권한을 검증하지 않음 |
| 2 | `get_waiter` Status Check(2/2) 감시·자동 원복 | 실제 부팅·헬스체크가 없어 대기·실패 시나리오가 재현되지 않음 |
| 3 | CloudWatch 메트릭 수집 | 실 AWS는 EC2가 자동 발행, LocalStack은 시드 주입 — 지연·해상도 특성이 다름 |
| 4 | ALB Target Group·ASG 경로 (P2 런북 3종) | **확정 편입(2026-08-24 실측)** — `elbv2`·`autoscaling`은 Community 미포함(Pro 전용, `InternalFailure: not included within your LocalStack license`). `ISOLATE`·`UNISOLATE`·`ENABLE_AUTOSCALING`은 실행뿐 아니라 Dry-Run 대체용 describe 조회도 로컬 불가 |
| 5 | `ec2.create_network_acl_entry` · `ec2.delete_network_acl_entry`의 `DryRun=True` | **LocalStack이 플래그를 무시하고 실제로 규칙을 생성·삭제한다**(예외 미발생). 실 AWS는 정상 지원하므로 `DryRun` 경로는 실 AWS에서 처음 검증된다 — 그때까지 두 런북은 조회 대체 검증으로 동작한다([ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md) §4) |
| 6 | `ec2.create_network_acl_entry`의 `Protocol` 표기 | **LocalStack은 보낸 문자열을 그대로 저장한다**(2026-09-08 실측 — `Protocol="tcp"`로 넣으면 `describe_network_acls`도 `"tcp"`를 돌려준다). 실 AWS는 같은 요청을 프로토콜 번호로 정규화한다. 저장 값이 곧 `NACL_RESTORE`의 백업 fingerprint 대조 상대라(ADR-0008 §5) 이름을 그대로 보내면 대조가 LocalStack에서만 맞는다. **행동 규칙: 삽입 시점에 번호로 바꿔 보낸다**(`schemas.runbook_parameters.NACL_PROTOCOL_NUMBERS`) — 그러면 두 환경이 같은 값을 저장하므로 이 격차는 이월이 아니라 코드로 닫힌다 |
| 7 | `ec2.create_network_acl_entry`의 `PortRange` | **LocalStack은 TCP·UDP 규칙에서 `PortRange`가 빠진 요청도 받아 준다**(2026-09-10 실측 — TCP 규칙이 그대로 생성되며 `describe_network_acls`의 `PortRange`는 `None`으로 남는다). 실 AWS는 같은 요청을 `InvalidParameterValue`로 거절한다([CreateNetworkAclEntry](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateNetworkAclEntry.html)). **행동 규칙: TCP·UDP에는 전체 범위 `0-65535`를 실어 보낸다**(`services/aws/executor._NACL_ALL_PORTS`) — 막는 축이 포트가 아니라 출발지 주소라 범위를 좁힐 이유가 없고, 6행과 같이 이월이 아니라 코드로 닫힌다 |
| 8 | 가드레일 4단계 `DryRun=True`의 **대상 존재 검사** | **LocalStack은 실재하지 않는 인스턴스에도 `DryRunOperation`을 돌려준다**(2026-09-10 실측 — 골든 A1 `i-0a1b2c3d4e5f00001`은 LocalStack에 없는데 `modify_instance_attribute(DryRun=True)`가 통과했고, 같은 ID로 부른 `stop_instances`는 `InvalidInstanceID.NotFound`로 실패했다). DryRun 플래그를 **대상 존재 검사보다 먼저** 처리하기 때문이다. 그래서 4단계가 전부 통과해 승인 버튼이 열리고, **관제자가 누른 뒤 실행 1단계에서 처음 깨진다**(`PRECHECK_TARGET_NOT_FOUND`). 1행과 같은 이월이다 — 실 AWS는 여기서 걸러 줄 것으로 보이나 **미측정**이며, 로컬에서 ④는 "대상이 실재하는가"를 보증하지 않는다. 9/11 게이트는 조치 대상을 실물에 바인딩해 회피한다(`scripts/load_golden_assets.py --bind-a1-to-seed`, #301) |

이 목록은 **6–7주차 실 AWS 스모크 테스트**에서 해소한다: P0 런북 4종(`RIGHTSIZING`+`REVERT_SIZE`, `NACL_ADD_DENY`+`NACL_RESTORE`) 실동작 + Dry-Run·Status Check 경로 각 1회 검증. 비용 통제 — 단일 계정, 최소 스펙(t3.micro급), 검증 직후 리소스 정리. P2 시연 인프라(ALB·다중 EC2)는 마일스톤대로 조기 준비하되 실 AWS에 구성한다.

**실 AWS 전환 절차(스모크·발표 직전 공통)** — `.env` 편집 두 가지를 반드시 **함께** 한다: ① 더미 자격증명(`test`)을 실제 키로 교체, ② `AWS_ENDPOINT_URL` 줄 삭제. 하나라도 빠지면 — ② 누락 시 모든 호출이 **조용히 LocalStack으로 가서 스모크가 가짜 AWS를 검증**하고(무증상 — 가장 위험), ① 누락 시 실 AWS가 `AuthFailure`로 시끄럽게 실패한다(인지 용이). 전환 여부는 편집 직후 `aws sts get-caller-identity`(또는 boto3 동일 호출)로 확인한다 — **계정 ID가 `000000000000`이면 아직 LocalStack이다.**

### 5. 단계 편성 (마일스톤 정합)

| 시점 | 작업 | 산출물 |
| --- | --- | --- |
| 1–2주차 말(이번 주) | 본 ADR + compose `localstack` + 시드 스크립트 + `.env.example` 스위치 기본 활성화 | 팀 표준 환경 PR (김세혁) |
| 3주차 | CI에 LocalStack service container + 시드 단계 추가(별도 CHORE), PR #29 후속 재작성 착수 가능(김승철 — 미해결 #2 차단 해제) | CI 통합 테스트 가동 |
| 3–5주차 | 전원 LocalStack 기반 개발·pytest. 실행 엔진·가드레일도 동일 규약(§3)으로 작성 | — |
| 6–7주차 | 실 AWS 스모크 테스트(§4 목록 해소) — 마일스톤 "백엔드-프론트엔드 연동 & 회복 엔진 통합" 기간 내 | 스모크 결과 기록 |
| 8주차(중간 발표 직전) | `AWS_ENDPOINT_URL` 제거 전환 리허설 + 시연 인프라 최종 점검 | 시연 환경 |

통합 테스트의 현행 skip 규약(LocalStack 미기동 시 전체 skip)은 유지한다 — CI service container 도입 전까지 CI 안전성을 보장하는 장치다.

## Consequences (결과·트레이드오프)

**장점**

- `docker compose up` 한 줄로 팀 전원 동일 환경 — 수집·판정 테스트가 개인 PC 의존에서 해제
- PR #29 후속 보완(미해결 #2)의 전제 조건 충족 — "시드 없으면 빈 결과로 통과" 문제의 구조적 해소
- 개발 중 실 AWS 비용 0, 자격증명 배포 불필요(`test`/`test` 더미 키)
- 검증 한계가 암묵이 아닌 목록(§4)으로 관리 → "LocalStack에서 됐으니 끝" 오판 방지

**비용/유의**

- LocalStack ≠ 실 AWS 격차(§4)는 구조적으로 남는다 — **6–7주차 스모크가 유일한 방어선**이므로 해당 주차 일정에서 빠지면 시연 직전 리스크로 직결
- ~~Community 커버리지가 P2 런북 리소스(ALB TG·ASG)에서 부족할 수 있음 — 구현 착수 시점(3–5주차)에 확인해 §4 목록을 갱신해야 함~~ → **확인 완료(2026-08-24)**: `elbv2`·`autoscaling`은 Community 미포함으로 확정, §4 4행 편입(1차 개정)
- 시드 데이터와 rule_engine 임계값의 결합 — 임계값 변경 PR에 시드 갱신 누락 시 통합 테스트가 조용히 무의미해짐
- 이미지 버전 고정 관리 부담(업그레이드는 PR로만)
- 시연 데이터(Golden Dataset·mock GuardDuty 위협 주입)는 본 ADR 범위 밖 — 시드는 자산·메트릭까지만 책임지며, 위협 이벤트 주입 방식은 별도 결정 대상

## Related

- 현황 기준: `docs/PROJECT_STATUS.md` — 결정 로그 2026-08-13(개발 = LocalStack), 미해결 #1(팀 표준 환경)·#2(PR #29 후속)
- 선행 결정: [ADR-0001](0001-mvp-monorepo-structure.md) — 단일 `apps/core-api`·docker-compose 개발 환경
- 마일스톤·구현 우선순위: [`docs/PROJECT_STATUS.md`](../PROJECT_STATUS.md) §현재 위치·§일정 리스크 & 구현 우선순위 — P0/P1/P2 착수 순서
- 기존 구현: `apps/core-api/services/collector.py`(`_runtime_config`/`_client` — 스위치 규약 원형), `apps/core-api/services/tests/test_collector_raw.py`(skip 규약), `.env.example`
- 영향 범위: `docker-compose.yml`, `scripts/seed_localstack.py`(신규), `.env.example`, `.github/workflows/ci.yml`(3주차), 이후 `services/aws`·`security/`의 클라이언트 생성 규약

## 개정 이력

- **2026-08-24 (1차 개정)** — §4 검증 한계 목록 갱신. 확정 10종 Runbook이 사용하는 AWS 작업
  전수를 LocalStack 4.14.0에서 실측한 결과 두 가지가 확인됐다(#113, [ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md) §Context).

  | 대상 | 변경 |
  | --- | --- |
  | 4행 (ALB TG·ASG 경로) | "커버리지 제한 **가능** — 구현 시점에 확인" → **확정 편입**. `elbv2`·`autoscaling`은 Community 미포함(Pro 전용)이라 §1의 Community 전용 방침 아래에서는 해소 불가 |
  | 5행 (신규) | `create_network_acl_entry`·`delete_network_acl_entry`가 `DryRun=True`를 무시하고 실제 수행 — 실 AWS는 정상 지원 |

- **2026-09-08 (2차 개정)** — §4에 6행 추가. `NACL_ADD_DENY` 실행 경로를 붙이며(#297) LocalStack
  Community의 NACL 쓰기 지원을 실측한 결과, 지원 자체는 확인됐고(`create_network_acl_entry`·
  `delete_network_acl_entry` 모두 성공, `describe_network_acls`로 삽입 확인) **`Protocol` 표기에서
  격차가 하나 나왔다.**

  | 대상 | 변경 |
  | --- | --- |
  | 6행 (신규) | LocalStack은 보낸 `Protocol` 문자열을 그대로 저장하고 실 AWS는 번호로 정규화한다. 저장 값이 `NACL_RESTORE`의 fingerprint 대조 상대라, 이름을 보내면 대조가 LocalStack에서만 맞는다 |

  앞선 5행과 처분이 다르다. **이 격차는 실 AWS 스모크로 이월하지 않고 코드로 닫는다** — 삽입
  시점에 번호로 바꿔 보내면 두 환경이 같은 값을 저장하기 때문이다. 표기 결정은
  `packages/schemas/runbook_parameters.py`의 `NACL_PROTOCOL_NUMBERS` 하나에 두고,
  회귀는 `test_execute_nacl_add_deny.py`·`test_execute_nacl_localstack.py`가 지킨다.
  §1(Community 전용)·§3(전환 스위치 규약)은 그대로 유지하며 핵심 결정은 불변이다.

  §3(전환 스위치 규약 — 코드 분기 금지)은 그대로 유지한다. 두 NACL 작업을 "LocalStack일 때만
  조회"로 나누지 않고 환경 무관 조회 대체 검증으로 처리하는 근거가 그 조항이다. 핵심 결정
  (단일 compose·Boto3 시드 단일 원천·전환 스위치·이월 목록 운용)은 불변.

- **2026-09-10 (3차 개정)** — §4에 7행 추가. PR #313 리뷰에서 **TCP·UDP 규칙의 `PortRange`
  누락**이 지적됐다. LocalStack이 그 요청을 받아 주기 때문에 로컬 테스트로는 드러나지 않고,
  실 AWS 전환에서 처음 거절로 나타난다.

  | 대상 | 변경 |
  | --- | --- |
  | 7행 (신규) | LocalStack은 `PortRange` 없는 TCP·UDP 규칙 생성을 허용하고 실 AWS는 거절한다. 삽입 시점에 전체 범위 `0-65535`를 실어 보내 두 환경에서 같은 요청이 서게 했다 |

  처분은 6행과 같다 — **실 AWS 스모크로 이월하지 않고 코드로 닫는다.** 범위 값은
  `services/aws/executor.py`의 `_NACL_ALL_PORTS` 하나에 두고, 회귀는
  `test_execute_nacl_add_deny.py`가 지킨다(TCP·UDP는 전체 범위, ICMP·`-1`은 미전송).
  핵심 결정은 불변이다.

- **2026-09-10 (4차 개정)** — §4에 8행 추가. 9/11 게이트 대본(#301)이 T1의 조치 대상을
  **골든 A1로 고정**하면서, 그 골든 A1이 LocalStack에 실재하지 않는데도 **가드레일 4단계가
  전부 통과하는 것**이 드러났다(PR #321 실측).

  | 대상 | 변경 |
  | --- | --- |
  | 8행 (신규) | LocalStack은 존재하지 않는 인스턴스에도 `DryRunOperation`을 돌려준다(DryRun 플래그를 대상 존재 검사보다 먼저 처리). 승인 버튼이 열린 뒤 실행 1단계에서 처음 깨진다 — 실 AWS 동작은 미측정 |

  **처분은 6·7행과 다르다 — 이것은 이월이다.** 코드로 닫을 수 있는 격차가 아니기 때문이다.
  4단계는 "AWS에 물어본 답"을 그대로 판정으로 쓰는 자리이고([ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md) §2),
  그 답이 에뮬레이터에서 다르다고 해서 우리 쪽에 존재 검사를 덧대면 §3(환경 감지 분기 금지)을
  어기게 된다. 대상 존재는 실행 시점의 `precheck`가 이미 `PRECHECK_TARGET_NOT_FOUND`로 잡는다 —
  로컬에서 잃는 것은 **그 판정이 승인 전으로 당겨지지 않는다**는 것 하나다.

  **스모크에서 잴 것**: 존재하지 않는 인스턴스 ID로 `modify_instance_attribute(DryRun=True)`를
  1회 불러 실 AWS가 `InvalidInstanceID.NotFound`로 거절하는지 확인한다. 거절하면 8행은 닫히고,
  통과하면 **실 AWS에서도 같은 공백이라는 뜻이므로** 그때 가드레일 쪽 처분을 다시 연다.

  9/11 게이트 당일은 조치 대상을 실물에 바인딩해 회피한다(`--bind-a1-to-seed`). 핵심 결정
  (단일 compose·Boto3 시드 단일 원천·전환 스위치·이월 목록 운용)은 불변이다.

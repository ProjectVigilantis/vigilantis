# ADR-0006: LocalStack 팀 표준 개발 환경과 실 AWS 전환 전략

- **Status**: Accepted
- **Date**: 2026-08-19
- **Deciders**: 김세혁(PM/Infra) 수립 — 2026-08-13 확정 결정(개발 = LocalStack, 발표 직전 실 AWS 전환)의 구체화

## Context (배경)

2026-08-13 "개발 환경 = LocalStack, 발표 직전 실 AWS 전환"이 확정됐고(`docs/PROJECT_STATUS.md` 결정 로그), 전환 스위치(`AWS_ENDPOINT_URL` 유무)는 이미 코드에 있다 — `apps/core-api/services/collector.py`의 `_runtime_config()`/`_client()`, `.env.example`의 주석 처리된 스위치. **그러나 LocalStack 구성물 자체는 저장소에 0개다**:

- `docker-compose.yml`에 `localstack` 서비스 없음 (db·api·adminer만 존재)
- 시드 스크립트 없음 — `test_collector_raw.py` docstring이 `scripts/seed_localstack`을 전제하지만 `scripts/` 디렉터리 자체가 없음
- 통합 테스트는 LocalStack 미기동 시 전체 skip → CI에서 항상 skip, 로컬에서도 개인 환경(김세혁·김승철 PC)에서만 통과

이 때문에 수집 통합 테스트를 타 팀원이 재현 불가하고(미해결 #1), PR #29 후속 보완(미해결 #2 — collector 실경로 검증 재작성)이 차단돼 있다. 3~5주차 집중 개발에서 팀원 전원이 같은 가짜 AWS를 보게 하려면 1~2주차(~8/23) 안에 표준 환경이 저장소에 들어가야 한다.

## Decision (결정)

**LocalStack Community를 docker-compose에 포함해 `docker compose up` 한 줄로 팀 전원이 동일한 시드 상태의 가짜 AWS를 얻게 하고, LocalStack이 검증하지 못하는 경로는 명시적 목록으로 관리해 6~7주차 실 AWS 스모크 테스트로 이월한다.**

### 1. 단일 compose — `localstack` 서비스 추가

- `docker-compose.yml`에 `localstack` 서비스 추가(포트 4566). 별도 compose 파일·profile을 만들지 않는다 — 팀 표준 진입로는 `cp .env.example .env && docker compose up` 하나다.
- **이미지 버전 고정**(minor까지 태그 고정). 팀원 간 "내 로컬에선 되는데" 편차의 최대 원인이 이미지 드리프트이므로, 업그레이드는 PR로만 한다.
- **Community(무료) 기능만 사용한다.** Pro 전용 기능에 의존하는 테스트·시드를 금지한다 — 5인 전원 무료 재현이 목적이다.

### 2. 시드 = Boto3 스크립트 단일 원천 (`scripts/seed_localstack.py`)

- 시드는 **Boto3 스크립트 하나**로 관리한다. Terraform 시드는 금지 — IaC는 Post-MVP이고(MVP는 Boto3 직접 실행), 시드 원천이 둘이 되면 재현성이 깨진다.
- **멱등(idempotent)**: 식별 태그(`vigilantis:seed`) 기준으로 기존 리소스를 확인하고 재실행 시 중복 생성하지 않는다. 테스트 전제 조건("시드 필요")을 사람 손이 아니라 스크립트가 보장한다.
- 시드 데이터셋은 Rule Engine·위협 시나리오와 정합하게 구성한다:

| 시드 리소스 | 목적 (검증 대상) |
| --- | --- |
| Idle EC2 (CPU 평균 < 5.0 메트릭 주입) | `RUNBOOK_EC2_RIGHTSIZING` 후보 판별 |
| 정상 EC2 (CPU 평균 ≥ 5.0) | 오탐 방지 — 후보 미선정 확인 |
| 스파이크 EC2 (평균 < 5.0, 최대 ≥ 40.0) | `SKIP_LOW_UTIL` Skip 경로 |
| OpenIP SG (0.0.0.0/0, 22/tcp) | 위협 탐지(OpenIP)·토폴로지 붉은 노드 |
| 사용 중 SG / 미사용 SG 각 1개 | 미사용 SG 판별 |
| 미연결(available) EBS 볼륨 | `RUNBOOK_EBS_DELETE_UNATTACHED` (P1) |

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
| 4 | ALB Target Group·ASG(Launch Template) 경로 (P2 런북) | Community 에뮬레이션 커버리지 제한 가능 — 구현 시점에 확인, 미동작 시 이 목록에 확정 편입 |

이 목록은 **6~7주차 실 AWS 스모크 테스트**에서 해소한다: P0 런북 4종(`RIGHTSIZING`+`REVERT_SIZE`, `NACL_ADD_DENY`+`NACL_RESTORE`) 실동작 + Dry-Run·Status Check 경로 각 1회 검증. 비용 통제 — 단일 계정, 최소 스펙(t3.micro급), 검증 직후 리소스 정리. P2 시연 인프라(ALB·다중 EC2)는 마일스톤대로 조기 준비하되 실 AWS에 구성한다.

### 5. 단계 편성 (마일스톤 정합)

| 시점 | 작업 | 산출물 |
| --- | --- | --- |
| 1~2주차 말(이번 주) | 본 ADR + compose `localstack` + 시드 스크립트 + `.env.example` 스위치 기본 활성화 | 팀 표준 환경 PR (김세혁) |
| 3주차 | CI에 LocalStack service container + 시드 단계 추가(별도 CHORE), PR #29 후속 재작성 착수 가능(김승철 — 미해결 #2 차단 해제) | CI 통합 테스트 가동 |
| 3~5주차 | 전원 LocalStack 기반 개발·pytest. 실행 엔진·가드레일도 동일 규약(§3)으로 작성 | — |
| 6~7주차 | 실 AWS 스모크 테스트(§4 목록 해소) — 마일스톤 "백엔드-프론트엔드 연동 & 회복 엔진 통합" 기간 내 | 스모크 결과 기록 |
| 8~9주차(발표 직전) | `AWS_ENDPOINT_URL` 제거 전환 리허설 + 시연 인프라 최종 점검 | 시연 환경 |

통합 테스트의 현행 skip 규약(LocalStack 미기동 시 전체 skip)은 유지한다 — CI service container 도입 전까지 CI 안전성을 보장하는 장치다.

## Consequences (결과·트레이드오프)

**장점**

- `docker compose up` 한 줄로 팀 전원 동일 환경 — 수집·판정 테스트가 개인 PC 의존에서 해제
- PR #29 후속 보완(미해결 #2)의 전제 조건 충족 — "시드 없으면 빈 결과로 통과" 문제의 구조적 해소
- 개발 중 실 AWS 비용 0, 자격증명 배포 불필요(`test`/`test` 더미 키)
- 검증 한계가 암묵이 아닌 목록(§4)으로 관리 → "LocalStack에서 됐으니 끝" 오판 방지

**비용/유의**

- LocalStack ≠ 실 AWS 격차(§4)는 구조적으로 남는다 — **6~7주차 스모크가 유일한 방어선**이므로 해당 주차 일정에서 빠지면 시연 직전 리스크로 직결
- Community 커버리지가 P2 런북 리소스(ALB TG·ASG)에서 부족할 수 있음 — 구현 착수 시점(3~5주차)에 확인해 §4 목록을 갱신해야 함
- 시드 데이터와 rule_engine 임계값의 결합 — 임계값 변경 PR에 시드 갱신 누락 시 통합 테스트가 조용히 무의미해짐
- 이미지 버전 고정 관리 부담(업그레이드는 PR로만)
- 시연 데이터(Golden Dataset·mock GuardDuty 위협 주입)는 본 ADR 범위 밖 — 시드는 자산·메트릭까지만 책임지며, 위협 이벤트 주입 방식은 별도 결정 대상

## Related

- 현황 기준: `docs/PROJECT_STATUS.md` — 결정 로그 2026-08-13(개발 = LocalStack), 미해결 #1(팀 표준 환경)·#2(PR #29 후속)
- 선행 결정: [ADR-0001](0001-mvp-monorepo-structure.md) — 단일 `apps/core-api`·docker-compose 개발 환경
- 마일스톤: `vigilantis-docs/1차 발표까지의 마일스톤 및 MVP 범위 명세.md` — 3~5주차 집중 개발, 6~7주차 통합, P0/P1/P2 착수 순서
- 기존 구현: `apps/core-api/services/collector.py`(`_runtime_config`/`_client` — 스위치 규약 원형), `apps/core-api/services/tests/test_collector_raw.py`(skip 규약), `.env.example`
- 영향 범위: `docker-compose.yml`, `scripts/seed_localstack.py`(신규), `.env.example`, `.github/workflows/ci.yml`(3주차), 이후 `services/aws`·`security/`의 클라이언트 생성 규약

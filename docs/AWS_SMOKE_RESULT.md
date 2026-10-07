# 실 AWS 스모크·P2 검증 결과 기록

> **담당**: 김승철 (QA & Scenario · 2026-09-16 박지현에게서 인수) · **실 AWS 실행**: 김세혁(PM 1인 · ADR-0009 §2)
> **산출물 근거**: [ADR-0006](adr/0006-localstack-team-standard-env.md) §5 — 9주차 "스모크 결과 기록" · 10주차 "P2 검증 기록"
> **대조 기준**: ADR-0006 §4 이월 목록 8행 · [ADR-0009](adr/0009-real-aws-smoke-environment.md) §6 운영 절차
>
> **이 문서는 종료 판정에 사용할 관측 기록이다.** 주차 종료 기준은 [PROJECT_STATUS.md](PROJECT_STATUS.md) §현재 위치만 참조한다. 아래 빈칸·밑줄·미선택 체크박스는 미측정이며, 기록할 때는 결과 또는 미측정 사유를 채운다.

---

## 0. 기록 규칙

- **관측한 것만 적는다.** 추정·예상은 `예상:`을 붙여 구분한다.
- **실패도 그대로 적는다.** 무엇을 하려다 어디서 멈췄는지가 이월 목록 처분의 근거다.
- **시각은 KST**, 명령은 실행한 그대로(비밀 값은 `***`로 가린다).
- 한 줄로 안 되는 것은 §7 관찰 기록에 길게 적고 표에서는 그 줄을 가리킨다.

### 검증 항목 번호 — 이 표가 번호의 정의다

아래 표 밖의 번호는 쓰지 않는다. 번호는 칸 이름이므로, 채우는 사람은 이 표에서 그 번호가 무엇을 확인하는 칸인지 읽는다. 하위 번호(`A4-n` · `P2-n`)는 각 절의 표가 정의한다.

| 번호 | 확인할 것 | 이 문서의 칸 |
| --- | --- | --- |
| A1 | 환경 기동 `up --yes` · `status`의 TG 대상 `healthy` · Budget $50 알림 선행 생성 | §1 |
| A2 | 앱 키 발급과 `.env` 전환(키 교체 + `AWS_ENDPOINT_URL` 삭제를 **함께**) · `sts get-caller-identity` Arn이 `user/vigilantis-smoke-app` · `AWS_REGIONS` 빈 값 | §1 |
| A3 | 앱 키로 스캔 1회 — 회차 상태와 실패 라벨, elbv2·autoscaling 조회가 실 AWS에서 채워지는지 | §2-1 · §2-2 |
| A4 | 가드레일 ④ `DryRun=True` — 런북별 1회, 실제 IAM 권한 검증 | §3 A4-1–A4-7 |
| A5 | `DryRun`의 대상 존재 검사 — 없는 인스턴스 ID를 실 AWS가 거르는지(미측정 항목) | §3 A5 |
| A6 | `NACL_ADD_DENY` → `NACL_RESTORE` 실동작과 저장된 `Protocol`·`PortRange` 값 | §4 A6 |
| A7 | `RIGHTSIZING`(`idle-dev`) → `REVERT_SIZE` 실동작 — 관측치 48개 확보 뒤에만 가능 | §4 A7 |
| A8 | Status Check 2/2 실 대기 + `stopped` 주입 → `FAILED` → `AUTO_ON_FAILURE` 자동 원복 | §4 A8 |
| A8-impaired | Status Check `impaired` 검사 결과 경로 — A8의 `stopped` 결과로 대체하지 않는 별도 칸 | §4 A8-impaired |
| A9 | `SKIP_PROD_PROTECTED`(`web-1`·`web-2`) 판정 — 관측치 확보 뒤 | §2-3 |
| A10 | P1 3종 실동작(`SG_DELETE_ISOLATED`·`SG_RECREATE`·`EBS_DELETE_UNATTACHED`) — 선택 | §4 A10 |
| A11 | 스모크 결과 기록 — 이 문서 자체 | 이 문서 전체 |

---

## 1. 환경 — 기동과 전환

| 항목 | 값 |
| --- | --- |
| 계정 ID | (저장소에 적지 않는다 — `--account` 인자로만) |
| 리전 | `ap-northeast-2` |
| 검증 기준 커밋 | `origin/dev` ______ |
| `up --yes` 실행 시각 | ______ |
| `status` TG 대상 `healthy` 확인 시각 | ______ |
| 앱 키 Arn (`sts get-caller-identity`) | `arn:aws:iam::***:user/vigilantis-smoke-app` 확인 ☐ |
| `.env` 전환(A2) — `AWS_ENDPOINT_URL` 삭제 ☐ · 키 교체 ☐ · `AWS_REGIONS` 빈 값 ☐ | 동시 수행 시각 ______ |
| 전환을 돌린 자리 | 본진 / 전용 worktree(권장) — ______ |
| Budget $50 알림 생성 | ☐ |

### 관측치 확보와 최초 판정 기록

Rule Engine은 경과 시간이 아니라 **실제 CPU 관측치 수(`cpu_datapoints`)가 48개 이상인지** 확인한다. `METRIC_PERIOD_SECONDS` 기본값은 3600초라 약 2일을 예상하지만, 실제 설정·누락·수집 지연에 따라 달라진다. 48개 확보는 데이터 부족 조건을 벗어나는 요건이며, 이후 태그와 사용률 등 각 판정 조건을 함께 확인한다.

| 항목 | 값 |
| --- | --- |
| 인스턴스 기동 시각(A1) | ______ |
| 실제 `METRIC_PERIOD_SECONDS` / `METRIC_LOOKBACK_DAYS` | ______ / ______ |
| 관측치 확보 예상 시각(확정 시각 아님) | 예상: ______ · 산정 근거 ______ |
| 대상별 실제 CPU 관측치 수 · 스캔 시각 | 대상 ______ · 개수 ______ · 시각 ______ |
| 최초 실제 판정 · 대상 · 시각 | 판정 ______ · 대상 ______ · 시각 ______ |
| 일정 내 검증 가능 여부와 제약 | ______ |

> 관측치가 부족하면 부족 수와 다음 확인 예정 시각을 §7에 적는다. 주차 이월은 자동으로 확정하지 않고 PM의 판단과 근거 링크를 §6에 기록한다.

---

## 2. 수집·판정 대조 (A3 · A9) — 김승철

이 절의 값은 **`scripts/qa_scan_compare.py`** 가 이 순서대로 찍는다(읽기 전용). 스모크 당일에 쿼리를 손으로 조립하지 않는다.

```bash
uv run python scripts/qa_scan_compare.py
```

`.env` 의 DSN 은 컨테이너용(`db:5432`)이라 호스트 셸에서 돌릴 때는 `--database-url` 로 호스트에 열린 포트를 넘긴다. 출력의 `mode` 열이 **그 회차가 실 AWS 였는지**의 원천이다 — `localstack` 회차를 실 AWS 결과로 적는 것이 이 대조에서 가장 비싼 실수다.

**LocalStack 값을 비교 기준선으로 두지 않는다.** 시드(`seed_localstack.py`)와 스모크(`provision_smoke_aws.py`)는 자산 구성이 다르고, 이월 목록 자체가 "LocalStack이 보여 주지 못한 것"의 목록이라 비교할 짝이 없다. 로컬에서 가져올 값은 **실패 사유 코드의 모양** 하나이며, 필요한 곳의 비고에 적어 뒀다.

### 2-0. 시험 적용 회차 (ADR-0009 §6 1단계 · 10/1(목) 전 1회)

ADR-0009 §6 1단계는 이 회차를 elbv2·autoscaling 조회의 실 AWS 첫 실측으로 정했다. 실행했으면 결과를, 안 했으면 미실행과 사유를 적는다. 본 스모크 회차(§2-1)와 섞지 않는다.

| 항목 | 값 |
| --- | --- |
| 실행 여부 · 시각 | 실행 / 미실행 — ______ · 미실행이면 사유 ______ |
| `collection_runs.status` | ______ |
| elbv2·autoscaling 조회 결과 | ______ |
| 이월 4행 처분에 보태는 근거 | ______ |

### 2-1. 스캔 1회 결과

`collector_failures`는 `_safe_describe`가 흡수한 조회의 **라벨 → 사유 코드** 묶음이며, 적재 시 `error_summary`에 compact JSON으로 실린다(`collector.py` `_failures_summary`). 흡수된 조회는 빈 목록으로 강등되므로, 사유 코드 없이는 **"정상 0건"과 구별되지 않는다.**

| 항목 | 실 AWS 관측 | 판정 · 비고 |
| --- | --- | --- |
| `mode` | ______ | `aws` 가 아니면 실 AWS 회차가 아니다 — 아래 칸을 채우기 전에 먼저 본다 |
| `collection_runs.status` | ______ | `PARTIAL` 이면 아래 사유 코드로 권한 누락인지 가린다 |
| `error_summary` | ______ | `collector_failures` 가 compact JSON 으로 실린 자리다. 로컬에서 나오는 모양은 `{"alb_target_groups":"InternalFailure","auto_scaling_groups":"InternalFailure"}` — **같은 라벨에 `AccessDenied` 가 오면 권한 누락이다** |
| `collector_failures` 라벨 → 사유 코드 | 라벨 ______ → 사유 ______ | 사유 코드가 판별 기준이다. `AccessDenied` 는 **권한 누락** — 빠진 조회 권한을 §7에 적고 `policy` 대조. **`InternalFailure` 는 회차의 `mode` 로 뜻이 갈린다** — `localstack` 이면 라이선스 밖이고, **`aws` 면 AWS 측 내부 오류이므로 재시도 후에도 같으면 §7에 적는다**(실 AWS 에서도 나오는 코드다). 어느 코드든 빈 목록으로 강등되므로 "정상 0건"과 구별되지 않는다 |
| `elbv2`(ALB Target Group) 조회 | ______ | **이월 4행 처분 근거.** LocalStack Community 에 없어 로컬에서 성공한 적이 없다 |
| `autoscaling`(ASG) 조회 | ______ | **이월 4행 처분 근거.** 같은 이유로 로컬에서 성공한 적이 없다 |
| CloudWatch 메트릭 | ______ | **이월 3행 처분 근거.** **시드 회차는 관측치를 즉시 72개로 채우므로 48개 게이트가 걸리지 않는다**(2026-10-06 실측 — 자산 4대 모두 72개). 게이트 자체는 골든 A4(`dp 47`)가 검증한다. 지연·해상도는 실 AWS에서 처음 관측된다 |

### 2-2. 자산 유형별 수집 수

수집기는 **리전 전체**를 훑으므로, 수집 수는 아래 스모크 자원 수보다 많을 수 있다(기본 VPC 등 이미 있던 자원). 적을 때 그 차이를 비고에 남긴다 — 많은 것이 이상은 아니고, **적은 것이 이상이다**. **수가 같거나 많아도 스모크 자원이 전부 잡혔다는 뜻은 아니다** — 이름은 §2-3 대상 목록으로 대조한다.

| 자산 유형 | 기대 — 스모크 자원(`provision_smoke_aws.py` 스펙) | 수집 수 | 비고 |
| --- | --- | --- | --- |
| EC2 | 3 — `web-1`·`web-2`·`idle-dev` | | |
| SG | 6 — 명명 5종(`alb`·`web`·`open-ssh`·`unused`·`isolation`) + 스모크 VPC 기본 SG | | |
| NACL | 2 — `vigilantis-smoke-nacl` + 스모크 VPC 기본 NACL | | |
| EBS | 4 — 인스턴스 루트 3 + 미연결 `vigilantis-smoke-unattached` 1 | | |
| Launch Template | 0 — `up` 은 만들지 않는다(`EC2_ENABLE_AUTOSCALING` 이 `vigilantis-lt-` 접두로 만든다) | | 0이 정상. **있으면 출처(이름·생성 시각)를 §7에 적는다** — 수집이 리전 전체를 훑으므로 스모크 밖에서 만든 LT도 잡힌다 |
| Auto Scaling Group | 0 — `up` 은 만들지 않는다(P2 검증이 만들고 직후 정리) | | 0과 미관측을 구분한다. precheck 조건 ③이 **동명 ASG 부재**라 0이 정상 |
| ALB Target Group | 1 — `vigilantis-smoke-tg` | | LocalStack에서는 조회 실패로 **미관측**(자산 0개를 뜻하지 않음) |

### 2-3. 판정 분포

| 판정 | 건수 | 대상 | 비고 |
| --- | --- | --- | --- |
| `COST_CANDIDATE` | | | CPU 관측치 48개 이상 확보 후 태그·사용률 조건도 대조 |
| `SKIP_INSUFFICIENT_DATA` | | | CPU 관측치 없음 또는 48개 미만인지 대조 |
| `SKIP_PROD_PROTECTED` | | `web-1`·`web-2` | **A9** |
| `SKIP_WHITELISTED` | | `vigilantis-smoke-isolation` + 이름이 `default` 인 SG 전부 | **경로가 둘이다** — 이름 `default`(삭제·변경 불가)와 `vigilantis:role=isolation` 태그(#359). `default` SG가 이 칸에 함께 뜨는 것이 정상이다 |
| `UNUSED` | | | |
| `THREAT` | | | |
| 그 밖 | | | |

### 2-4. 조인 무결성

**이 절은 선택한 회차로 좁혀지지 않는다.** `find_dangling_arns` 에 회차·계정 범위 인자가 없어 **DB 전체**를 본다 — 이전 회차나 골든 적재의 잔존 행이 함께 잡힌다. 그래서 두 수를 따로 적는다.

| 항목 | 값 |
| --- | --- |
| `find_dangling_arns` 원출력 (**DB 전체 기준**) | total ____ / investigate ____ |
| 종류별 | ______ |
| 이 스모크 계정 기준 — `(다른 계정)` 행 제외 후 | total ____ / investigate ____ · 제외한 행 ____ 건 |
| 제외 기준과 남은 행의 성격 | ______ |
| 판정 | **제외 후 investigate 0건이면 정상.** 남으면 §7에 원인. 같은 계정의 **다른 회차·자원**까지 걸러진 수가 아니므로 "이 회차의 결과"로 적지 않는다 |

> 스크립트가 회차의 `account_id` 와 ARN 계정을 대조해 `(다른 계정)` 을 붙인다. 기록할 때 그 행을 뺀 수와 원출력을 함께 남겨, 뒤에 읽는 사람이 어느 범위의 수인지 알 수 있게 한다.

---

## 3. 가드레일 · DryRun (A4 · A5)

실제 가드레일 `precheck()`와 AWS API 직접 `DryRun=True` 실측은 구분해 기록한다. [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §4에 따라 NACL의 런타임 사전 검사는 환경과 무관하게 describe로 대체한다. describe 성공을 변경 작업의 IAM 권한 검증 성공으로 기록하지 않는다. 여러 API를 확인한 경우 API별 응답과 `verification_summary`를 §7에 남긴다.

| # | 런북 | 검사 방식 · API · 응답/결과 | 권한 누락 | 비고 |
| --- | --- | --- | --- | --- |
| A4-1 | `EC2_RIGHTSIZING` | | | |
| A4-2 | `EC2_REVERT_SIZE` | | | |
| A4-3 | `NACL_ADD_DENY` | | | 런타임 describe와 별도 직접 DryRun 실측을 구분 — **이월 5행** |
| A4-4 | `NACL_RESTORE` | | | 런타임 describe와 별도 직접 DryRun 실측을 구분 — **이월 5행** |
| A4-5 | `SG_DELETE_ISOLATED` | | | |
| A4-6 | `SG_RECREATE` | | | |
| A4-7 | `EBS_DELETE_UNATTACHED` | | | |

**A5 — 대상 존재 검사(이월 8행 · 미측정 항목)**

| 항목 | 값 |
| --- | --- |
| 쓴 가짜 인스턴스 ID | ______ |
| 실 AWS 응답 | ______ |
| 걸러졌는가 | 예 / 아니오 |
| 판정 | 걸러지면 **이월 8행 해소**. 아니면 가드레일 ④가 대상 존재를 보증하지 않는다는 사실이 실 AWS에서도 확인된 것 — 그대로 적는다 |

---

## 4. 런북 실동작

| # | 시나리오 | 결과 | 시각 | 비고 |
| --- | --- | --- | --- | --- |
| A6 | `NACL_ADD_DENY` → `NACL_RESTORE` | | | 저장된 `Protocol` 표기 ____ · `PortRange` ____ (이월 6·7행 — 코드로 닫은 것이 실 AWS에서 맞는지) |
| A7 | `RIGHTSIZING`(`idle-dev`) → `REVERT_SIZE` | | | §1의 관측치 수·실제 판정과 실행 전제 확인 |
| A8 | Status Check 2/2 실 대기 → `stopped` 주입 → `FAILED` → 자동 원복 | | | 실 대기와 stopped 실패 분기의 결과. impaired 경로 해소 근거로 쓰지 않음 |
| A8-impaired | Status Check `impaired` 검사 결과 경로 | | | **이월 2행** — 미검증이면 미해소와 사유 기록; stopped 결과로 대체하지 않음 |
| A10 | P1 3종(`SG_DELETE_ISOLATED`·`SG_RECREATE`·`EBS_DELETE_UNATTACHED`) | | | 선택 |

---

## 5. ADR-0006 §4 이월 목록 — 행별 처분

행마다 해소/미해소와 근거를 적고, 미해소면 사유를 남긴다. 미측정 항목은 해소로 표시하지 않는다. 이 표는 관측 결과이며 주차 종료 기준을 정의하지 않는다.

| 행 | 격차 | 처분 | 근거(이 문서의 어느 칸) |
| --- | --- | --- | --- |
| 1 | 가드레일 ④ `DryRun=True` — 실제 IAM 권한 검증 | 해소 / 미해소 | §3 A4 |
| 2 | `get_waiter` Status Check — `impaired`(ⓐ)만 | 해소 / 미해소 | §4 A8-impaired (stopped 실험 제외) |
| 3 | CloudWatch 메트릭 수집(지연·해상도) | 해소 / 미해소 | §2-1 |
| 4 | ALB Target Group · ASG 경로(P2 3종) | 해소 / 미해소 | §2-0 · §2-1 · §2-2 · 10주차 §8 · §8-1 |
| 5 | NACL 생성·삭제의 `DryRun=True` | 해소 / 미해소 | §3 A4-3·A4-4 |
| 6 | `Protocol` 표기 | 해소 / 미해소 | §4 A6 |
| 7 | `PortRange` | 해소 / 미해소 | §4 A6 |
| 8 | `DryRun`의 대상 존재 검사 | 해소 / 미해소 | §3 A5 |

---

## 6. SSOT 기준 참조와 PM 판단 기록

주차 종료 기준은 [PROJECT_STATUS.md](PROJECT_STATUS.md) §현재 위치의 확정본만 참조한다. 기준 문구와 미확정 초안은 이 문서에 복제하지 않는다. 해당 주차 기준이 아직 확정되지 않았으면 판단 대기로 기록한다.

| 항목 | 기록 |
| --- | --- |
| 대상 주차 · 참조한 SSOT 커밋/절 링크 | ______ |
| 판단에 사용한 관측 근거 | 이 문서 §______ / 결과 링크 ______ |
| PM 판단 · 판단자 · 시각 | ______ |
| 미측정·미해소 항목과 일정 영향 | ______ |
| 후속 조치·이월 결정 근거 링크 | ______ |

---

## 7. 관찰 기록 — 표에 안 들어가는 것

실패·막힘·예상과 달랐던 것을 시간 순으로 적는다.

| 시각 | 무엇 | 어떻게 처리했나 |
| --- | --- | --- |
| | | |

---

## 8. 10주차 — P2 3종 검증 기록 (10/12(월)–10/15(목))

| # | 시나리오 | 결과 | 시각 | 비고 |
| --- | --- | --- | --- | --- |
| P2-1 | `EC2_ISOLATE` → `EC2_UNISOLATE` | | | TG 등록 해제 + 격리 SG 교체 → 백업 기준 원복 |
| P2-2 | `EC2_ENABLE_AUTOSCALING` | | | ASG 상한 4대 · **검증 직후 정리**(비용이 예산표 밖) |

### 8-1. 가드레일 ④ — DryRun 부분과 조회 대체 통과 조건 확정 (P2-3)

[ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §4는 이 3종을 MIXED(일부 DryRun + 일부 조회)로 규정하고, **조회 쪽 통과 조건을 "실 AWS 스모크에서 확정한다(현재는 잠정안)"** 으로 남겼다. LocalStack Community에 elbv2·autoscaling이 없어 로컬에서 그 조회가 돈 적이 없다. 이 칸을 비워 두면 10주차 뒤에도 §4의 3행은 잠정으로 남는다. DryRun 부분과 describe 부분을 섞어 적지 않는다.

| # | 런북 | DryRun 부분 결과 | describe 통과 조건 실측(①②③④ 각각) | 잠정 조건 확정 / 수정 필요 |
| --- | --- | --- | --- | --- |
| P2-3a | `EC2_ISOLATE`<br>ENI `modify_network_interface_attribute` DryRun + `describe_target_health`·`describe_security_groups` | | ① DryRun 통과 ______<br>② `isolation_group_id` SG 존재 ______<br>③ TG 존재·대상 등록됨(`Target.NotRegistered` 설명은 미등록으로 본다) ______ | 확정 / 수정 필요 — ______ |
| P2-3b | `EC2_UNISOLATE`<br>ENI DryRun + `describe_target_groups`·`describe_security_groups` | | ① DryRun 통과 ______<br>② 백업 레코드의 복원 대상 SG 전부 현존 ______<br>③ TG 존재·대상이 같은 VPC(`describe_target_groups`의 `VpcId`) ______ | 확정 / 수정 필요 — ______ |
| P2-3c | `EC2_ENABLE_AUTOSCALING`<br>LT `create_launch_template` DryRun + `describe_instances`·`describe_auto_scaling_groups` | | ① DryRun 통과 ______<br>② 원본 EC2 존재·`running` ______<br>③ 동명 ASG 부재 ______<br>④ `min_size <= max_size <= 4` ______ | 확정 / 수정 필요 — ______ |

> 거절이 났으면 사유 코드(`PRECHECK_TARGET_NOT_FOUND` · `PRECHECK_INVALID_STATE` · `PRECHECK_PARAM_INVALID`)와 `verification_summary`를 그대로 적는다. 조건을 고쳐야 하면 ADR-0007 §4 개정 대상으로 §7에 남긴다.

**이월 4행 최종 처분**: ______

---

## 9. 비용

| 항목 | 값 |
| --- | --- |
| 실제 자원 기동–정리 기간(재기동 시 구간별 기록) | ______ – ______ |
| 비용 집계 기간 · 확인 시각 | ______ – ______ · 확인 ______ |
| Budget 알림 수신 | ☐ 50% ☐ 80% ☐ 100% ☐ 예측 100% |
| 실제 청구(확인 가능 시점에) | ______ |

---

## 10. 정리 (10/16(금))

| 항목 | 값 |
| --- | --- |
| `down --yes` 실행 시각 | ______ |
| `status` 관리 대상 자원의 잔여 수 · 미정리 사유 | ______ · ______ |
| 별도 잔존 스냅샷 · 스모크 관련성 확인 근거 | ______ · ______ |
| 스모크 스냅샷 처리 결과 또는 보존 사유·책임자·기한 | ______ |
| 앱 키·정책 삭제 확인(`down`이 사용자째 삭제) | ☐ |
| `.env` LocalStack 복귀(엔드포인트·로컬용 키·리전 설정 확인) | ☐ · 적용/재기동 확인 시각 ______ |

`down`은 EBS 런북이 만든 스냅샷을 자동 삭제하지 않는다. `status`의 스냅샷 목록은 해당 리전의 계정 소유 스냅샷으로, 목록 전체를 스모크 자원으로 간주하지 않는다. 관리 대상 잔여 0건과 스냅샷 잔존 여부를 구분하고, 관련성을 확인한 스냅샷의 처리·보존 결과를 별도로 적는다.

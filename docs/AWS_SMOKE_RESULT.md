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
| A1 | 환경 기동 `up --yes` · `status`의 TG 대상 `healthy` · Budget 알림 선행 생성(월 $300 · 크레딧 제외 — ADR-0009 1차 개정, 당초 $50) | §1 |
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
| 검증 기준 커밋 | 앱: `origin/dev` `fd38c01` · 기동 스크립트: PR #436 head `e7787c2`(Budget $300·크레딧 제외 정의) |
| A6 실행 기준(배포 이미지) | **재실측(10/13(화)) — 배포 이미지 `mvp-5eb2148`**(= `origin/dev` `5eb2148`, PR #440·#441 머지 뒤 `dev`로 재빌드 · ECS 작업 정의 `vigilantis-platform-api:3`). A6 결과 칸의 값은 이 이미지 기준이다. 10/12(월) 1차 실행은 `mvp-7a7209d`(PR #440·#441을 로컬에서 합친 미머지 병합본 — 그 커밋은 `dev`에 남지 않는다)였고 원본을 보존하지 않아 재실측했다(§7 10/12 14:41 · 10/13 10:41) |
| `up --yes` 실행 시각 | 10/12(월) 11:19 KST(인스턴스 `LaunchTime`) · 학원 계정(멤버 계정) — vCPU 한도 5 → 64 상향 승인 직후 |
| `status` TG 대상 `healthy` 확인 시각 | 10/12(월) `up` 직후(ALB 생성 11:20 KST) — `web-1`·`web-2` 2/2 `healthy` |
| 앱 키 Arn (`sts get-caller-identity`) | `arn:aws:iam::***:user/vigilantis-smoke-app` 확인 ☑ |
| `.env` 전환(A2) — `AWS_ENDPOINT_URL` 삭제 ☑ · 키 교체 ☑ · `AWS_REGIONS` 빈 값 ☑ | 10/12(월) 13:22 KST 확인(PM 수행 — 키 값은 출력하지 않고 프로필과 일치만 대조) |
| 전환을 돌린 자리 | 전용 worktree — `docs/INFRA-aws-smoke-run` 브랜치 폴더. 본진 `.env`는 LocalStack 그대로 |
| Budget 알림 생성(월 $300 · 크레딧 제외 — ADR-0009 1차 개정, 당초 $50) | ☑ — **월 $300 · `IncludeCredit=false`**로 섰다(`describe-budgets` 실측) |

### 관측치 확보와 최초 판정 기록

Rule Engine은 경과 시간이 아니라 **실제 CPU 관측치 수(`cpu_datapoints`)가 48개 이상인지** 확인한다. `METRIC_PERIOD_SECONDS` 기본값은 3600초라 약 2일을 예상하지만, 실제 설정·누락·수집 지연에 따라 달라진다. 48개 확보는 데이터 부족 조건을 벗어나는 요건이며, 이후 태그와 사용률 등 각 판정 조건을 함께 확인한다.

| 항목 | 값 |
| --- | --- |
| 인스턴스 기동 시각(A1) | 10/12(월) 11:19 KST — 3대 동시 |
| 실제 `METRIC_PERIOD_SECONDS` / `METRIC_LOOKBACK_DAYS` | 3600 / 14 |
| 관측치 확보 예상 시각(확정 시각 아님) | 예상: 10/14(수) 11:19 KST 무렵 · 산정 근거: 기동 + 48 × 3600초(`status` 출력과 같음) |
| 대상별 실제 CPU 관측치 수 · 스캔 시각 | `web-1`·`web-2`·`idle-dev` · 각 3개 · 10/12(월) 13:24 KST |
| 최초 실제 판정 · 대상 · 시각 | 판정 `SKIP_PROD_PROTECTED`(`web-1`·`web-2`) · `COST_CANDIDATE`(`idle-dev`) · 회차 10/14(수) 10:58:39 KST(`last_collected_at` `01:58:39Z` — 배포된 플랫폼). 직전 회차 10:53:38은 세 대 모두 `SKIP_INSUFFICIENT_DATA` |
| 일정 내 검증 가능 여부와 제약 | **가능 — 예상(11:19)보다 21분 이르다.** 앱은 `get_metric_data`를 "지금−14일" 시작으로 부르므로 1시간 칸 경계가 조회 시각의 분에 맞춰진다 — 48번째 칸이 언제 차는지는 조회 시각에 따라 수십 분 흔들린다(10:20 KST 같은 방식 조회는 47개 · `StatusCode` `Complete`) |

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
| 실행 여부 · 시각 | 미실행 — 9/29(화)까지 환경이 서지 않았다(대상 계정이 학원 계정으로 바뀌어 10/12에 처음 기동). §2-1 본 회차가 첫 실측이다 |
| `collection_runs.status` | (미실행) |
| elbv2·autoscaling 조회 결과 | (미실행 — §2-1 참조) |
| 이월 4행 처분에 보태는 근거 | (미실행 — §2-1 참조) |

### 2-1. 스캔 1회 결과

`collector_failures`는 `_safe_describe`가 흡수한 조회의 **라벨 → 사유 코드** 묶음이며, 적재 시 `error_summary`에 compact JSON으로 실린다(`collector.py` `_failures_summary`). 흡수된 조회는 빈 목록으로 강등되므로, 사유 코드 없이는 **"정상 0건"과 구별되지 않는다.**

| 항목 | 실 AWS 관측 | 판정 · 비고 |
| --- | --- | --- |
| `mode` | `aws` — 회차 `3bb8e0e5-a6e7-4a05-8387-600d73bb77d5` · 10/12(월) 13:24 KST | `aws` 가 아니면 실 AWS 회차가 아니다 — 아래 칸을 채우기 전에 먼저 본다 |
| `collection_runs.status` | `SUCCESS` | `PARTIAL` 이면 아래 사유 코드로 권한 누락인지 가린다 |
| `error_summary` | 빈 값 | `collector_failures` 가 compact JSON 으로 실린 자리다. 로컬에서 나오는 모양은 `{"alb_target_groups":"InternalFailure","auto_scaling_groups":"InternalFailure"}` — **같은 라벨에 `AccessDenied` 가 오면 권한 누락이다** |
| `collector_failures` 라벨 → 사유 코드 | 없음(`error_summary` 빈 값 — 빈 묶음 `{}`이 아니다. PR #439 본문 `## 원본 출력` ③ `(없음)`) — 흡수된 조회 0건 | 사유 코드가 판별 기준이다. `AccessDenied` 는 **권한 누락** — 빠진 조회 권한을 §7에 적고 `policy` 대조. **`InternalFailure` 는 회차의 `mode` 로 뜻이 갈린다** — `localstack` 이면 라이선스 밖이고, **`aws` 면 AWS 측 내부 오류이므로 재시도 후에도 같으면 §7에 적는다**(실 AWS 에서도 나오는 코드다). 어느 코드든 빈 목록으로 강등되므로 "정상 0건"과 구별되지 않는다 |
| `elbv2`(ALB Target Group) 조회 | **성공 — TG 1건 수집**(`vigilantis-smoke-tg`). 실 AWS 첫 성공 | **이월 4행 처분 근거.** LocalStack Community 에 없어 로컬에서 성공한 적이 없다 |
| `autoscaling`(ASG) 조회 | **성공 — 0건**(ASG 없음이 정상). `collector_failures`가 비어 있어 권한 누락으로 인한 강등이 아니다 | **이월 4행 처분 근거.** 같은 이유로 로컬에서 성공한 적이 없다 |
| CloudWatch 메트릭 | 기동 2시간 뒤 3대 모두 관측치 3개(1시간 해상도) — 시간당 1개씩 쌓임. **10/14(수) 실측(`idle-dev` · `get-metric-statistics` 정시 정렬 · 10/12 11:00–10/14 11:00 KST)**: 1시간 칸 49개, **연속 간격 48개 전부 3600초(결손 0)**. 1분 조회의 첫 관측치 10/12 11:20 KST(기동 11:19:59 — **지연 1분 이내**) · 이후 5분 간격(기본 모니터링). 칸별 `SampleCount` 60(첫 칸 40 · 진행 중 칸 57). 관측치 개별 시각은 DB에 없다(수집기가 요약 `MetricSummary`만 적재) — 이 값은 CloudWatch 직접 조회로만 나온다 | **이월 3행 처분 근거.** **시드 회차는 관측치를 즉시 72개로 채우므로 48개 게이트가 걸리지 않는다**(2026-10-06 실측 — 자산 4대 모두 72개). 게이트 자체는 골든 A4(`dp 47`)가 검증한다. 지연·해상도는 실 AWS에서 처음 관측된다 |

### 2-2. 자산 유형별 수집 수

**PR #440(수집 범위 필터) 머지 전 기준:** 수집기는 **리전 전체**를 훑으므로, 수집 수는 아래 스모크 자원 수보다 많을 수 있다(기본 VPC 등 이미 있던 자원). 적을 때 그 차이를 비고에 남긴다 — 많은 것이 이상은 아니고, **적은 것이 이상이다**. **수가 같거나 많아도 스모크 자원이 전부 잡혔다는 뜻은 아니다** — 이름은 §2-3 대상 목록으로 대조한다.

| 자산 유형 | 기대 — 스모크 자원(`provision_smoke_aws.py` 스펙) | 수집 수 | 비고 |
| --- | --- | --- | --- |
| EC2 | 3 — `web-1`·`web-2`·`idle-dev` | 3 | |
| SG | 6 — 명명 5종(`alb`·`web`·`open-ssh`·`unused`·`isolation`) + 스모크 VPC 기본 SG | 7 | +1은 계정 기본 VPC의 기본 SG(`SKIP_WHITELISTED`) — #440 전 동작(리전 전체 수집). #440이 들어간 배포 회차는 6(§7 10/12 14:41) |
| NACL | 2 — `vigilantis-smoke-nacl` + 스모크 VPC 기본 NACL | 3 | +1은 계정 기본 VPC의 기본 NACL — #440 전 동작(리전 전체 수집). #440이 들어간 배포 회차는 2(§7 10/12 14:41) |
| EBS | 4 — 인스턴스 루트 3 + 미연결 `vigilantis-smoke-unattached` 1 | 4 | |
| Launch Template | 0 — `up` 은 만들지 않는다(`EC2_ENABLE_AUTOSCALING` 이 `vigilantis-lt-` 접두로 만든다) | 0 | 0이 정상. **있으면 출처(이름·생성 시각)를 §7에 적는다** — 수집이 리전 전체를 훑으므로 스모크 밖에서 만든 LT도 잡힌다 |
| Auto Scaling Group | 0 — `up` 은 만들지 않는다(P2 검증이 만들고 직후 정리) | 0 — 조회 성공(§2-1) | 0과 미관측을 구분한다. precheck 조건 ③이 **동명 ASG 부재**라 0이 정상 |
| ALB Target Group | 1 — `vigilantis-smoke-tg` | 1 | LocalStack에서는 조회 실패로 **미관측**(자산 0개를 뜻하지 않음) |

### 2-3. 판정 분포

| 판정 | 건수 | 대상 | 비고 |
| --- | --- | --- | --- |
| `COST_CANDIDATE` | 0 | — | CPU 관측치 48개 이상 확보 후 태그·사용률 조건도 대조 |
| `SKIP_INSUFFICIENT_DATA` | 3 | `web-1`·`web-2`·`idle-dev` | 각 관측치 3개(48개 미만) — CPU 관측치 없음 또는 48개 미만인지 대조 |
| `SKIP_PROD_PROTECTED` | 0 | `web-1`·`web-2` | **A9** — 관측치 게이트가 prod 검사보다 앞이라 이 회차(10/12)에는 보이지 않는다. **10/14(수) 10:58:39 KST 회차에서 `web-1`·`web-2` 둘 다 `SKIP_PROD_PROTECTED`, `idle-dev`는 `COST_CANDIDATE`로 확인**(배포 플랫폼 `GET /api/v1/assets` — §1 최초 실제 판정). 이 표의 건수는 10/12 A3 회차 그대로 둔다 |
| `SKIP_WHITELISTED` | 3 | `vigilantis-smoke-isolation` + 이름이 `default` 인 SG 전부 | **경로가 둘이다** — 이름 `default`(삭제·변경 불가)와 `vigilantis:role=isolation` 태그(#359). `default` SG가 이 칸에 함께 뜨는 것이 정상이다 |
| `UNUSED` | 2 | `vigilantis-smoke-unused`(SG) · `vol-0c4297aea92109f3b`(EBS) | 인시던트 2건 생성 — AI 분석은 끈 회차라 분석 대기로 남음 |
| `THREAT` | 1 | `vigilantis-smoke-open-ssh` | |
| 그 밖 | 5 | `SKIP_ACTIVE` — SG `alb`·`web`, 인스턴스 루트 EBS 3 | |

### 2-4. 조인 무결성

**원출력은 선택한 회차로 좁혀지지 않는다.** `find_dangling_arns`에는 회차·계정 범위 인자가 없어 **DB 전체**를 본다. `total`·`investigate`와 종류별 수는 원출력 그대로 기록한다. 집계 단위는 DB 행 수나 고유 자산 수가 아니라 **`(kind, ARN)` 건수**다.

| 항목 | 값 |
| --- | --- |
| `find_dangling_arns` 원출력 (**DB 전체 기준**) | total 2 / investigate 2 |
| 종류별 | `broken_reference` 2 |
| 계정별 조사 대상 기록의 기준 | `--run`으로 지정한 스모크 회차 ID `3bb8e0e5-a6e7-4a05-8387-600d73bb77d5` · 해당 회차가 대상 스모크 계정인지 `mode=aws` 확인 ☑ · `account_id`(회차를 수집한 계정 — PR #439 본문 `## 원본 출력` ③에 있으나 가림)가 §1 계정과 같은지는 **미확인 — 재실측 필요**(가린 값끼리는 대조할 수 없고 대조 기록을 남기지 않았다) |
| 위 조건을 확인한 경우에만 — `(다른 계정)` 조사 대상 제외 후 | investigate 0 · 제외한 조사 대상 2 건 |
| 제외 기준과 남은 조사 대상의 성격 | 2건 모두 `(다른 계정)` — 로컬 DB에 남은 골든 데이터셋(LocalStack 계정)의 ASG·TG 관계 기록이다. 스모크 계정의 조사 대상은 0건 |
| 판정 | 대상 계정의 조사 대상이 0건인지 또는 남아 있는지를 기록하고, 남으면 §7에 원인을 적는다. 같은 계정의 **다른 회차·자원**까지 걸러진 수가 아니므로 "이 회차의 결과"나 스모크 전체 통과로 적지 않는다 |

> 계정별 조사 대상 수를 적을 때는 `scripts/qa_scan_compare.py --run <collection_run_id>`로 대상 스모크 회차를 지정하고, 출력의 `mode=aws`와 `account_id`를 확인한다(계정 ID는 저장소에 적지 않는다). 스크립트는 **출력한 회차들의 계정 집합**에 없는 ARN에 `(다른 계정)`을 붙이므로, 여러 계정의 최신 회차가 섞일 수 있는 기본 실행의 표시를 단일 스모크 계정 기준으로 쓰지 않는다.
>
> 상세 출력과 `(다른 계정)` 표시는 **조사 대상에만** 있다. 제외 후 `investigate`는 DB 전체 `investigate`에서 타 계정으로 확인한 조사 대상 건수를 빼서 기록한다. 정상 보존 항목은 계정별로 출력하지 않으므로 **계정별 `total`은 이 출력으로 산정하지 않는다.** ARN의 계정 칸이 없거나 형식이 불명확한 항목은 **스크립트가 `(계정 확인 불가)`로 따로 표시한다** — 타 계정으로 제외하지 말고 §7에 별도 조사 대상으로 남기며, 계정별 판정은 보류한다.

---

## 3. 가드레일 · DryRun (A4 · A5)

실제 가드레일 `precheck()`와 AWS API 직접 `DryRun=True` 실측은 구분해 기록한다. [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §4에 따라 NACL의 런타임 사전 검사는 환경과 무관하게 describe로 대체한다. describe 성공을 변경 작업의 IAM 권한 검증 성공으로 기록하지 않는다. 여러 API를 확인한 경우 API별 응답과 `verification_summary`를 §7에 남긴다.

| # | 런북 | 검사 방식 · API · 응답/결과 | 권한 누락 | 비고 |
| --- | --- | --- | --- | --- |
| A4-1 | `EC2_RIGHTSIZING` | `precheck()` 통과 — DryRun `ec2.modify_instance_attribute`(`idle-dev` t3.medium→t3.small) | 없음 | 앱 키 · 10/12 오후(A3 스캔 뒤) |
| A4-2 | `EC2_REVERT_SIZE` | 직접 DryRun `ec2.modify_instance_attribute`(`idle-dev`→t3.medium) → `DryRunOperation` | 없음 | `precheck()`는 백업 레코드가 있어야 돌므로 A7 실동작에서 다시 본다 |
| A4-3 | `NACL_ADD_DENY` | `precheck()` 통과(describe `ec2.describe_network_acls`) · 직접 DryRun `ec2.create_network_acl_entry` → `DryRunOperation` | 없음 | 런타임 describe와 별도 직접 DryRun 실측을 구분 — **이월 5행.** DryRun 뒤 규칙 100이 실제로 생기지 않았음을 describe로 확인 |
| A4-4 | `NACL_RESTORE` | 직접 DryRun `ec2.delete_network_acl_entry` → `DryRunOperation` | 없음 | 런타임 describe와 별도 직접 DryRun 실측을 구분 — **이월 5행.** `precheck()`는 백업 필요 — A6에서 다시 본다 |
| A4-5 | `SG_DELETE_ISOLATED` | `precheck()` 통과 — DryRun `ec2.delete_security_group`(`unused` SG) | 없음 | |
| A4-6 | `SG_RECREATE` | 직접 DryRun `create_security_group`(스모크 VPC)·`authorize_security_group_ingress`·`authorize_security_group_egress` → 모두 `DryRunOperation` | 없음 | **울타리 대조**: 기본 VPC에 `create_security_group` → `UnauthorizedOperation`(ADR-0009 §2의 VPC 문장이 의도대로 막음). `precheck()`는 백업 필요 |
| A4-7 | `EBS_DELETE_UNATTACHED` | `precheck()` 통과 — DryRun `ec2.create_snapshot`·`ec2.delete_volume`(미연결 볼륨) | 없음 | |

**A5 — 대상 존재 검사(이월 8행 · 미측정 항목)**

| 항목 | 값 |
| --- | --- |
| 쓴 가짜 인스턴스 ID | `i-0f3a9c2b7d1e4a5b6`(및 대조용 `vol-0f3a9c2b7d1e4a5b6` · `sg-0f3a9c2b7d1e4a5b6`) |
| 실 AWS 응답 | 인스턴스 `modify_instance_attribute` DryRun — **관리자 키 `DryRunOperation` / 앱 키 `UnauthorizedOperation`** · 볼륨 `delete_volume` DryRun — 두 키 모두 `InvalidVolume.NotFound` · SG `delete_security_group` DryRun — 두 키 모두 `InvalidGroup.NotFound`. `precheck()`로는 EBS가 `PRECHECK_TARGET_NOT_FOUND`, 다운사이징이 `PRECHECK_UNAUTHORIZED` |
| 걸러졌는가 | **작업마다 다르다.** 볼륨·SG 삭제 = **예** — 실 AWS가 `InvalidVolume.NotFound`·`InvalidGroup.NotFound`로 대상 존재를 검사한다(두 키 동일). 다운사이징(`modify_instance_attribute` — 인스턴스 타입 변경 API) = **아니오** — 관리자 키에서 없는 인스턴스에도 `DryRunOperation`(DryRun 통과 응답)이 온다. 앱 키의 `UnauthorizedOperation`은 앱 키 정책의 태그 조건(`aws:ResourceTag/vigilantis:smoke` — 스모크 태그가 붙은 자원에만 변경을 허용하는 IAM 조건 키)을 없는 대상에서 평가할 수 없어 생긴 부수효과이며, 가드레일 ④가 대상 존재를 보증한 것이 아니다 |
| 판정 | **이월 8행 미해소 — 가정 반증.** 8행이 든 사례(`modify_instance_attribute` DryRun)를 실 AWS도 거르지 않는다 — 8행의 "실 AWS는 여기서 걸러 줄 것으로 보인다"는 관리자 키 `DryRunOperation`으로 반증됐다. 런북별로 갈린다: `EC2_ISOLATE`(격리)는 사전 검사 `_precheck_isolate`가 인스턴스 조회로 시작해 존재를 검사하고, `EC2_RIGHTSIZING`(다운사이징)의 `_precheck_rightsizing`만 DryRun 호출뿐이다(`apps/core-api/services/aws/executor.py`). 처분은 §5 8행에서 PM이 정한다(§7 A5 행). **시험 범위 주의(안성일 의견 · 10/14 #439 코멘트)**: 이 A5는 `executor.precheck()`를 직접 부른 결과다. 앱의 제안 검증은 그 앞에 ③ ARN Match(수집된 관리 대상인가)와, 다운사이징 후보면 `workflows._candidate_precheck`의 `describe_instances` 현재 타입 조회를 거쳐 실패 시 DryRun 전에 거절한다(`dev` `a929975`에서 코드로 확인). **즉 반증된 것은 "DryRun 단독의 존재 검사"이며, 앱 경로에 존재 확인이 빠졌다는 근거는 아니다.** 그 앱 경로를 없는 대상으로 실 AWS에서 시험하지는 않았다 |

---

## 4. 런북 실동작

| # | 시나리오 | 결과 | 시각 | 비고 |
| --- | --- | --- | --- | --- |
| A6 | `NACL_ADD_DENY` → `NACL_RESTORE` | **둘 다 `SUCCESS`** (재실측 — 배포된 플랫폼 · 사람 승인 `USER_APPROVAL`). **AWS 저장값**: 차단 직후 `describe_network_acls`가 규칙 100(인바운드 deny · `203.0.113.10/32`)을 **`Protocol: "6"`**(번호 문자열) · **`PortRange: {From: 0, To: 65535}`**로 돌려줬다. 백업 레코드(`RECORD_NACL_RULE_INDEX` — `NACL_RESTORE`가 지울 규칙이 우리가 넣은 것인지 대조하는 기록)의 `protocol`도 `"6"`이라 **저장값과 백업이 일치**했고, 해제가 그 대조를 통과해 `SUCCESS`로 끝났다. 해제 직후 조회에서 규칙 100이 사라져 기준선(차단 전 조회)과 같아졌다 | 차단 10/13(화) 10:43:04 승인 → 10:43:08 완료 · 해제 10:43:29 승인 → 10:43:37 완료 KST (1차 10/12(월) 15:04 · 15:06 — 원본 미보존) | **이월 6행**: 실 AWS는 번호로 저장하고, 코드가 번호로 바꿔 보내므로(`NACL_PROTOCOL_NUMBERS`) 백업 대조가 실 AWS에서도 맞는다 — 해소 근거. **이월 7행**: 전체 범위 `0-65535`를 실은 TCP 요청이 거절 없이 들어가 그대로 저장됐다 — 해소 근거(포트 범위 없는 요청의 실 AWS 거절은 재현하지 않았다 — 문서 근거만). **CloudTrail**: 두 호출 모두 주체 `assumed-role/vigilantis-access/vigilantis-platform`, 요청 ID가 실행 단계 기록의 `aws_request_id`와 일치. 원본은 PR #439 본문 `## 원본 출력` ⑥. §5 처분 칸 기입은 집계 담당(판정 기준 ⓓ)이 한다 |
| A7 | `RIGHTSIZING`(`idle-dev`) → `REVERT_SIZE` | **둘 다 `SUCCESS`** — 인시던트 `4a9fb9ae-776c-40c4-abdc-edb2ebc68951`(AI 추천 `RIGHTSIZING` t3.medium→t3.small 1건 · 절감 추정 월 $17.08). 다운사이징 실행 `cc81419c-6f42-4ba9-a6b8-2cc208f5db5e`: 정지 → t3.small → 기동 → Status Check 2/2 대기 뒤 `SUCCESS`. 원복 실행 `0036cc93-d933-4d45-bf9a-e58ad6366873`(사람 승인): t3.medium 복귀 · `SUCCESS`, 원래 실행은 `ROLLED_BACK`. AWS 조회로 타입·`running` 확인, Status Check 2/2 `ok` | 승인 10:59:58 · 다운사이징 `SUCCESS` 11:03:08 · 원복 승인 11:03:21 · 원복 `SUCCESS` 11:03:50 · 2/2 `ok` 11:06:20 · 종료 11:06:21(`JUSTIFIED`) | **원복 `SUCCESS`가 Status Check 2/2 `ok`보다 2분 30초 앞섰다** — 원복 성공 시점의 검사 상태는 `initializing`이었다(§7 10/14 11:03). #434 1-1(원복 뒤 상태와 성공 기록) 대조 항목 |
| A8 | Status Check 2/2 실 대기 → `stopped` 주입 → `FAILED` → 자동 원복 | **자동 원복 `SUCCESS`** — 인시던트 `3e68eefb-48f8-4f29-86ae-da4447875c27`. 다운사이징 실행 `0759c5a1-f121-4951-a576-018b382d6211`이 기동한 직후(t3.small · `pending`) 관리자 키로 `stop-instances` 주입 → 판정 대기 끝에 `ROLLBACK_INITIATED` → 원복 `d387e871-b14c-4a17-b019-d82b9d2c51e2`가 **사람 승인 없이** 생겨 `SUCCESS`(공개 API에 `trigger_source`가 없어 `AUTO_ON_FAILURE`는 승인 호출이 없었다는 사실로 판단), 원래 실행 `ROLLED_BACK`. AWS 조회로 t3.medium·`running` 복귀 확인 | 승인 11:24:38 · 주입 11:25:11.792 · `ROLLBACK_INITIATED` 11:28:14 무렵 · 원복 `SUCCESS` 11:28:19 · 종료(`JUSTIFIED`) | 주입부터 실패 확정까지 약 3분 — #434 1-3(3분 대기 제한)과 대조할 값. 첫 시도는 주입 실패로 무효(§7 10/14 11:10). impaired 경로 해소 근거로 쓰지 않음 |
| A8-impaired | Status Check `impaired` 검사 결과 경로 | **미실행 — 미해소.** 실 AWS에서 `impaired`를 안전하게 일으킬 수단이 없다 — Status Check 실패는 AWS 하드웨어·OS 상태에서 나오며, 사용자가 주입하는 공식 경로(FIS 등)는 스모크 정책·예산 밖이고 커널 손상 같은 OS 내부 유발은 인스턴스를 되살릴 보장이 없다 | 10/14(수) 판단 | **이월 2행** — `stopped` 결과(A8)로 대체하지 않는다. 판정 코드의 `impaired` 분기는 스텁 단위 테스트(`apps/core-api/services/tests/test_status_check.py`의 `test_impaired_check_is_a_failure` — `impaired`를 실패로 판정하는지)로만 덮인다. 이월처: Post-MVP — 장애 주입 도구(FIS) 도입 시 재검토 |
| A10 | P1 3종(`SG_DELETE_ISOLATED`·`SG_RECREATE`·`EBS_DELETE_UNATTACHED`) | **미실행(선택)** — 대상 인시던트 2건(`unused` SG `90231a55-c83a-4d5c-95ee-a7a681818d00` · 미연결 EBS `7945b784-7ac9-466a-b4a2-c2053916516d`)이 배포 플랫폼에서 `AWAITING_APPROVAL`로 서 있으나 승인하지 않았다. 실 AWS 확인은 §3 A4-5–A4-7(`precheck()`·직접 DryRun 권한 검증)까지다 | 10/15(목) 판단 | 선택 항목이라 미실행이 미달이 아니다(SSOT 판정 기준 ⓐ). 12/11(금)까지 스모크 환경이 유지되므로(ADR-0009 1차 개정) Post-MVP에서 승인해 잴 수 있다 — 지운 SG·EBS는 다음 `up`이 다시 만든다 |

---

## 5. ADR-0006 §4 이월 목록 — 행별 처분

행마다 해소/미해소와 근거를 적고, 미해소면 사유를 남긴다. 미측정 항목은 해소로 표시하지 않는다. 이 표는 관측 결과이며 주차 종료 기준을 정의하지 않는다.

| 행 | 격차 | 처분 | 근거(이 문서의 어느 칸) |
| --- | --- | --- | --- |
| 1 | 가드레일 ④ `DryRun=True` — 실제 IAM 권한 검증 | **해소** | §3 A4-1–A4-7 — 앱 키로 런북별 1회, 「권한 누락」 7행 전부 없음 |
| 2 | `get_waiter` Status Check — `impaired`(ⓐ)만 | **미해소** — 사유: 실 AWS에서 `impaired`를 안전하게 일으킬 수단이 없어 미실행. 이월처: Post-MVP(장애 주입 도구 도입 시) | §4 A8-impaired (`stopped` 실험 A8로 대체하지 않음) |
| 3 | CloudWatch 메트릭 수집(지연·해상도) | **해소** | §2-1 — 10/14 실측: 1시간 칸 간격 전부 3600초(결손 0) · 첫 관측치 지연 1분 이내 · 기본 모니터링 5분 간격 |
| 4 | ALB Target Group · ASG 경로(P2 3종) | **해소** — 조회 · 격리·해제 실행 · 자동 확장 실행 모두 실 AWS에서 성공. 서비스 연결·트래픽 전환(#466 운영 표면)은 이 행의 범위 밖이며 미실행(§8 P2-2 비고) | 조회: §2-1(`elbv2`·`autoscaling` 실 AWS 첫 성공). 격리·해제: §8 P2-1(3회 `SUCCESS`). 자동 확장: §8 P2-2(1회 `SUCCESS` · 정리 후 부재 확인) |
| 5 | NACL 생성·삭제의 `DryRun=True` | **해소** | §3 A4-3·A4-4 — 직접 DryRun `DryRunOperation`, DryRun 뒤 규칙이 생기지 않았음을 describe로 확인 |
| 6 | `Protocol` 표기 | **해소** | §4 A6 — 재실측 describe가 `Protocol: "6"`(번호)을 돌려줬고, 해제 precheck(백업 fingerprint `protocol` 대조)가 통과해 `SUCCESS` |
| 7 | `PortRange` | **해소** | §4 A6 — 요청값 `0–65535`(`executor._NACL_ALL_PORTS`)로 차단 `SUCCESS`(거절 없음), 저장값도 `{From: 0, To: 65535}` |
| 8 | `DryRun`의 대상 존재 검사 | **미해소 — 가정 반증.** 사유: `modify_instance_attribute` DryRun은 없는 인스턴스도 거르지 않는다(관리자 키 `DryRunOperation`). **코드는 바꾸지 않는다**(안성일 처분 · 10/14) — 앱 제안 경로는 ③ ARN Match와 `describe_instances` 선조회가 대상 존재를 먼저 확인하므로 사유 매핑(`UnauthorizedOperation → PRECHECK_UNAUTHORIZED`)을 유지하고 조회를 중복 추가하지 않는다. 이월처: 앱 경로의 없는 대상 실 AWS 시험은 Post-MVP | §3 A5 (시험 범위 주의 포함) |

---

## 6. SSOT 기준 참조와 PM 판단 기록

주차 종료 기준은 [PROJECT_STATUS.md](PROJECT_STATUS.md) §현재 위치의 확정본만 참조한다. 기준 문구와 미확정 초안은 이 문서에 복제하지 않는다. 해당 주차 기준이 아직 확정되지 않았으면 판단 대기로 기록한다.

| 항목 | 기록 |
| --- | --- |
| 대상 주차 · 참조한 SSOT 커밋/절 링크 | 10주차(10/12(월)–10/15(목)) = 10/15(목) MVP 마감 판정 · 기준은 SSOT §현재 위치 「주차 종료 판정 기준 (10주차)」 ⓐ–ⓓ(PR #461 · `dev` `2b4a7a6` 시점 문구 그대로) |
| 판단에 사용한 관측 근거 | ⓐ 이 문서 §1–§4(A1–A9 — A9는 §1 「최초 실제 판정」) / PR #439 머지 · ⓑ §8 P2-1·P2-2 · §8-1(P2-3 거절 경로 포함) · §8-2(B5 발동) · ⓒ §11 · ⓓ §5 · §8-1 「이월 4행 최종 처분」 |
| PM 판단 · 판단자 · 시각 | **4/4 충족 — MVP 마감 판정 통과** · 김세혁 · 2026-10-15(목). 칸별 판정 문장은 SSOT §현재 위치 10주차 판정 결과 각주에만 적는다 |
| 미측정·미해소 항목과 일정 영향 | 이월 2행(`impaired` 유도 수단 없음)·8행(DryRun 대상 존재 검사 — 가정 반증) 미해소 + 사유·이월처 기재(§5) · A8-impaired 미실행 · A10(선택) 미실행 · P2-2의 서비스 연결·트래픽 전환과 ASG 상한 확장 미관측. **판정 요건이 아니어서 일정 영향 없다** — 전부 Post-MVP로 넘긴다 |
| 후속 조치·이월 결정 근거 링크 | [ADR-0011](adr/0011-pre-mitigation-latency-target.md)(선차단 목표 3초 · 팀 환경 반복 측정) · [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) 4차 개정(§4 확정) · #434(동작 결함) · #435 · #438(Post-MVP 범위) · #448(B6) |

---

## 7. 관찰 기록 — 표에 안 들어가는 것

실패·막힘·예상과 달랐던 것을 시간 순으로 적는다.

| 시각 | 무엇 | 어떻게 처리했나 |
| --- | --- | --- |
| 10/12 11:17 | 학원 계정의 On-Demand vCPU 한도가 5라 스모크 3대(6 vCPU)가 들어가지 않음 | Service Quotas로 16 요청 → Support 케이스 → 64로 승인. 그 뒤 `up` |
| 10/12 13:24 | A3 회차의 조인 무결성 조사 대상 2건 | 로컬 DB의 골든 데이터셋(다른 계정) 기록 — 스모크 무관(§2-4) |
| 10/12 14:41 | **배포된 플랫폼의 첫 스캔**(ADR-0010 · PR #441) — 회차 `SUCCESS`, 흡수된 조회 0건(`error_summary`가 빈 값인지 빈 묶음 `{}`인지는 이 회차의 `qa_scan_compare.py` 원본을 남기지 않아 미확인 — 재실측 필요). 수집 수 SG 6·NACL 2로 **스모크 VPC만** 잡힘(로컬 A3 회차의 7·3에서 기본 VPC분이 빠짐 — 수집 범위 필터 PR #440) | 앱은 장기 키 없이 태스크 역할 → `vigilantis-access` 전환으로만 AWS에 닿는다. 배포 이미지는 #440·#441을 로컬에서 합친 `mvp-7a7209d`(두 PR 머지 뒤 `dev` 기준으로 재빌드 예정) |
| 10/12 15:00 | 배포 환경에는 모의 위협 폴더가 없다 — SSH 무차별 대입 관측(`evt_ssh_bruteforce_001`, 대상 `web-1`, 출발지 `203.0.113.10`)을 **일회성 ECS 태스크**로 운영 DB에 넣었다 | AI 분석 `PROPOSALS_GENERATED` · 위험도 HIGH · 후보 `NACL_ADD_DENY` 가드레일 거절 0건 → 배포 주소로 승인(A6). 대응 방식이 `PRE_MITIGATION_0_5S`로 정해졌지만 발동 경로(B5)가 없어 자동 선차단은 없었다 — 사람 승인 실행이다 |
| 10/12 15:04 | A6 차단의 CloudTrail 기록 — `CreateNetworkAclEntry` 호출 주체 **`assumed-role/vigilantis-access/vigilantis-platform`** | 배포된 앱이 접근 역할 전환으로 조치했다는 직접 증거(ADR-0010 §3). 이벤트 원본(주체·요청 파라미터)은 보존하지 않았다 — 10/13(화) 재실측분을 PR #439 본문 `## 원본 출력` ⑥에 붙였다(아래 10/13 행) |
| 10/12 오후 | **A5 — 없는 대상에 대한 DryRun 응답이 작업마다 다르다.** 볼륨·SG 삭제는 실 AWS가 대상 존재를 검사해 `NotFound`를 준다(LocalStack은 없는 SG에도 `DryRunOperation` — ADR-0007 실측과 다름). 인스턴스 `modify_instance_attribute`는 존재를 검사하지 않고(관리자 키 `DryRunOperation`), **앱 키에서는 태그 조건(`vigilantis:smoke=true`)을 평가할 수 없어 `UnauthorizedOperation`**이 된다 | **ADR-0006 이월 8행의 "실 AWS는 여기서 걸러 줄 것으로 보인다"는 가정이 반증됐다**(관리자 키 `DryRunOperation` 실측 — §3 A5 판정). 앱 키에서 막힌 것은 IAM 태그 조건 덕이라, 울타리가 느슨한 키에서는 가드레일 ④가 통과한다. 앱 키에서도 다운사이징 대상이 사라진 경우 사유 코드가 `PRECHECK_UNAUTHORIZED`(권한 문제)로 찍힌다 — 관제자가 "대상 없음"이 아니라 "권한 누락"으로 읽을 수 있다. 처분(사유 매핑을 고칠지, describe를 앞세울지)은 이월 8행 처분과 함께 PM이 정한다 |
| 10/13 10:41 | **A6 재실측** — 모의 위협(SSH 무차별 대입 120회/300초, 대상 `web-1`, 출발지 `203.0.113.10`, 관측 ID `evt-ssh-bruteforce-a6r-20261013`)을 일회성 ECS 태스크로 운영 DB에 넣었다(`threat_ingress.receive_threat` 직접 호출 — 배포 환경에 모의 위협 폴더가 없다) | 인시던트 `3f5ed399-56e8-4d8d-a981-b9d8c51ff515` · AI 분석 14초 만에 `PROPOSALS_GENERATED` · 위험도 HIGH · 후보 `NACL_ADD_DENY` 가드레일 거절 0건. 대응 방식이 `PRE_MITIGATION_0_5S`로 정해졌지만 B5(#447)가 아직 없어 자동 선차단은 없었다 — 1차와 같이 사람 승인 실행(배포 주소의 `POST /api/v1/actions/execute`) |
| 10/13 10:43 | A6 재실측의 CloudTrail — `CreateNetworkAclEntry` 01:43:07Z · `DeleteNetworkAclEntry` 01:43:37Z, 두 건 모두 주체 `assumed-role/vigilantis-access/vigilantis-platform` · 오류 없음 | 요청 파라미터 `aclProtocol: "6"` · `portRange: {from: 0, to: 65535}`. 요청 ID가 실행 단계 기록의 `aws_request_id`와 같다(같은 호출임을 묶는 근거). 원본은 PR #439 ⑥ |
| 10/13 16:40 | **P2 검증용 재배포** — `origin/dev` `004ed0a`(#469 B5 · #466 B4 포함)를 `mvp-004ed0a`로 빌드·배포(`--enable-dispatch`) · ECS 작업 정의 `vigilantis-platform-api:6` · 16:43:37 기동 로그에 `automatic_isolation_watcher_started`(1초 주기) | 앱 정책은 따로 갱신하지 않았다 — 실 계정 `vigilantis-smoke-app` 기본 버전 v3(10/13 14:44)에 #452·#459·#466 권한(TG 등록·해제, ASG 생성, scale-in 보호 등)이 이미 있었다 |
| 10/13 17:08 | P2-1 첫 주입 실패 — 일회성 태스크가 `ModuleNotFoundError: schemas` | 운영 진입점 `serve.py`를 거치지 않아 import 경로가 없었다. 덮어쓰기에 `PYTHONPATH=/app/packages:/app/apps/core-api`를 더했다. 아무것도 저장되지 않았다 |
| 10/13 17:11 | P2-1 두 번째 주입 — 선차단이 발동했지만 **가드레일 ④가 `PRECHECK_UNAUTHORIZED`로 거절**(인시던트 `5db1c4ea-71cd-4052-bf9c-3a675cdeb06e`, `EC2_ISOLATE` 실행 `FAILED`) | **주입 방법의 문제이며 제품 결함이 아니다.** CloudTrail상 거절된 `DescribeInstances`의 주체가 `vigilantis-access`가 아니라 태스크 역할 `vigilantis-platform` 자체였다 — 접근 역할 프로필은 `serve.py`가 기동 때 쓰는데 일회성 태스크는 그 진입점을 거치지 않는다. 이후 주입 코드 첫머리에서 `serve.write_access_profile`을 호출했다. 거절된 시도가 `IN_PROGRESS`로 남지 않고 `FAILED`로 확정된 것은 B5 계약대로다. 이 인시던트는 실측 대상이 아니다 |
| 10/13 17:17 | Medium 첫 주입(대상 `web-2`)이 승인 대기에 들어가지 않고 **제안 없이 `AWAITING_CLOSURE`**(인시던트 `30ae473e-4461-453e-bb16-827060e214c3`) | AI가 낸 `NACL_ADD_DENY`가 가드레일 ④에서 `PRECHECK_INVALID_STATE`로 거절됐다. **`web-2` 서브넷은 VPC 기본 NACL(`acl-0f90545…`)에 묶여 있고 그 NACL은 규칙 100번(전체 허용)을 이미 쓴다** — 차단 규칙이 넣을 번호가 차 있다. 스모크 NACL(`vigilantis-smoke-nacl`)은 `web-1` 서브넷에만 붙어 있다. 응답 기한이 생기지 않아 자동 격리 대상이 아니므로, Medium 검증은 `web-1`로 다시 했다. 가드레일 동작은 정상이며, 스모크 구성상 `web-2` 대상 NACL 차단은 늘 거절된다는 관찰이다 |
| 10/13 17:14–17:43 | **P2-1 실측 3회 — 전부 격리 확인 성공 · 해제 성공**(§8 · §8-2) | CloudTrail(08:14–08:43Z): 격리마다 `ModifyNetworkInterfaceAttribute` DryRun(`Client.DryRunOperation`) → `DeregisterTargets` → `ModifyNetworkInterfaceAttribute`(격리 SG), 해제마다 DryRun → `ModifyNetworkInterfaceAttribute`(원래 SG 2개) → `RegisterTargets`. **전 호출 주체 `assumed-role/vigilantis-access/vigilantis-platform`** · 오류 없음 |
| 10/14 11:03 | **A7 원복이 Status Check 2/2 `ok` 전에 `SUCCESS`로 끝났다** — `REVERT_SIZE` `SUCCESS` 11:03:50 시점에 `describe-instance-status`는 `initializing`/`initializing`, 2/2 `ok`는 11:06:20 | 다운사이징(`RIGHTSIZING`)은 2/2 대기 뒤 `SUCCESS`였다(약 2분 30초). 원복 쪽이 같은 대기를 거치는지는 이 관측으로 가리지 않는다 — #434 1-1에 대조 사실로 넘긴다. 이번 회차는 이후 2/2 `ok`까지 확인해 실제 자산은 정상 |
| 10/14 11:10 | **A8 첫 시도 무효 — 정지 주입이 안 들어갔다**(인시던트 `076c2a2c-115b-4bbc-af9b-586795aa58e4`) | 주입기(`40-a8-stop-injector.ps1` — 저장소 밖 개인 도구)가 PowerShell 5.1에서 BOM 없는 UTF-8을 못 읽어 파싱 실패 → bash로 옮긴 주입기는 Windows용 aws CLI 출력 끝의 `\r` 때문에 `running` 비교가 늘 거짓이었다. 실행은 정상 다운사이징 `SUCCESS`로 끝나 사람 승인으로 원복·종료했다. `\r`을 지운 주입기로 다음 회차(A8 본 회차)에 성공 |
| 10/14 11:30 | **P2-2 후보가 서지 않는다** — 10:58 · 11:08 · 11:23 회차의 `idle-dev` 인시던트 3건 모두 AI 추천이 `RIGHTSIZING` 1건뿐이었다(메뉴에는 `ENABLE_AUTOSCALING`도 오른다 — t3.medium은 다운사이징 목표가 있다) | 저사용 EC2에 AI가 다운사이징을 고르는 것은 의도대로다. P2-2를 앱 경로로 시작할 후보 생성 방법이 필요하다 — §8 P2-2 |
| 10/14 16:38 | **P2-2 후보를 앱 경로로 세웠다** — 열려 있던 `idle-dev` 인시던트 `3af11ab8-e4ab-4641-8ef4-961351b37489`(11:28 생성 · `RIGHTSIZING` t3.medium→t3.small)를 승인(16:38:44) → 실행 `ef81239e-cefd-44c6-8c3c-bc7a01a9fc82` `SUCCESS` 16:41:18 → **원복하지 않고** `JUSTIFIED` 종료(16:41:58) | t3.small은 다운사이징 규칙의 목표가 없어(`rightsizing_target_type` → 없음) FinOps 메뉴에 `ENABLE_AUTOSCALING`만 남는다(`ai/capabilities.py` `build_finops_capabilities`). 다음 스캔의 인시던트 `c8c21456-c2bd-4627-a1cf-c6b43559d221`(16:43:39)에서 AI 추천이 `ENABLE_AUTOSCALING` 1건(min 1 · max 1)으로 섰다. 주입이 아니라 앱 경로다. **`idle-dev`는 t3.small로 남아 있다** — A7 재현이 필요하면 t3.medium으로 되돌린다 |
| 10/14 16:49 | **P2-2 생성물 넷을 관리자 CLI로 정리**(16:49:51–16:50:58) — ASG `--force-delete`(인스턴스 `i-088bf09520edf6e85` 종료) → 시작 템플릿 삭제 → AMI 등록 해제 → 스냅숏 삭제 | `service --operation cleanup` 경로는 이 구성에서 쓰지 못한다(CLI가 로컬 DB를 읽어 플랫폼 RDS의 실행 기록을 못 찾는다 — E2E 대본 T4-4). 16:51:04 AWS 조회로 ASG 0 · 시작 템플릿 0 · AMI 0(계정 자체 AMI 전체 0) · 스냅숏 `InvalidSnapshot.NotFound` · ASG 인스턴스 `terminated` 확인 |
| 10/15 오전(09:08 전) | **스모크 계정 밖에 Vigilantis 자원이 없는지 확인** — 로컬 AWS CLI의 `default` 프로필이 개인 계정을 가리키고 있었다(10/12 오배포 사고의 원인). 그 계정을 전 리전 조회했다(EC2·VPC·AMI·스냅숏·EBS·LT·SG·NAT·EIP·ASG·ELB·TG·RDS·ECS·ECR·Secrets·로그 그룹 + IAM·S3·Budget·CloudFront·WAF·Route 53·ACM) | **Vigilantis 자원 0건.** 남은 4건(기본 VPC의 `launch-wizard-1` SG · IAM 역할 2개 · CloudFormation 템플릿 버킷)은 전부 7월 생성으로 다른 개인 프로젝트 소속이라 손대지 않았다. `default` 프로필을 학원 계정으로 바꿔 같은 사고가 다시 나지 않게 했다(개인 계정은 별도 이름의 프로필로 분리). **Vigilantis 실 AWS 자원은 학원 계정에만 있다** |
| 10/15 09:08 | **P2-3 거절 경로 실측**(§8-1 「거절 경로 실측」) — 앱 키 12건(거절 9 · 통과 3) · 관리자 키 3건, 전부 `precheck()` 직접 호출(읽기·DryRun). 원본 출력은 PR #477 본문 `## 원본 출력` | 사유 코드가 ADR-0007 §4 규약과 달랐던 것은 앱 키의 없는 SG 2건뿐이다(`PRECHECK_UNAUTHORIZED` — 태그 조건이 없는 자원에서 평가 불가). 첫 시도의 없는 ID(`i-0123456789abcdef0` 등)는 `InvalidInstanceID.Malformed`로 다른 길을 타 무효 — 형식이 맞는 실재하지 않는 ID로 다시 쟀다 |
| 10/15 09:10–09:12 | **측정용 임시 자원 2종을 만들고 지웠다** — ① 시작 템플릿 `vigilantis-probe-lt-1015` + ASG `vigilantis-asg-i-084f4d1953138068b`(min·max·desired 0, 인스턴스 없음) — 동명 ASG 거절(c-③) 측정 ② `stopped` 인스턴스 `i-013120968f1d3df6e`(t3.nano · 스모크 서브넷 a) — `running` 아님 거절(c-②) 측정 | 관리자 키로 만들고 측정 직후 삭제·종료했다. AWS 조회로 ASG 0 · 시작 템플릿 0 · 인스턴스 `terminated` 확인. 스모크 자원(`status`의 18종)은 건드리지 않았다 |

---

## 8. 10주차 — P2 3종 검증 기록 (10/12(월)–10/15(목))

| # | 시나리오 | 결과 | 시각 | 비고 |
| --- | --- | --- | --- | --- |
| P2-1 | `EC2_ISOLATE` → `EC2_UNISOLATE` | **3회 모두 둘 다 `SUCCESS`**(대상 `web-1` · 배포된 플랫폼 `mvp-004ed0a`). 격리 발동은 사람 승인이 아니라 B5 자동 경로다 — High 선차단 `PRE_MITIGATION_0_5S` 2회 · Medium 1분 무응답 `TIMEOUT_ISOLATION_1M` 1회. 매회 AWS 조회로 확인: 격리 뒤 ENI `eni-09bddbb1d27e83b1a`의 SG가 격리 SG 하나(`vigilantis-smoke-isolation`)로 바뀌고 TG에서 `web-1`이 `draining`(`web-2`는 `healthy` 유지). 해제 뒤 SG가 격리 전 기준값(`open-ssh`·`web` 2개)과 같은 집합으로 돌아오고 TG에 `web-1`이 `healthy`로 복귀. 해제 성공 시 원래 격리 실행은 `ROLLED_BACK` | 격리 10/13 17:14:40 · 17:31:50 · 17:40:35 / 해제 17:15:49 · 17:36:59 · 17:42:48 KST | 해제는 관제자 요청(`POST /api/v1/actions/execute` · `RUNBOOK_EC2_UNISOLATE`)이다 — 화면의 [EC2 격리 해제]와 같은 경로. 인시던트·실행 ID는 §8-2. **HTTP 서비스 응답은 관측하지 않았다**(ALB가 internal이라 PC에서 닿지 않는다) — 서비스 유지는 TG 상태로만 본다. 앞선 실패 두 건은 주입 방법의 문제다(§7 10/13 17:08 · 17:11) |
| P2-2 | `EC2_ENABLE_AUTOSCALING` | **1회 `SUCCESS`**(대상 `idle-dev` `i-084f4d1953138068b` · 인시던트 `c8c21456-c2bd-4627-a1cf-c6b43559d221` · 실행 `0ce4c6bc-f216-4c93-bcfb-bc198d317094` · 사람 승인 `USER_APPROVAL` · 배포된 플랫폼 `mvp-004ed0a`). 네 단계 생성물을 AWS 조회로 확인: AMI `ami-0b1aa18e823d6401c`(`available` · 스냅숏 `snap-09a905b80924a1254`) · 시작 템플릿 `vigilantis-lt-i-084f4d1953138068b`(`lt-06825fbb2e946187b`) · ASG `vigilantis-asg-i-084f4d1953138068b`(min 1 · max 1 · desired 1 · 인스턴스 `i-088bf09520edf6e85` `InService`/`Healthy`) · 정책 `vigilantis-cpu-target-50`(목표 추적 CPU 50%). 원본 `idle-dev`는 `running` 유지. **정리 완료** — 넷 모두 삭제 뒤 부재 확인(§7 10/14 16:49) | 승인 10/14 16:45:19 · AMI 생성 16:45:29 · LT·ASG·정책 16:47:00–16:47:01 · `SUCCESS` 16:49:10 · 정리 16:49:51–16:50:58 · 부재 확인 16:51:04 KST | 후보는 앱 경로로 세웠다(§7 10/14 16:38 — `idle-dev`를 t3.small로 내림). CloudTrail상 `CreateImage`·`CreateLaunchTemplate`·`CreateAutoScalingGroup`·`PutScalingPolicy` 주체 전부 `assumed-role/vigilantis-access/vigilantis-platform`. **`SUCCESS`는 인프라 준비 확인까지다** — 서비스 연결·트래픽 전환(#466 `service` 표면)은 이 구성에서 돌지 않아 미실행(E2E 대본 T4). ASG가 상한까지 늘어나는 동작은 관측하지 않았다(AI가 max 1을 골랐다) |

### 8-1. 가드레일 ④ — DryRun 부분과 조회 대체 통과 조건 확정 (P2-3)

[ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §4는 이 3종을 MIXED(일부 DryRun + 일부 조회)로 규정하고, **조회 쪽 통과 조건을 "실 AWS 스모크에서 확정한다(현재는 잠정안)"** 으로 남겼다. LocalStack Community에 elbv2·autoscaling이 없어 로컬에서 그 조회가 돈 적이 없다. 이 칸을 비워 두면 10주차 뒤에도 §4의 3행은 잠정으로 남는다. DryRun 부분과 describe 부분을 섞어 적지 않는다.

| # | 런북 | DryRun 부분 결과 | describe 통과 조건 실측(①②③④ 각각) | 잠정 조건 확정 / 수정 필요 |
| --- | --- | --- | --- | --- |
| P2-3a | `EC2_ISOLATE`<br>ENI `modify_network_interface_attribute` DryRun + `describe_target_health`·`describe_security_groups` | `precheck()` 통과(앱 키 · `web-1`) — ENI 교체 DryRun 통과 | ① DryRun 통과 **통과**<br>② `isolation_group_id` SG 존재 **통과**<br>③ TG 존재·대상 등록됨(`Target.NotRegistered` 설명은 미등록으로 본다) **통과**<br>(근거: PR #439 본문 `## 원본 출력` ④ P2 행 `summary` — "ENI 교체 DryRun 통과·격리용 SG 존재·TG 존재와 대상 등록") | **확정** — 조건 수정 없음(2026-10-15 · ADR-0007 4차 개정). 거절 경로는 아래 「거절 경로 실측」 표 ②·③ |
| P2-3b | `EC2_UNISOLATE`<br>ENI DryRun + `describe_target_groups`·`describe_security_groups` | 해제 3회 모두 ENI DryRun `Client.DryRunOperation`(CloudTrail · 주체 `vigilantis-access`) 뒤 실제 호출 | ① DryRun 통과 **통과**(3/3)<br>② 백업 레코드의 복원 대상 SG 전부 현존 **통과**(3/3 — 실행이 사전 검사를 지나 `SUCCESS`)<br>③ TG 존재·대상이 같은 VPC **통과**(3/3 — 같은 근거)<br>②③의 `verification_summary` 원문은 공개 API에 없어 옮기지 못했다 | **확정** — 조건 수정 없음(2026-10-15 · ADR-0007 4차 개정). 거절 경로는 아래 표 ②·③ |
| P2-3c | `EC2_ENABLE_AUTOSCALING`<br>LT `create_launch_template` DryRun + `describe_instances`·`describe_auto_scaling_groups` | `precheck()` 통과(앱 키 · `idle-dev`) — LT DryRun 통과 | ① DryRun 통과 **통과**<br>② 원본 EC2 존재·`running` **통과**<br>③ 동명 ASG 부재 **통과**<br>④ `min_size <= max_size <= 4` **통과**<br>(근거: PR #439 본문 `## 원본 출력` ④ P2 행 `summary` — "인스턴스 running·Launch Template DryRun 통과·동명 ASG 부재·min_size/max_size 범위") | **확정** — 조건 수정 없음(2026-10-15 · ADR-0007 4차 개정). 거절 경로는 아래 표 ②·③·④ |

> 거절이 났으면 사유 코드(`PRECHECK_TARGET_NOT_FOUND` · `PRECHECK_INVALID_STATE` · `PRECHECK_PARAM_INVALID`)와 `verification_summary`를 그대로 적는다. 조건을 고쳐야 하면 ADR-0007 §4 개정 대상으로 §7에 남긴다.

**거절 경로 실측 (P2-3 · 2026-10-15(목) 09:08–09:13 KST · 김세혁)** — 위 3행의 「확정」 근거다. `executor.precheck()`를 `dev` `2b4a7a6` 코드로 **직접 호출**했다(읽기·DryRun만 — 자산 변경 없음). 같은 입력을 **앱 키**(`vigilantis-smoke-app` — 운영 접근 역할과 같은 정책)로 12건(거절 9 · 통과 3) 불렀고, 사유 코드가 갈린 2건(a-② · b-②)과 대조용 c-②(없는 인스턴스)는 **관리자 키**로도 불렀다(3건). 없는 자원 ID는 형식이 맞는 실재하지 않는 값을 썼다(`i-084f4d1953138068c` · `sg-074fae62f8856a444` — 형식이 틀린 ID는 `Malformed`로 다른 길을 탄다). 정지 인스턴스와 동명 ASG는 측정용으로 잠깐 만들고 바로 지웠다(§7 10/15).

| # | 조건 | 입력 | 앱 키 결과 | 관리자 키 결과 | 판정 |
| --- | --- | --- | --- | --- | --- |
| a-② | `ISOLATE` 격리 SG 존재 | `web-1` + 없는 격리 SG | `PRECHECK_UNAUTHORIZED` — 확인: 인스턴스와 ENI 존재 | `PRECHECK_TARGET_NOT_FOUND` — 확인: ENI 교체 DryRun 통과 | 거절 ✅ · 사유 표기는 자격마다 다름(아래) |
| a-③ | `ISOLATE` TG 등록 | `idle-dev`(TG 미등록) + `vigilantis-smoke-tg` | `PRECHECK_TARGET_NOT_FOUND` — 확인: ENI 교체 DryRun 통과·격리용 SG 존재 | — | 거절 ✅ · `Target.NotRegistered` 판별이 실 AWS 응답에서 동작 |
| b-② | `UNISOLATE` 복원 SG 현존 | 백업의 복원 SG에 없는 SG 1개 | `PRECHECK_UNAUTHORIZED` — 확인: 인스턴스와 ENI 존재 | `PRECHECK_TARGET_NOT_FOUND` — 확인: SG 복원 DryRun 통과 | 거절 ✅ · 사유 표기는 자격마다 다름(아래) |
| b-③ | `UNISOLATE` TG가 같은 VPC | 백업의 TG = 플랫폼 VPC의 `vigilantis-platform-api` | `PRECHECK_INVALID_STATE` — 확인: SG 복원 DryRun 통과·복원 대상 SG 현존 | — | 거절 ✅ · `describe_target_groups`의 `VpcId` 대조가 실 AWS에서 동작 |
| c-② | `ENABLE_AUTOSCALING` 원본 존재 | 없는 인스턴스 | `PRECHECK_TARGET_NOT_FOUND` — 확인: 없음(인스턴스 조회 실패) | `PRECHECK_TARGET_NOT_FOUND` | 거절 ✅ |
| c-② | `ENABLE_AUTOSCALING` 원본 `running` | 측정용 `stopped` 인스턴스(t3.nano) | `PRECHECK_INVALID_STATE` — 확인: 인스턴스 존재 | — | 거절 ✅ |
| c-③ | `ENABLE_AUTOSCALING` 동명 ASG 부재 | 측정용 ASG `vigilantis-asg-i-084f4d1953138068b`(desired 0)를 먼저 만듦 | `PRECHECK_INVALID_STATE` — 확인: 인스턴스 running·Launch Template DryRun 통과 | — | 거절 ✅ |
| c-④ | `min_size <= max_size <= 4` | `max_size` 5 / `min_size` 3 > `max_size` 2 | 둘 다 `PRECHECK_PARAM_INVALID`(AWS 호출 전) | — | 거절 ✅ |

같은 회차에서 통과 경로 3건(`ISOLATE` `web-1` · `UNISOLATE` 현재 SG·`vigilantis-smoke-tg` · `ENABLE_AUTOSCALING` `idle-dev` 1–2)도 앱 키로 다시 통과했다.

**사유 표기가 자격마다 다른 이유(a-② · b-②)**: 앱 정책은 ENI 교체에 `aws:ResourceTag/vigilantis:smoke` 조건을 건다. 없는 SG에는 태그가 없어 조건을 평가할 수 없으므로 AWS가 ① DryRun 단계에서 `UnauthorizedOperation`을 돌려주고, 가드레일은 ②의 조회에 닿기 전에 `PRECHECK_UNAUTHORIZED`로 멈춘다. 관리자 키는 DryRun이 통과하고 ②가 `InvalidGroup.NotFound`로 걸러 ADR-0007 §4 규약대로 `PRECHECK_TARGET_NOT_FOUND`가 된다. **어느 쪽이든 거절되므로 안전 판정은 같다** — 관제자에게 "대상 없음"이 "권한 누락"으로 보이는 표기 문제이며, §3 A5 · 이월 8행과 같은 성격이다. 처분도 같다(코드 변경 없음 — 2026-10-14 안성일 처분 · ADR-0007 4차 개정 ②).

**이월 4행 최종 처분**: **해소** — P2 3종의 조회(§2-1)와 실행(§8 P2-1 격리·해제 3회 · P2-2 자동 확장 1회)이 모두 실 AWS에서 `SUCCESS`. P2-3a–c의 조회 통과 조건은 **2026-10-15 거절 경로까지 실측해 확정했다**(위 표 · ADR-0007 4차 개정 — 판정 기준 ⓓ의 #426 항목)

### 8-2. #447 자동 격리 — 팀 실 AWS 검증과 시간 기록

> **상태: 팀 실 AWS 실행 기록 완료(10/13(화) — High 2회 · Medium 1회 확인된 성공).** 이 절은 #447 자동 발동 경로가 팀 실 AWS 환경에서 `EC2_ISOLATE`로 이어진 실행과 걸린 시간을 기록하는 자리다. 실행을 마치면 아래 표를 채운다. 개인 AWS 참고 측정은 PR #469 본문에 두고, 이 절에는 팀 실 AWS 실행 결과를 적는다.

**판정 기준과의 관계**

- 10/15(목) MVP 마감 판정의 공식 기준은 [`PROJECT_STATUS.md`](PROJECT_STATUS.md) §현재 위치의 판정 기준 ⓑ다. 그중 #447의 몫은 머지된 자동 발동 경로(High `PRE_MITIGATION_0_5S` · Medium `TIMEOUT_ISOLATION_1M`)가 실 AWS에서 `EC2_ISOLATE`까지 이어지는 것이다(2026-10-13 PM 결정).
- 발동부터 격리 확인까지 걸린 시간은 이 절에 기록한다. **걸린 시간은 판정 요건이 아니다.** 0.5초 목표를 개정할지는 이 문서가 정하지 않는다 — SSOT 10주차 리스크 2번대로 **10/15(목) 판정 전에 `SEC` 복수 담당(안성일 · 김세혁)이 정하고**, 결과는 SSOT §Action Whitelist와 ADR 개정으로 반영한다. 이 절의 수치는 그 결정의 근거로 쓰인다.

**실행 전제**

- 격리 대상은 EC2다. `resolve_isolation_parameters`가 그 EC2가 `REGISTERED_IN`인 TG 1개와, 같은 계정·리전·VPC에 있는 `vigilantis:role=isolation` 태그 SG 1개를 정확히 하나씩 찾으면 실행으로 이어진다.
- 해석 거부·TG/SG 부재·대상 복수는 차단 요인으로 따로 기록한다.
- 예약은 `EC2_ISOLATE` runner와 judge가 모두 등록된 상태(`dispatcher.supports_isolation()`)에서 진행되고, 그 밖의 상태는 `prerequisite_blocked`로 끝난다.

**회차마다 확인할 경계**

- 백업 선행: SG·TG 매핑 백업이 첫 AWS 변경 전에 commit된다. `backup_committed`가 `execution_started`보다 앞서는지 확인한다.
- 실제 격리: 대상 ENI의 SG가 격리 SG로 교체되고 TG에서 대상이 등록 해제됐는지 AWS 조회로 확인한다.
- 확인된 성공: `judge_ec2_isolate` 판정을 거쳐 `outcome=success`·`confirmed=True`로 남은 회차다. runner의 API 호출 성공과 AWS 요청 수락은 그 앞 단계의 기록이다.
- 부분 격리는 `judge_ec2_isolate`가 FAILED로 확정하고, 복원은 수동으로 한다(Post-MVP #435).

**기록 방법**

- 원천은 `vigilantis.isolation_timing` 로거의 `isolation_latency` 로그다. 로그 1건이 시도 1건이다.
- 실패·중단을 포함해 모든 시도를 순서대로 적는다. 시도 시각은 계측 값(`started_at`·`response_deadline_at`)에 맞춰 §0의 KST 규칙 대신 **UTC**로 적는다.
- 시도마다 `started_at`, `trigger_source`, IncidentID, ExecutionID, `outcome`, `confirmed`, `elapsed_to_outcome_ms`, phase 7개(`risk_evaluated` · `incident_committed` · `execution_reserved` · `guardrail_passed` · `backup_committed` · `execution_started` · `isolation_confirmed`)의 `elapsed_ms`·`delta_ms`를 남긴다. `isolation_confirmed`는 확인된 성공에서 찍힌다.
- 결과 구분: `success`(확인된 성공) · `unconfirmed`(확인 전에 끝난 종료) · `failed` · `interrupted`(예외로 중단) · `prerequisite_blocked`(실행 전제 미충족).

**High와 Medium의 구간**

- High(`start_boundary=threat_received`): `end_to_end_ms`가 위협 접수부터 격리 확인까지의 단조 시계 구간이다. 확인된 성공에서 채워지며, 그 밖의 종료는 `elapsed_to_outcome_ms`로 기록한다.
- Medium(`start_boundary=timeout_scan`): 저장된 `agent_wait_started_at`과 `response_deadline_at`(= 시작 + 60초)을 기준으로 아래 값을 High 총 소요와 별도 칸에 적는다. 1초 감시 주기(`AGENT_WAIT_POLL_SECONDS`)는 스캔 주기다.
  - `deadline_to_reservation_ms`: 저장된 마감부터 예약까지의 UTC 차이
  - `deadline_to_confirmation_ms`: 저장된 마감부터 확인까지의 UTC 차이
  - `scan_to_confirmation_ms`·`reservation_to_confirmation_ms`: 스캔 시작 또는 예약부터 확인까지의 단조 시계 구간

| 항목 | 기록 |
| --- | --- |
| 실행 환경(리전·VPC) · 코드 버전(commit) | `ap-northeast-2` · 스모크 VPC `vpc-06c17394fee2f40e9` · 배포 이미지 `mvp-004ed0a`(= `origin/dev` `004ed0a`) · ECS 작업 정의 `vigilantis-platform-api:6`. **회차 조건: 세 회차 모두 main 레인에 진행 중 실행 없음**(다운사이징·EBS 삭제 등 장시간 대기 실행이 없던 시각) |
| 대상 EC2 · TG · 격리 SG | `web-1` `i-044998273825e941e`(ENI 1개) · `vigilantis-smoke-tg` · `vigilantis-smoke-isolation` `sg-074fae62f8856a443` |
| High 발동 → `EC2_ISOLATE`(UTC · IncidentID · ExecutionID · `outcome` · `confirmed`) | ① 08:14:37Z · `7441f799-1271-4703-9677-70278ce83fe8` · `c504b814-74f4-48e5-b6b0-fdd0f55c8b5c` · `SUCCESS`(계측 로그 미수집 — 아래 그 밖의 시도) ② **08:40:32.671Z** · `1b38cb79-ffec-4593-81d1-a687e98f8cb7` · `a8ac617f-9f88-417d-93cb-bac8357e9674` · `success` · `confirmed=true` |
| High `end_to_end_ms` · phase별 `elapsed_ms`/`delta_ms` | ② **`end_to_end_ms` 2932.7** · `risk_evaluated` 0.3/0.3 · `incident_committed` 137.5/137.2 · `guardrail_passed` 1519.6/1382.1 · `execution_reserved` 1561.0/41.4 · `backup_committed` 1846.6/285.6 · `execution_started` 1846.7/0.1 · `isolation_confirmed` 2932.7/1086.1(ms). `reservation_to_confirmation_ms` 1371.8. **가장 큰 구간은 가드레일 4단계(1.38초 — AWS DryRun·조회 포함)와 격리 실행·확인(1.09초)이다** |
| Medium 발동 → `EC2_ISOLATE`(UTC · IncidentID · ExecutionID · `outcome` · `confirmed`) | **08:31:48.287Z** · `5febcd36-7c0f-4819-bfcb-03dd44dd6948` · `3ca939e2-2bf4-4064-827e-73d73c14bc69` · `success` · `confirmed=true`. 저장 기한 `response_deadline_at` 08:31:47.416Z. 승인 대기 중 **관제자 무조작**이었고, 전환 뒤 `response_mode`가 `TIMEOUT_ISOLATION_1M`으로 바뀌고 남은 `NACL_ADD_DENY` 제안은 목록에서 빠졌다 |
| Medium `deadline_to_reservation_ms` · `deadline_to_confirmation_ms` · `scan_to_confirmation_ms` · `reservation_to_confirmation_ms` | 1780.5 · **2991.6** · 2120.1 · 1211.0(ms). phase: `guardrail_passed` 866.2/866.2 · `execution_reserved` 909.1/42.9 · `backup_committed` 1194.4/285.3 · `execution_started` 1194.5/0.1 · `isolation_confirmed` 2120.1/925.6 |
| 백업 선행(`backup_committed` → `execution_started`) · AWS 조회로 본 SG 교체·TG 등록 해제 | 계측된 두 회차 모두 `backup_committed`가 `execution_started`보다 앞섰다(0.1ms 차 — 같은 흐름에서 연이어 찍힌다). SG 교체·TG 등록 해제는 매 회차 `describe_network_interfaces`·`describe_target_health`로 확인했다(§8 P2-1) |
| 그 밖의 시도(`failed` · `interrupted` · `unconfirmed` · `prerequisite_blocked`)와 사유 | High ① 회차는 격리·확인 모두 성공했으나 **계측 로그가 남지 않았다** — 주입한 일회성 태스크가 운영 진입점의 로깅 설정을 거치지 않았다. ②는 주입 코드에서 `setup_logging('INFO')`를 불러 같은 조건으로 다시 잰 회차다. 그 밖에 **가드레일 거절 1건**(`failed` — 08:11Z · 인시던트 `5db1c4ea-…` · `PRECHECK_UNAUTHORIZED`, 주입 태스크가 접근 역할 대신 태스크 역할로 호출 — §7 10/13 17:11)과 **발동 조건 미성립 1건**(Medium `web-2` — 승인 대기에 들어가지 못해 기한이 없음, §7 10/13 17:17)이 있다. 둘 다 제품 경로가 아니라 실측 방법·스모크 구성에서 나왔다 |
| 목표 대비(기록만 — 판정 요건 아님) | High 2.93초 · Medium 기한 후 2.99초. **목표는 2026-10-15에 0.5초 → 3초로 개정됐다**([ADR-0011](adr/0011-pre-mitigation-latency-target.md) — 이 절의 수치와 PR #469 개인 계정 30회가 근거). **두 회차 모두 새 목표 안이다.** 다만 팀 환경 표본이 2회라 p95 확인은 Post-MVP 반복 측정 몫이다(ADR-0011 Consequences) |

---

## 9. 비용

| 항목 | 값 |
| --- | --- |
| 실제 자원 기동–정리 기간(재기동 시 구간별 기록) | ______ – ______ |
| 비용 집계 기간 · 확인 시각 | ______ – ______ · 확인 ______ |
| Budget 알림 수신 | ☐ 50% ☐ 80% ☐ 100% ☐ 예측 100% |
| 실제 청구(확인 가능 시점에) | ______ |

---

## 10. 정리 (12/11(금) 최종 발표 뒤 — ADR-0009 1차 개정, 당초 10/16(금))

| 항목 | 값 |
| --- | --- |
| `down --yes` 실행 시각 | ______ |
| `status` 관리 대상 자원의 잔여 수 · 미정리 사유 | ______ · ______ |
| 별도 잔존 스냅샷 · 스모크 관련성 확인 근거 | ______ · ______ |
| 스모크 스냅샷 처리 결과 또는 보존 사유·책임자·기한 | ______ |
| 앱 키·정책 삭제 확인(`down`이 사용자째 삭제) | ☐ |
| `.env` LocalStack 복귀(엔드포인트·로컬용 키·리전 설정 확인) | ☐ · 적용/재기동 확인 시각 ______ |

`down`은 EBS 런북이 만든 스냅샷을 자동 삭제하지 않는다. `status`의 스냅샷 목록은 해당 리전의 계정 소유 스냅샷으로, 목록 전체를 스모크 자원으로 간주하지 않는다. 관리 대상 잔여 0건과 스냅샷 잔존 여부를 구분하고, 관련성을 확인한 스냅샷의 처리·보존 결과를 별도로 적는다.

---

## 11. 플랫폼 배포 확인 (SSOT 10주차 판정 기준 ⓒ)

판정 기준 ⓒ(플랫폼이 배포돼 CloudFront 주소에서 화면이 선다 · ADR-0010 `Accepted`)의 관측 근거를 이 절에 모은다. 스모크 A군·P2와 같은 계정·리전에 같은 시기 배포돼 같은 PR로 머지되므로 별도 문서를 두지 않는다. §0의 번호 표에 없는 칸이라 번호를 붙이지 않는다. 판정 자체는 [PROJECT_STATUS.md](PROJECT_STATUS.md) §현재 위치에만 적는다.

| 항목 | 값 | 확인 방법 · 시각 |
| --- | --- | --- |
| ADR-0010 Status | `Accepted` — PR #464(10/13(화) 머지) | `docs/adr/0010-platform-deployment-saas-shape.md` |
| 배포 도구 | `scripts/provision_platform_aws.py`(PR #441 · 사용자 도메인·CORS PR #467) | — |
| CloudFront · 사용자 도메인 | 배포 `E3TE9Y8VJ9W6CT` · 기본 도메인 `dgezkqp376m8k.cloudfront.net` · 별칭 `vigilantis.dev` · 뷰어 인증서 ACM(us-east-1) `ISSUED` · WAF `vigilantis-platform-waf` 연결 | 읽기 전용 조회 · 10/13(화) 15시대 KST(#468 코멘트) |
| 오리진 구간 | CloudFront → ALB **`http-only`** — 브라우저→CloudFront만 HTTPS다. 전체 구간 HTTPS로 적지 않는다 | 같은 조회 |
| 배포 코드 버전 | 이미지 `mvp-004ed0a`(= `origin/dev` `004ed0a` — B4 #466 · B5 #469 포함) · ECS 작업 정의 `vigilantis-platform-api:6` · 디스패치 켬 | `provision_platform_aws.py build`·`deploy` 출력 · 10/13(화) 16:40–16:44 KST |
| 수집 범위 | 배포 후 첫 스캔이 스모크 VPC만 잡음(`COLLECT_VPC_IDS` — 플랫폼 VPC 미혼입) | §7 10/12 14:41 |
| 사용자 도메인 응답 | `https://vigilantis.dev/` → 200 · `https://vigilantis.dev/api/v1/assets` → 200, `collection_status: READY` · `last_collected_at` `2026-10-14T00:03:38Z` · 스모크 자산(`vigilantis-smoke-web-1` 등) 반환 | 허용 IP에서 `curl` · 10/14(수) 09:0x KST |
| 브라우저 화면 — 대시보드가 실 `core-api` 데이터로 렌더 | **렌더됨** — `https://vigilantis.dev/` 대시보드: 마지막 수집 10/14 09:18:38 KST · 자산 16건(EC2 3 · SG 6 · EBS 4 · 판정 비대상 3) · 인시던트 9건 · 상단 상태 `수집 정상` · `실시간 연결됨`(WebSocket) · AI 조치 제안 2건(스모크 `UNUSED` SG·EBS). 수치가 위 `api/v1/assets` 응답과 같은 스모크 자산이다 | 허용 IP Chrome · 10/14(수) 09:23 KST(화면 `마지막 갱신 09:23:37`) · 캡처는 PM 로컬 보관 |
| 실 동작 경로 | 배포된 플랫폼에서 A6 재실측(사람 승인)과 P2-1 3회(B5 자동 발동)가 `SUCCESS` | §4 A6 · §8 P2-1 · §8-2 |
| 알려진 제약 | 로그인 없음 — WAF 허용 IP 안의 누구나 승인 가능(ADR-0010 Consequences · Post-MVP #438 ④) | — |

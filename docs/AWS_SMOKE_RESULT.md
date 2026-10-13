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
| A6 실행 기준(배포 이미지) | 배포 이미지 태그 `mvp-7a7209d` — **PR #440(수집 범위 필터)·#441(배포 플랫폼)을 로컬에서 합친 미머지 병합본**이며 위 `fd38c01`과 다르다(§7 10/12 14:41). 두 PR 머지 뒤 `dev`로 재빌드하면 A6 근거 이미지의 커밋은 `dev`에 남지 않는다 — A6을 재현할 기준은 이 줄의 태그다 |
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
| CloudWatch 메트릭 | 기동 2시간 뒤 3대 모두 관측치 3개(1시간 해상도) — 지연 없이 시간당 1개씩 쌓임 | **이월 3행 처분 근거.** **시드 회차는 관측치를 즉시 72개로 채우므로 48개 게이트가 걸리지 않는다**(2026-10-06 실측 — 자산 4대 모두 72개). 게이트 자체는 골든 A4(`dp 47`)가 검증한다. 지연·해상도는 실 AWS에서 처음 관측된다 |

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
| `SKIP_PROD_PROTECTED` | 0 | `web-1`·`web-2` | **A9** — 관측치 게이트가 prod 검사보다 앞이라 아직 보이지 않는다. 10/14(수) 11:19 KST 뒤 재확인 |
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
| 판정 | **이월 8행 미해소 — 가정 반증.** 8행이 든 사례(`modify_instance_attribute` DryRun)를 실 AWS도 거르지 않는다 — 8행의 "실 AWS는 여기서 걸러 줄 것으로 보인다"는 관리자 키 `DryRunOperation`으로 반증됐다. 런북별로 갈린다: `EC2_ISOLATE`(격리)는 사전 검사 `_precheck_isolate`가 인스턴스 조회로 시작해 존재를 검사하고, `EC2_RIGHTSIZING`(다운사이징)의 `_precheck_rightsizing`만 DryRun 호출뿐이다(`apps/core-api/services/aws/executor.py`). 처분은 §5 8행에서 PM이 정한다(§7 A5 행) |

---

## 4. 런북 실동작

| # | 시나리오 | 결과 | 시각 | 비고 |
| --- | --- | --- | --- | --- |
| A6 | `NACL_ADD_DENY` → `NACL_RESTORE` | **둘 다 `SUCCESS`** — 차단 뒤 NACL에 규칙 100(인바운드 deny · `203.0.113.10/32`)이 실제로 생기고, 해제 뒤 실제로 지워짐(describe 확인 — 응답 원본은 남기지 않았다) | 차단 10/12(월) 15:04 · 해제 15:06 KST | **저장값은 미확인 — 재실측 필요.** 이월 6·7행이 묻는 것은 AWS가 저장해 `describe_network_acls`(NACL 규칙 조회 — `NACL_RESTORE`가 백업과 대조하는 상대)가 돌려주는 `Protocol`·`PortRange`인데, 그 응답 원본을 보존하지 않았다. 참고로 CloudTrail `CreateNetworkAclEntry` 이벤트의 **요청값**은 프로토콜 번호 6 · 포트 범위 0–65535 — 코드가 번호·전체 범위로 바꿔 보냈다는 사실일 뿐 AWS가 저장한 값의 근거가 아니다. A6의 원본 출력(CloudTrail 이벤트·차단 후/해제 후 describe·실행 결과 `SUCCESS` 2건)도 미보존 — 재실측 때 PR #439 본문 `## 원본 출력` ⑥으로 붙인다. **배포된 플랫폼으로 실행**(로컬 앱 아님) — 호출 주체가 `assumed-role/vigilantis-access/vigilantis-platform`(§7) |
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
| 10/12 11:17 | 학원 계정의 On-Demand vCPU 한도가 5라 스모크 3대(6 vCPU)가 들어가지 않음 | Service Quotas로 16 요청 → Support 케이스 → 64로 승인. 그 뒤 `up` |
| 10/12 13:24 | A3 회차의 조인 무결성 조사 대상 2건 | 로컬 DB의 골든 데이터셋(다른 계정) 기록 — 스모크 무관(§2-4) |
| 10/12 14:41 | **배포된 플랫폼의 첫 스캔**(ADR-0010 · PR #441) — 회차 `SUCCESS`, 흡수된 조회 0건(`error_summary`가 빈 값인지 빈 묶음 `{}`인지는 이 회차의 `qa_scan_compare.py` 원본을 남기지 않아 미확인 — 재실측 필요). 수집 수 SG 6·NACL 2로 **스모크 VPC만** 잡힘(로컬 A3 회차의 7·3에서 기본 VPC분이 빠짐 — 수집 범위 필터 PR #440) | 앱은 장기 키 없이 태스크 역할 → `vigilantis-access` 전환으로만 AWS에 닿는다. 배포 이미지는 #440·#441을 로컬에서 합친 `mvp-7a7209d`(두 PR 머지 뒤 `dev` 기준으로 재빌드 예정) |
| 10/12 15:00 | 배포 환경에는 모의 위협 폴더가 없다 — SSH 무차별 대입 관측(`evt_ssh_bruteforce_001`, 대상 `web-1`, 출발지 `203.0.113.10`)을 **일회성 ECS 태스크**로 운영 DB에 넣었다 | AI 분석 `PROPOSALS_GENERATED` · 위험도 HIGH · 후보 `NACL_ADD_DENY` 가드레일 거절 0건 → 배포 주소로 승인(A6). 대응 방식이 `PRE_MITIGATION_0_5S`로 정해졌지만 발동 경로(B5)가 없어 자동 선차단은 없었다 — 사람 승인 실행이다 |
| 10/12 15:04 | A6 차단의 CloudTrail 기록 — `CreateNetworkAclEntry` 호출 주체 **`assumed-role/vigilantis-access/vigilantis-platform`** | 배포된 앱이 접근 역할 전환으로 조치했다는 직접 증거(ADR-0010 §3). 이벤트 원본(주체·요청 파라미터)은 보존하지 않았다 — 재실측 때 PR #439 본문 `## 원본 출력` ⑥으로 붙인다 |
| 10/12 오후 | **A5 — 없는 대상에 대한 DryRun 응답이 작업마다 다르다.** 볼륨·SG 삭제는 실 AWS가 대상 존재를 검사해 `NotFound`를 준다(LocalStack은 없는 SG에도 `DryRunOperation` — ADR-0007 실측과 다름). 인스턴스 `modify_instance_attribute`는 존재를 검사하지 않고(관리자 키 `DryRunOperation`), **앱 키에서는 태그 조건(`vigilantis:smoke=true`)을 평가할 수 없어 `UnauthorizedOperation`**이 된다 | **ADR-0006 이월 8행의 "실 AWS는 여기서 걸러 줄 것으로 보인다"는 가정이 반증됐다**(관리자 키 `DryRunOperation` 실측 — §3 A5 판정). 앱 키에서 막힌 것은 IAM 태그 조건 덕이라, 울타리가 느슨한 키에서는 가드레일 ④가 통과한다. 앱 키에서도 다운사이징 대상이 사라진 경우 사유 코드가 `PRECHECK_UNAUTHORIZED`(권한 문제)로 찍힌다 — 관제자가 "대상 없음"이 아니라 "권한 누락"으로 읽을 수 있다. 처분(사유 매핑을 고칠지, describe를 앞세울지)은 이월 8행 처분과 함께 PM이 정한다 |

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
| P2-3a | `EC2_ISOLATE`<br>ENI `modify_network_interface_attribute` DryRun + `describe_target_health`·`describe_security_groups` | `precheck()` 통과(앱 키 · `web-1`) — ENI 교체 DryRun 통과 | ① DryRun 통과 **통과**<br>② `isolation_group_id` SG 존재 **통과**<br>③ TG 존재·대상 등록됨(`Target.NotRegistered` 설명은 미등록으로 본다) **통과**<br>(근거: PR #439 본문 `## 원본 출력` ④ P2 행 `summary` — "ENI 교체 DryRun 통과·격리용 SG 존재·TG 존재와 대상 등록") | 확정 / 수정 필요 — ______ · 비고: **통과 경로만 실측** — 거절 경로(예: `Target.NotRegistered`만 오는 대상)는 미측정이라 조건 확정을 아직 고르지 않는다 |
| P2-3b | `EC2_UNISOLATE`<br>ENI DryRun + `describe_target_groups`·`describe_security_groups` | | ① DryRun 통과 ______<br>② 백업 레코드의 복원 대상 SG 전부 현존 ______<br>③ TG 존재·대상이 같은 VPC(`describe_target_groups`의 `VpcId`) ______ | 확정 / 수정 필요 — ______ |
| P2-3c | `EC2_ENABLE_AUTOSCALING`<br>LT `create_launch_template` DryRun + `describe_instances`·`describe_auto_scaling_groups` | `precheck()` 통과(앱 키 · `idle-dev`) — LT DryRun 통과 | ① DryRun 통과 **통과**<br>② 원본 EC2 존재·`running` **통과**<br>③ 동명 ASG 부재 **통과**<br>④ `min_size <= max_size <= 4` **통과**<br>(근거: PR #439 본문 `## 원본 출력` ④ P2 행 `summary` — "인스턴스 running·Launch Template DryRun 통과·동명 ASG 부재·min_size/max_size 범위") | 확정 / 수정 필요 — ______ · 비고: **통과 경로만 실측** — 거절 경로(예: 동명 ASG가 이미 있는 경우)는 미측정이라 조건 확정을 아직 고르지 않는다 |

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

# ADR-0009: 실 AWS 스모크 환경 — 계정·자격증명·비용 통제와 시연 인프라

- **Status**: Accepted
- **Date**: 2026-09-15
- **Deciders**: 김세혁(PM/Infra) — `INFRA` 담당 결정
- **Refs**: [ADR-0006](0006-localstack-team-standard-env.md) §4·§5 · [ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md) §4 · `docs/PROJECT_STATUS.md` 6주차(9/14–9/18) 판정 기준 ⓕ

## Context (배경)

9주차(10/02–10/08) 실 AWS 스모크는 [ADR-0006](0006-localstack-team-standard-env.md) §4 이월 목록을 해소하는 유일한 자리이고, 10주차(10/12–10/15) P2 3종(`EC2_ISOLATE`·`UNISOLATE`·`ENABLE_AUTOSCALING`) 첫 검증은 그 환경 위에서만 돈다 — `elbv2`·`autoscaling`이 LocalStack Community에 없기 때문이다(ADR-0007 §Context). **스모크 실작업은 9주차 안에서 10/6(화)–10/8(목) 3일이다** — 10/2(금)이 Connect Day 2일차, 10/5(월)이 개천절 대체 휴일, 10/6(화)이 업무 재배정일이다(2026-09-16 긴급 회의 결정). 그래도 계정·키·비용 통제와 ALB·다중 EC2 인프라는 **10/1(목) 이전에 서 있어야 한다** — 8주차(9/28–10/01) 4일에는 릴리스 컷·리허설·Connect Day 1일차가 이미 들어 있고, 10/1(목)–10/2(금)은 Connect Day에 통째로 묶인다.

착수 전에 실측으로 확인한 사실 넷이 이 결정의 바탕이다.

1. **판정에는 48시간 관측 리드 타임이 있고, 그것은 인프라를 세우는 것과 별개로 흐른다.** 판정은 CPU 관측치가 `MIN_DATAPOINTS = 48`(1시간 해상도 — 약 2일) 미만이면 데이터 부족으로 넘긴다(`services/rule_engine.py`). 실 AWS는 `AWS/EC2` 메트릭을 주입할 수 없으므로(ADR-0006 §2) **RIGHTSIZING 대상은 48시간 이상 실제로 떠 있어야 후보가 된다.** 10/6(화) 오전에 판정을 보려면 늦어도 10/4(토) 오전에 기동해야 한다 — **스모크가 10/6(화)로 밀리면서 이 48시간은 여유가 됐고, 기동일을 묶는 것은 메트릭이 아니라 "환경이 10/1(목) 전에 서 있어야 한다"는 쪽이다.** 그래도 이 값은 §6의 기동일이 뒤로 밀릴 때 다시 바닥이 되므로 함께 적어 둔다.
2. **ADR-0006 §4의 "최소 스펙(t3.micro급)"대로면 RIGHTSIZING 후보가 0대다.** 다운사이징 규칙은 메모리 2 GiB 하한 때문에 `t3.small` 이하에는 목표 타입을 내지 않는다(`schemas/rightsizing_policy.py` ③). FinOps 경로를 실 AWS에서 보려면 적어도 1대는 `t3.medium`(→ `t3.small`)이어야 한다.
3. **실 AWS의 커스텀 NACL은 deny-all(32767)만 갖고 태어난다.** 허용 규칙 없이 서브넷에 붙이면 그 서브넷의 통신이 전부 끊긴다. LocalStack은 트래픽을 흉내 내지 않아 이 차이가 로컬에서는 드러나지 않았다.
4. **대상 계정 실측(2026-09-15, 읽기 전용 조회)** — PM이 가진 기존 계정. 서울 리전(`ap-northeast-2`)에는 기본 VPC만 있고 EC2·ALB·ASG가 0개, AWS Budgets 0건, IAM 사용자 1명(AdministratorAccess · 장기 액세스 키), Organizations 미사용. On-Demand 표준 vCPU 한도 32 · ALB 한도 50으로 이번 구성(최대 10 vCPU 내외)에 제약이 없다. `AWSServiceRoleForAutoScaling`은 아직 없다.

## Decision (결정)

**기존 PM 계정의 서울 리전에 인터넷과 끊긴 전용 VPC를 세우고, 앱은 코드가 부르는 AWS 작업만 허용된 전용 키로만 돌린다. 구성·정리는 Boto3 스크립트 `scripts/provision_smoke_aws.py` 하나로 하며, 9/29(화) 오전에 세워 10/16(금)에 걷는다.**

### 1. 계정 — 기존 계정 재사용, 단일 리전, 격리된 전용 VPC

- **새 전용 계정은 만들지 않는다.** 계정 단위 격리가 가장 강하지만, 결제 수단 등록·검증 대기로 리드 타임이 생겨 10/1(목) 전 시험 적용의 여유를 먹는다. 격리 이득의 대부분은 아래 VPC 울타리와 §2의 IAM 울타리로 얻는다.
- **계정 ID는 저장소에 적지 않는다.** 스크립트는 `--account`로 받아 호출 주체(sts)와 대조하고, 다르면 멈춘다.
- **리전은 `ap-northeast-2` 하나다.** MVP 범위(1–2개 리전) 안이며, 두 번째 리전은 스모크 대상이 아니다.
- **전용 VPC(`10.42.0.0/16`)에는 인터넷 게이트웨이를 두지 않는다.** ALB는 `internal`, 인스턴스는 공인 IP가 없다. 그래서 OpenIP 위협 시나리오의 미끼 SG(22/tcp `0.0.0.0/0`)가 **실 계정에서 실제로 열리는 일이 없다.** 스모크가 보는 것은 AWS API의 응답(DryRun·describe·Target Group 상태)이지 인터넷 트래픽이 아니므로 잃는 것이 없다. 웹 서버는 AL2023 기본 `python3`라 패키지 설치도 필요 없다.

### 2. 자격증명 — 키는 둘, 섞지 않는다

| 키 | 쥔 사람 | 쓰는 자리 | 권한 |
| --- | --- | --- | --- |
| 관리자 키(기존 IAM 사용자) | PM | `scripts/provision_smoke_aws.py` 실행 **만** | AdministratorAccess |
| 앱 키(`vigilantis-smoke-app`) | PM | 로컬 `.env` 한 곳 — core-api가 쓴다 | 아래 정책 |

- **관리자 키는 `.env`에 넣지 않는다.** 가드레일은 앱 안의 방어선이고, 앱이 관리자 권한으로 돌면 가드레일이 뚫렸을 때 AWS 쪽에서 막을 것이 없다. **앱 정책은 Action Whitelist의 AWS 쪽 거울**이다 — 코드가 부르지 않는 조치는 가드레일을 우회해도 AWS가 거절한다.
- **정책 울타리는 셋이다.** ① 리전 — 조치 문장은 리전이 박힌 ARN, 조회 문장은 `aws:RequestedRegion` ② 인스턴스·NACL·볼륨 조치는 **`vigilantis:smoke=true` 태그 자원만** ③ SG·ENI 조치는 **스모크 VPC 안만**(`ec2:Vpc`) — `SG_RECREATE`가 만드는 SG에는 우리 태그가 없어 태그가 아니라 VPC로 가둔다. 단 **새 SG 생성**은 가두는 자리가 다르다. `CreateSecurityGroup`은 새 SG와 생성 장소 VPC를 각각 검사하는데 새 SG 쪽 조건 키에는 `ec2:Vpc`가 없으므로(AWS 서비스 권한 참조), 새 SG 쪽은 조건 없이 허용하고 **생성 장소인 스모크 VPC ARN 문장**이 울타리가 된다.
- **정책은 코드가 부르는 작업에서 도출하고, 테스트가 두 방향을 지킨다**(`tests/test_provision_smoke_aws.py`). 실행 경로를 더한 PR이 정책을 빠뜨리면 깨지고, 코드가 부르지 않는 조치 권한이 끼어도 깨진다. DryRun도 같은 권한을 요구하므로 precheck만 있는 런북의 작업도 들어간다. P2 실행 작업(`elbv2.deregister_targets`·`register_targets`·`autoscaling.create_auto_scaling_group`)은 아직 코드에 없어 정책에도 없다 — **그 실행 경로를 붙이는 PR이 정책을 함께 고친다.**
- **조회 권한도 같은 거울로 지킨다.** 조회 문장(`READ_ACTIONS`)은 `ec2:Describe*` 같은 와일드카드라 손으로 적은 목록이고, 위 두 방향 테스트는 그것을 통째로 제외한다. 그래서 수집기가 `Describe*` 밖의 조회를 더하면 정책이 그대로여도 테스트가 초록불이 된다 — 게다가 수집기는 autoscaling·elbv2·launch template 조회를 `_safe_describe`로 감싸 **AccessDenied까지 빈 목록으로 강등하고 회차를 PARTIAL로 마감하므로**(ADR-0006 §4, C4) 권한이 빠진 채 스모크를 돌려도 LocalStack에서 늘 보던 PARTIAL과 겉모습이 같다. `test_policy_grants_every_read_the_collector_makes`가 `services/collector.py`가 부르는 조회를 소스에서 뽑아 정책과 대조한다.
- **정책은 사용자 인라인이 아니라 고객 관리형 정책으로 붙인다.** 사용자 인라인 정책은 합계 2,048자(공백 제외)가 한도인데, 코드가 지금 부르는 작업만 담은 정책이 이미 2,084자다. 관리형 정책 한도는 6,144자이고, 위 P2 실행 경로가 붙으며 늘어날 문장도 여기에 담긴다. 한도는 테스트가 지킨다 — 넘기는 PR은 `up`이 아니라 CI에서 깨진다. `up`은 문서가 바뀐 때(VPC를 다시 세운 때)만 새 버전을 기본으로 올리고(정책당 버전 5개 한도 — 가장 오래된 비기본 버전부터 지운다), `down`은 사용자와 함께 정책도 지운다. **앱 사용자의 권한은 이 정책 하나뿐이다** — `up`은 전환 전에 붙었을 인라인 정책을 걷어 권한이 두 문서의 합집합이 되지 않게 한다.
- **조건 키가 실제로 채워지는지는 적용 직후 잰다.** 앱 키로 DryRun을 1회씩 불러 `UnauthorizedOperation`이 나면 정책을 고친다. ADR-0006 §4 1행(DryRun이 IAM을 검증하지 않음)을 해소하는 첫걸음이 이것이다.
- **앱 키는 1개이고 PM만 쥔다.** 팀원에게 배포하지 않는다 — 팀원의 개발·테스트 표준은 그대로 LocalStack이다. Slack·이슈·PR에 붙이지 않는다.
- **스크립트는 키를 만들지 않는다.** 비밀이 스크립트 출력·로그에 남지 않게 PM이 `aws iam create-access-key`로 발급해 `.env`에만 넣는다.
- **수명은 스모크 기간 하나다.** 10/6(화) `.env` 전환 때 발급하고(§6 3단계), 10/16(금) `down`이 사용자째(키·정책 포함) 지운다. 9/29(화) 기동은 관리자 키로 하므로 앱 키가 그보다 먼저 있을 이유가 없다.
- **유출 시**: 즉시 `aws iam delete-access-key`로 끊는다. 앱 키가 부른 호출은 CloudTrail 이벤트 기록(90일, 무료)으로 추적한다.

### 3. 비용 통제

- **AWS Budgets 월 $50** — 실제 비용 50·80·100%와 예측 100%에서 이메일 알림. **알림선이지 상한이 아니다.** 스크립트는 알림 주소(`--budget-email`) 없이 `up`을 거부하고, Budget을 과금 자원보다 먼저 만든다.
- **예상 비용**(AWS Pricing API, 서울 온디맨드 · 2026-09-15 조회):

  | 자원 | 단가 | 수량 | 시간당 |
  | --- | --- | --- | --- |
  | EC2 `t3.micro` | $0.013/h | 2 | $0.026 |
  | EC2 `t3.medium` | $0.052/h | 1 | $0.052 |
  | ALB(internal) | $0.0225/h + LCU $0.008/LCU-h(사용분) | 1 | 약 $0.023–0.031 |
  | EBS gp3 | $0.0912/GB-월 | 루트 3개(AMI 기본 8 GiB 가정) + 1 GiB | 월 약 $2.3 |

  하루 약 **$2.5–2.7**, 9/29(화)–10/16(금) 18일에 약 **$45–49**다. 10월분만 따지면 월 예산 $50 안이다. `ENABLE_AUTOSCALING` 검증 때 ASG가 띄우는 인스턴스(`AutoScalingSize` 상한 4대)는 여기에 없다 — 검증 직후 정리한다.
- **비용을 줄이는 설계**: 인터넷 게이트웨이·NAT 게이트웨이·공인 IPv4 없음(공인 IPv4는 개당 $0.005/h), t3 CPU 크레딧 `standard`(버스트 초과분 과금 없음 — t3 기본값은 `unlimited`), 상세 모니터링 끔(기본 5분 해상도는 무료이고 판정은 1시간 집계를 쓴다).
- **기동은 9/29(화) 오전, 정리는 10/16(금)이다. 사이에 끄지 않는다.** 끄면 48시간 관측이 끊긴다(Context 1). ADR-0006 §4의 "검증 직후 리소스 정리"는 **스모크마다가 아니라 10/15(목) MVP 마감 판정 직후 한 번**으로 읽는다.
- **정리 확인은 `status`의 잔여 0건이다.** 앱이 런북으로 만든 것(재생성 SG·ASG와 그 인스턴스)은 VPC 안이면 `down`이 함께 걷는다. VPC 밖에 남는 것 — `EBS_DELETE_UNATTACHED`가 남기는 스냅숏 — 은 태그가 없어 우리 것인지 가릴 수 없으므로 **보고만 한다.**
- **Budget은 `down` 뒤에도 월말까지 남긴다.** 정리 뒤 남은 과금을 잡는 그물이다.

### 4. 시연 인프라 — 무엇을 세우나

| 자원 | 스모크에서 맡는 자리 |
| --- | --- |
| `web-1` (`t3.micro` · 서브넷 a · `Environment=production`) | `EC2_ISOLATE` 대상 · OpenIP 위협 SG · `NACL_ADD_DENY`가 겨누는 서브넷. production이라 비용 판정은 **관측치가 찬 뒤** `SKIP_PROD_PROTECTED` — 보호 규칙도 실 AWS에서 함께 본다 |
| `web-2` (`t3.micro` · 서브넷 c · `production`) | 격리 뒤에도 Target Group을 잇는 두 번째 대상 — **다중 EC2의 자리** |
| `idle-dev` (`t3.medium` · 서브넷 a · `development`) | `RIGHTSIZING` → `REVERT_SIZE`, Status Check 주입(ADR-0006 §4 2행) |
| internal ALB · Target Group · 80 리스너 | `ISOLATE`·`UNISOLATE`의 `elbv2` 경로(ADR-0007 §4 잠정 통과 조건의 첫 실행) |
| 전용 NACL(서브넷 a) · 허용 규칙 **32766** | `NACL_ADD_DENY`·`NACL_RESTORE`와 그 `DryRun`(ADR-0006 §4 5행) |
| SG 5개 — alb · web · open-ssh(미끼) · unused · isolation(규칙 0개 · `vigilantis:role=isolation`) | `SG_DELETE_ISOLATED` 대상(unused) · `EC2_ISOLATE`의 `isolation_group_id`(isolation) |
| 미연결 EBS 1 GiB gp3 | `EBS_DELETE_UNATTACHED` |
| 앱 IAM 사용자·관리형 정책 · Budget · `AWSServiceRoleForAutoScaling` | §2 · §3 · 아래 |

- **NACL 허용 규칙을 32766에 두는 이유**: AI는 `rule_number`를 1–32766에서 고른다(`ai/agent.py`, `RuleNumber`). 허용을 그 맨 끝에 둬야 AI가 넣는 어떤 차단 규칙도 허용보다 먼저 평가된다. 같은 번호를 고르면 precheck ②가 점유로 거절하므로 조용히 무력화되는 차단은 없다. 허용 규칙은 NACL을 서브넷에 붙이기 **전에** 넣는다.
- **격리용 SG에는 자리를 밝히는 태그 `vigilantis:role=isolation`을 단다.** 격리 전까지 어디에도 붙지 않아 판정이 미사용 후보로 보기 때문이다(Consequences). 이름이나 규칙 수로 가리는 것은 추정이라 태그로 가둔다 — **스크립트는 자리만 선언하고, 어느 `role`을 판정에서 뺄지는 규칙 쪽이 정한다**(규칙 소유자 김승철의 DATA 카드).
- **ASG는 세우지 않는다** — `ENABLE_AUTOSCALING`이 만드는 것이다. 다만 계정의 첫 ASG에는 서비스 연결 역할이 필요하고 앱 키에는 `iam:CreateServiceLinkedRole`을 주지 않으므로, 역할은 스크립트가 관리자 권한으로 미리 만든다(무료, `down` 뒤에도 둔다).
- **`up`은 없는 것을 만들 뿐 바뀐 것을 되돌리지 않는다.** 스모크 도중 다시 돌려도 격리(Target Group 등록 해제)나 차단 규칙을 조용히 되돌리지 않게 하기 위해서다. 런북이 지운 자원(미사용 SG·미연결 EBS)은 다시 만든다 — 재실행 준비가 그것이다. `RIGHTSIZING`이 바꾼 타입은 `up`이 되돌리지 않으며 `REVERT_SIZE`(관제자 수동 요청)로 되돌린다.
- **예외는 만든 직후의 설정이 끝나지 않은 자원이다.** SG 규칙·NACL 허용 규칙·Target Group 대상 등록은 생성과 별개 호출이라 도중에 끊길 수 있다. 그래서 생성 때 초기화 표지 태그(`vigilantis:smoke-init=pending`)를 함께 달고 설정이 끝나야 `done`으로 바꾼다. 다음 `up`은 `pending`인 자원만 설정을 이어서 끝내며, 허용 규칙이 다 서기 전의 NACL은 서브넷에 붙이지 않는다. 설정이 끝난 자원과 표지 없는 자원(앱이 만든 것)은 위 원칙대로 손대지 않는다.

### 5. 구성 도구 — Terraform이 아니라 Boto3 스크립트

- **IaC는 Post-MVP다.** ADR-0006 §2가 "IaC는 Post-MVP이고(MVP는 Boto3 직접 실행)"라고 정했고, `README.md`도 `packages/iac/`를 Post-MVP 자리표시자로 둔다. Terraform으로 세우려면 그 결정을 개정해야 하는데, 18일짜리 스모크 환경 하나가 그 이유가 되지 않는다.
- **방식은 LocalStack 시드와 같다** — 태그(`vigilantis:smoke`) 식별·멱등·실행 전 가드. 가드는 시드의 거울상이다: 시드는 `AWS_ENDPOINT_URL`이 **없으면** 멈추고, 이 스크립트는 **있으면** 멈춘다.
- **시드 원천이 둘이 되는 것이 아니다.** ADR-0006 §2의 "원천 하나"는 LocalStack 시드의 규칙이며 그대로다. 이 스크립트는 다른 환경(실 AWS)의 원천이고, 두 환경은 원래 같을 수 없다(elbv2 유무·메트릭 주입 가능 여부).
- **대가**: Terraform이 거저 주는 삭제 순서를 스크립트가 직접 챙긴다. 앱이 만든 자원까지 한 번에 걷기 위해 **VPC 단위로 쓸어 담는 방식**으로 단순화했다.

### 6. 운영 절차

1. **시험 적용(10/1(목) 전, 1회)** — `status` → `up --yes` → `status`(TG 대상 `healthy`) → 앱 키로 DryRun 1회씩 → **앱 키로 스캔 1회**(`collection_runs.status == SUCCESS` · `error_summary` 빈 값 — `PARTIAL`이면 `collector_failures`의 라벨·사유에서 빠진 권한을 찾는다) → `down --yes` → `status` 잔여 0건. 권한·쿼터·부팅 문제를 8주차(9/28–10/01) 전에 털어낸다.
   - **DryRun과 스캔은 재는 것이 다르다.** DryRun은 조치 권한만 실측하고, 조회 권한(`aws:RequestedRegion` 조건 포함)이 런타임에 실제로 채워지는지는 수집기를 한 번 돌려야 보인다. 그 실패는 `_safe_describe`가 PARTIAL로 삼키므로 회차 상태를 눈으로 확인한다.
   - 이 회차는 **ADR-0006 §4 이월 목록(autoscaling·elbv2 조회가 실 AWS에서 실제로 채워지는지)의 첫 실측**이기도 하다.
2. **본 기동 9/29(화) 오전** — `up --yes`. `status`가 인스턴스마다 첫 판정 가능 시각(기동 + 48시간)을 보여 준다. 그 전에는 `idle-dev`의 `RIGHTSIZING`도 `web-1`·`web-2`의 `SKIP_PROD_PROTECTED`도 보이지 않고 셋 다 `SKIP_INSUFFICIENT_DATA`다 — `evaluate_ec2`가 관측치를 prod보다 먼저 보기 때문이다.
3. **앱 키 발급과 `.env` 전환 — 10/6(화) 오전, Connect Day 이틀이 끝난 뒤** — ADR-0006 §4 전환 절차 그대로: 키 교체 + `AWS_ENDPOINT_URL` 줄 삭제를 **함께** 하고, `aws sts get-caller-identity`의 Arn이 `user/vigilantis-smoke-app`인지 확인한다(계정 ID가 `000000000000`이면 아직 LocalStack이다).
   - **Connect Day가 끝날 때까지 `.env`는 LocalStack 그대로다.** 8주차(9/28–10/01)의 릴리스 컷·리허설도, 10/1(목)–10/2(금) Connect Day 두 세션도 LocalStack 기반이다(`docs/PROJECT_STATUS.md` 마일스톤 표). 전환을 9/29(화) 기동에 붙이면 그 전부가 실 AWS를 향한다. 전환은 스모크 첫날(10/6(화))에 붙인다.
   - **스크립트를 돌리려고 `.env`를 고치지 않는다.** 스크립트는 `.env`를 읽지 않고(`config.py`의 `AwsSettings`는 실제 환경변수만 읽는다) 관리자 프로필의 자격증명으로 돈다. compose의 앱은 `env_file`로 `.env`만 받으므로, 스크립트를 돌린 셸의 설정이 시연 앱에 새지 않는다.
   - **`AWS_REGIONS`가 비어 있는지 함께 확인한다.** 조회 문장이 `aws:RequestedRegion`으로 서울 하나에 묶여 있어서, 두 번째 리전이 설정돼 있으면 그 리전의 `describe_instances`는 `_safe_describe` 밖이라 **그 리전 회차가 매번 FAILED로 마감된다.** 리전별로 회차가 갈리므로 서울 회차는 살지만 실패 로그가 5분마다 쌓인다.
   - RIGHTSIZING 판정은 앱이 9/29(화)부터 돌 필요가 없다 — 수집기는 CloudWatch에 쌓인 과거 관측치를 조회 창(`METRIC_LOOKBACK_DAYS`)만큼 한 번에 읽는다. 필요한 것은 인스턴스가 48시간 떠 있었다는 사실뿐이다.
4. **스모크 10/6(화)–10/8(목) · P2 검증 10주차(10/12–10/15)** — 런북이 겨눌 값(Target Group ARN·격리 SG·NACL ARN 등)은 `up`이 출력한다. 대본이 손으로 조립하지 않는다.
5. **10/16(금) 정리** — `down --yes` → `status` 잔여 0건 → `.env`를 LocalStack 기본값으로 되돌린다. 12/11(금) 최종 발표의 배포 환경은 Post-MVP 범위 확정 뒤 따로 정한다.

## Consequences (결과·트레이드오프)

**장점**

- 10/6(화) 스모크의 전제가 코드와 절차로 선다 — 계정·키·비용·인프라가 한 스크립트와 한 문서에 있다.
- 인터넷에서 닿는 자원이 0개라 실 계정에서 위협 미끼를 세워도 노출이 없고, 공인 IPv4·NAT 과금도 없다.
- 앱 정책이 코드의 거울이라 가드레일 밖에 AWS 쪽 방어선이 하나 더 생기고, 실행 경로를 더한 PR이 권한을 빠뜨리는 것을 CI가 잡는다.

**비용/유의**

- **실 AWS 접근이 PM 1명에게 몰린다.** PM 부재 시 스모크가 멈춘다. 키 유출면을 최소로 두는 대가로 감수한다.
- **정책의 조건 키(`ec2:Vpc`·태그 조건)는 아직 실측 전이다.** 작업·자원별로 그 조건 키를 지원하는지는 AWS 서비스 권한 참조와 대조했다. 남은 것은 런타임에 실제로 채워지는지이며, 시험 적용의 DryRun이 첫 실측이다.
- **격리 SG는 평소에 `UNUSED`로 판정되며, 판정 규칙이 태그로 제외한다.** `evaluate_sg`는 미부착 SG를 곧장 미사용 후보로 보고 EC2와 달리 관측치 게이트(`MIN_DATAPOINTS`)가 없으므로(`services/rule_engine.py`), 격리 때까지 붙지 않는 isolation SG는 **첫 회차부터** `vigilantis-smoke-unused`와 나란히 `SG_DELETE_ISOLATED` 후보로 올라온다. 지워지면 `EC2_ISOLATE`는 precheck에서 `PRECHECK_TARGET_NOT_FOUND`로 멈추므로 잘못된 격리는 없지만 시연 경로가 막힌다. 그래서 스크립트가 그 SG에 `vigilantis:role=isolation`을 달고(§4), **규칙 쪽이 그 태그를 `SKIP_WHITELISTED`로 읽는다** — 이름·규칙 수 같은 추정이 아니라 태그로 가둔다. 규칙 반영은 규칙 소유자(김승철)의 DATA 카드이며, 그 전까지는 관제자가 두 SG를 구별해 골라야 한다.
- **수집은 계정의 서울 리전 전체를 본다** — 기본 VPC의 자원도 함께 들어온다. 기본 SG는 `SKIP_WHITELISTED`로 빠진다.
- **48시간 리드 타임은 `MIN_DATAPOINTS`에 묶여 있다.** 그 값이 바뀌면 §6의 기동일도 바뀐다.
- **`EBS_DELETE_UNATTACHED`가 남기는 스냅숏은 자동으로 걷히지 않는다**(태그 없음 — 보고만). 1 GiB라 월 $0.1 미만이지만 `status`로 확인해 손으로 지운다.

## Related

- 선행 결정: [ADR-0006](0006-localstack-team-standard-env.md) §2(IaC Post-MVP · 시드 원천 규칙) · §3(전환 스위치) · §4(이월 목록 · 전환 절차) — 이 ADR은 §4의 "비용 통제 — 단일 계정, 최소 스펙(t3.micro급), 검증 직후 리소스 정리"를 구체화하며, t3.micro급 원칙의 예외 1대(`t3.medium`)와 정리 시점(10/16(금) 일괄)을 정한다
- [ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md) §4 — P2 3종의 잠정 통과 조건이 처음 실행되는 환경이 이것이다
- 구현: `scripts/provision_smoke_aws.py` · `tests/test_provision_smoke_aws.py` · `.env.example`(전환 주석)
- 코드 근거: `services/rule_engine.py`(`MIN_DATAPOINTS`·`evaluate_sg`) · `schemas/rightsizing_policy.py`(하한 규칙) · `schemas/runbook_parameters.py`(`RuleNumber`·`AutoScalingSize`) · `services/aws/executor.py`(`RUNBOOK_SPECS`·실행 작업 상수)

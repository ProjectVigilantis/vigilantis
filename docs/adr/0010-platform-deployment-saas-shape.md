# ADR-0010: 플랫폼 배포 — 단일 계정이지만 SaaS 모양으로 (CloudFront 기본 도메인 · 역할 전환 · 수집 범위)

- **Status**: Accepted (2026-10-13 — 안성일 검토 PR #441 · 김세혁 확인 PR #464)
- **Date**: 2026-10-12
- **Deciders**: 김세혁(PM/Infra — `INFRA` 담당 결정), 안성일(AI·Architect — FastAPI 아키텍처·DB 검토)
- **Refs**: [ADR-0006](0006-localstack-team-standard-env.md) §2(IaC Post-MVP) · [ADR-0009](0009-real-aws-smoke-environment.md)(스모크 환경 · 앱 정책) · #435 4-2·6-1

## Context (배경)

2026-10-12 PM 결정으로 **10/15(목) MVP 마감 판정 기준에 플랫폼 배포가 들어왔다.** 지금까지 앱은 로컬 compose에서만 돌았고 `packages/iac/main.tf`는 빈 자리표시자다. 같은 날 Post-MVP 방향도 정해졌다 — **AWS Marketplace 기반 SaaS**: 중앙 계정의 플랫폼이 고객 계정의 역할로 AssumeRole해 관제하고, 구독이 끊기면 그 길을 회수한다.

MVP는 단일 계정(SSOT §MVP 확정 범위)이므로 계정을 가를 수 없다. 그래서 이 배포의 목표는 **"단일 계정 안에서 SaaS와 같은 모양"** 이다 — 역할 전환·장기 키 없음·공개 경로처럼 Post-MVP에서 다시 쓸 방향을 지금 구조에 담는다. Post-MVP의 테넌트 분리·인증·원격 회수·VPC 설계는 후속 SaaS 설계에서 정한다.

배포 전에 확인한 사실 넷이 결정의 바탕이다.

1. **수집기는 리전 전체를 읽는다**(`services/collector.py`, VPC 필터 없음). 플랫폼을 관제 대상과 같은 계정·리전에 두면 플랫폼 ALB의 보안그룹(443/80 공개)이 위협으로, 플랫폼 SG가 미사용 후보로 판정된다. SaaS에서는 계정 경계가 이것을 가른다.
2. **도메인이 없다.** 대상 계정은 학원이 제공한 Organizations 멤버 계정이고, Route 53 도메인 등록은 AWS 크레딧 적용 대상이 아니며 학원 정산 대상이 된다(구매 여부는 학원 확인 뒤).
3. **웹은 서버 컴포넌트에서 API를 부른다**(`apps/web/src/app/*/page.tsx`). API 주소(`NEXT_PUBLIC_API_BASE_URL`)는 빌드 때 번들에 박히고, 서버 측 호출도 같은 주소로 나간다.
4. **api는 프로세스 하나여야 한다.** 스캔 스케줄러·실행 디스패처·AI 디스패처·WebSocket이 프로세스 메모리에서 돌고, 디스패처에는 프로세스 간 잠금이 없다(#435 4-2).

## Decision (결정)

**서울 리전 단일 계정에 플랫폼 전용 VPC를 세우고, CloudFront 기본 도메인 + WAF 허용 IP로 공개한다. 앱은 장기 키 없이 태스크 역할 → 접근 역할(AssumeRole + ExternalId)로만 관제 대상에 닿으며, 수집 범위는 관제 대상 VPC로 한정한다. 구성은 Boto3 스크립트 `scripts/provision_platform_aws.py`로 한다.**

### 1. 리전 — 관제 대상과 같은 서울

플랫폼과 고객이 모두 국내라 지연·리전 간 전송비·데이터 국외 이전 측면에서 같은 리전이 맞다. 플랫폼이 관제 대상에 섞이는 문제(Context 1)는 리전이 아니라 §6의 수집 범위로 푼다 — 리전을 가르면 운영(SaaS) 구성과 달라진다.

### 2. 네트워크

| 자리 | 구성 |
| --- | --- |
| 플랫폼 VPC | `10.50.0.0/16` — 기본 VPC·스모크 VPC(`10.42.0.0/16`)와 겹치지 않음 |
| 공개 서브넷 a·c | ALB · NAT |
| 프라이빗 서브넷 a·c | ECS 태스크 · RDS — 공인 IP 없음 |
| NAT | 1개(a). OpenAI·AWS API로 나가는 길 |

### 3. 권한 — 역할 넷, 장기 키 0개

| 역할 | 권한 | 후속 SaaS 설계 후보 |
| --- | --- | --- |
| `vigilantis-platform-exec` | 이미지 받기 · 플랫폼 비밀 3개 읽기 | 그대로 쓰는 안 |
| `vigilantis-platform`(api 태스크 역할) | **`vigilantis-access`에 대한 `sts:AssumeRole` 하나뿐** | 고객별 역할 ARN을 대상으로 두는 안. 원격 회수 방식은 후속 SaaS 설계에서 정한다 |
| `vigilantis-access` | 스모크 스크립트의 Action Whitelist 거울 정책(`vigilantis-smoke-app`)을 **빌려 붙인다** | 고객 계정에 고객이 CloudFormation으로 만드는 역할 안 |
| 배포용 | 관리자 프로필(MVP) | GitHub OIDC 역할 안 |

- 신뢰 정책은 `vigilantis-platform`만, `sts:ExternalId`가 맞을 때만 받는다. 앱이 요청하는 세션 길이의 기본값은 900초다(`apps/core-api/serve.py`의 `VIGILANTIS_ACCESS_SESSION_SECONDS` 기본값). 원격 회수의 방식과 노출 상한은 후속 SaaS 설계에서 정한다.
- 앱 코드는 바뀌지 않는다. 운영 진입점 `apps/core-api/serve.py`가 기동 때 AWS 설정 파일에 역할 전환 프로필(`credential_source = EcsContainer` · `role_arn` · `external_id`)을 쓰고 기본 프로필로 잡는다. 이후 Boto3가 전환과 갱신을 맡는다.
- 접근 역할 정책을 따로 두지 않는 이유: 정책이 두 벌이면 `tests/test_provision_smoke_aws.py`의 두 방향 보증(코드가 부르는 작업 = 정책)이 한쪽에만 걸린다.

### 4. 공개 — CloudFront 기본 도메인 + WAF

- 주소는 CloudFront 기본 도메인(`d….cloudfront.net`)이다. HTTPS 인증서를 AWS가 준다. 도메인은 학원 확인 뒤 12/11(금) 최종 발표 전에 붙인다 — 배포를 다시 만들 필요 없이 대체 도메인과 ACM 인증서만 더한다.
- **WAF(CLOUDFRONT 범위) 기본 차단 + 허용 IP만 통과.** 로그인이 없는 MVP라 이것이 출입문이다. 허용 목록에는 PM이 준 IP와 **플랫폼 NAT의 공인 IP**가 들어간다 — 웹의 서버 측 API 호출이 CloudFront를 거치기 때문이다(Context 3). 허용 IP는 저장소에 적지 않고 `--allow-cidr`로 받는다. 스크립트는 IPv4 CIDR만 받고 **/24보다 넓은 범위는 거부한다** — WAF 자체는 `/0`만 거절하므로 `0.0.0.0/1` 같은 광역이 출입문을 무력화하지 않게 한다.
- **ALB는 공개지만 CloudFront만 받는다.** 두 겹이다 — ① ALB 보안그룹은 CloudFront 관리형 prefix list(`com.amazonaws.global.cloudfront.origin-facing`)만 허용 ② 리스너 규칙은 CloudFront가 붙이는 비밀 헤더(`X-Vigilantis-Origin`)가 있어야 앱으로 보내고, 없으면 기본 403.
- CloudFront → ALB는 **평문 HTTP**다. ALB에 붙일 인증서는 도메인이 있어야 받는다. 브라우저 → CloudFront는 HTTPS지만, 오리진이 internet-facing ALB의 공인 주소이고 요청을 넘기는 엣지가 서울 밖일 수도 있어 **이 구간과 비밀 헤더 값은 인터넷 구간을 평문으로 지날 수 있다.** 헤더 값이 새면 남의 CloudFront 배포를 거쳐 WAF를 우회할 수 있다(ALB 보안그룹은 **모든** CloudFront 배포의 prefix list를 허용한다). 도메인·인증서 확보 전까지의 대가로 받으며, 도메인을 붙일 때 HTTPS로 바꾼다.
- 캐시는 끈다(관리형 `CachingDisabled`). 헤더·쿼리는 Host만 빼고 넘긴다(`AllViewerExceptHostHeader`) — WebSocket(`/api/v1/ws`) 업그레이드가 그대로 간다.

### 5. 실행 — ECS Fargate · RDS

| 자리 | 구성 |
| --- | --- |
| api | Fargate 0.5 vCPU / 1 GiB · **태스크 1개 고정 · 배포 시 min 0 / max 100**(겹쳐 뜨지 않게 — Context 4). 대가는 배포 중 수십 초 중단 |
| web | Fargate 0.25 vCPU / 0.5 GiB · Next.js `output: "standalone"` 이미지 · 기본 롤링 |
| DB | RDS PostgreSQL 16 · `db.t4g.micro` · 단일 AZ · 암호화 · 백업 1일 · 비공개 |
| 마이그레이션 | api 이미지로 `alembic upgrade head` 일회성 태스크를 배포 때마다 먼저 돌린다(compose `migrate`와 같은 자리) |
| 비밀 | Secrets Manager — DB 접속 문자열 · OpenAI 키 · ExternalId · CloudFront 비밀 헤더 값. OpenAI 키는 **PM이 직접 넣는다**(스크립트는 자리만 만든다). DB 비밀번호는 **RDS 생성 요청 전에** 접속 문자열 비밀(호스트 자리표시)로 먼저 저장하고, 인스턴스가 뜬 뒤 호스트를 채운다 — 대기 중에 끊겨도 비밀번호를 잃지 않는다. RDS 관리 비밀(`ManageMasterUserPassword`)은 기본 7일마다 회전돼 접속 문자열 사본이 낡으므로 쓰지 않는다 |
| 로그 | stdout → CloudWatch Logs(30일). 파일 로그는 끈다 — 컨테이너 파일은 태스크와 함께 사라진다 |
| AI 분석 · 조치 실행 | **기본 끔**(`DISPATCH_ENABLED=false`). 이 값 하나가 AI 디스패처와 **조치 실행 디스패처를 함께** 켜고 끈다(`apps/core-api/config.py`) — 끈 채로는 화면에서 조치를 승인해도 실행되지 않고 접수 상태로 멈춘다. 켜면 인시던트마다 OpenAI가 과금되므로 `deploy --enable-dispatch`로 명시한다. 조치 실행을 실측하려면 켜야 하며, 과금 경로와 떼어 켤 수 없다(스위치 분리는 앱 코드 변경이라 이 결정 밖) |

### 6. 수집 범위 — 계정 경계를 흉내 낸다

- 수집기에 `COLLECT_VPC_IDS`(관제할 VPC 목록, 포함 목록)를 둔다. 배포는 스모크 VPC를 넣는다 — 플랫폼 VPC의 자원은 수집·판정되지 않는다.
- 빈 값이면 지금처럼 리전 전체다(로컬·LocalStack 동작 불변).
- **단일 계정에서 계정 경계를 대신하는 장치다.** 관제 범위가 고객 계정과 일치하는 SaaS 구성이라면 없앨 후보이지만, 존치 여부는 후속 SaaS의 테넌트·VPC 설계에서 정한다.
- IAM 쪽 울타리(접근 역할의 조치 권한이 스모크 태그·스모크 VPC로 묶임)는 그대로 둔다 — 필터에 빈틈이 생겨도 앱이 플랫폼 자원을 바꿀 수 없다.

### 7. 구성 도구 · 절차

- **Boto3 스크립트다**(ADR-0006 §2 — IaC는 Post-MVP). 방식은 스모크 스크립트와 같다 — 태그(`vigilantis:platform=true`) 식별 · 멱등 · `--yes` 없으면 미리 보기 · 실 AWS 전용 가드 · `--account` 대조.
- 순서: `up --allow-cidr <IP>/32 --yes` → OpenAI 키 넣기 → `build --tag <태그>`(ECR 푸시 — 웹은 CloudFront 주소를 박아 빌드) → `deploy --tag <태그> --yes`(마이그레이션 → 서비스) → 판정 기준 실측.
- `up`은 무엇이든 만들기 전에(미리 보기에서도) 스모크 앱 정책과 스모크 VPC가 있는지 본다 — 없으면 NAT·RDS를 세우기 전에 멈춘다.
- `down`은 플랫폼 태그 자원과 플랫폼 VPC 안만 지운다. 스모크 환경과 그 앱 정책은 건드리지 않는다. 이름이 같아도 플랫폼 태그(`vigilantis:platform=true`)가 없는 IAM 역할은 `up`이 덮어쓰지 않고 `down`이 지우지 않는다.
- **정리 순서는 플랫폼 `down` → 스모크 `down`이다.** 접근 역할이 스모크 앱 정책을 붙잡고 있는 동안 스모크 `down`은 정책을 지울 수 없어(IAM `DeleteConflict`) 무엇이든 지우기 전에 멈춘다. 반대로 스모크 VPC를 다시 세우면 앱 정책의 VPC ARN은 따라가지만 플랫폼의 `COLLECT_VPC_IDS`는 다음 `deploy` 전까지 옛 VPC를 가리킨다 — 스모크를 다시 세웠으면 `deploy`를 다시 돌린다.

## Consequences (결과·트레이드오프)

**장점**

- 역할 전환·장기 키 없음·공개 경로를 Post-MVP SaaS에서 다시 쓸 방향으로 세운다. SaaS 전환에서 바뀔 범위는 후속 SaaS의 테넌트·인증·원격 회수·VPC 설계에서 정한다.
- 플랫폼은 관제 대상 자원을 직접 바꿀 권한이 없다 — 태스크 역할이 가진 것은 역할 전환 하나다.
- 공개 진입점이 CloudFront 하나이고, ALB 우회를 두 겹으로 막는다.

**비용/유의**

- **비용**: 월 약 $130(추정 — NAT 약 $43 · Fargate 2개 약 $40 · RDS 약 $22 · ALB 약 $16 · WAF 약 $6 · 그 밖 수 달러). 스모크 환경(월 약 $80)과 합쳐 월 약 $210으로 ADR-0009의 Budget 알림선(월 $300, 계정 전체) 아래다.
- **NAT 1개** — AZ a 장애 시 c의 태스크도 밖으로 못 나간다. 비용을 우선했다.
- **배포 중 api 중단**(min 0) — 디스패처 선점(lease)이 생기면(#435 4-2) 기본 롤링으로 바꾼다.
- **웹의 서버 측 API 호출이 CloudFront를 한 바퀴 돈다**(Context 3). NAT IP를 허용 목록에 넣어 통과시키는 대가다. 서버 측은 내부 주소로 부르게 바꾸는 것은 FE 변경이라 Post-MVP다.
- **CloudFront 주소가 바뀌면 웹 이미지를 다시 빌드한다**(주소가 번들에 박힌다). 도메인을 붙인 뒤에는 바뀌지 않는다.
- **로그인 없음** — 허용 IP 안의 누구나 조치를 승인할 수 있다. 로그인 도입 전까지 허용 목록을 넓히지 않는다.
- 수집 범위 필터(§6)가 머지되기 전 이미지로 배포하면 플랫폼 VPC가 관제 대상에 섞인다 — 필터 PR을 먼저 머지한다.

## Related

- 스모크 환경과 앱 정책: [ADR-0009](0009-real-aws-smoke-environment.md) §2
- Post-MVP SaaS 전환·IaC: #438 ①②④⑨
- 구현: `scripts/provision_platform_aws.py` · `tests/test_provision_platform_aws.py` · `scripts/provision_smoke_aws.py`(`down`의 정리 순서 가드) · `apps/core-api/serve.py` · `apps/core-api/tests/test_serve.py` · `apps/core-api/Dockerfile` · `apps/web/Dockerfile` · `apps/web/next.config.ts`

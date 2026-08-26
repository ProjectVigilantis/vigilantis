# ADR-0007: 가드레일 4단계 AWS Dry-Run은 executor의 단일 `precheck()` 호출로 판정한다

- **Status**: Accepted (2026-08-24 — 안성일(AI/Guardrail) 확인 완료, PR #117 승인)
- **Date**: 2026-08-24
- **Amended**: 2026-08-25(1차 — precheck 구현 실측 반영) · 2026-08-26(2차 — ④ 사유 코드 정의 위치 현행화). 하단 "개정 이력" 참조, **핵심 결정 불변**
- **Deciders**: 김세혁(PM/Infra, executor 소유자) 결정, 안성일(AI/Guardrail) 합의 완료
- **Refs**: #113

## Context (배경)

가드레일 4단계 `SCHEMA_CHECK → ACTION_WHITELIST → ARN_MATCH → AWS_DRY_RUN` 중 **마지막 단계만 실제 AWS를 호출**하고, 그 호출은 `apps/core-api/services/aws/executor.py`(김세혁)가 담당한다. 앞 세 단계는 `ai/guardrails.py`(안성일) 안에서 끝난다. 즉 4단계는 두 소유자의 경계이며, 호출 규약이 없으면 3주차 이후 양쪽이 서로를 기다린다(#113, 3주차 종료 판정 기준 ⓐ).

받을 값의 자리는 이미 계약에 있다 — `packages/schemas/guardrails.py`의 `GuardrailStepResult`가 `result`·`reason_code`·`verification_summary` 셋을 갖는다. `GuardrailDecision`이 `PASS`/`FAIL` 2값이므로 **"확인 불가"라는 제3의 판정은 담을 자리가 없다.** 조회로도 확인하지 못하는 부분이 있으면 FAIL이거나, 확인 범위를 요약에 남기고 PASS다.

문제는 `DryRun`이 모든 작업에서 쓸 수 있는 수단이 아니라는 점이다. 확정 10종(ADR-0002 본편 7 + ADR-0004 롤백 3)이 사용하는 AWS 작업 전수를 LocalStack 4.14.0에서 실측했다.

### 실측 — 확정 10종 `target_api` 전수 (LocalStack 4.14.0)

| AWS 작업 | 사용 런북 | 예외 | 자원 변경 | 판정 |
| --- | --- | --- | --- | --- |
| `ec2.modify_instance_attribute` | RIGHTSIZING · REVERT_SIZE | `DryRunOperation` | 없음 | DryRun |
| `ec2.modify_network_interface_attribute` | ISOLATE · UNISOLATE | `DryRunOperation` | 없음 | DryRun |
| `ec2.create_security_group` | SG_RECREATE | `DryRunOperation` | 없음 | DryRun |
| `ec2.delete_security_group` | SG_DELETE_ISOLATED | `DryRunOperation` | 없음 | DryRun |
| `ec2.authorize_security_group_ingress` | SG_RECREATE | `DryRunOperation` | 없음 | DryRun |
| `ec2.authorize_security_group_egress` | SG_RECREATE | `DryRunOperation` | 없음 | DryRun |
| `ec2.create_launch_template` | ENABLE_AUTOSCALING | `DryRunOperation` | 없음 | DryRun |
| `ec2.create_snapshot` | EBS_DELETE_UNATTACHED | `DryRunOperation` | 없음 | DryRun |
| `ec2.delete_volume` | EBS_DELETE_UNATTACHED | `DryRunOperation` | 없음 | DryRun |
| **`ec2.create_network_acl_entry`** | **NACL_ADD_DENY** | **없음** | **규칙 생성됨** | **조회 대체** |
| **`ec2.delete_network_acl_entry`** | **NACL_RESTORE** | **없음** | **규칙 삭제됨** | **조회 대체** |
| **`elbv2.deregister_targets`** | **ISOLATE** | `ParamValidationError` | 없음 | **조회 대체** |
| **`elbv2.register_targets`** | **UNISOLATE** | `ParamValidationError` | 없음 | **조회 대체** |
| **`autoscaling.create_auto_scaling_group`** | **ENABLE_AUTOSCALING** | `ParamValidationError` | 없음 | **조회 대체** |

두 가지 서로 다른 원인이 섞여 있다.

- **NACL 2종**: AWS는 `DryRun` 성공도 `DryRunOperation` **예외**로 돌려준다. 예외가 나지 않았다는 것은 플래그가 적용되지 않았다는 뜻이고, 실제로 규칙이 생성·삭제됐다. LocalStack 구현 결함이며 실 AWS는 정상 지원한다.
- **`elbv2`·`autoscaling` 3종**: `ParamValidationError`는 botocore 클라이언트 단에서 발생한다. 네트워크 호출 전에 나므로 LocalStack 특성이 아니라 **실 AWS API에 `DryRun` 파라미터 자체가 없다**는 뜻이다. 환경과 무관하게 확정이다.

NACL 2종은 "LocalStack일 때만 조회로 확인"으로 나눌 수 있어 보이지만, [ADR-0006](0006-localstack-team-standard-env.md) §3(전환 스위치 규약 — 코드 분기 금지)이 이를 금지한다. 나누면 게이트까지 LocalStack에서는 조회 경로만 돌고 `DryRun` 경로는 실 AWS에서 처음 실행된다 — 그 조항이 막으려는 상황 그대로다. `RUNBOOK_NACL_ADD_DENY`·`NACL_RESTORE`는 9/13 게이트 P0 4종이므로 여기서 틀리면 SecOps 시연 경로가 통째로 막힌다.

### 추가로 확인된 제약 — `elbv2`·`autoscaling`은 LocalStack Community에 없다

```
elbv2       -> InternalFailure: the elbv2 service is not included within your LocalStack license
autoscaling -> InternalFailure: the autoscaling service is not included within your LocalStack license
```

`SERVICES` 목록 문제가 아니라 **Pro 전용 서비스**다. ADR-0006 §1이 Community 전용을 못 박았으므로 로컬에서 해소할 수 없다. 따라서 `ISOLATE`·`UNISOLATE`·`ENABLE_AUTOSCALING`은 실행뿐 아니라 **`DryRun` 대체용 describe 조회조차 로컬에서 돌지 않는다.** ADR-0006 §4 검증 한계 표 4행("구현 시점에 확인, 미동작 시 이 목록에 확정 편입")의 편입 조건이 충족됐다.

## Decision (결정)

**가드레일 4단계는 executor가 노출하는 동기 함수 `precheck()` 한 번으로 판정한다. `DryRun`을 쓸 수 없는 작업은 환경과 무관하게 조회(describe)로 대체 검증하며, 무엇을 확인하고 무엇을 확인하지 못했는지는 `verification_summary`에 남긴다.**

### 1. 호출 규약

```python
# packages/schemas/guardrails.py — 가드레일 네 단계 거절 사유 코드의 단일 원천
@unique
class PrecheckReasonCode(str, Enum):        # ④ AWS Dry-Run 단계의 어휘
    PRECHECK_UNAUTHORIZED = "PRECHECK_UNAUTHORIZED"
    PRECHECK_TARGET_NOT_FOUND = "PRECHECK_TARGET_NOT_FOUND"
    PRECHECK_INVALID_STATE = "PRECHECK_INVALID_STATE"
    PRECHECK_NOT_IMPLEMENTED = "PRECHECK_NOT_IMPLEMENTED"
    PRECHECK_PARAM_INVALID = "PRECHECK_PARAM_INVALID"
    PRECHECK_AWS_ERROR = "PRECHECK_AWS_ERROR"


# packages/schemas/precheck.py — executor 호출 계약. 위 Enum을 재노출한다
from .guardrails import PrecheckReasonCode


class PrecheckOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    reason_code: Optional[PrecheckReasonCode] = None   # FAIL일 때만
    verification_summary: str = Field(min_length=1)    # PASS·FAIL 모두 필수


# apps/core-api/services/aws/executor.py
class BackupRecordLoader(Protocol):
    def get(self, backup_record_id: str) -> Optional[BackupRecordView]: ...

    def latest_for_target(
        self,
        target_arn: str,
        backup_type: str,
        payload_match: Optional[Mapping[str, Any]] = None,
    ) -> Optional[BackupRecordView]: ...


def precheck(
    runbook_id: RunbookId,
    target_arn: str,
    parameters: Mapping[str, Any],
    *,
    backup_loader: Optional[BackupRecordLoader] = None,
) -> PrecheckOutcome: ...
```

| 축 | 결정 | 근거 |
| --- | --- | --- |
| 호출 시점 | AI 제안 생성 **직후 1회** | 후보가 `EXECUTABLE`이 되려면 4단계를 통과해야 한다(`packages/schemas/candidates.py` 상태 전이). 승인·실행 시점의 재검증은 가드레일 4단계 밖의 별도 사안이다 |
| 동기/비동기 | **동기** | boto3가 동기이고 collector의 기존 클라이언트 규약과 같다. async 문맥에서는 호출부가 threadpool로 감싼다 |
| 예외 | **던지지 않는다** (예외 1건: 아래) | AWS 오류·미구현·파라미터 문제를 모두 `PrecheckOutcome`으로 반환한다. 가드레일 쪽에 `try/except`를 요구하면 사유 코드 분류가 두 곳으로 갈라진다 |
| 백업 레코드 조회 | 키워드 전용 `backup_loader` **주입** | 롤백 계열 4종(`NACL_RESTORE`·`UNISOLATE`·`SG_RECREATE`·`REVERT_SIZE`)의 통과 조건이 백업 레코드의 **내용**을 필요로 한다. executor는 DB 트랜잭션을 소유하지 않으므로 조회를 주입받고, 그래야 `precheck()`가 DB 없이 단위 테스트된다 |
| 미구현 런북 | executor가 `PRECHECK_NOT_IMPLEMENTED` 반환 | 디스패치 테이블이 executor에 있으므로 판정 소유권도 같은 쪽에 둔다. 호출 전 필터가 필요하면 `IMPLEMENTED_RUNBOOK_IDS: frozenset[str]`를 함께 export한다 |

**④의 사유 코드는 `packages/schemas/guardrails.py`가 정의하고 `precheck.py`가 재노출한다.** 본 ADR 채택 시점에는 `precheck.py`가 유일한 정의처였으나, 그 뒤 ①②③의 어휘가 생기면서 네 단계 목록이 한 파일로 통합됐다(#125). 값 문자열 6종·`PRECHECK_` 접두·`from schemas.precheck import PrecheckReasonCode` import 경로는 그대로이므로 **executor 계약에 바뀐 것은 없다.**

`GuardrailStepResult` 매핑은 1:1이다 — `passed` → `result`, `reason_code` → `reason_code`, `verification_summary` → `verification_summary`. 다만 `reason_code`의 타입이 네 단계 Enum의 union으로 좁혀져 **단계와 맞지 않는 코드는 계약이 거절한다** — ④ 결과에 다른 단계의 코드를 담으면 `ValidationError`다(#125).

**"예외를 던지지 않는다"의 유일한 예외는 `backup_loader` 미배선이다.** 이 규칙은 `precheck()`가 **받은 페이로드에 대해** 내리는 판정의 규칙이다. 백업 조회가 필요한 런북인데 loader가 배선되지 않은 것은 페이로드 문제가 아니라 **호출부의 배선 오류**이고, `RuntimeError`로 즉시 드러내야 한다. FAIL로 남기면 멀쩡한 원복 요청에 거절 기록이 붙어 관제 화면에 남는다. `ai/guardrails.py`가 미배선 검증 문맥을 `NotImplementedError`로 막는 것과 같은 구분이다.

### 2. 판정 규약 — `DryRunOperation` 예외가 났을 때만 PASS

`DryRun` 경로는 **`DryRunOperation` 예외 발생만 PASS로 인정한다.** 예외 없이 정상 반환하면 플래그가 적용되지 않은 것이므로 `PRECHECK_AWS_ERROR`로 FAIL 처리한다.

이 규약이 없으면 이번 NACL 사례처럼 "확인 단계가 조용히 실제 실행을 수행하고 통과로 기록되는" 상황을 탐지할 수 없다. 에뮬레이터 결함·SDK 변경·신규 런북 추가 어디서 발생하든 같은 그물에 걸린다.

AWS 오류 → 사유 코드 매핑:

| AWS 응답 | reason_code |
| --- | --- |
| `DryRunOperation` | (PASS) |
| `UnauthorizedOperation` · `AccessDenied*` | `PRECHECK_UNAUTHORIZED` |
| `*.NotFound` · `InvalidTarget` | `PRECHECK_TARGET_NOT_FOUND` |
| `IncorrectInstanceState` · `DependencyViolation` · `*InUse*` | `PRECHECK_INVALID_STATE` |
| 그 밖의 `ClientError` · **예외 미발생** | `PRECHECK_AWS_ERROR` |

`PRECHECK_PARAM_INVALID`는 #113 제안 5종에 executor가 추가한 코드다. #154(런북별 typed 파라미터 계약)가 아직 서지 않아 파라미터 키 누락·형식 위반이 1단계 Schema Check에서 걸리지 않고 4단계에서 처음 드러난다. 이를 `PRECHECK_AWS_ERROR`에 섞으면 거절 기록에서 "우리 쪽 계약 문제"와 "AWS 문제"가 구분되지 않는다. **#154 확정 시 이 코드는 자연히 쓰이지 않게 된다.**

### 3. `verification_summary` 형식

사람이 읽는 필드지만 형식을 고정한다 — 거절 근거를 FE가 그대로 노출할 수 있어야 한다.

```
<방식> | 확인: <...> | 미확인: <...>
방식 ∈ DRY_RUN · DESCRIBE · MIXED
```

예: `DRY_RUN(ec2.modify_instance_attribute) | 확인: 호출 권한과 파라미터 형식(DryRun) | 미확인: 대상 자원 존재와 현재 상태(DryRun 비검증)`

`미확인:` 항목은 비워 두지 않는다. 확인 범위의 한계를 남기는 것이 이 필드의 존재 이유이며, 조회 대체 경로는 **항상 IAM 권한을 검증하지 못한다.**

**`DryRun` 통과는 대상 자원의 존재를 증명하지 않는다.** LocalStack 4.14.0 실측:

```
delete_security_group(GroupId='sg-176c22ab493bcb450')   # 존재 -> DryRunOperation
delete_security_group(GroupId='sg-00000000000000000')   # 부재 -> DryRunOperation
authorize_security_group_ingress(GroupId='sg-000...0')  # 부재 -> DryRunOperation
```

`DryRun`이 보는 것은 **호출 권한과 파라미터 형식**이고 자원 조회는 그 뒤 단계다. 따라서 DryRun 전면 6종의 요약에 `대상 자원 유효`를 쓰면 **확인하지 않은 것을 확인했다고 기록**하게 된다 — 거절 근거를 FE가 그대로 노출하는 필드라 그 차이가 관제자에게 그대로 간다.

이 사실을 근거로 **DryRun 전면 6종에 존재 확인 describe를 덧붙이지 않는다.** 제안 생성 시점의 존재 확인은 실행 시점의 존재를 보장하지 못하므로(상태 변화는 본 ADR 범위 밖), 호출 1회를 더 쓰고도 같은 한계가 남는다. 대신 그 한계를 `미확인:`에 남기고, 실제 존재 확인은 실행 직전 단계의 몫으로 둔다.

### 4. 대체 검증 대상 5종과 확인 방식

`DryRun`을 쓰지 않고 조회로 확인하는 런북은 **5종**이며, 환경과 무관하게 동일하게 적용한다.

| 런북 | 범위 | 방식 | 조회 | 통과 조건 |
| --- | --- | --- | --- | --- |
| `RUNBOOK_NACL_ADD_DENY` | 전면 | DESCRIBE | `describe_network_acls` | ① ACL 존재 ② `rule_number`가 **인바운드**(`egress=False`) Entries에 없음 ③ `cidr_block` 파싱 가능·`protocol` enum 일치 |
| `RUNBOOK_NACL_RESTORE` | 전면 | DESCRIBE | `describe_network_acls` | ① ACL 존재 ② `(rule_number, egress)` 항목이 있음 ③ 그 항목이 `RuleAction=deny`이고 백업 레코드의 rule index와 일치 |
| `RUNBOOK_EC2_ISOLATE` | 부분(elbv2만) | MIXED | ENI `DryRun` + `describe_target_health` · `describe_security_groups` | ① `modify_network_interface_attribute` DryRun 통과 ② `isolation_group_id` SG 존재 ③ TG 존재·대상이 등록돼 있음(`Target.NotRegistered` 설명은 미등록으로 본다) |
| `RUNBOOK_EC2_UNISOLATE` | 부분(elbv2만) | MIXED | ENI `DryRun` + `describe_target_groups` · `describe_security_groups` | ① DryRun 통과 ② 백업 레코드의 복원 대상 SG가 전부 현존 ③ TG 존재·대상이 같은 VPC |
| `RUNBOOK_EC2_ENABLE_AUTOSCALING` | 부분(asg만) | MIXED | LT `DryRun` + `describe_instances` · `describe_auto_scaling_groups` | ① `create_launch_template` DryRun 통과 ② 원본 EC2 존재·`running` ③ 동명 ASG 부재 ④ `min_size <= max_size <= 4` |

②③ 실패는 각각 `PRECHECK_TARGET_NOT_FOUND` / `PRECHECK_INVALID_STATE`, 파라미터 형식 위반은 `PRECHECK_PARAM_INVALID`다.

표의 조회 두 곳은 구현(#129) 과정의 실측으로 정정한 것이다.

- **`UNISOLATE`의 조회는 `describe_target_groups`다.** 통과 조건 ③이 요구하는 것은   "대상이 같은 VPC"인데 `describe_target_health`의 응답(`TargetHealthDescriptions`)에는   **`VpcId`가 없다.** Target Group의 VPC를 알 수 있는 조회는 `describe_target_groups`뿐이다.
- **`ISOLATE`는 등록 여부를 `Target.NotRegistered`로 판별한다.** `Targets`를 명시해   `describe_target_health`를 부르면 **등록되지 않은 대상에도** `unused` /   `Target.NotRegistered` 설명이 채워져 돌아온다. 응답 목록이 비었는지만 보면 수집 이후   이미 이탈한 대상이 "등록됨"으로 통과한다.

**항상 거절되는 런북은 없다** — 5종 모두 통과 경로가 존재한다. 다만 §Context의 Community 제약 때문에 `ISOLATE`·`UNISOLATE`·`ENABLE_AUTOSCALING`의 elbv2·asg 조회는 로컬에서 실행되지 않으며, **이 3행의 통과 조건은 6–7주차 실 AWS 스모크에서 확정한다**(현재는 잠정안).

`ISOLATE`·`UNISOLATE`가 부분 대체라는 사실 자체는 새로운 결정이 아니다 — 런북 명세서가 이미 `dry_run_supported: partial` + "`describe_target_health` 사전 조회로 대체 검증"으로 규정하고 있고, 본 ADR은 그것을 실측으로 확인해 executor 규약에 편입한 것이다.

### 5. 파라미터 계약

`precheck()`의 `parameters`는 런북 명세서의 `parameters_schema`를 따른다. 전부 required이며, typed 계약은 #154 확정 시 이 표를 원천으로 생성한다.

| runbook_id | 키 |
| --- | --- |
| `RUNBOOK_EC2_ISOLATE` | `instance_id` · `target_group_arn` · `isolation_group_id` · `evidence_id` |
| `RUNBOOK_NACL_ADD_DENY` | `network_acl_id` · `rule_number` · `cidr_block` · `protocol` · `evidence_id` |
| `RUNBOOK_NACL_RESTORE` | `network_acl_id` · `rule_number` · `egress` · `evidence_id` |
| `RUNBOOK_SG_DELETE_ISOLATED` | `group_id` · `evidence_id` |
| `RUNBOOK_EC2_RIGHTSIZING` | `instance_id` · `current_instance_type` · `target_instance_type` · `evidence_id` |
| `RUNBOOK_EC2_ENABLE_AUTOSCALING` | `instance_id` · `min_size` · `max_size` · `evidence_id` |
| `RUNBOOK_EBS_DELETE_UNATTACHED` | `volume_id` · `evidence_id` |
| `RUNBOOK_EC2_UNISOLATE` | `instance_id` · `backup_record_id` · `evidence_id` |
| `RUNBOOK_SG_RECREATE` | `backup_record_id` · `evidence_id` |
| `RUNBOOK_EC2_REVERT_SIZE` | `instance_id` · `backup_record_id` · `evidence_id` |

두 가지를 precheck가 강제한다.

1. **롤백 3종은 원복 값을 파라미터로 받지 않는다.** 원본 SG 규칙·인스턴스 타입은 `backup_record_id`로만 로드한다(런북 공통 정책 ③, ADR-0004). 원복 값이 파라미터에 들어오면 `PRECHECK_PARAM_INVALID`로 거절한다.
2. **`parameters`의 리소스 ID가 `target_arn`과 같은 자원을 가리키는지 재확인한다.** 3단계 ARN Match는 `target_arn` 하나를 보지만 파라미터에는 리소스 ID가 여럿 들어온다(예: `ISOLATE`의 `instance_id`·`target_group_arn`·`isolation_group_id`). 불일치는 `PRECHECK_PARAM_INVALID`로 거절한다 — Scope Escalation 2차 방어다.
3. **AWS 클라이언트는 `target_arn`이 가리키는 리전으로 만든다.** 기본 리전으로 고정하면 MVP 범위(단일 계정 / 1–2개 리전)의 두 번째 리전 자산이 **없는 자원으로 판정되거나 같은 ID의 다른 자원을 보게 된다.** 같은 이유로 파라미터로 들어오는 ARN(`target_group_arn` 등)도 `target_arn`과 **같은 리전**이어야 하며, 다르면 `PRECHECK_PARAM_INVALID`로 거절한다 — 다른 리전 자원은 조회 자체가 되지 않으므로 오판정 대신 거절이 맞다.
4. **`NACL_RESTORE`의 백업 레코드는 rule index로 특정한다.** 이 런북만 `backup_record_id`를 파라미터로 받지 않아 대상(`target_arn`)으로 백업을 찾는데, 한 NACL에 차단 조치가 누적되면 "대상의 최신" 하나로는 복원 대상을 고를 수 없다 — 최신 백업이 늘 다른 규칙을 가리켜 **오래된 규칙은 영영 복원되지 않는다.** 조회를 `(rule_number, egress)`로 좁힌다(`latest_for_target(..., payload_match=...)`).

### 6. 런북 추가 시 `DryRun` 적용 여부를 먼저 실측한다

§Context의 표를 뺀 근거가 실측이므로, 목록이 유효하려면 늘어나는 런북도 같은 확인을 거쳐야 한다.

- 실측 스크립트를 `scripts/probe_dryrun.py`로 저장소에 편입하고 회귀 테스트에 연결한다.
- **런북을 추가하거나 `target_api`를 바꾸는 PR은 해당 작업의 실측 결과 첨부를 머지 조건으로 한다.**

### 7. ADR-0006 개정 (§4 검증 한계 표)

본 ADR과 함께 [ADR-0006](0006-localstack-team-standard-env.md) §4를 개정한다.

- **4행 확정 편입**: "ALB Target Group·ASG 경로 — Community 커버리지 제한 **가능**"은 실측으로 확인됐다. `elbv2`·`autoscaling`은 Community 미포함(Pro 전용)이며, 해당 경로는 실행·조회 모두 로컬에서 불가하다.
- **행 추가**: LocalStack이 `DryRun`을 무시하고 실제 수행하는 작업(`create_network_acl_entry`·`delete_network_acl_entry`) — 실 AWS는 정상 지원하므로 실 AWS 스모크에서 `DryRun` 경로가 처음 검증된다.

## Consequences (결과·트레이드오프)

**장점**

- executor ↔ 가드레일 경계가 함수 하나로 좁혀져 양쪽이 서로를 기다리지 않고 4주차 병렬 개발이 가능하다(3주차 종료 판정 ⓐ).
- 거절 사유가 코드로 분류되고 확인 한계가 문자열로 남아, 관제자가 "왜 실행되지 않았는가"를 대시보드에서 설명할 수 있다.
- `DryRunOperation` 예외 강제 규약이 에뮬레이터 결함·SDK 변경을 상시 탐지한다. 이번 NACL 결함도 같은 방식으로 발견됐다.
- P0 4종(`RIGHTSIZING`+`REVERT_SIZE`, `NACL_ADD_DENY`+`NACL_RESTORE`)은 전부 로컬에서 통과 경로가 있어 9/13 게이트에 영향이 없다.

**비용/유의**

- 조회 대체 경로는 **IAM 권한을 검증하지 못한다.** ADR-0006 §4 1행과 같은 한계이며 실 AWS 스모크가 유일한 방어선이다.
- `ISOLATE`·`UNISOLATE`·`ENABLE_AUTOSCALING`(P2 3종)은 로컬에서 precheck가 항상 `PRECHECK_AWS_ERROR`로 FAIL이다. **이것이 ADR-0006 §3을 지킨 결과의 정상 동작이다.** 다만 그 실패가 드러나는 자리는 런북마다 다르므로, 가드레일·QA의 테스트 픽스처는 후보 경로 하나가 아니라 `GuardrailValidationContext`(`packages/schemas/guardrails.py`) 기준으로 전제를 잡아야 한다.

  | 런북 | 로컬 FAIL이 나타나는 문맥 | 그 문맥에서 관측되는 결과 |
  | --- | --- | --- |
  | `RUNBOOK_EC2_ISOLATE` | `AI_CANDIDATE` · `AUTO_ISOLATION` | 후보가 `EXECUTABLE`이 되지 못한다 / 사람 승인 없이 시작한 격리가 4단계에서 거절된다 |
  | `RUNBOOK_EC2_UNISOLATE` | `ROLLBACK_EXECUTION` | 롤백 3종은 AI 후보가 될 수 없으므로(ADR-0004) 후보 경로가 아니라 원클릭 해제 실행이 거절된다 |
  | `RUNBOOK_EC2_ENABLE_AUTOSCALING` | `AI_CANDIDATE` | 후보가 `EXECUTABLE`이 되지 못한다 |

  `RUNBOOK_EC2_ISOLATE`가 `AUTO_ISOLATION`에서도 쓰이는 근거는 `docs/PROJECT_STATUS.md` §3단계 위험 대응이다 — High `PRE_MITIGATION_0_5S`(0.5초 선차단)와 1분 미응답 `TIMEOUT_ISOLATION_1M`(자동 격리) 둘 다 이 런북을 쓰며, 그 구분은 Execution의 `trigger_source`가 담는다. 즉 로컬에서 "실패가 정상"인 경로는 후보 생성 하나가 아니라 **자동 격리·롤백 실행을 포함한 셋**이다.
- §1 표의 호출 시점("AI 제안 생성 직후 1회")은 `AI_CANDIDATE` 문맥 기준 서술이다. `AUTO_ISOLATION`·`ROLLBACK_EXECUTION`에는 대응하는 후보 생성 시점이 없으므로(검증 요청이 `candidate_id`가 아니라 `execution_id`를 참조한다) 두 문맥의 호출 시점은 본 ADR에서 확정하지 않는다 — 후속 결정 대상이다.
- 같은 이유로 §4 표의 elbv2·asg 통과 조건은 실 AWS에서 처음 실행된다. 실 AWS 시연 인프라 조기 준비(P2 방침)가 이 3종의 유일한 검증 경로다.
- `PRECHECK_PARAM_INVALID`는 #154 이전의 과도기 코드다. typed 파라미터 계약이 확정되면 4단계가 아니라 1단계에서 걸리게 되고, 이 코드는 사용되지 않는다.
- 승인·실행 시점의 재검증은 본 ADR 범위 밖이다. 제안 생성 시점과 실행 시점 사이에 자원 상태가 바뀔 수 있으며, 그 처리는 가드레일 4단계 밖에서 별도로 결정한다.

## Related

- 배경 이슈: #113 — `[BE/DOCS] 가드레일 4단계 AWS Dry-Run — executor 호출 규약 확정`
- 계약 원천: `packages/schemas/guardrails.py` · `packages/schemas/runbooks.py` · `packages/schemas/candidates.py`
- 선행 결정: [ADR-0002](0002-runbook-whitelist-mvp-scope.md)(본편 7종) · [ADR-0004](0004-rollback-runbook-whitelist-registration.md)(롤백 3종·공통 정책) · [ADR-0006](0006-localstack-team-standard-env.md) §3(전환 스위치 규약)·§4(검증 한계)
- 확정 규격: `vigilantis-docs/런북 명세서.md` — `parameters_schema` · `dry_run_supported`
- 현황 기준: `docs/PROJECT_STATUS.md` — 3주차 종료 판정 기준 ⓐ, 구현 우선순위 P0/P1/P2
- 후속: #154(런북별 typed 파라미터 계약 — 후보 `display_parameters`·`evidence_ids` → `precheck(parameters)` 변환 포함) · `AUTO_ISOLATION`·`ROLLBACK_EXECUTION` 문맥의 precheck 호출 시점 · 승인·실행 시점 재검증 정책 · 거절 이후 알림·에스컬레이션
- 영향 범위: `packages/schemas/precheck.py`(신규 — 호출 계약), `packages/schemas/guardrails.py`(④ 사유 코드 정의처 — #125 이후), `apps/core-api/services/aws/executor.py`, `apps/core-api/ai/guardrails.py`, `scripts/probe_dryrun.py`(신규), `docs/adr/0006-localstack-team-standard-env.md`

## 개정 이력

- **2026-08-25 (1차 개정)** — precheck 구현(#129, PR #147)에서 드러난 실측을 반영한다.
  **판정 구조·사유 코드·5종 대체 검증이라는 핵심 결정은 바뀌지 않는다.** 개정 근거는
  #133이며, 다섯 항목 모두 §6("런북을 추가하거나 `target_api`를 바꾸는 PR은 실측 결과
  첨부를 머지 조건으로 한다")이 요구하는 종류의 확인이다.

  | # | 절 | 개정 내용 | 근거 |
  | --- | --- | --- | --- |
  | ① | §3 | `DryRun` 통과는 **대상 자원 존재를 증명하지 않는다.** 예시 문자열의 `확인: 파라미터·대상 자원 유효`를 `확인: 호출 권한과 파라미터 형식` / `미확인: 대상 자원 존재와 현재 상태`로 정정. DryRun 전면 6종에 존재 확인 describe를 **덧붙이지 않기로** 확정 | 부재 자원에도 `DryRunOperation`이 반환된다(LocalStack 4.14.0 실측 3건) |
  | ② | §4 | `UNISOLATE`의 조회를 `describe_target_health` → **`describe_target_groups`** 로 정정 | 통과 조건 ③이 요구하는 `VpcId`가 `TargetHealthDescriptions`에 없다 |
  | ③ | §1 | 시그니처에 키워드 전용 **`backup_loader`** 를 명시하고, "예외를 던지지 않는다"의 예외 범위(**배선 오류만**)를 못 박음 | §4·§5의 통과 조건이 백업 레코드 내용을 요구하는데 §1 시그니처에 조회 경로가 없었다 |
  | ④ | §4 · §5 | `ISOLATE`의 등록 판별을 **`Target.NotRegistered`** 기준으로 명시. `NACL_ADD_DENY`의 중복 검사를 **인바운드(`egress=False`)** 기준으로 명시. `NACL_RESTORE`의 백업 조회를 **rule index로 좁히도록** 규약화 | 미등록 대상에도 설명이 채워져 돌아온다 / `ADD_DENY`의 `parameters_schema`에 `egress`가 없다 / 한 NACL에 조치가 누적되면 옛 규칙이 복원 불가 |
  | ⑤ | §5 | **리전 규약 신설** — 클라이언트는 `target_arn`의 리전으로 만들고, ARN 파라미터도 같은 리전이어야 한다 | 기본 리전 고정 시 2번째 리전 자산이 오판정된다(MVP 범위가 1–2개 리전) |

  함께 정리한 것: 후속 이슈 참조를 **#49 → #154**로 교체한다. #49(`[SCHEMA/FEAT] 내부 공통
  계약 — Incident·위협·AI 계열`)는 2026-08-18 종료됐고, `packages/schemas/agents.py:16-17`이
  런북별 typed parameters를 **그 이슈의 범위 밖으로 명시**하고 있어 후속 근거가 될 수 없다.

- **2026-08-26 (2차 개정)** — ④ 사유 코드의 **정의 위치**를 현행화한다. 판정 구조·코드
  6종·값 문자열·호출 규약은 **바뀌지 않는다.** 근거는 #125(PR #164)이며, 문서가 코드
  이동을 따라가지 못하고 있던 것을 맞추는 개정이다.

  | # | 절 | 개정 내용 | 근거 |
  | --- | --- | --- | --- |
  | ① | §1 | `PrecheckReasonCode`의 정의처를 `packages/schemas/precheck.py` → **`packages/schemas/guardrails.py`**(네 단계 공용 목록)로 정정. `precheck.py`는 재노출이며 `from schemas.precheck import PrecheckReasonCode` 경로는 불변 | 본 ADR 채택 시점에는 ④만 Enum이었고 ①②는 앱 안 문자열, ③은 어휘 자체가 없었다. #125가 넷을 한 파일로 모으면서 정의가 옮겨 갔다 |
  | ② | §1 | `GuardrailStepResult.reason_code`가 네 단계 Enum union으로 좁혀져 **단계↔코드 정합을 계약이 강제**한다는 사실을 매핑 서술에 명시 | 같은 PR. ④ 결과에 다른 단계 코드를 담으면 `ValidationError`이므로, 1:1 매핑만 읽고 임의 문자열을 넣을 수 없다 |

# ADR-0008: 백업 레코드는 조치 직전 1회 캡처·불변 보존하고, 원복 재개는 시간이 아니라 상태 대조로 판단한다

- **Status**: Proposed
- **Date**: 2026-09-02
- **Deciders**: 김세혁(PM/Infra — `backup.py`·`rollback.py`·실행 경로 소유자) 결정, 안성일(AI·Architect — `ExecutionStep` 계약 소유자) 검토 대상
- **Refs**: 4주차 P0 카드 `[DOCS] ADR-0008 백업 레코드 수명주기·드리프트·복구 무결성`

## Context (배경)

원복 파라미터의 유일한 원천은 DB 백업 레코드다([ADR-0004](0004-rollback-runbook-whitelist-registration.md) 롤백 공통 정책 ③). 그 정책은 **"어디서 읽는가"만 정했고 "언제 만들고, 언제까지 살고, 다시 쓸 때 무엇과 대조하는가"는 정하지 않았다.** 백업이 이미 원복의 단일 근거이므로, 이 공백은 자동 원복(`RUNBOOK_EC2_REVERT_SIZE` · `AUTO_ON_FAILURE`)이 붙는 5주차에 그대로 실행 규칙의 공백이 된다.

### 지금 서 있는 것 — 코드 실측

| 축 | 현행 | 소재 |
| --- | --- | --- |
| 캡처 | 조치 직전 AWS 조회 1회. 실패는 예외가 아니라 사유 코드 | `services/aws/backup.py::capture_instance_spec` |
| 저장·결속·commit | **AWS 변경 호출 이전에** 커밋까지 끝낸다. 같은 실행에 두 번 불러도 레코드는 하나 | `workflows.py::store_instance_spec_backup` |
| 조회 | `backup_record_id` 직접 조회 또는 대상+`payload_match` 조회. 종류·`target_arn` 3중 대조 | `services/aws/executor.py::_load_backup` |
| 불변 | 생성 후 수정 경로 없음(`payload`는 JSONB, UPDATE 없음) | `db/models.py::BackupRecord` |
| 재실행 | 단계 기록 0건이면 실행, 1건 이상이면 재실행하지 않고 종료 판정 | `dispatcher.py::_dispatch_one` |

### 서 있지 않은 것 — 이 ADR이 메워야 할 공백

1. **소멸 규칙이 없다.** `BackupRecord`를 지우는 코드도, 보존 기간 설정도 없다(`config.Settings` 전수 확인). "지우지 않기로 정한 것"과 "정한 적이 없는 것"은 다르다.
2. **드리프트 대응이 없다.** 백업이 만들어진 시각과 그것이 쓰이는 시각의 간격은 런북마다 다르다 — `REVERT_SIZE`는 분 단위(Status Check 판정 직후)지만 `UNISOLATE`·`SG_RECREATE`·`NACL_RESTORE`는 관제자 판단 대기라 시·일 단위가 된다. 그 사이 제3자가 같은 자산을 바꿨을 때 무엇을 하는지 정한 자리가 없다.
3. **payload 계약이 한쪽에만 있다.** 백업 4종 중 typed 모델이 있는 것은 `SAVE_INSTANCE_SPEC_JSON` 하나이고, 나머지 3종의 키는 **읽는 쪽(executor precheck)의 `dict.get` 문자열**로만 존재한다. 만드는 쪽이 아직 없어서 지금은 어긋날 수 없지만, K5(`NACL_ADD_DENY`)에서 캡처가 붙는 순간 오타 하나가 조회 실패로 나타난다.
4. **원복 실행이 어느 백업을 썼는지 자기 행에 남지 않는다.** `ActionExecution.backup_record_id`는 **백업을 만든 원본 실행**에만 결속되고, 롤백 자식 실행은 `parent_execution_id`만 갖는다(`workflows.reserve_execution`의 `create_execution` 인자에 `backup_record_id`가 없다). "원천이 하나"라는 정책은 어느 레코드에서 왔는지가 기록에 남아야 사후에 검증된다.
5. **재실행 시 성공 단계 skip 규칙이 명문화돼 있지 않다.** 현행 동작(단계 1건이라도 있으면 재실행 금지)은 `dispatcher.py` 주석에만 있고, 결정으로 못 박히지 않아 "3단계 중 2단계가 성공했으니 3단계만 다시"라는 최적화가 언제든 들어올 수 있다.

### 참조 무결성 — 되돌려도 원상이 아닌 것들

백업 payload를 1:1로 복원해도 자산이 조치 이전과 같아지지 않는 지점이 넷 있다. 지금은 어느 문서에도 목록이 없어 "자동 원복했다"는 표현이 실제보다 넓게 읽힌다.

| 런북 | 되돌아오지 않는 것 |
| --- | --- |
| `RUNBOOK_SG_RECREATE` | **신규 `sg-id`가 발급된다.** 원본 `sg-id`를 참조하던 타 리소스의 규칙(다른 SG의 `UserIdGroupPairs`, ENI 연결)은 복원되지 않는다 |
| `RUNBOOK_EC2_REVERT_SIZE` | 타입 변경은 정지를 거치므로, **EIP가 붙어 있지 않으면 퍼블릭 IPv4가 바뀐다.** 원복해도 원래 주소로 돌아오지 않는다. `instance-store` 볼륨 데이터는 정지 시점에 소실된다 |
| `RUNBOOK_EC2_UNISOLATE` | 백업된 SG가 그사이 삭제됐으면 복원 자체가 불가하다(precheck ②가 거절). TG 재등록 시 **등록 포트**를 백업하지 않으면 비기본 포트로 등록돼 있던 대상이 TG 기본 포트로 돌아간다 |
| `RUNBOOK_NACL_RESTORE` | `rule_number`는 재사용되는 슬롯 번호다. 같은 번호에 다른 규칙이 들어와 있으면 지워서는 안 되는데, **현행 precheck는 `(rule_number, egress)`와 `RuleAction=deny`만 대조해 같은 슬롯의 제3자 deny 규칙을 우리 것으로 오인한다**(`executor.py::_precheck_nacl_restore` 실측) → §5가 규칙 fingerprint 대조를 통과 조건으로 정한다 |

## Decision (결정)

**백업 레코드는 조치 직전 1회 캡처하고, AWS 변경 이전에 커밋하며, 생성 후 불변이고, 삭제하지 않는다. 원복을 진행할지는 백업의 나이가 아니라 자산의 현재 상태와의 대조로 판단한다. 재개 단위는 단계가 아니라 실행이다.**

### 1. 수명주기 5단계

| # | 단계 | 규칙 | 소유 |
| --- | --- | --- | --- |
| ① | 캡처 | 조치 직전 AWS 조회 1회. **캡처에 실패하면 조치를 시작하지 않는다** — 원복 값이 없는 변경을 만들지 않기 위해서다 | `services/aws/backup.py` |
| ② | 저장·결속·commit | **모든 AWS 변경 호출보다 앞서 커밋까지 끝난다.** 호출부 트랜잭션에 얹지 않고 스스로 커밋한다 | `workflows.py` |
| ③ | 재사용 | 한 실행에 백업은 하나. 재시도가 새 레코드를 만들면 "조치 직전"이 아니라 "이미 바뀐 뒤"의 값이 원복 값이 된다 | `workflows.py` |
| ④ | 조회 | 종류·`target_arn`·`payload_match` 3중 대조를 통과한 레코드만 원복 입력이다. **백업이 없으면 원복을 시작하지 않는다** — 현물 조회로 값을 추정해 복원하지 않는다 | `executor.py::_load_backup` |
| ⑤ | 소멸 | **없다. MVP 범위에서 백업 레코드를 삭제하지 않는다**(아래 §2) | — |

②의 순서는 실패 모드로 정당화된다. 변경과 백업 기록 사이에서 프로세스가 죽으면 자산은 바뀐 채로 남고 되돌릴 값은 어디에도 없다. 반대 방향의 사고(백업만 남고 변경이 없음)는 쓰이지 않는 레코드 하나가 남을 뿐이다.

### 2. 보존·정리 — 삭제하지 않는다

**MVP에서 `backup_records`에 대한 삭제·만료·아카이빙을 도입하지 않는다.** 보존 기간은 무기한이다.

- 백업 레코드는 복원 입력이자 **감사 기록**이다 — "어떤 값으로 되돌렸는가"의 유일한 근거이며, 원본 실행이 끝난 뒤에도 그 실행이 무엇을 바꿨는지 설명하는 자료다.
- 삭제를 도입하려면 `ActionExecution.backup_record_id` FK 결속을 먼저 끊어야 하는데, 그 결속이 곧 원복 근거다. **결속을 끊는 정리 작업은 도입하지 않는다.**
- 규모가 근거를 뒷받침한다. MVP는 단일 계정 / 1–2개 리전이고 레코드는 **조치 1건당 1행**이다. 저장 비용이 문제가 되는 지점이 시연 범위 안에 없다.
- Post-MVP에서 정리를 도입한다면 조건은 셋이다 — ① 원본 실행이 종료 상태이고 ② 그 실행에 붙을 수 있는 복구 경로가 닫혔으며 ③ 보존 기간이 지났을 것. 셋을 다 만족하기 전의 삭제는 복구 가능한 실행에서 근거를 빼앗는 일이다.

`payload`에는 복원에 필요한 AWS 자원 속성만 담는다. 자격증명·토큰은 담지 않는다. SG 규칙의 CIDR는 **복원에 필요하므로 담는다** — 마스킹 대상은 LLM 전송 경로이고([ADR-0005](0005-langgraph-stateless-domain-graphs.md) 미보존 원칙) 백업은 그 경로에 실리지 않는다.

### 3. 드리프트 — 시간이 아니라 상태로 판단한다

드리프트는 세 종류이며 각각 다른 자리에서 다룬다.

| # | 구간 | 크기 | 처분 |
| --- | --- | --- | --- |
| ⓐ | 캡처 → AWS 변경 | 같은 실행 안, 초 단위 | §1 ②의 선커밋이 닫는 것은 **내구성**이다 — 프로세스가 죽어도 백업이 남는다. 캡처 후 AWS 호출 전의 제3자 변경까지 막지는 않으며, 그 창이 남긴 결과는 ⓑ의 상태 대조에서 드러난다 |
| ⓑ | 변경 → 원복 | 분(자동 원복) – 일(관제자 원복) | **본 절이 정한다** |
| ⓒ | precheck → 실행 | 후보 생성 후 승인 대기 | [ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md)이 범위 밖으로 남긴 사안 — 여기서도 확정하지 않는다 |

**결정 3항.**

1. **백업 나이 상한(TTL)을 두지 않는다.** 경과 시간은 드리프트의 근사일 뿐이다. 상한을 두면 1분 만에 사람이 바꾼 자산은 그대로 통과시키고(거짓 음성), 사흘간 아무도 건드리지 않은 자산의 정당한 원복은 시간 때문에 거절한다(거짓 양성). 원복이 막히는 것은 `UNISOLATE`처럼 **막히면 서비스가 격리된 채로 남는** 런북에서 특히 나쁜 실패다.

2. **`REVERT_SIZE`는 우리가 바꾼 축이 지금도 우리가 바꾼 값일 때만 진행한다.** 발동 직전에 현재 `InstanceType`을 조회해 3분기로 가른다. **`trigger_source`와 무관하게 적용된다** — 자동 발동(`AUTO_ON_FAILURE`)이든 관제자의 수동 요청(`USER_APPROVAL`)이든 같은 대조를 거친다. 제3자 변경을 덮어쓰지 않을 근거는 **발동 주체가 아니라 자산의 현재 상태**에 있기 때문이다.

   **판정은 위에서 아래로, 처음 일치하는 행에서 멈춘다.** 원본 조치가 타입을 실제로 바꾸지 않은 경우(`target_instance_type`이 백업의 `instance_type`과 같음)에는 ①·②가 동시에 참이 되는데, 이때는 **①이 이긴다.**

   | # | 현재 타입 | 해석 | 처분 |
   | --- | --- | --- | --- |
   | ① | 백업의 `instance_type`과 같음 | 변경이 적용되지 않았거나 누군가 이미 되돌렸다 | **AWS 변경 호출을 하지 않는다.** 되돌릴 것이 없음을 단계 기록(`effect=NOT_APPLIED`)으로 남긴다 |
   | ② | 원본 실행의 `target_instance_type`과 같음 | 우리가 바꾼 그대로다 | 원복을 진행한다 |
   | ③ | 둘 다 아님 | 제3자가 그사이 타입을 바꿨다 | **원복을 중단하고 CRITICAL 알림 · 수동 개입.** 자동 재시도는 없다(§6) |

   **①을 우선하고 사전 거절 조건은 두지 않는다.** 두 값이 같다는 것은 되돌릴 것이 없다는 뜻인데, 이를 precheck FAIL로 거절하면 **할 일이 없는 실행이 CRITICAL로 올라가 사람을 부른다** — 아무 일도 일어나지 않았다는 사실은 알림이 아니라 `NOT_APPLIED` 기록으로 남는 편이 옳다. 애초에 같은 타입으로 변경 제안이 서지 않게 막는 것은 **원본 `RIGHTSIZING`의 몫이지 원복의 몫이 아니다.**

   ③이 이 절의 핵심이다. 백업을 무조건 진실로 삼으면 원복이 **제3자의 변경을 조용히 덮어쓴다** — 자율 조치 플랫폼에서 가장 설명하기 어려운 종류의 사고다. 조회 1회로 막을 수 있으면 막는다. 각 분기의 실행 상태 표기는 원복 발동 경로(#241)의 계약이며 본 ADR은 판단 규칙만 정한다.

3. **관제자 복구 경로(`USER_APPROVAL`)는 나이로 거절하지 않는다.** 사람이 이미 그 자산을 보고 판단한 실행이고, precheck가 이미 현물과 대조하기 때문이다. 대상은 **롤백 3종**(`UNISOLATE`·`SG_RECREATE`·`REVERT_SIZE` — SSOT §Action Whitelist의 고정 용어다)과, 롤백 런북이 아니라 **주 조치 경로로 차단을 해제하는** `NACL_RESTORE`다.

   | 런북 | precheck의 현물 대조 |
   | --- | --- |
   | `UNISOLATE` | 복원 대상 SG 전부 현존 |
   | `SG_RECREATE` | 백업의 그룹 정의·규칙 목록 존재 |
   | `REVERT_SIZE` | §3-2의 타입 3분기 — **수동 실행에도 그대로 적용된다** |
   | `NACL_RESTORE` | 대상 규칙이 `deny`이고 **§5의 규칙 fingerprint가 일치** |

   대신 **백업 시각과 경과를 실행 확인 화면에 노출한다** — 판단의 재료를 사람에게 주는 것이 시간 상한을 대신한다(FE 영향은 후속).

### 4. 원복 파라미터 원천 단일화 — 명문화와 보강 1건

ADR-0004 정책 ③을 실행 규칙으로 못 박는다.

- 원복 값(원본 SG 규칙·인스턴스 타입·SG/TG 매핑·NACL rule index)은 **요청 페이로드·AI 출력·화면 입력 어디에서도 오지 않는다.** 파라미터에 원복 값이 실려 오면 `PRECHECK_PARAM_INVALID`로 거절한다.
- **원복 후 "다시 켤 것인가"의 원천도 백업 레코드의 `state`다.** 원본 실행이 정지 응답에서 읽은 `PreviousState`는 그 실행 안에서만 쓴다(`execute_rightsizing`) — 원복은 별개 실행 행이므로 실행 로그가 아니라 백업이 원천이다.
- **보강 — 원복 실행은 실제로 로드한 백업을 자기 행의 `backup_record_id`에 결속한다.** 결속 시점은 레코드를 읽은 직후, 첫 AWS 변경 이전이다. 컬럼은 이미 있고 nullable이며(`db/models.py`), 지금은 롤백 자식에 채워지지 않는다. `NACL_RESTORE`처럼 파라미터가 아니라 rule index로 레코드를 찾는 런북도 같은 규칙을 따른다 — 찾은 뒤에 결속하면 된다. 근거: **원천이 하나라는 정책은 어느 레코드에서 왔는지가 기록에 남을 때만 사후 검증된다.**

### 5. 스냅샷 항목 — 백업 4종 payload 목록

**백업 4종의 payload는 전부 `packages/schemas/backups.py`의 Pydantic 모델을 계약으로 갖는다.** 현재 typed인 것은 1종이며, 나머지 3종은 만드는 쪽이 붙는 시점(K5·P1·P2)에 같은 파일에 모델을 추가한다. 읽는 쪽의 `dict.get` 문자열은 계약이 아니다 — 만드는 쪽과 읽는 쪽이 다른 시점에 살기 때문에, 어긋남은 원복 시점에야 드러나고 그때는 이미 자산이 바뀐 뒤다.

| BackupType | 사용 런북(생성 → 소비) | 필수 항목 | 부가 항목(판단 근거) |
| --- | --- | --- | --- |
| `SAVE_INSTANCE_SPEC_JSON` | RIGHTSIZING → REVERT_SIZE | `instance_id` · `instance_type` · `state` | `image_id` · `architecture` · `root_device_type` · `ebs_optimized` · `availability_zone` · `vpc_id` · `subnet_id` **+ 신설: `public_ip_address` · `elastic_ip_association_id`** |
| `SAVE_CURRENT_SG_AND_TG_MAPPING` | EC2_ISOLATE → UNISOLATE | `security_group_ids[]` · `target_group_arn` **+ 신설: `target_port`** | — |
| `SAVE_SG_FULL_RULES_JSON` | SG_DELETE_ISOLATED → SG_RECREATE | `group_name` · `description` · `vpc_id` · `ingress_permissions[]` · `egress_permissions[]` | **신설: `group_id`**(원본 ID — 신규 발급 ID와 대조해 수동 재연결 대상을 짚는 근거) |
| `RECORD_NACL_RULE_INDEX` | NACL_ADD_DENY → NACL_RESTORE | `rule_number` · `egress` **+ 승격: `cidr_block` · `protocol` · `rule_action`**(규칙 fingerprint) | — |

**신설 3항목의 성격은 서로 다르다.**

- `target_port`는 **원복 값이다.** 없으면 비기본 포트로 등록돼 있던 대상이 TG 기본 포트로 재등록돼 원상 복구가 아니다.
- `public_ip_address`·`elastic_ip_association_id`·`group_id`는 **원복 값이 아니라 한계 고지의 근거다.** 되돌릴 수 없는 것(퍼블릭 IPv4 변경, 신규 `sg-id`)을 관제자에게 사실대로 말하려면 조치 이전 값이 어딘가 남아 있어야 한다. 없으면 조치 후에는 영영 알 수 없다.
- **부가 항목의 부재는 조치를 막지 않는다.** 필수 항목이 없어 되돌리지 못하는 것과 부가 정보가 비어 있는 것은 다른 사건이다.

**`RECORD_NACL_RULE_INDEX`의 fingerprint 3항목은 부가 정보가 아니라 통과 조건이다.**

`rule_number`는 재사용되는 슬롯 번호다. 우리 규칙이 삭제된 뒤 같은 번호에 제3자의 다른 deny 규칙이 들어오면, `(rule_number, egress)`와 `RuleAction=deny`만 보는 현행 대조는 **그 남의 규칙을 우리 것으로 오인해 삭제한다**(`executor.py::_precheck_nacl_restore` 실측). 삭제는 되돌릴 수 없으므로 §3의 상태 대조 원칙이 여기서 성립하려면 대조 축이 rule index보다 넓어야 한다. 그래서 `cidr_block`·`protocol`·`rule_action`을 부가 항목에서 **필수로 승격하고 `NACL_RESTORE` precheck의 통과 조건으로 삼는다.**

| 백업 fingerprint vs 현재 엔트리 | 처분 | 사유 코드 |
| --- | --- | --- |
| 3항목 전부 일치 | 삭제를 진행한다 | — |
| 하나라도 불일치 | 제3자 규칙이다 — **삭제하지 않는다** | `PRECHECK_INVALID_STATE` |
| 백업 payload에 항목이 없다 | 판정 불가 — **삭제하지 않는다** | `PRECHECK_PARAM_INVALID` |

**fingerprint 값은 백업 payload에서만 온다.** `NaclRestoreParameters`에 싣지 않는다 — 실으면 §4(원복 값은 요청 페이로드에서 오지 않는다)를 스스로 깬다. `RECORD_NACL_RULE_INDEX`를 만드는 코드가 아직 없어(캡처는 K5에서 붙는다) 이 승격에 깨질 기존 레코드는 없다.

### 6. 롤백 단계별 실패 추적 — `(execution_id, sequence)`

- **롤백은 자기 `ActionExecution` 행을 갖고(`parent_execution_id`로 원본을 가리킴) 단계 번호를 1부터 다시 센다.** 원본과 단계를 섞지 않는다 — `UniqueConstraint(execution_id, sequence)`가 그 전제이고, 섞으면 "원본이 어디까지 갔는가"와 "원복이 어디까지 갔는가"가 한 축에 눌린다.
- 어느 단계에서 왜 실패했는가는 기존 `ExecutionStep` 필드로 전부 답한다 — `sequence`(몇 번째) · `step_type`·`aws_operation`(무엇) · `status` · `effect`(자산이 바뀌었는가) · `error_summary`(왜). **추가 필드를 요구하지 않는다.**
- **실패한 단계 이후의 단계는 만들지 않는다.** 시도하지 않은 단계를 `FAILED`로 적으면 실패가 실제보다 넓게 기록된다. 몇 번째에서 멈췄는지는 마지막 `sequence`가 말한다.
- `effect` 해석은 원본 실행과 같은 표를 쓴다 — **`NOT_APPLIED`만 "확실히 안 바뀜"이고**, `PARTIAL`·`UNKNOWN`은 바뀌었을 수 있음이다(`ASSET_MAY_HAVE_CHANGED_EFFECTS`).
- **롤백 자식의 실패는 자동 재시도하지 않는다 — 본 ADR의 신규 결정이다.** ADR-0004 정책 ④는 **가드레일 거절**의 무재시도만 정한다. 실행 도중 실패와 §3-2 ③(제3자 변경으로 인한 중단)까지 무재시도를 넓히는 것은 여기서 새로 정하는 것이며, 근거는 같다 — 원복의 원복은 없고, 자산이 이미 만져진 뒤의 자동 재시도는 실패의 범위를 넓힌다. 처분도 같다: CRITICAL 알림 후 수동 개입. 그래서 롤백 자식이 남긴 단계 기록은 재시도의 입력이 아니라 **사람이 이어받을 지점의 좌표**다.
- **자동 재시도를 하지 않는다는 것이 "종료 판정을 하지 않는다"는 뜻은 아니다.** 중단된 롤백 자식도 종료 상태로 확정돼야 한다. 그런데 `dispatcher.py::_judge_one`은 단계 기록이 1건 이상인 실행을 `_JUDGES`로 보내고 **거기 등록된 런북은 `RIGHTSIZING` 하나뿐이다**(실측). `REVERT_SIZE`를 `_RUNNERS`에만 먼저 등록하면 중단된 자식은 재실행도 종료도 되지 않고 `IN_PROGRESS`에 남는다. → **runner와 judge는 짝으로 등록한다.** 재시작 후 실자산을 대조하는 주체, 자식 `SUCCESS|FAILED`와 원본 `ROLLED_BACK|ROLLBACK_FAILED`의 확정 조건, 판정 불가를 CRITICAL·수동 개입으로 넘기는 시점은 **#241이 계약으로 정하고 #249**(판정 불가·재시도 상한)와 맞물린다. 본 ADR은 그것이 #241의 완료 조건임을 못 박는 데까지다.

### 7. 재실행 — 재개 단위는 단계가 아니라 실행

**부분 재개(성공 단계 skip)를 도입하지 않는다.** 비종료 실행이 가는 길은 둘뿐이다.

| 단계 기록 | 해석 | 처분 |
| --- | --- | --- |
| 0건 | 자산이 아직 만져지지 않았다(AWS 호출 직전에 `IN_PROGRESS` 단계가 먼저 커밋되므로) | 처음부터 실행한다. **백업은 이미 결속돼 있으면 재사용한다** |
| 1건 이상 | 자산이 이미 바뀌었을 수 있다 | 재실행하지 않고 **종료 판정**으로 보낸다. 남은 질문은 "다시 돌릴까"가 아니라 "되돌릴까"다 |

근거 셋.

1. **skip은 "그 단계가 지금도 적용된 상태인가"를 알아야 성립하는데, 그것을 모르는 경우가 계약에 있다.** `effect=UNKNOWN`(5xx·연결 실패)은 적용 여부가 불명이라는 뜻이다. 불명을 성공으로 읽고 건너뛰면 실행되지 않은 단계를 실행된 것으로 취급한다.
2. **틀린 skip의 비용이 얻는 이득보다 크다.** `RIGHTSIZING`은 3단계뿐이라 처음부터 다시 돌아 아끼는 시간이 크지 않은데, 정지 단계를 잘못 건너뛰면 타입 변경이 `IncorrectInstanceState`로 거절된다.
3. **같은 성격의 선택을 이미 했다.** ADR-0005는 AI 호출에서 프로세스가 죽으면 처음부터 재호출하고 부분 재개를 지원하지 않기로 정했다. 자산을 바꾸는 경로에서 그보다 느슨한 규칙을 쓸 이유가 없다.

**유일한 예외는 백업 캡처다**(§1 ③). 캡처는 멱등이 아니라 **재실행하면 해로운** 작업이라 건너뛰는 것이 옳다 — 두 번째 캡처는 이미 바뀐 값을 "조치 직전"으로 기록한다.

## Consequences (결과·트레이드오프)

**장점**

- 백업 레코드의 생성·사용·소멸이 한 문서에 모여, 5주차 자동 원복(#241)이 착수 시점에 물을 것이 남지 않는다.
- 제3자 변경 위에 자동 원복이 덮어쓰는 경로가 조회 1회로 닫힌다. 발표 방어 논리에서 "자동으로 되돌립니다"에 붙는 단서를 우리가 먼저 말할 수 있다.
- 되돌아오지 않는 것(퍼블릭 IPv4·신규 `sg-id`·TG 등록 포트)이 목록으로 고정돼, 관제 화면과 시연 대본이 같은 한계를 말하게 된다.
- 재개 규칙이 결정으로 못 박혀, "성공한 단계는 건너뛰자"는 최적화가 리뷰에서 근거 없이 들어오지 않는다.

**비용/유의**

- **백업 레코드가 무한히 쌓인다.** MVP 규모에서 문제가 되지 않는다는 판단이며, Post-MVP에서 정리를 도입할 때 §2의 3조건을 먼저 세워야 한다.
- **§3-2의 대조는 AWS 조회 1회를 더 쓴다.** 자동 원복은 이미 급한 경로(부팅 실패 직후)이지만, 조회 없이 덮어쓰는 것보다 낫다고 판단한다. 조회 자체가 실패하면 원복을 진행하지 않고 보류한다 — 판정 불가의 저장 계약은 #249다.
- **신설 payload 항목 4종은 코드 변경을 부른다.** `InstanceSpecBackup` 2필드는 자동 원복 경로(#241)와 함께, `target_port`·`group_id`는 해당 백업 캡처가 붙는 시점(P1·P2)에 들어간다. 지금 만드는 쪽이 없는 3종은 모델과 캡처가 같은 PR에서 서야 한다.
- **`BackupRecordLoader`의 DB 구현이 아직 없다.** `db/repositories/executions.py`에는 `get_backup_record`만 있고 `latest_for_target`은 테스트 더블에만 있다. `NACL_RESTORE`는 AI 추천 7종이라 가드레일 ④까지 도달하는데, 로더 미배선은 FAIL이 아니라 `RuntimeError`다(ADR-0007 §1) — **`NACL_ADD_DENY`·`NACL_RESTORE` 실행 경로 착수 전에 이 구현이 선행돼야 한다.**
- **§3-3의 백업 시각 노출은 FE 변경이다.** 서버가 이미 가진 값(`BackupRecord.created_at`)이지만 상세 응답 계약에 자리가 없다. 후속으로 분리한다.
- **#241의 완료 조건이 하나 늘었다.** `REVERT_SIZE`를 `_RUNNERS`에 등록할 때 `_JUDGES`도 함께 등록해야 한다(§6). 짝이 어긋나면 중단된 롤백 자식이 종료도 재실행도 되지 않는다.
- **`NACL_RESTORE` precheck에 fingerprint 대조를 더하는 코드 변경이 따라온다**(§5). 본 ADR은 통과 조건만 정하며, 구현은 위 `BackupRecordLoader` DB 구현과 함께 NACL 실행 경로 착수 시점에 선다.
- ⓒ(precheck ↔ 실행 사이 드리프트)는 여전히 미결이다. 본 ADR은 백업을 원천으로 하는 원복 경로만 다루며, 승인·실행 시점 재검증 정책은 ADR-0007이 남긴 그대로다.

## Related

- 선행 결정: [ADR-0004](0004-rollback-runbook-whitelist-registration.md)(롤백 공통 정책 ③④) · [ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md)(§1 `backup_loader` 주입 · §4 롤백 4종 통과 조건 · §5 파라미터 계약) · [ADR-0005](0005-langgraph-stateless-domain-graphs.md)(부분 재개 미지원 선례) · [ADR-0006](0006-localstack-team-standard-env.md) §4(P2 3종 로컬 검증 한계)
- 확정 규격: [`docs/PROJECT_STATUS.md`](../PROJECT_STATUS.md) §Action Whitelist · §MVP 확정 범위(양방향 회복)
- 계약 소재: `packages/schemas/backups.py`(payload) · `packages/schemas/executions.py`(`ExecutionStep`·`effect`) · `packages/schemas/runbook_parameters.py`(원복 파라미터)
- 후속: #241(원복 발동 — §3-2 대조 3분기·§4 결속 보강·`_RUNNERS`↔`_JUDGES` 짝 등록) · #249(판정 불가·재시도 상한) · `BackupRecordLoader` DB 구현 · `NACL_RESTORE` precheck fingerprint 대조(§5) · 백업 3종 캡처와 typed payload · 백업 시각 상세 응답 노출(FE)
- 영향 범위: `packages/schemas/backups.py`, `apps/core-api/services/aws/backup.py`, `apps/core-api/services/aws/rollback.py`, `apps/core-api/services/aws/executor.py`, `apps/core-api/workflows.py`, `apps/core-api/dispatcher.py`, `apps/core-api/db/repositories/executions.py`

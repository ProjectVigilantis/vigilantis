# E2E 시연 시나리오 설계서 (1차)

> **담당**: 박지현 (QA & Scenario) · **이슈**: #132 · **작성**: 2026-08-25
> **목적**: 1차 발표(10/15) 시연 대본의 원천이자 `tests/test_e2e_scenario.py`의 명세.
> **범위 기준**: `docs/PROJECT_STATUS.md`(SSOT)를 따른다. 충돌하면 SSOT가 이긴다.

---

## 이 문서를 읽는 법

두 트랙은 **MVP의 두 축인 "양방향 회복"을 각각 한 번씩** 보여준다.

| 트랙 | 보여주는 것 | 회복 방향 |
| --- | --- | --- |
| **T1 · FinOps** | 자산 다운사이징 → 실패 감지 → **시스템 자동 원복** | 사람 개입 없이 되돌린다 |
| **T2 · SecOps** | 위협 선제 차단 → **관제자 원클릭 해제** | 사람이 판단해 되돌린다 |

같은 4단계 가드레일을 지나지만 **되돌리는 주체가 다르다** — 이 대비가 시연의 핵심이다.

각 단계는 아래 5개 축으로 적는다(#132 완료 조건).

| 축 | 왜 적는가 |
| --- | --- |
| 화면(FE) | 유건희 구현 범위와 대조 |
| API | `POST /actions/execute` 6종 상태 중 무엇이 언제 나오는가 |
| WS 이벤트 | 실시간 갱신 시점 |
| 입력 출처 | Golden Dataset 케이스 ID — 시연 재현성 |
| 실패 시 대체 컷 | 그 단계가 안 되면 무엇을 보여줄지 |

---

## 원천 문서에 대한 주의

`vigilantis-docs/런북 명세서.md`(문서 지도 2위)와 `시스템 흐름도.md`(3위)는 **저장소에 없고 팀장 로컬에만 있다**. 본 1차 설계서는 저장소 안의 확정 원천으로만 작성했다.

| 사용한 원천 | 신뢰도 |
| --- | --- |
| [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §Context 실측표 — 런북별 `target_api` 전수 | 높음(LocalStack 실측) |
| [ADR-0002](adr/0002-runbook-whitelist-mvp-scope.md)·[ADR-0004](adr/0004-rollback-runbook-whitelist-registration.md) — 범위·롤백 정책 | 높음 |
| `packages/schemas/runbooks.py`·`api/` — 실행 축 어휘·API 계약 | 확정(코드) |
| `datasets/golden/` — 입력 케이스 | 확정 |

**확정본 대조가 필요한 항목은 §대조 필요 목록에 모아뒀다.** 런북별 세부 실행 단계와 `parameters_schema`가 여기 해당하며, 본문에서 🔶 로 표시했다.

---

## 공통 축 — 두 트랙이 함께 쓰는 계약

### 실행 상태 6종 (`ExecutionStatus`)

```
IN_PROGRESS → SUCCESS
            → FAILED
            → ROLLBACK_INITIATED → ROLLED_BACK
                                 → ROLLBACK_FAILED
```

뒤 2종은 **복구의 최종 결과**이며 원본 Execution에만 기록된다(SSOT §API 계약).

### 실행 사유 4종 (`TriggerSource`) — 시연에서 3종이 나온다

| 값 | 나오는 곳 |
| --- | --- |
| `USER_APPROVAL` | T1 다운사이징 승인 · T2 차단 승인 · T2 원클릭 해제 |
| `PRE_MITIGATION_0_5S` | T2 High 즉시 선차단 |
| `AUTO_ON_FAILURE` | **T1 자동 원복** |
| `TIMEOUT_ISOLATION_1M` | 1차 시연에는 없음(§트랙 밖) |

### WebSocket 이벤트 3종

`INCIDENT_CREATED` · `INCIDENT_UPDATED` · `EXECUTION_UPDATED` — 전부 **DB commit 이후** 전송되며 상태의 원본이 아니다.

### 카테고리별 필드 차이 (계약이 강제함)

`IncidentResponse`는 **FINOPS일 때 `initial_risk_level`·`reviewed_risk_level`·`response_mode`가 전부 `null`이어야 한다.** 위험 대응 축은 SECOPS에만 있다. 시연 화면에서 T1에 위험도 배지가 보이면 계약 위반이다.

---

## T1 · FinOps — Idle EC2 다운사이징과 자동 원복

**한 줄**: 놀고 있는 서버를 줄였는데 서버가 못 버티자, 사람이 손대기 전에 시스템이 되돌린다.

**입력**: Golden `finops/input/asset_inventory_001.json` **A1**
`arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f00001` · `t3.xlarge` · `cpu_avg 4.9` · `dp 336`
→ 임계값(`IDLE_CPU_AVG 5.0`) **바로 아래**라 `COST_CANDIDATE`. 경계값을 쓰는 이유는 "왜 이게 낭비냐"는 질문에 숫자로 답하기 위해서다.

### 단계

| # | 단계 | 화면(FE) | API | WS 이벤트 | 실패 시 대체 컷 |
| --- | --- | --- | --- | --- | --- |
| 1 | 수집·판정 | 자산 목록에 `COST_CANDIDATE` 배지 | `GET /api/v1/assets` | — | 시드 스크립트 재실행 후 목록만 |
| 2 | Incident 생성 | 목록에 신규 행, `status: ANALYZING` | `GET /api/v1/incidents` | `INCIDENT_CREATED` | mock 데이터로 목록 표시 |
| 3 | AI CoT + 추천 | 상세에 3줄 요약 + 추천 `RUNBOOK_EC2_RIGHTSIZING` | `GET /api/v1/incidents/{id}` | `INCIDENT_UPDATED` | 미리 저장한 CoT 텍스트 표시 |
| 4 | 가드레일 4단계 | 단계별 PASS 표시, `status: AWAITING_APPROVAL` | (내부) | `INCIDENT_UPDATED` | 단계 결과 4행을 정적으로 표시 |
| 5 | 관제자 승인 | **[조치 실행]** 클릭 | `POST /api/v1/actions/execute`<br>→ **202** `IN_PROGRESS` | `EXECUTION_UPDATED` | — |
| 6 | 실행 | 진행 표시 | 🔶 `ec2.modify_instance_attribute` | `EXECUTION_UPDATED` | LocalStack 재기동 후 재시도 |
| 7 | **Status Check 실패** | 실패 표시 | 🔶 `get_waiter` 2/2 실패 | `EXECUTION_UPDATED` `FAILED` | **핵심 컷** — 실패 주입이 안 되면 T1 성립 안 함 |
| 8 | **자동 원복 발동** | "자동 복구 중" | `RUNBOOK_EC2_REVERT_SIZE`<br>`trigger_source: AUTO_ON_FAILURE` | `EXECUTION_UPDATED` `ROLLBACK_INITIATED` | 상태 전이만 화면으로 설명 |
| 9 | 원복 완료 | 원래 타입 복귀 | 원본 Execution `ROLLED_BACK` | `EXECUTION_UPDATED` | — |

### 이 트랙이 증명하는 것

- **버튼은 하나뿐이다.** 5번의 [조치 실행] 이후 사람은 아무것도 누르지 않는다. 8~9번은 전부 시스템이 한다.
- `RUNBOOK_EC2_REVERT_SIZE`는 `ai_recommendable: false`(ADR-0004)라 **AI가 제안한 적이 없다.** 시스템만 발동할 수 있다.
- 원복 파라미터는 AI나 화면이 아니라 **DB 백업 레코드(`backup_record_id`)** 에서만 온다.

### 로컬 실행 가능성 ✅

`ec2.modify_instance_attribute`는 RIGHTSIZING·REVERT_SIZE 양쪽이 쓰며 LocalStack에서 `DryRunOperation`이 정상적으로 뜬다(ADR-0007 실측표 1행). **T1은 로컬에서 전 구간 시연 가능하다.**

---

## T2 · SecOps — 선제 차단과 원클릭 해제

**한 줄**: 22번 포트가 전 세계에 열린 걸 잡아서 먼저 막고, 관제자가 확인한 뒤 한 번 클릭으로 되돌린다.

**입력**: Golden `secops/input/evt_open_ip_001.json` **S1**
`OPEN_IP` · `tcp 22` · `0.0.0.0/0` · 대상 `sg-0a1b2c3d4e5f00005`
→ 이 SG는 자산 골든 **A5**(`golden-sg-open-ssh`, `THREAT`)와 **같은 ARN**이다. 위협 이벤트와 자산 문맥이 실제로 조인되는 것을 보여준다.

### 단계

| # | 단계 | 화면(FE) | API | WS 이벤트 | 실패 시 대체 컷 |
| --- | --- | --- | --- | --- | --- |
| 1 | 위협 주입 | 토폴로지에 **붉은 노드** | (mock 주입) | `INCIDENT_CREATED` | 토폴로지 정적 이미지 |
| 2 | 위험도 판정 | `initial_risk_level` 배지 | `GET /api/v1/incidents/{id}` | `INCIDENT_UPDATED` | 🔶 **판정 규칙 미확정** — §대조 필요 1번 |
| 3 | **0.5초 선차단** | "선제 차단됨" | `response_mode: PRE_MITIGATION_0_5S`<br>`trigger_source: PRE_MITIGATION_0_5S` | `EXECUTION_UPDATED` | 타이밍 시각화가 어려우면 로그로 대체 |
| 4 | 가드레일 4단계 | 단계별 PASS | (내부) | — | 정적 표시 |
| 5 | 차단 실행 | 차단 결과 | `RUNBOOK_NACL_ADD_DENY`<br>🔶 `ec2.create_network_acl_entry` | `EXECUTION_UPDATED` `SUCCESS` | — |
| 6 | 관제자 확인 | 상세에서 근거·CoT 확인 | `GET /api/v1/incidents/{id}` | — | — |
| 7 | **원클릭 해제** | **[해제]** 클릭 | `POST /actions/execute`<br>`RUNBOOK_NACL_RESTORE`<br>`trigger_source: USER_APPROVAL` | `EXECUTION_UPDATED` | **핵심 컷** |
| 8 | 해제 완료 | 노드 정상 복귀 | 🔶 `ec2.delete_network_acl_entry` | `EXECUTION_UPDATED` `SUCCESS` | — |

### 이 트랙이 증명하는 것

- **먼저 막고 나중에 묻는다.** 3번 선차단은 사람 승인 전에 일어난다(`approval_mode: SYSTEM_OR_HUMAN`).
- 되돌리는 것은 **사람의 클릭**이다. T1과 정확히 반대다.
- `RUNBOOK_NACL_RESTORE`는 롤백 3종이 **아니다** — 주 조치 경로의 정식 런북이며 `ai_recommendable`이다. 롤백 3종(`UNISOLATE`·`SG_RECREATE`·`REVERT_SIZE`)과 혼동하지 말 것.

### 로컬 실행 가능성 ⚠️ 조건부

NACL 2종은 LocalStack이 `DryRun`을 지원하지 않아 **조회 대체 검증**으로 판정한다(ADR-0007). 가드레일 ④는 통과하지만, `DryRun` 경로 자체는 **실 AWS에서 처음 실행된다.**

> 두 런북은 9/13 중간 점검 P0 4종에 포함된다. 여기서 어긋나면 **T2 시연 경로가 통째로 막힌다.** 실 AWS 스모크(6–7주차)에서 최우선으로 확인할 대상이다.

---

## 1차 시연에서 빼는 것과 그 이유

| 항목 | 빼는 이유 |
| --- | --- |
| `RUNBOOK_EC2_ISOLATE` / `UNISOLATE` (P2) | `elbv2`가 LocalStack Community에 없어 **조회 대체조차 로컬에서 안 돈다**(ADR-0007). 실 AWS 인프라가 선행 조건 |
| `RUNBOOK_EC2_ENABLE_AUTOSCALING` (P2) | `autoscaling` 동일. 구현량도 최대 |
| `TIMEOUT_ISOLATION_1M` | 위 `ISOLATE`에 의존한다. 1분을 실시간으로 기다리는 것도 시연에 부적합 |
| `RUNBOOK_SG_DELETE_ISOLATED` / `SG_RECREATE` (P1) | 두 트랙이 이미 양방향 회복을 각각 보여준다. 세 번째는 중복 |
| `RUNBOOK_EBS_DELETE_UNATTACHED` (P1) | 입력 스키마에 `ebs_volumes`가 아직 없다 |

**2차 설계서 대상**: 실 AWS 전환(6–7주차) 후 P2 트랙 추가 여부를 다시 판단한다.

---

## 대조 필요 목록 (🔶)

확정본 확보 또는 구현 완료 시 이 절을 먼저 갱신한다.

| # | 항목 | 막힌 이유 | 풀리는 시점 |
| --- | --- | --- | --- |
| 1 | T2 2번 `initial_risk_level`·`response_mode` 판정값 | Risk Evaluator 미구현 · `RiskReasonCode` 목록 미확정 | SSOT 미해결 6번 해소 |
| 2 | 런북별 세부 실행 단계·`parameters_schema` | `런북 명세서.md`가 저장소 밖 | 확정본 확보 또는 #49 |
| 3 | Status Check 실패 **주입 방법** | 자동 원복 엔진 미구현 | 김세혁 원복 엔진 |
| 4 | 가드레일 ③④ 실제 통과 화면 | ③ ARN Match 미구현(#134 확인), ④ precheck 진행 중(#129) | ③④ 구현 |
| 5 | FE 화면 명칭·전환 | `apps/web`에 `assets`·`incidents` 2개 화면만 있음 | #106 mock 연동 마감 |

---

## `tests/test_e2e_scenario.py` 대응

현재 skip 2건이 본 설계서의 어느 범위를 검증할지 고정한다.

| 테스트 | 대응 트랙 | 검증 범위 | 여는 조건 |
| --- | --- | --- | --- |
| `test_idle_ec2_downsize_flow` | **T1** | Golden A1 → `COST_CANDIDATE` → 가드레일 → 실행 접수 → Status Check 실패 → `ROLLED_BACK` | 대조 3번(원복 엔진) |
| `test_open_ssh_ip_block_flow` | **T2** | Golden S1 → Incident → 선차단 → `NACL_ADD_DENY` → 원클릭 해제 → `NACL_RESTORE` | 대조 1번(Risk Evaluator) |

**두 테스트 모두 Golden Dataset을 입력으로 쓴다.** 시연에 쓰는 데이터와 테스트에 쓰는 데이터가 같아야 "시연이 되면 테스트도 된다"가 성립한다.

실행 계열 공통 fixture는 **#136**에서 선구축한다. 그 픽스처가 P2 3종의 로컬 FAIL을 `GuardrailValidationContext` 문맥별로 표현해야 한다는 전제도 같은 이슈에 적었다.

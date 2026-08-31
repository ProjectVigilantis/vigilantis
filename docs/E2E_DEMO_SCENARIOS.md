# E2E 시연 시나리오 설계서 (1차)

> **담당**: 박지현 (QA & Scenario) · **이슈**: #132 · **작성**: 2026-08-25 · **현황 갱신**: 2026-08-31 (김세혁 — §대조 필요 목록 2번 해소·원천 재지정 / 박지현 — 본문 🔶 잔여 정리·번호 표기)
> **목적**: 1차 발표(10/15) 시연 대본의 원천이자 `tests/test_e2e_scenario.py`의 명세.
> **범위 기준**: `docs/PROJECT_STATUS.md`(SSOT)를 따른다. 충돌하면 SSOT가 이긴다.

---

## 이 문서를 읽는 법

두 트랙은 **MVP의 두 축인 "양방향 회복"을 각각 한 번씩** 보여준다.

| 트랙 | 보여주는 것 | 되돌리는 주체 |
| --- | --- | --- |
| **T1 · FinOps** | 자산 다운사이징 → 실패 감지 → **시스템 자동 원복** | **시스템** |
| **T2 · SecOps** | 위협 차단 → **관제자 원클릭 해제** | **사람** |

같은 4단계 가드레일을 지나지만 **되돌리는 주체가 다르다** — 이 대비가 시연의 핵심이다.

**왜 보안만 사람을 거치는가**가 발표에서 나올 질문이다. 답은 설계 의도다 — NACL 차단은 오탐 시 **서브넷 전체**에 영향이 가므로 `RUNBOOK_NACL_ADD_DENY`의 `approval_mode`가 `HUMAN_ONLY`로 확정돼 있다([`docs/PROJECT_STATUS.md`](PROJECT_STATUS.md) §Action Whitelist). 자산 원복은 대상이 인스턴스 1대라 자동화해도 폭발 반경이 좁다. **자동화 범위를 폭발 반경으로 나눈 것**이 두 트랙의 대비다.

각 단계는 아래 5개 축으로 적는다(#132 완료 조건).

| 축 | 왜 적는가 |
| --- | --- |
| 화면(FE) | 유건희 구현 범위와 대조 |
| API | `POST /actions/execute` 6종 상태 중 무엇이 언제 나오는가 |
| WS 이벤트 | 실시간 갱신 시점 |
| 입력 출처 | Golden Dataset 케이스 ID — 시연 재현성 |
| 실패 시 대체 컷 | 그 단계가 안 되면 무엇을 보여줄지 |

**화면 문구는 실제 표기를 쓴다** — 이 문서가 대본이기 때문이다. 계약 enum(`COST_CANDIDATE`)이 아니라 화면에 뜨는 말(**최적화 후보**)로 적는다. 화면에 "CoT"라는 말은 쓰지 않는다(**판단 근거**). 표기 사전은 FE 화면설계서 §3.2다.

---

## 원천 문서

본 설계서는 저장소 안의 확정 원천으로만 작성했다. 확정본은 [`docs/PROJECT_STATUS.md`](PROJECT_STATUS.md)(문서 지도 1위)이며, 이 설계서는 그것을 원천으로 삼는 파생 문서다(문서 지도 4위).

| 사용한 원천 | 신뢰도 |
| --- | --- |
| [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §Context 실측표 — 런북별 `target_api` 전수 | 높음(LocalStack 실측) |
| [ADR-0002](adr/0002-runbook-whitelist-mvp-scope.md)·[ADR-0004](adr/0004-rollback-runbook-whitelist-registration.md) — 범위·롤백 정책 | 높음 |
| `packages/schemas/runbooks.py`·`api/` — 실행 축 어휘·API 계약 | 확정(코드) |
| `datasets/golden/` — 입력 케이스 | 확정 |

**확정본 대조가 필요한 항목은 §대조 필요 목록에 모아뒀다.** 본문의 🔶 에는 **그 목록의 번호를 함께 적는다** — 번호가 없으면 무엇을 기다리는 표시인지 읽는 사람이 알 수 없다. 런북별 세부 실행 단계와 `parameters_schema`는 §대조 2번으로 해소됐고(2026-08-31, PR #205), 남은 🔶 는 위험도 판정(1번)과 Status Check 실패 주입(3번) 둘이다.

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

### 실행 사유 4종 (`TriggerSource`) — 시연에서 2종이 나온다

| 값 | 나오는 곳 |
| --- | --- |
| `USER_APPROVAL` | T1 다운사이징 승인 · T2 차단 승인 · T2 원클릭 해제 |
| `AUTO_ON_FAILURE` | **T1 자동 원복** |
| `PRE_MITIGATION_0_5S` | 1차 시연에 없음 — 이 값을 갖는 런북은 `RUNBOOK_EC2_ISOLATE` 하나뿐이고 P2로 제외했다 |
| `TIMEOUT_ISOLATION_1M` | 1차 시연에 없음(§트랙 밖) |

> `PRE_MITIGATION_0_5S`는 **Incident의 `response_mode`로는 T2에 등장한다.** 같은 이름이지만 다른 축이다 — §T2 「실행 축과 Incident 축은 다르다」 참고. 두 축을 같은 값으로 적으면 가드레일 ②에서 거절된다.

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
| 1 | 수집·판정 | 자산 목록에 **최적화 후보** 배지 | `GET /api/v1/assets` | — | 시드 스크립트 재실행 후 목록만 |
| 2 | Incident 생성 | INC-001 **카드 그리드**에 신규 카드, `status: ANALYZING` | `GET /api/v1/incidents` | `INCIDENT_CREATED` | mock 데이터로 카드 표시 |
| 3 | AI 판단 근거 + 추천 | 상세에 **판단 근거** 3줄 + 추천 `RUNBOOK_EC2_RIGHTSIZING` | `GET /api/v1/incidents/{id}` | `INCIDENT_UPDATED` | 미리 저장한 근거 텍스트 표시 |
| 4 | 가드레일 4단계 | — (화면 표시 없음) · 통과 신호는 `status: AWAITING_APPROVAL`로 실행 버튼이 열리는 것 | (내부) | `INCIDENT_UPDATED` | 슬라이드 컷으로 분리 |
| 5 | 관제자 승인 | **[조치 실행]** 클릭 | `POST /api/v1/actions/execute`<br>**`202 Accepted`** → `IN_PROGRESS`<br>*(같은 `idempotency_key` 재요청은 `200 OK` 멱등 재생)* | `EXECUTION_UPDATED` | — |
| 6 | 실행 | 진행 표시 | `ec2.modify_instance_attribute` | `EXECUTION_UPDATED` | LocalStack 재기동 후 재시도 |
| 7 | **Status Check 실패** | 실패 표시 | 🔶 `get_waiter` 2/2 실패 — §대조 필요 3번 | `EXECUTION_UPDATED` `FAILED` | **핵심 컷** — 실패 주입이 안 되면 T1 성립 안 함 |
| 8 | **자동 원복 발동** | **복구 중** | `RUNBOOK_EC2_REVERT_SIZE`<br>`trigger_source: AUTO_ON_FAILURE` | `EXECUTION_UPDATED` `ROLLBACK_INITIATED` | 상태 전이만 화면으로 설명 |
| 9 | 원복 완료 | **AST-001로 이동해** 인스턴스 유형 복귀 확인 | 원본 Execution `ROLLED_BACK` | `EXECUTION_UPDATED` | — |

### 이 트랙이 증명하는 것

- **버튼은 하나뿐이다.** 5번의 [조치 실행] 이후 사람은 아무것도 누르지 않는다. 8–9번은 전부 시스템이 한다.
- `RUNBOOK_EC2_REVERT_SIZE`는 `ai_recommendable: false`(ADR-0004)라 **AI가 제안한 적이 없다.** 확정값은 `trigger_source: [AUTO_ON_FAILURE, USER_APPROVAL]` · `approval_mode: SYSTEM_OR_HUMAN`이라 관제자 수동 원복 경로도 열려 있지만, **이 시나리오에서는 시스템이 발동한다.**
- 원복 파라미터는 AI나 화면이 아니라 **DB 백업 레코드(`backup_record_id`)** 에서만 온다.

### 로컬 실행 가능성 ✅

`ec2.modify_instance_attribute`는 RIGHTSIZING·REVERT_SIZE 양쪽이 쓰며 LocalStack에서 `DryRunOperation`이 정상적으로 뜬다(ADR-0007 실측표 1행). **T1은 로컬에서 전 구간 시연 가능하다.**

---

## T2 · SecOps — 위협 차단과 원클릭 해제

**한 줄**: 한 IP가 SSH를 두드려대는 걸 잡아 그 주소만 핀셋으로 막고, 관제자가 확인한 뒤 한 번 클릭으로 되돌린다.

**입력**: Golden `secops/input/evt_ssh_bruteforce_001.json` **S3**
`SSH_BRUTE_FORCE` · `source_ip 203.0.113.10` · `120회 / 300초` · 대상 `i-0a1b2c3d4e5f00001`

**입력 선택 근거**: `RUNBOOK_NACL_ADD_DENY`의 `cidr_block`은 *"차단할 악성 IP 대역"* 이고, 명세서 `[SecOps-02]` 안전장치가 **"특정 IP/32 단일 주소만 핀셋 지정"** 을 요구한다. S3의 `source_ip`는 /32 단일 주소라 그대로 들어간다. `parameters_schema`에 포트 필드가 없어 "22번만 골라 막기"는 불가능하다.

> `evt_open_ip_001.json`(S1)을 쓰면 `cidr_block`이 `0.0.0.0/0`이 되어 **서브넷 인바운드가 전면 차단**된다. 명세서의 트리거 조건도 *"특정 IP의 반복적 브루트포스 공격 감지"* 로 OPEN_IP 설정 오류가 아니다.

**자산 조인**: S3의 `target_arn`은 **T1이 쓰는 A1과 같은 인스턴스**다. 두 트랙이 한 자산에서 만나므로, 발표에서 "이 서버가 아까 그 서버"라고 짚을 수 있다.

### 단계

| # | 단계 | 화면(FE) | API | WS 이벤트 | 실패 시 대체 컷 |
| --- | --- | --- | --- | --- | --- |
| 1 | 위협 주입 | 토폴로지에 **붉은 노드** | (mock 주입) | `INCIDENT_CREATED` | 토폴로지 정적 이미지 |
| 2 | 위험도 판정 | 위험도 배지 | `GET /api/v1/incidents/{id}` | `INCIDENT_UPDATED` | 🔶 **판정 규칙 미확정** — §대조 필요 1번 |
| 3 | 대응 경로 진입 | "선제 차단" 경로 표시 | `response_mode: PRE_MITIGATION_0_5S`<br>*(Incident 축 — 실행 축 아님)* | `INCIDENT_UPDATED` | 경로 표시 없이 4번으로 |
| 4 | 가드레일 4단계 | — (화면 표시 없음) | (내부) | — | 슬라이드 컷으로 분리 |
| 5 | **관제자 승인 → 차단** | **[조치 실행]** 클릭 | `RUNBOOK_NACL_ADD_DENY`<br>`trigger_source: USER_APPROVAL`<br>`approval_mode: HUMAN_ONLY`<br>`ec2.create_network_acl_entry` | `EXECUTION_UPDATED` `SUCCESS` | — |
| 6 | 관제자 확인 | 상세에서 **판단 근거** 확인 | `GET /api/v1/incidents/{id}` | — | — |
| 7 | **원클릭 해제** | **[해제]** 클릭 | `RUNBOOK_NACL_RESTORE`<br>`trigger_source: USER_APPROVAL` | `EXECUTION_UPDATED` | **핵심 컷** |
| 8 | 해제 완료 | 토폴로지 노드 정상 복귀 | `ec2.delete_network_acl_entry` | `EXECUTION_UPDATED` `SUCCESS` | — |

### 실행 축과 Incident 축은 다르다 (3번의 핵심)

두 축을 같은 값으로 적으면 **가드레일 ②에서 거절되어 T2가 성립하지 않는다.**

| 축 | 무엇을 담나 | 3번의 값 |
| --- | --- | --- |
| `response_mode` | **Incident의** 위험 대응 경로 | `PRE_MITIGATION_0_5S` ✅ |
| `trigger_source` | **실행 건별** 시작 사유 | 여기 없음 — 5번의 `USER_APPROVAL` |

`PRE_MITIGATION_0_5S`를 `trigger_source`로 갖는 런북은 **`RUNBOOK_EC2_ISOLATE` 하나뿐**이고, 그 런북은 1차 시연에서 제외한 P2다. 가드레일 ②는 *"실행의 `trigger_source` ∈ 런북의 허용 목록"* 을 대조한다(명세서 §실행 축 어휘).

### [해제] 버튼이 렌더되는 필드

`RUNBOOK_NACL_RESTORE`는 **본편 7종**이라 `ExecutionSummary.available_recovery_runbook_ids`로 올 수 없다 — 그 필드는 validator가 **롤백 3종만** 허용한다. 따라서 [해제] 버튼은 **`recommendations`** 로 렌더된다.

### 이 트랙이 증명하는 것

- **막는 것도 푸는 것도 사람이 판단한다.** 오탐 시 서브넷 전체가 끊기므로 의도적으로 사람을 넣었다(`HUMAN_ONLY`).
- 차단 대상은 `/32` 단일 주소다 — 정상 트래픽을 함께 막지 않는다.
- `RUNBOOK_NACL_RESTORE`는 롤백 3종이 **아니다.** 주 조치 경로의 정식 런북이며 AI 추천 가능하다. 롤백 3종(`UNISOLATE`·`SG_RECREATE`·`REVERT_SIZE`)과 혼동하지 말 것.

### 로컬 실행 가능성 ⚠️ 조건부

NACL 2종은 LocalStack이 `DryRun`을 지원하지 않아 **조회 대체 검증**으로 판정한다(ADR-0007). 가드레일 ④는 통과하지만 `DryRun` 경로 자체는 **실 AWS에서 처음 실행된다.**

> 두 런북은 9/13 중간 점검 P0 4종에 포함된다. 여기서 어긋나면 **T2 시연 경로가 통째로 막힌다.** 실 AWS 스모크(6–7주차) 최우선 확인 대상이다.

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

## 시연 선행 조건 — 화면 (PR #148 리뷰: @yoogh3546)

두 트랙의 **시작·종료 컷**이 아직 없는 화면에 걸려 있다. 시연 일정보다 먼저 확보돼야 한다.

| 컷 | 필요한 화면 | 현재 상태 |
| --- | --- | --- |
| T1-2 Incident 카드 | **INC-001** 카드 그리드 | ✅ **확보**(2026-08-26, #167 / PR #171 — 카드 그리드·위험도 정렬·승인 대기 프리셋). 목록에서 조치 실행·ACT-002 딥링크까지 연결됨(#179 / PR #180) |
| T2-1 · T2-8 붉은 노드 | **AST-001 토폴로지 뷰**(#146) 또는 **DSH-001** 통합 위협 토폴로지 | **미확보.** #146은 PR #137 리뷰 대응으로 분리된 뒤 열려 있고, DSH-001은 카드 없음 |

**붉은 노드 컷은 여전히 mock 기준이다.** 다만 BE 쪽 근거는 바뀌었다 — #149 완료로 자산 4종(EBS·ASG·Launch Template·ALB TG)과 `RelationType` **6종이 코드상 전부** 산출된다(PR #156·#161·#165). 대신 `autoscaling`·`elbv2`가 LocalStack Community에 없어 collector가 호출 실패를 흡수해 degrade 하므로(`PARTIAL` 표면화), `MEMBER_OF`·`USES`·`REGISTERED_IN`은 **실 AWS 스모크(6–7주차) 전까지 로컬에서 값이 채워지지 않는다.**

## 대조 필요 목록 (🔶)

확정본 확보 또는 구현 완료 시 이 절을 먼저 갱신한다.

| # | 항목 | 막힌 이유 | 풀리는 시점 |
| --- | --- | --- | --- |
| 1 | T2 2번 위험도 판정값(`initial_risk_level`) | Risk Evaluator 미구현 · `RiskReasonCode` 목록 미확정 | SSOT 미해결 6번 해소 |
| 2 | ~~런북별 세부 실행 단계·`parameters_schema`~~ ✅ 해소(2026-08-31) | 확정본이 SSOT §Action Whitelist로 이관되고, `parameters_schema`는 `packages/schemas/runbook_parameters.py`(#154 / PR #178), 세부 실행 단계·`target_api`는 [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §Context·§5가 갖는다 | — |
| 3 | Status Check 실패 **주입 방법** | 자동 원복 엔진 미구현 | 김세혁 원복 엔진 |
| 4 | ~~가드레일 ③ 실제 통과~~ ✅ 해소(2026-08-31) | **4단계가 전부 섰다.** ③ ARN Match 구현(#177 / PR #202 — DB 수집 ARN 대조로 Scope Escalation 차단, ① NUL 문자 차단 포함)으로 `tests/test_guardrails.py`의 placeholder skip 1건이 해제됐다. ④ Dry-Run은 `precheck()` 확정 10종 구현 완료(#129 / PR #147 · 실측 #130 / PR #170) | — |
| 5 | 화면 구현 상태 | 아래 표 | 카드별 |
| 6 | **WS 이벤트로 화면이 실시간 갱신되는 것** | FE 연동 구현됨 — 소켓 수명주기·이벤트 3종·Toast·재연결(#168 / PR #181). 로컬 `core-api`로 **연결·중단·자동 복구 확인**. 다만 **이벤트 실배달은 미확인**(코어 DB가 비어 발생시킬 인시던트가 없다) | #168 / PR #181. 실배달은 시드 확보 후 |
| 7 | ~~T1 5번 `POST /actions/execute` HTTP 상태 코드~~ ✅ 해소(2026-08-27) | 라우터·멱등 처리 구현 완료(#116 / PR #119), 롤백 3종 실행 접수는 #126 / PR #158. **신규 접수 `202 Accepted` · 같은 `idempotency_key` 재요청 `200 OK`** 로 확정돼 SSOT §API 계약에 등재됐다. 남은 것은 `execute` 본체(Boto3 실행·자동 원복 — 김세혁) | — |

**문서의 WS 이벤트 열은 "서버가 그 시점에 보내는 이벤트"로는 정확하다.** 다만 그 이벤트로 화면이 실시간으로 바뀌는 것을 시연하려면 6번이 필요하다.

### 5번 상세 — 화면 카드별 상태 (2026-08-27 기준)

| 화면 | 상태 |
| --- | --- |
| AST-001 · AST-002 | ✅ 완료(PR #137 — 카드 그리드·상세 Drawer). 토폴로지 뷰만 **#146으로 분리돼 열려 있음** |
| INC-002 A 변형 | ✅ 완료(#138 / PR #145) |
| INC-002 B 변형 | ✅ 완료(#155 / PR #162 — 위험도·`response_mode`·수행된 조치). **초 단위 카운트다운은 제거 대상**(2026-08-27 결정 — SSOT §확정 결정 로그, 표기는 `제안 생성 시간`·`실행 예정 시간` 2종) |
| ACT-001 · ACT-002 | ✅ 완료(#166 / PR #169 — 실행 확인 모달·실행 상태 인라인). 승인 화면의 조치 대상 문맥은 **#183 후속**(#154로 `display_parameters`가 서버 파생이 되며 mock 5건이 갈림) |
| INC-001 | ✅ 완료(#167 / PR #171) · INC-001 → ACT-001 연결(#179 / PR #180) |
| CMN-001 실시간 | ✅ 연동 완료(#168 / PR #181). **이벤트 실배달만 미확인** — 위 대조 6번 |

## `tests/test_e2e_scenario.py` 대응

현재 skip 2건이 본 설계서의 어느 범위를 검증할지 고정한다.

| 테스트 | 대응 트랙 | 검증 범위 | 여는 조건 |
| --- | --- | --- | --- |
| `test_idle_ec2_downsize_flow` | **T1** | Golden A1 → `COST_CANDIDATE` → 가드레일 → 실행 접수 → Status Check 실패 → `ROLLED_BACK` | 대조 3번(원복 엔진) |
| `test_open_ssh_ip_block_flow` | **T2** | Golden **S3** → Incident → `response_mode` 진입 → 승인 → `NACL_ADD_DENY`(`USER_APPROVAL`) → 원클릭 해제 → `NACL_RESTORE` | 대조 1번(Risk Evaluator) |

**두 테스트 모두 Golden Dataset을 입력으로 쓴다.** 시연에 쓰는 데이터와 테스트에 쓰는 데이터가 같아야 "시연이 되면 테스트도 된다"가 성립한다.

**테스트 이름과 입력이 어긋난다** — `test_open_ssh_ip_block_flow`는 `OPEN_IP`를 가리키는데 입력은 `SSH_BRUTE_FORCE`(S3)다. 1주차에 지은 이름이고 T2 입력이 PR #148 리뷰로 바뀐 결과다. skip을 해제하는 시점(대조 1번 해소)에 `test_ssh_bruteforce_nacl_block_flow` 등으로 함께 고친다.

실행 계열 공통 fixture는 **#136**에서 선구축한다. 그 픽스처가 P2 3종의 로컬 FAIL을 `GuardrailValidationContext` 문맥별로 표현해야 한다는 전제도 같은 이슈에 적었다.

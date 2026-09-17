# E2E 시연 시나리오 설계서 (1차)

> **담당**: 김승철 (QA & Scenario · 2026-09-16 박지현에게서 인수) · **이슈**: #132 · **작성**: 2026-08-25 · **현황 갱신**: 2026-08-31 (김세혁 — §대조 필요 목록 1·2번 상태·원천 재지정 / 박지현 — 본문 🔶 잔여 정리·번호 표기) · 2026-09-01 (박지현 — §대조 1번 ② 해소 반영) · 2026-09-02 (박지현 — T1 1단계 골든 실경로 실측·기재, §대조 8·9번 신설) · 2026-09-04 (박지현 — 골든 EBS 편입 반영, §자산 화면의 분포 숫자를 재현 명령으로 대체) · 2026-09-08 (김세혁 — §T1 7·8·9번 상태 축 정정, §대조 3번 분리·9번 재서술) · 2026-09-09 (박지현 — §대조 8번 해소 반영, 골든 커버리지 표에 관계 축 추가) · 2026-09-15 (김세혁 — §대조 3번 ⓑ 실측 해소, §T1 7번 행·§T2 관통 실측 반영) · 2026-09-17 (김승철 — FE mock 계층 제거(PR #351) 반영·T1 2번 대체 컷 없음 기재(#352), §대조 1·9번 해소 반영, 토폴로지 색 원천 정정, §테스트 대응을 흐름 테스트 이전(PR #358) 기준으로) · 2026-09-17 (김승철 — §10/1 컷 시트 신설(LocalStack 시드 실측), T2 단계표 1·8번 화면 문구·자산 조인을 시연 기준으로)
> **목적**: 중간 발표(10/1) MVP 시연 대본의 원천이자 `tests/test_e2e_scenario.py`·`apps/core-api/tests/test_e2e_flow.py`의 명세.
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

**10/1(목) 시연에서 각 단계를 실제로 어떻게 보여줄지는 §10/1 컷 시트가 정한다.** 단계표는 설계 의도이고, 컷 시트는 시연 환경(LocalStack 시드)에서 실측한 결과다. 둘이 다르면 컷 시트가 우선한다.

---

## 원천 문서

본 설계서는 저장소 안의 확정 원천으로만 작성했다. 확정본은 [`docs/PROJECT_STATUS.md`](PROJECT_STATUS.md)(문서 지도 1위)이며, 이 설계서는 그것을 원천으로 삼는 파생 문서다(문서 지도 4위).

| 사용한 원천 | 신뢰도 |
| --- | --- |
| [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §Context 실측표 — 런북별 `target_api` 전수 | 높음(LocalStack 실측) |
| [ADR-0002](adr/0002-runbook-whitelist-mvp-scope.md)·[ADR-0004](adr/0004-rollback-runbook-whitelist-registration.md) — 범위·롤백 정책 | 높음 |
| `packages/schemas/runbooks.py`·`api/` — 실행 축 어휘·API 계약 | 확정(코드) |
| `datasets/golden/` — 입력 케이스 | 확정 |

**확정본 대조가 필요한 항목은 §대조 필요 목록에 모아뒀다.** 본문의 🔶 에는 **그 목록의 번호를 함께 적는다** — 번호가 없으면 무엇을 기다리는 표시인지 읽는 사람이 알 수 없다. 런북별 세부 실행 단계와 `parameters_schema`는 §대조 2번으로 해소됐고(2026-08-31, PR #205), **1번도 해소됐다** — 정답 12건(2026-09-01, PR #223 · #242)에 이어 `evaluate_threat()`가 위협 접수 워크플로에 배선됐다(#322).

남은 🔶 는 **둘**이다 — ① Status Check **실패 주입 방법**(3번 — 자동 원복 엔진은 2026-09-03에(#241 / PR #256), **`stop_instances` 주입 경로(ⓑ)는 2026-09-15 실측으로 해소됐고 `impaired`(ⓐ)만 실 AWS로 남는다**) ② **WS 이벤트 실배달**(6번 — 9번이 닫히며 독립 항목이 됐다). **1번 배선(#322)과 9번 판정→Intake 배선(#306 / PR #320)은 해소됐다** — T1 2단계 이후와 T2의 Incident가 실경로로 생긴다. **8번(토폴로지 뷰를 골든으로 못 채우는 것)은 2026-09-08에 해소됐다** — 자산 유형 **7/7종** · `RelationType` **6/6종**이 골든에서 선다(#271 / PR #314). 8·9번은 2026-09-02에 신설했다. **막혀 있던 것이 늘어난 게 아니라, mock으로 덮여 안 보이던 것을 목록에 올린 것이다** — 같은 날 자산 화면(T1 1단계)은 골든 실데이터로 서는 것을 실측해 mock을 벗겼고, 8번은 그 뒤 골든이 노드·엣지까지 채우며 닫혔다.

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
| 1 | 수집·판정 | 자산 목록에 **최적화 후보** 배지 | `GET /api/v1/assets` — **골든 실데이터로 응답한다**(아래 §자산 화면) | — | 시드 스크립트 재실행 후 목록만 |
| 2 | Incident 생성 | INC-001 **카드 그리드**에 신규 카드, `status: ANALYZING` | `GET /api/v1/incidents` | `INCIDENT_CREATED` | **대체 컷 없음**(FE mock 계층 제거, 2026-09-17 · PR #351) |
| 3 | AI 판단 근거 + 추천 | 상세에 **판단 근거** 3줄 + 추천 `RUNBOOK_EC2_RIGHTSIZING` | `GET /api/v1/incidents/{id}` | `INCIDENT_UPDATED` | 미리 저장한 근거 텍스트 표시 |
| 4 | 가드레일 4단계 | — (화면 표시 없음) · 통과 신호는 `status: AWAITING_APPROVAL`로 실행 버튼이 열리는 것 | (내부) | `INCIDENT_UPDATED` | 슬라이드 컷으로 분리 |
| 5 | 관제자 승인 | **[조치 실행]** 클릭 | `POST /api/v1/actions/execute`<br>**`202 Accepted`** → `IN_PROGRESS`<br>*(같은 `idempotency_key` 재요청은 `200 OK` 멱등 재생)* | `EXECUTION_UPDATED` | — |
| 6 | 실행 | 진행 표시 | `ec2.modify_instance_attribute` | `EXECUTION_UPDATED` | LocalStack 재기동 후 재시도 |
| 7 | **Status Check 실패** | 실패 표시 | `get_waiter` 2/2 실패 — 6번 실행 주기가 끝난 뒤 대상 인스턴스를 **`stop_instances`로 멈춰** 만든다(§대조 필요 3번 ⓑ) | `EXECUTION_UPDATED` **Execution `ROLLBACK_INITIATED`**<br>`INCIDENT_UPDATED` **Incident `ACTION_IN_PROGRESS`** | **핵심 컷** — 주입 창(실행 주기 뒤·판정 주기 전)을 놓치면 판정이 `OK`로 끝나 원복이 일어나지 않는다 |
| 8 | **자동 원복 발동** | **복구 중** | `RUNBOOK_EC2_REVERT_SIZE` **자식 실행 접수**<br>`trigger_source: AUTO_ON_FAILURE` · `parent_execution_id` = 원본<br>*(상태 전이가 아니라 새 실행 레코드다)* | **없다** — 접수는 발행하지 않는다(`dispatcher.py:335-337`). 화면의 "복구 중"은 **7번 이벤트로 이미 그려져 있다** | 접수만 화면으로 설명 |
| 9 | 원복 완료 | **AST-001로 이동해** 인스턴스 유형 복귀 확인 | 자식 `SUCCESS` → 원본 Execution `ROLLED_BACK`(함께 확정) | `EXECUTION_UPDATED`<br>`INCIDENT_UPDATED` **Incident `AWAITING_CLOSURE`** | 관제자 [종료 판단]이 남는다 |

### 7·8·9번의 상태값은 어느 축인가

**어느 값이 무엇인지는 위 단계표가 이미 적는다.** 여기서는 그 값이 **왜 그래야 하는지**만 코드 좌표와 함께 남긴다 — 축을 다시 뭉개는 서술이 들어오는 것을 막는 것은 표가 아니라 아래 세 문단이다.

**7번 Incident가 `FAILED`가 아닌 이유**: `_incident_status_after()`는 실행의 성패가 아니라 **그 인시던트에 남은 것**으로 목적 상태를 가른다(`workflows.py:843`). `ROLLBACK_INITIATED`가 비종료 상태라(`packages/schemas/executions.py:28 EXECUTION_NON_TERMINAL_STATUSES`) *"진행 중 실행이 있다"* 가 되어 `ACTION_IN_PROGRESS`다. **되돌릴 것이 남았는데 `FAILED`로 적으면 화면이 복구 중인 조치를 '진행 불가'로 그린다.**

**8번이 상태 전이가 아닌 이유**: #241은 원본을 옮기지 않고 **`parent_execution_id`로 묶인 자식 행을 새로 만든다**(`workflows.py:1146 initiate_auto_rollback()`). 원본과 자식을 한 축으로 적으면 7번과 8번이 같은 값(`ROLLBACK_INITIATED`)을 두 번 말하게 되어 **무엇이 새로 일어났는지가 사라진다.** **이 시점에 원본과 Incident는 그대로다** — 원본은 `ROLLBACK_INITIATED`, Incident는 `ACTION_IN_PROGRESS`에 머문다(단계표 8번 행은 새로 생기는 자식만 적는다).

**9번이 두 실행을 함께 확정하는 이유**: `_ORIGIN_STATUS_AFTER_ROLLBACK`(`workflows.py:895`)이 자식 `SUCCESS` → 원본 `ROLLED_BACK`으로 잇는다. 원본이 `ROLLBACK_INITIATED`에 남으면 비종료라 인시던트가 영원히 진행 중이 된다.

### 이 트랙이 증명하는 것

- **버튼은 하나뿐이다.** 5번의 [조치 실행] 이후 사람은 아무것도 누르지 않는다. 8–9번은 전부 시스템이 한다.
- `RUNBOOK_EC2_REVERT_SIZE`는 `ai_recommendable: false`(ADR-0004)라 **AI가 제안한 적이 없다.** 확정값은 `trigger_source: [AUTO_ON_FAILURE, USER_APPROVAL]` · `approval_mode: SYSTEM_OR_HUMAN`이라 관제자 수동 원복 경로도 열려 있지만, **이 시나리오에서는 시스템이 발동한다.**
- 원복 파라미터는 AI나 화면이 아니라 **DB 백업 레코드(`backup_record_id`)** 에서만 온다.

### 로컬 실행 가능성 ✅

`ec2.modify_instance_attribute`는 RIGHTSIZING·REVERT_SIZE 양쪽이 쓰며 LocalStack에서 `DryRunOperation`이 정상적으로 뜬다(ADR-0007 실측표 1행). **T1은 로컬에서 전 구간 시연 가능하다.**

### 자산 화면(1단계)은 골든 실데이터로 선다

**1단계는 골든 실데이터로 응답한다**(2026-09-02 실측 · 2026-09-04 재확인). 지금까지 이 문서는 그 경로를 적지 않았고, 그래서 FE가 한동안 자산 화면을 별도 mock(당시 `apps/web/src/app/api/v1/_mock/data.ts` — 2026-09-17 PR #351로 제거)으로 채웠다. **경로가 없어서가 아니라 경로가 적혀 있지 않아서다** — 아래 세 단계는 전부 원래 있던 프로덕션 함수다.

```text
datasets/golden/finops/input/*.json
  → collector.persist_inventory()
  → rule_engine.run_rule_engine()
  → GET /api/v1/assets
```

한 줄로 재현한다(PostgreSQL 기동 + `alembic upgrade head` 후).

```bash
uv run python scripts/load_golden_assets.py --verify
```

`GET /api/v1/assets` → `200` · `collection_status: READY` · 골든 정답 대조 **어긋남 0**.

**적재 건수와 판정 분포는 이 문서에 적지 않는다.** 골든이 한 건만 늘어도 낡는데, 이 문서의 어떤 판단도 그 숫자에 기대지 않기 때문이다 — 실제 값은 위 명령의 출력이 원천이다. 실제로 같은 표가 **세 번 낡았다**(PR #266 편입 → #275 원복 → #280 재착륙). 문서에 남기는 것은 **분포가 아니라 커버리지**다.

**골든이 채우는 분기** — **네 줄 전부** `apps/core-api/tests/test_golden_assets_api.py`가 **CI에서 등식으로 강제한다.** 사람이 옮겨 적는 값이 아니다. 자산 유형 줄도 2026-09-08부터 그렇다(#271 / PR #314) — 그전까지 이 줄만 사람이 지켰고, 계약에 유형이 늘어도 문서가 조용히 낡을 자리였다. 등식이라 **계약이 늘면 여기가 먼저 실패한다**(고칠 곳은 문서가 아니라 `datasets/golden/finops/input/`이다).

| 축 | 골든이 채우는 것 |
| --- | --- |
| `verdict` | **4종 전부** |
| `skip_reason_code` | **6종 전부** — 마지막 값이던 `SKIP_UNSUPPORTED_STATE`는 EBS 전이·비정상·미상 상태 5건(골든 E4~E8)이 채웠다(**#276**) |
| 자산 유형 | **7종 전부** — 마지막까지 0건이던 `NACL`·`LAUNCH_TEMPLATE`·`AUTO_SCALING_GROUP`·`ALB_TARGET_GROUP`이 `asset_inventory_005`로 들어왔다(**#271** / PR #314). **§대조 8번을 닫은 축이다** |
| `RelationType` | **6종 전부**가 골든에서 **파생**되고 **끊긴 엣지 0**이다 — 토폴로지가 그릴 엣지. 관계는 입력에 적는 값이 아니라 `collector.persist_inventory`가 **같은 인벤토리 파일 안의 참조**에서 파생시키므로 짝이 한 파일에 있어야 선다 |

**화면의 배지·사유 분기는 골든만으로 전부 눌러 볼 수 있다.** 마지막까지 비어 있던 `SKIP_UNSUPPORTED_STATE`는 판정 규칙 확정(#276 / PR #284) 뒤 정답지에 편입돼 예외 목록(`UNCOVERED_SKIP_REASONS`)이 비워졌다 — 그 목록이 실제로 "낡았다"고 먼저 실패해 **이 표를 고치라고 알렸다.** 게다가 이 값들은 임의로 적은 것이 아니라 `tests/test_golden_dataset.py`가 임계값 드리프트까지 지키는 정답지에서 나온다 — 판정 규칙이 바뀌면 화면에 앞서 테스트가 먼저 깨진다.

FE는 `NEXT_PUBLIC_API_BASE_URL`이 가리키는 백엔드만 본다(`apps/web/src/lib/api/client.ts` `apiBaseUrl()` — 미설정 시 `http://localhost:8000` = compose `api` 서비스(core-api 앱)). **FE mock 계층은 없다**(PR #351) — 백엔드가 떠 있지 않으면 화면은 조회 오류다. 기동 순서는 루트 `README.md` §로컬 실행이다.

경계가 깨지지 않는지는 `apps/core-api/tests/test_golden_assets_api.py` **6건**이 CI에서 지킨다(판정 축 3 + 토폴로지 축 3 — 자산 유형·관계 유형·끊긴 엣지).

~~**🔶 아직 mock이 필요한 것**~~ → **없다**(2026-09-17 · PR #351 FE mock 계층 제거)
- ~~**2단계 이후**(Incident 카드·AI 3줄·추천)는 그대로 mock이다~~ ✅ 실 API다 — Incident를 만드는 판정→Intake 배선이 섰다(#306 / PR #320 · §대조 필요 9번 해소).
- ~~**토폴로지 뷰**는 골든으로 못 채운다 — §대조 필요 8번~~ ✅ **해소**(2026-09-08 · #271 / PR #314). 자산 유형 **7/7종** · `RelationType` **6/6종** · 끊긴 엣지 **0** — 노드와 엣지 모두 골든에서 온다. 화면 조건은 위와 같다(백엔드 기동).

---

## T2 · SecOps — 위협 차단과 원클릭 해제

**한 줄**: 한 IP가 SSH를 두드려대는 걸 잡아 그 주소만 핀셋으로 막고, 관제자가 확인한 뒤 한 번 클릭으로 되돌린다.

**입력**: Golden `secops/input/evt_ssh_bruteforce_001.json` **S3**
`SSH_BRUTE_FORCE` · `source_ip 203.0.113.10` · `120회 / 300초` · 대상 `i-0a1b2c3d4e5f00001`

**입력 선택 근거**: `RUNBOOK_NACL_ADD_DENY`의 `cidr_block`은 *"차단할 악성 IP 대역"* 이고, 명세서 `[SecOps-02]` 안전장치가 **"특정 IP/32 단일 주소만 핀셋 지정"** 을 요구한다. S3의 `source_ip`는 /32 단일 주소라 그대로 들어간다. `parameters_schema`에 포트 필드가 없어 "22번만 골라 막기"는 불가능하다.

> `evt_open_ip_001.json`(S1)을 쓰면 `cidr_block`이 `0.0.0.0/0`이 되어 **서브넷 인바운드가 전면 차단**된다. 명세서의 트리거 조건도 *"특정 IP의 반복적 브루트포스 공격 감지"* 로 OPEN_IP 설정 오류가 아니다.

**자산 조인**: 골든 기준으로 S3의 `target_arn`은 **T1이 쓰는 A1과 같은 인스턴스**다(흐름 테스트가 이 조인을 쓴다). **10/1(목) 시연(LocalStack 시드)에서는 두 트랙의 대상이 다르다** — T1은 `vigilantis-seed-idle-dev`, T2는 `vigilantis-seed-idle`이다(§10/1 컷 시트).

### 단계

| # | 단계 | 화면(FE) | API | WS 이벤트 | 실패 시 대체 컷 |
| --- | --- | --- | --- | --- | --- |
| 1 | 위협 주입 | INC-001에 **SecOps 카드** 신규 · 토폴로지에서 대상 옆 전체개방 SG가 **빨강**(주입 전부터 — §10/1 컷 시트) | (mock 주입) | `INCIDENT_CREATED` | 토폴로지 정적 이미지 |
| 2 | 위험도 판정 | 위험도 배지 | `GET /api/v1/incidents/{id}`<br>*(배지는 실 판정값 — 위협 접수가 `evaluate_threat()`를 부른다 · #322 · 아래 관통 실측 `HIGH`)* | `INCIDENT_UPDATED` | — |
| 3 | 대응 경로 진입 | "선제 차단" 경로 표시 | `response_mode: PRE_MITIGATION_0_5S`<br>*(Incident 축 — 실행 축 아님)* | `INCIDENT_UPDATED` | 경로 표시 없이 4번으로 |
| 4 | 가드레일 4단계 | — (화면 표시 없음) | (내부) | — | 슬라이드 컷으로 분리 |
| 5 | **관제자 승인 → 차단** | **[조치 실행]** 클릭 | `RUNBOOK_NACL_ADD_DENY`<br>`trigger_source: USER_APPROVAL`<br>`approval_mode: HUMAN_ONLY`<br>`ec2.create_network_acl_entry` | `EXECUTION_UPDATED` `SUCCESS` | — |
| 6 | 관제자 확인 | 상세에서 **판단 근거** 확인 | `GET /api/v1/incidents/{id}` | — | — |
| 7 | **원클릭 해제** | **[해제]** 클릭 | `RUNBOOK_NACL_RESTORE`<br>`trigger_source: USER_APPROVAL` | `EXECUTION_UPDATED` | **핵심 컷** |
| 8 | 해제 완료 | 해제 실행 **성공** · Incident **종료 판단 대기**(토폴로지 색은 바뀌지 않는다 — §10/1 컷 시트) | `ec2.delete_network_acl_entry` | `EXECUTION_UPDATED` `SUCCESS` · `INCIDENT_UPDATED` Incident `AWAITING_CLOSURE` | — |

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

> 두 런북은 9/11 내부 P0 게이트의 P0 4종에 포함된다. 여기서 어긋나면 **T2 시연 경로가 통째로 막힌다.** 실 AWS 스모크(9주차 10/02–10/08) 최우선 확인 대상이다.

### 관통 실측 — 2026-09-15 (SSOT 6주차 판정 기준 ⓔ)

**위협 관측부터 해제까지 LocalStack에서 끝까지 갔고, 사람 조작은 [조치 실행] 2회뿐이었다.** 입력은 S3이고, 대상만 시드 인스턴스 `vigilantis-seed-idle`로 바꿨다(`--target-arn`) — 그 서브넷에 시드 NACL이 연결돼 있어 `PROTECTED_BY` 관계가 선다.

| # | 단계 | 누가 부르나 | 결과 |
| --- | --- | --- | --- |
| 1 | 관측 → 접수 | `scripts/inject_mock_threat.py --prepare-inbox` → `MockThreatConsumer`(#322) | SECOPS Incident `ANALYZING` · 초기 `HIGH` · `PRE_MITIGATION_0_5S` · `INCIDENT_CREATED` |
| 2–4 | AI 분석 → 계약 검증 → 가드레일 | agent dispatcher의 SecOps 그래프(#323 · 실 모델 호출 3회) | 재평가 `HIGH` · 후보 `RUNBOOK_NACL_ADD_DENY`(`203.0.113.10/32` · tcp · 규칙 100) `EXECUTABLE` · 가드레일 `PASS` → `AWAITING_APPROVAL` |
| 5 | **[조치 실행] ①** | 관제자 | `202` → dispatcher 1주기 안에 차단 `SUCCESS` · NACL에 `100 · 203.0.113.10/32 · 6 · deny` |
| 6 | 해제 후보 | 같은 주기의 해제 제안(#329) | `RUNBOOK_NACL_RESTORE`(규칙 100 · 인바운드) `EXECUTABLE` · 가드레일 `PASS` → `AWAITING_APPROVAL` |
| 7–8 | **[조치 실행] ②** | 관제자 | `202` → 해제 `SUCCESS` · NACL 커스텀 규칙 0건 · Incident `AWAITING_CLOSURE` |

**재는 방식의 한계** — 앱을 띄워 타이머에 맡긴 것이 아니라, 타이머가 부르는 함수(`MockThreatConsumer.consume_once` · agent dispatcher의 건별 처리 · `dispatcher.run_dispatch_cycle`)를 **같은 순서로 직접** 불렀고, HTTP는 같은 라우터를 `TestClient`로 탔다. 확인한 것은 **경로가 끝까지 이어진다**는 것이며, 타이머 기동은 9/11 게이트에서 **앱을 실제로 띄워** 확인했다 — 그 기록이던 게이트 판정서는 **2026-09-16 저장소에서 삭제됐고**(PR #355) 내용은 git 이력에만 있다(`git show 60f329f:docs/E2E_GATE_0911.md` §2-⑥·⑦의 기동 로그). AI 분석은 대상 Incident 1건만 골라 불렀다(수집이 함께 만든 FINOPS Incident는 분석하지 않았다 — 모델 호출 비용). 모델 호출 없이 분석 결과를 주입해 dev `77d5cae`에서 한 번 더 돌렸고 같은 전이가 나왔다.

## 10/1(목) 컷 시트

> **기준**: LocalStack 시드 환경(`scripts/seed_localstack.py`) — 10/1(목) 시연은 LocalStack 기반이다(SSOT §확정 결정 로그 2026-09-14). **측정**: 2026-09-17, 시드 → 수집·판정 1회(`services.scheduler.run_pipeline`, 일회용 DB) — 앱 기동·모델 호출 없이. 실행 단계(T1 5–9번 · T2 4–8번)는 PR #346 실측(T1)과 §T2 관통 실측(2026-09-15)을 근거로 한다. **결정**: PR #371 리뷰(김세혁, 2026-09-17). **담당**: 김승철(#356 ③ · 기한 9/23(수)).
>
> 컷 시트는 단계표를 대신하지 않는다 — **각 칸을 10/1에 실경로로 보여줄 수 있는가, 막히면 무엇을 하는가**만 확정한다.

### 시연 대상 — 테스트 기준(골든)과 다르다

| 트랙 | 시연 대상 (LocalStack 시드) | 실측 판정 | 테스트 기준(흐름 테스트) |
| --- | --- | --- | --- |
| **T1** | `vigilantis-seed-idle-dev` · m5.2xlarge · Environment `development` | **최적화 후보**(`COST_CANDIDATE`) — 시드 전체에서 이 한 대뿐 | 골든 A1(`t3.xlarge`) |
| **T2** | `vigilantis-seed-idle` · t3.xlarge · Environment `production` — `scripts/inject_mock_threat.py --prepare-inbox --target-arn`으로 지정 | 판정은 운영 보호(`SKIP_PROD_PROTECTED`). 위협 접수는 판정과 무관하게 Incident를 만든다 | 골든 S3 대상(= A1) |

**골든 인스턴스는 LocalStack에 없어 실행 단계가 성립하지 않는다**(골든 계정 `123456789012` · LocalStack `000000000000`) — 그래서 시연은 시드를 쓴다. 두 트랙의 대상이 달라 **"이 서버가 아까 그 서버"는 시연에서 쓰지 않는다.** T2를 `seed-idle`로 둔 이유는 ① §T2 관통 실측(2026-09-15)이 그 대상으로 끝까지 갔고 ② 옆 SG `vigilantis-seed-open-ssh`가 전체개방 **위협**(빨강)이라 "보안 사건이 난 서버"로 화면에서 짚을 수 있기 때문이다. 네 시드 인스턴스는 같은 서브넷이라 모두 시드 NACL(`vigilantis-seed-nacl`)에 걸린다.

### 사전 준비 — 무대 전

**T1 1–3번은 무대에서 하지 않는다**(결정 ①). 스캔 잡은 `IntervalTrigger`로만 등록돼 **기동 뒤 한 주기(300초)가 지나야 첫 스캔**이 돌고, 그 뒤 AI 분석(모델 호출)이 이어진다. 무대 밖에서 끝내 두면 실패가 무대 전에 드러나 고칠 시간이 있다. 잃는 것은 "스캔이 카드를 만드는 순간"의 실시간 장면이며, 그 장면은 T2 1번 주입이 보여준다.

`docker compose up -d --wait db localstack` → 시드(`scripts/seed_localstack.py`) → `docker compose up -d api` → **첫 스캔과 AI 분석 완료 확인** — `GET /api/v1/incidents`에서 idle-dev 카드가 **승인 대기**(`AWAITING_APPROVAL`)인지 본다.

같은 스캔이 **카드 3건**을 만든다(idle-dev · 미사용 SG `vigilantis-seed-unused` · 미연결 EBS). 나머지 둘은 시드에서 빼지 않는다 — LocalStack 사전 검증 테스트(`apps/core-api/services/tests/test_precheck_localstack.py`)가 그 자원을 쓴다. **대본에서 한 줄로 설명한다**: "스캔이 서버 1대 말고도 미사용 자원 2건을 함께 찾았다." EBS 카드는 이름 없이 리소스 ID(`vol-` 접두)로 보인다(수집이 EBS 이름을 비워 둔다). 모델 호출은 사전 준비 1회에 3건으로 고정된다.

### 시연 환경변수 (결정 ② · 운영 머신 `.env`)

| 변수 | 값 | 이유 |
| --- | --- | --- |
| `SCAN_INTERVAL_SECONDS` | **300**(기본) | 첫 스캔이 무대 밖으로 나갔다. 짧게 두면 무대 도중 스캔이 자산 정보를 중간값으로 바꿀 수 있다 |
| `DISPATCH_INTERVAL_SECONDS` | **5** | T1-7 실패 주입 창의 길이다. 리허설에서 헬퍼가 3회 연속 창 안에 들면 확정, 한 번이라도 놓치면 10 |
| `AGENT_DISPATCH_INTERVAL_SECONDS` | **3** | T2 주입 뒤 분석 시작까지의 지연 |
| `STATUS_CHECK_WAIT_DELAY_SECONDS` · `STATUS_CHECK_WAIT_MAX_ATTEMPTS` | **2 · 3** | PR #346에서 이 값으로 판정이 4.2초에 났다. **LocalStack 전용** — 실 AWS 스모크(9주차) 전에 기본값으로 되돌린다 |
| `MOCK_THREAT_INBOX_DIR` | `/app/apps/core-api/.mock-threat-inbox` | `.env.example` 값 · compose 마운트 안 경로(호스트는 `apps/core-api/.mock-threat-inbox`) |
| `OPENAI_API_KEY` | 운영 머신 키 | 값은 적지 않는다 |

### T1 · FinOps — 무대는 5번부터

| # | 단계 | 10/1 판정 | 조작·전제 | 막히면 |
| --- | --- | --- | --- | --- |
| 1 | 수집·판정 | ✅ 실경로 · **사전 준비(무대 전)** — 실측 최적화 후보 | §사전 준비 | 사전 준비를 처음부터 |
| 2 | Incident 생성 | ✅ 실경로 · **사전 준비(무대 전)** — 스캔 1회로 생성 실측 | 카드 3건 — §사전 준비 | **무대 전에 드러난다** → 사전 준비를 처음부터(대체 컷은 만들지 않는다 — 결정 ①) |
| 3 | AI 판단 근거 + 추천 | 🔶 실경로 · **사전 준비(무대 전)** · idle-dev 모델 호출 미측정(§결정 기록 ④) | `OPENAI_API_KEY` · `AGENT_DISPATCH_INTERVAL_SECONDS` | 사전 준비를 처음부터 |
| 4 | 가드레일 4단계 | ✅ 실경로 | — | 슬라이드 컷 |
| 5 | 관제자 승인 | ✅ 실경로 · **무대 시작** | 승인 대기 카드 → **[조치 실행]** 1회 | — |
| 6 | 실행 | ✅ 실경로(PR #346) | `DISPATCH_INTERVAL_SECONDS` | **사전 준비를 처음부터** — LocalStack 재기동은 자원 ID가 새로 생겨 이 Incident의 대상이 사라진다 |
| 7 | Status Check 실패 | 🔶 **실경로 · 헬퍼 머지 전까지** | **실패 주입 헬퍼 실행**(#356 ① · 김세혁 · 리허설 9/29(화) 전) — 대상 인스턴스를 조회하다가 유형이 바뀌고 `running`이 된 순간 `stop_instances`를 부른다. 사람이 창을 맞출 수 없다: 판정 대기 동안 실행 상태는 `IN_PROGRESS`이고 이벤트도 나가지 않는다 | 창을 놓치면 실행이 `SUCCESS`로 닫힌다 → 상세 화면 실행 항목의 **[이전 스펙 복원]**(`RUNBOOK_EC2_REVERT_SIZE` · 관제자 승인)으로 되돌린다. 자동 발동 장면은 빠지지만 같은 확인 화면(9번)까지 간다. **시드 재실행은 줄어든 유형을 되돌리지 않는다**(이름으로 찾아 건너뛴다) |
| 8 | 자동 원복 발동 | ✅ 실경로(PR #346) | 사람 조작 없음 | 7번 "막히면"과 같다 |
| 9 | 원복 완료 | ✅ 실경로 · **무대 끝** · 확인 화면을 바꾼다 | **AST-001의 인스턴스 유형은 수집만 갱신한다**(실행 경로는 자산 정보를 바꾸지 않는다) — 축소 전과 원복 뒤가 같은 값으로 보여 복귀를 증명하지 못한다. **실행 상태 패널의 "이전 상태로 복구했습니다."(원본 `ROLLED_BACK` — `apps/web/src/components/incidents/execution-status-panel.tsx`)로 확인**한다. 화면은 실행 단계(정지 → 유형 변경 → 기동)를 표시하지 않는다(API 계약에 단계 목록이 없다) | Incident는 **종료 판단 대기**로 남긴다 — **[종료 판단]은 무대에서 누르지 않는다**(§반복). 누르면 다음 스캔이 같은 서버에 새 카드를 만들고 모델까지 불러 T2 도중에 카드가 뜰 수 있다 |

### T2 · SecOps

| # | 단계 | 10/1 판정 | 조작·전제 | 막히면 |
| --- | --- | --- | --- | --- |
| 1 | 위협 주입 | ✅ 실경로(§T2 관통 실측) · **"붉은 노드" 문구를 바꾼다** | `MOCK_THREAT_INBOX_DIR` · 주입 명령에 `--target-arn`(seed-idle). **토폴로지 색은 자산 판정에서 온다** — 빨강은 대상 옆 SG이고 **주입 전부터** 빨갛다. 주입이 새로 만드는 것은 INC-001의 SecOps 카드다 | 토폴로지 정적 이미지 |
| 2 | 위험도 판정 | ✅ 실경로 — 초기 `HIGH` 실측 | — | — |
| 3 | 대응 경로 진입 | ✅ 실경로 — `PRE_MITIGATION_0_5S` 실측 · INC-001 카드의 **선제 차단됨** 배지로 표시(`apps/web/src/components/incidents/incident-card.tsx`) | — | 경로 표시 없이 4번으로 |
| 4 | 가드레일 4단계 | ✅ 실경로 | 실 모델 호출(SecOps 그래프) | 슬라이드 컷 |
| 5 | 관제자 승인 → 차단 | ✅ 실경로 | **[조치 실행]** ① · 시드 NACL 규칙 100 `203.0.113.10/32` | — |
| 6 | 관제자 확인 | ✅ 실경로 | — | — |
| 7 | 원클릭 해제 | ✅ 실경로 · 해제 후보는 차단이 닫힌 주기에 선다(#329) | **[조치 실행]** ② | 핵심 컷 |
| 8 | 해제 완료 | ✅ 실경로 · **"노드 정상 복귀" 문구를 바꾼다** | 확인할 것은 해제 **성공** · 규칙 0건 · Incident **종료 판단 대기**. **SG는 해제 뒤에도 빨강**이다(색은 판정에서 오고 해제는 판정을 바꾸지 않는다). 질문 대비 한 줄: "NACL 차단은 공격 IP 대응이고, SG 전체개방은 따로 남은 설정 오류다" | — |

### 공통

| 항목 | 10/1 판정 | 막히면 |
| --- | --- | --- |
| WS 실시간 갱신(§대조 6번) | 🔶 **실배달 미확인** — 7주차(9/21(월)–9/23(수)) FE 확인 | 화면 새로고침 |
| 조치별 절감 예상(#347 · PR #361) | **9/23(수) 확정본에는 넣지 않는다** — FE 표기가 아직 없다. 9/28(월) 컷에 FE 표기가 있으면 T1-5 승인 화면에 한 줄 추가 | — |

### 반복 — Connect Day 하루 2세션

10/1(목)은 하루 2세션이라 시연을 두 번 돌린다(SSOT §현재 위치 발표 일정 표). 세션 사이 초기화에서 셋을 한다.

| 무엇 | 왜 · 어떻게 |
| --- | --- |
| **T1 Incident [종료 판단]** | 열린 Incident(종료 판단 대기 포함)가 있으면 스캔이 같은 서버에 새 카드를 만들지 않는다(`apps/core-api/incident_intake.py` `_create_finops`). 닫은 뒤 다음 스캔(최대 300초) → AI 분석 → 승인 대기 확인 = §사전 준비를 다시 하는 셈이다 |
| **T2 재주입 시각** | 같은 관측 재전달은 멱등이라 새 Incident가 생기지 않는다. 주입 명령에 **`--occurred-at <새 ISO 시각>`**을 붙인다(`scripts/inject_mock_threat.py`) |
| **시드 NACL 규칙** | 시드 재실행이 커스텀 규칙을 비운다 — 차단 슬롯을 다시 쓸 수 있다 |

### 결정 기록과 남은 것

| # | 항목 | 상태 |
| --- | --- | --- |
| ① | T1-2 대체 컷 | **결정 2026-09-17 · PR #371 리뷰** — 새로 만들지 않고 T1 1–3번을 사전 준비로 옮긴다 |
| ② | 시연 환경변수 · T1 실패 주입 방식 | **결정 2026-09-17 · PR #371 리뷰** — §시연 환경변수 · 주입은 헬퍼(#356 ①) |
| ③ | 스캔이 함께 만드는 Incident 2건 | **결정 2026-09-17** — 시드에서 빼지 않고 대본에서 한 줄 설명(§사전 준비) |
| ④ | **T1-3 idle-dev 모델 호출 실측** — 절감 추정 호출(#347)이 함께 붙는다 | **남음** · 김승철 |
| ⑤ | **T1-7 헬퍼** — 머지 후 T1-7 🔶 해소, 리허설에서 디스패치 5초 확정 | **남음** · 김세혁(#356 ①) |
| ⑥ | **공격 경로 표시(#362)** — 완성되면 T2-1·T2-8 대본을 되돌릴지 | **남음** · 9/28(월) 릴리스 컷(#349). 9/23(수) 확정은 지금 화면(공격 경로 없음) 기준 |

**재현**: `docker compose up -d --wait db localstack` → `AWS_ENDPOINT_URL=http://localhost:4566 uv run python scripts/seed_localstack.py` → 일회용 DB에 `alembic upgrade head` → `run_pipeline()` 1회. 판정 분포는 적지 않는다(시드가 바뀌면 낡는다) — 이 시트가 기대는 것은 두 대상의 판정과 SG 연결뿐이다.

## 1차 시연에서 빼는 것과 그 이유

| 항목 | 빼는 이유 |
| --- | --- |
| `RUNBOOK_EC2_ISOLATE` / `UNISOLATE` (P2) | `elbv2`가 LocalStack Community에 없어 **조회 대체조차 로컬에서 안 돈다**(ADR-0007). 실 AWS 인프라가 선행 조건 |
| `RUNBOOK_EC2_ENABLE_AUTOSCALING` (P2) | `autoscaling` 동일. 구현량도 최대 |
| `TIMEOUT_ISOLATION_1M` | 위 `ISOLATE`에 의존한다. 1분을 실시간으로 기다리는 것도 시연에 부적합 |
| `RUNBOOK_SG_DELETE_ISOLATED` / `SG_RECREATE` (P1) | 두 트랙이 이미 양방향 회복을 각각 보여준다. 세 번째는 중복 |
| `RUNBOOK_EBS_DELETE_UNATTACHED` (P1) | 입력 스키마에 `ebs_volumes`가 아직 없다 |

**2차 설계서 대상**: 실 AWS 전환(9주차 10/02–10/08) 후 P2 트랙 추가 여부를 다시 판단한다 — P2 3종의 실 AWS 첫 검증은 10주차(10/12–10/15)다.

---

## 시연 선행 조건 — 화면 (PR #148 리뷰: @yoogh3546)

두 트랙의 **시작·종료 컷**에 필요한 화면은 **둘 다 확보됐다**(2026-08-26 · 2026-08-31). 데이터도 실 API로 온다 — 판정→Intake 배선(§대조 필요 9번)이 섰고 FE mock 계층은 없다(PR #351).

| 컷 | 필요한 화면 | 현재 상태 |
| --- | --- | --- |
| T1-2 Incident 카드 | **INC-001** 카드 그리드 | ✅ **확보**(2026-08-26, #167 / PR #171 — 카드 그리드·위험도 정렬·승인 대기 프리셋). 목록에서 조치 실행·ACT-002 딥링크까지 연결됨(#179 / PR #180) |
| T2-1 · T2-8 붉은 노드 | **AST-001 토폴로지 뷰**(#146) 또는 **DSH-001** 통합 위협 토폴로지 | ✅ **화면은 확보**(2026-08-31 · #146 CLOSED) — `AssetGraph`가 `GET /api/v1/assets` 응답을 그대로 그린다(`apps/web/src/components/assets/assets-view.tsx:189`). 노드 테두리 색은 **Incident가 아니라 자산 판정(`verdict`)** 에서 온다 — `THREAT`만 빨강이고(`apps/web/src/components/assets/asset-graph.tsx` `VERDICT_BORDER`), `THREAT`는 SG 전체개방에서만 나온다(`services/rule_engine.py`). DSH-001 토폴로지도 같은 `AssetGraph`를 쓴다(#294 / PR #351) |

**붉은 노드 컷에 mock 기준은 없다**(PR #351). **다만 골든 적재 기준으로 T2-1·T2-8의 "붉은 노드"는 S3 대상 인스턴스에 서지 않는다.** S3 대상 `i-0a1b2c3d4e5f00001`은 골든 A1이라 판정이 `COST_CANDIDATE` — 테두리는 **주황**이다. 빨강은 그 인스턴스와 `SECURED_BY`로 이어진 SG `sg-0a1b2c3d4e5f00005`(`golden-sg-open-ssh` · A5 · 22번 전체개방 `THREAT`)다. 두 색 모두 **적재할 때 정해지고** 위협 주입이나 NACL 해제로 바뀌지 않는다 — 위협 접수가 쓰는 것은 `ThreatEvent`·`Incident`뿐이다. 그래서 T2-1("주입 → 붉은 노드")은 **주입 전부터 옆 SG가 빨강**이고, T2-8("해제 → 정상 복귀")은 **해제 뒤에도 SG가 빨강**이다. LocalStack 시드 경로(`vigilantis-seed-idle`)의 색은 재지 않았다. 대본을 어떻게 바꿀지는 10/1(목) 컷 시트 확정(9/23(수))에서 판단한다.

**어느 경로로 채우느냐에 따라 갈린다.**

| 경로 | `MEMBER_OF`·`USES`·`REGISTERED_IN` |
| --- | --- |
| **골든 적재** — `scripts/load_golden_assets.py` → `collector.persist_inventory` | ✅ **로컬에서 선다.** 이 경로는 AWS를 **부르지 않고** 골든 JSON을 그대로 파싱하므로 LocalStack의 한계를 타지 않는다. `RelationType` **6종 전부** 파생(#271 / PR #314 · CI 등식 가드) |
| **AWS 수집** — `collector.collect_region` | 🔶 **실 AWS 스모크(9주차 10/02–10/08) 전까지 안 채워진다.** `autoscaling`·`elbv2`가 LocalStack Community에 없어 collector가 호출 실패를 흡수해 degrade 한다(`PARTIAL` 표면화) |

두 경로 공통으로, #149 완료 뒤 자산 4종(EBS·ASG·Launch Template·ALB TG)과 `RelationType` 6종이 **코드상** 전부 산출된다(PR #156·#161·#165). **종전 서술은 이 둘을 가르지 않아 골든 경로까지 막힌 것처럼 읽혔다.**

## 대조 필요 목록 (🔶)

확정본 확보 또는 구현 완료 시 이 절을 먼저 갱신한다.

| # | 항목 | 막힌 이유 | 풀리는 시점 |
| --- | --- | --- | --- |
| 1 | ~~T2 2번 위험도 판정값(`initial_risk_level`)~~ ✅ **해소**(#322 — `threat_ingress.py`가 `evaluate_threat()`를 부른다) | **판정 규칙과 `RiskReasonCode` 6종은 확정**(#210 / PR #206 — `packages/schemas/events.py`, `apps/core-api/security/risk_evaluator.py::evaluate_threat`). **② 정답지는 해소됐다**(2026-09-01) — `datasets/golden/secops/expected/`에 입력 12건과 1:1로 대응하는 정답 12건이 있다(PR #223 10건 · PR #242 SSH MEDIUM 밴드 2건). **① 배선도 해소됐다** — `threat_ingress.py`가 `evaluate_threat()`를 부른다(#322). 관통 실측의 초기 위험도는 `HIGH`다(§T2 관통 실측) | — |
| 2 | ~~런북별 세부 실행 단계·`parameters_schema`~~ ✅ 해소(2026-08-31) | 확정본이 SSOT §Action Whitelist로 이관되고, `parameters_schema`는 `packages/schemas/runbook_parameters.py`(#154 / PR #178), 세부 실행 단계·`target_api`는 [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §Context·§5가 갖는다 | — |
| 3 | Status Check 실패 **주입 방법** — **ⓑ 해소(2026-09-15)** · ⓐ만 남음 | **판정기(`wait_for_status_check()` 3분기)와 자동 원복 엔진은 둘 다 섰다**(아래 3-B). 막힌 것은 주입이었고 분기가 둘이다. **ⓑ `_NOT_BOOTING_STATES` 분기는 LocalStack에서 선다**(2026-09-15 실측 · [ADR-0006](adr/0006-localstack-team-standard-env.md) §4 2행 6차 개정). 6번 실행 주기가 끝난 뒤 대상 인스턴스를 `stop_instances`로 멈추면 `describe_instance_status(IncludeAllInstances=True)`가 `stopped`를 돌려줘 판정이 `FAILED`("기동 실패 — 인스턴스 상태가 stopped입니다")로 떨어진다 — 사유가 `PRECHECK_TARGET_NOT_FOUND`로 갈리지 않는다. 그 뒤 7·8·9번이 단계표의 상태값 그대로 흐른다(원본 `ROLLBACK_INITIATED` → `REVERT_SIZE` 자식 `AUTO_ON_FAILURE` → 원본 `ROLLED_BACK` · Incident `AWAITING_CLOSURE`). 가짜 AWS의 상태만 바꾸므로 프로덕션 코드에 데모 분기가 없고, 에뮬레이터 동작은 `apps/core-api/services/tests/test_status_check_localstack.py`가 지킨다. **대본이 지킬 조건 둘** — ① 창: 실행 주기 뒤·판정 주기 전(`DISPATCH_INTERVAL_SECONDS`, 기본 10초). 대기 도중에는 틈이 없다 ② 대기: 멈춘 인스턴스는 waiter가 `STATUS_CHECK_WAIT_*` 전량(기본 3분)을 쓴 뒤 `FAILED`가 나므로 시연에서는 조인다. **ⓐ `impaired` 분기만 남는다** — LocalStack에 헬스체크가 없어 만들 수 없다 | **ⓑ 해소 → T1 7·8·9번을 10/1(목) 시연에서 실경로로 세울 수 있다**(판정 기준 ⓑ).<br>**ⓐ 해소 = 9주차(10/02–10/08) 실 AWS 스모크**(ADR-0006 §4, PR #244 본문 — 2026-09-14 일정 재편으로 7주차에서 옮겼다) |
| 3-B | ~~자동 원복 엔진~~ ✅ 해소(2026-09-03) | `RUNBOOK_EC2_REVERT_SIZE` 실행과 `AUTO_ON_FAILURE` 자동 발동이 dev에 들어갔다(#241 / PR #256). 2/2 판정 자체는 #240 / PR #244로 먼저 섰다. **3번을 한 줄로 두면 이 머지가 3번 전체를 해소한 것처럼 읽히므로 갈라 둔다** — 주입 방법은 그대로 남는다 | — |
| 4 | ~~가드레일 ③ 실제 통과~~ ✅ 해소(2026-08-31) | **4단계가 전부 섰다.** ③ ARN Match 구현(#177 / PR #202 — DB 수집 ARN 대조로 Scope Escalation 차단, ① NUL 문자 차단 포함)으로 `tests/test_guardrails.py`의 placeholder skip 1건이 해제됐다. ④ Dry-Run은 `precheck()` 확정 10종 구현 완료(#129 / PR #147 · 실측 #130 / PR #170) | — |
| 5 | 화면 구현 상태 | 아래 표 | 카드별 |
| 6 | **WS 이벤트로 화면이 실시간 갱신되는 것** | FE 연동 구현됨 — 소켓 수명주기·이벤트 3종·Toast·재연결(#168 / PR #181). 로컬 `core-api`로 **연결·중단·자동 복구 확인**. 다만 **이벤트 실배달은 미확인**. 막던 이유였던 "코어 DB가 비었다"는 **자산에 대해서는 풀렸다**(2026-09-02 — `scripts/load_golden_assets.py`로 골든 FinOps 전량 적재). Incident를 만드는 계층(아래 9번)도 섰다 — **남은 것은 이벤트 실배달 확인**이다 | **7주차(9/21(월)–9/23(수)) · FE(김세혁)** — 리허설에서 알면 9/30(수) 시연물 마감 전에 고칠 날이 없다 |
| 7 | ~~T1 5번 `POST /actions/execute` HTTP 상태 코드~~ ✅ 해소(2026-08-27) | 라우터·멱등 처리 구현 완료(#116 / PR #119), 롤백 3종 실행 접수는 #126 / PR #158. **신규 접수 `202 Accepted` · 같은 `idempotency_key` 재요청 `200 OK`** 로 확정돼 SSOT §API 계약에 등재됐다. 남은 것은 `execute` 본체(Boto3 실행·자동 원복 — 김세혁) | — |
| 8 | ~~**토폴로지 뷰를 골든으로 못 채운다**~~ ✅ **해소** (2026-09-02 신설 · 2026-09-04 EBS 편입 · **2026-09-08 해소**) | **노드와 엣지가 모두 골든에서 선다.** 마지막까지 0건이던 NACL·Launch Template·ASG·ALB Target Group이 `asset_inventory_005`로 들어와 **자산 유형 7/7종**이 되고, `RelationType` **6/6종**이 파생되며 **끊긴 엣지 0**이다(**#271** / PR #314). 셋 다 `apps/core-api/tests/test_golden_assets_api.py`가 **CI에서 등식으로** 지킨다 — 계약에 유형·관계가 늘면 여기가 먼저 실패한다. **자산을 넣는 것과 엣지가 서는 것은 다르다**: 관계는 `collector.persist_inventory`가 **같은 인벤토리 파일 안의 참조**에서만 파생시켜서, 골든에 EBS가 들어온 뒤에도(#264 · #276) `ATTACHED_TO`는 **한 번도 파생된 적이 없었다** — 004의 볼륨이 부착 대상으로 적은 EC2가 골든 어디에도 없었기 때문이다. 아무도 세지 않아 아무도 몰랐고, 그래서 등식 가드를 함께 세웠다. **화면에 띄우는 조건은 백엔드 기동 하나다** — FE mock 계층이 없어(PR #351) 화면은 `NEXT_PUBLIC_API_BASE_URL`(미설정 시 `http://localhost:8000`)의 이 데이터만 본다(위 T1 §자산 화면과 같은 축) | — |
| 9 | ~~**판정→Intake 배선이 없다**~~ ✅ **해소**(2026-09-10 · #306 / PR #320) | `services/scheduler.py` `run_pipeline()`이 판정 뒤 `create_incident_from_intake`를 부르고(FINOPS), 위협 접수(`threat_ingress.py` · #322)가 같은 진입점을 부른다(SECOPS). 두 트랙의 Incident가 실경로로 생긴다. 종전 서술(*"호출부가 `tests/` 밖에 0건"*)은 git 이력에 있다 | — |

**문서의 WS 이벤트 열은 "서버가 그 시점에 보내는 이벤트"로는 정확하다.** 다만 그 이벤트로 화면이 실시간으로 바뀌는 것을 시연하려면 6번이 필요하다.

### 5번 상세 — 화면 카드별 상태 (2026-08-27 기준)

| 화면 | 상태 |
| --- | --- |
| AST-001 · AST-002 | ✅ 완료(PR #137 — 카드 그리드·상세 Drawer). **토폴로지 뷰도 완료** — PR #137 리뷰 대응으로 #146에 분리됐다가 2026-08-31에 닫혔다 |
| INC-002 A 변형 | ✅ 완료(#138 / PR #145) |
| INC-002 B 변형 | ✅ 완료(#155 / PR #162 — 위험도·`response_mode`·수행된 조치). **초 단위 카운트다운은 제거 대상**(2026-08-27 결정 — SSOT §확정 결정 로그, 표기는 `제안 생성 시간`·`실행 예정 시간` 2종) |
| ACT-001 · ACT-002 | ✅ 완료(#166 / PR #169 — 실행 확인 모달·실행 상태 인라인). 승인 화면의 조치 대상 문맥은 **#183 후속**(#154로 `display_parameters`가 서버 파생이 되며 mock 5건이 갈림) |
| INC-001 | ✅ 완료(#167 / PR #171) · INC-001 → ACT-001 연결(#179 / PR #180) |
| CMN-001 실시간 | ✅ 연동 완료(#168 / PR #181). **이벤트 실배달만 미확인** — 위 대조 6번 |

## 테스트 대응 — `tests/test_e2e_scenario.py` · `apps/core-api/tests/test_e2e_flow.py`

시나리오 회귀는 **두 층**이고(#219), 2026-09-17부터 **두 파일**에 나뉜다 — ② 흐름은 DB 픽스처가 있는 `apps/core-api/tests/`로 옮겼다(PR #354 확정 · PR #358).

| 층 | 무엇 | 상태 |
| --- | --- | --- |
| ① **시연 전제 대조 11건** — `tests/test_e2e_scenario.py` | 이 설계서가 인용한 값(입력 수치·런북 짝·계약 제약)을 원천과 결속한다. 파이프라인 없이 돈다 | ✅ 실행 중 |
| ② **전 구간 흐름 2건** — `apps/core-api/tests/test_e2e_flow.py` | 아래 표. 프로덕션 진입점만 불러 상태 전이를 잇는다(AWS는 가짜) | ✅ 실행 중(PR #358 · 2026-09-17 머지 · 판정 기준 ⓐ) |

①이 있는 이유는 이 문서가 인용한 값들이 **지금은 참이지만 아무도 지켜보지 않는 주장**이기 때문이다. 골든 숫자 하나, 런북 분류 하나가 바뀌면 대본이 조용히 틀어지고 게이트 당일에야 드러난다.

②의 2건이 본 설계서의 어느 범위를 검증하는지 고정한다.

| 테스트 | 대응 트랙 | 검증 범위 | 선행(전부 해소) |
| --- | --- | --- | --- |
| `test_t1_idle_ec2_downsize_and_auto_rollback_flow` | **T1** | Golden **A1** → `COST_CANDIDATE` → 가드레일 → 실행 접수 → Status Check 실패 → 원복 자식이 정지·타입 복원·기동을 실제로 적용 → `ROLLED_BACK` · Incident `AWAITING_CLOSURE` | 대조 3번 — 실패 주입 방법 ✅ **2026-09-15 해소**(`stop_instances` 주입 · 자동 원복은 2026-09-03 해소) · 9번(#306) |
| `test_t2_ssh_bruteforce_block_and_one_click_release_flow` | **T2** | Golden **S3** → Incident → 승인 → `NACL_ADD_DENY`(`USER_APPROVAL`) · 규칙 1건 → 해제 후보(#329) → 원클릭 해제 → `NACL_RESTORE` · 규칙 0건 | 대조 1번(#322) · 9번(#306) |

**두 테스트 모두 Golden Dataset을 입력으로 쓴다**(PR #365부터 파일에서 직접 읽는다 — T1은 정답지의 `case_id: A1`로 대상을 찾아 입력 인벤토리의 값을, T2는 `evt_ssh_bruteforce_001.json`을). 시연에 쓰는 데이터와 테스트에 쓰는 데이터가 같아야 "시연이 되면 테스트도 된다"가 성립한다 — 골든이 바뀌면 흐름 테스트도 따라간다.

~~**테스트 이름과 입력이 어긋난다**~~ ✅ 해소(2026-08-31, #219) — 옛 이름 `test_open_ssh_ip_block_flow`가 `OPEN_IP`를 가리키는데 입력은 `SSH_BRUTE_FORCE`(S3)였다. 1주차에 지은 이름이고 T2 입력이 PR #148 리뷰로 바뀐 결과다. 당시 skip 해제를 기다리지 않고 위 표의 이름으로 함께 고쳤다 — **이 표가 명세인 이상 이름이 어긋난 채로 두면 문서가 없는 테스트를 가리킨다.**

실행 계열 공통 fixture는 **#136**에서 선구축한다. 그 픽스처가 P2 3종의 로컬 FAIL을 `GuardrailValidationContext` 문맥별로 표현해야 한다는 전제도 같은 이슈에 적었다.

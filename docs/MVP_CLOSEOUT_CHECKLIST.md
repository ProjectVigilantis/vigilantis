# MVP 마감 판정 체크리스트 (10/15(목)) — 초안

> **담당**: 김승철 (QA & Scenario) — 이슈 [#422](https://github.com/ProjectVigilantis/vigilantis/issues/422)
> **판정 주체**: 김세혁(PM). 판정 기록은 SSOT [PROJECT_STATUS.md](PROJECT_STATUS.md)에 PM이 남긴다.
>
> **이 문서는 판정 기준을 정의하지 않는다.** 10/15(목) 판정 기준은 **10주차 전환 갱신(실제 2026-10-13(화) · 계획 10/12(월))에서 PM이 확정했으며**, 이 문서는 **그 기준에 답할 때 볼 칸과 근거의 자리**다. 확정 전 초안을 확정 정책으로 취급하지 않는다.
> 빈칸·밑줄·미선택 체크박스는 **미확인**이다. 채울 때는 결과 또는 미확인 사유를 적는다.

---

## 0. 판정 대상의 원천

| 항목 | 원천 | 상태 |
| --- | --- | --- |
| 10/15(목)이 무엇을 묻는 날인가 | SSOT §현재 위치 §발표 일정 표 10/15 행 — "실 AWS 스모크 완료 + P2 3종 실 AWS 첫 검증까지. 발표 자리가 아니라 **내부 완료 판정일**" | 확정 |
| 판정 기준 ⓐⓑⓒⓓ | SSOT 10주차 전환 갱신(**2026-10-13(화)** · PR #461) — "10주차 판정 기준 = MVP 마감 판정 기준" | **확정됨** |
| MVP 범위 | SSOT §MVP 확정 범위 · §Action Whitelist 10종 표 | 확정 |
| 스모크 관측 기록 | [AWS_SMOKE_RESULT.md](AWS_SMOKE_RESULT.md) | 양식 신설 완료 · 값 미기록 |
| 이월 목록 | [ADR-0006](adr/0006-localstack-team-standard-env.md) §4 (8행) | 처분 미기록 |
| 조회 대체 통과 조건 | [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §4 — 3행이 "잠정안" | **확정 대기** |

> 주차 구간은 평일만 센다(SSOT). 10주차는 10/12(월)–10/15(목) 실작업 4일이고, 앞의 한글날 10/9(금)은 구간 밖이다.

---

## 1. A군 — 실 AWS 스모크 완료 (**10주차**)

근거는 모두 `AWS_SMOKE_RESULT.md`의 칸이다. 이 표에 결과를 복제하지 않고 **어느 칸이 답했는지** 가리킨다.

| # | 확인할 것 | 근거 칸 | 충족 | 비고 |
| --- | --- | --- | --- | --- |
| A1 | 환경 기동 · TG 대상 `healthy` · Budget 알림(월 $300 · 크레딧 제외) | §1 | ☐ | |
| A2 | 앱 키 전환 · Arn `user/vigilantis-smoke-app` · `AWS_REGIONS` 빈 값 | §1 | ☐ | |
| A3 | 앱 키 스캔 — 회차 상태와 `collector_failures` 사유 코드 | §2-1 · §2-2 | ☐ | `PARTIAL`이면 `AccessDenied` 여부가 갈림 |
| A4 | 가드레일 ④ `DryRun=True` 런북별 1회 | §3 | ☐ | |
| A5 | `DryRun`의 대상 존재 검사 | §3 A5 | ☐ | 미측정 항목 — 결과 기록 자체가 요건 |
| A6 | `NACL_ADD_DENY` → `NACL_RESTORE` 실동작 · 저장값 | §4 A6 | ☐ | |
| A7 | `RIGHTSIZING` → `REVERT_SIZE` 실동작 | §4 A7 | ☐ | **48 관측치 게이트 뒤에만 가능** |
| A8 | Status Check 실 대기 + `stopped` → 자동 원복 | §4 A8 | ☐ | |
| A8-impaired | `impaired` 검사 결과 경로 | §4 A8-impaired | ☐ | `stopped` 결과로 대체하지 않는다 |
| A9 | `SKIP_PROD_PROTECTED`(`web-1`·`web-2`) | §2-3 | ☐ | **48 관측치 게이트 뒤에만 가능** |
| A10 | P1 3종 실동작 (선택) | §4 A10 | ☐ | 선택 항목 — 미실행이 미달은 아니다 |
| A11 | 스모크 결과 기록 — **결과가 채워진** `AWS_SMOKE_RESULT.md` 가 `dev` 에 들어온다(#439 머지) | 그 문서 전체 | ☐ | **양식은 10/6(화) PR #419 로 이미 `dev` 에 있다(`43aeceb`) — 이 행은 양식이 아니라 기록의 머지다.** 이 표의 다른 행이 전부 그 문서의 칸을 가리키므로, 기록이 머지되지 않으면 근거가 없다. SSOT 판정 기준 ⓐ 의 「#439 가 머지된다」가 이 행이다 |

**48 관측치 게이트**: `rule_engine.MIN_DATAPOINTS = 48`이고 `METRIC_PERIOD_SECONDS` 기본값이 3600초다. 그리고 `evaluate_ec2`는 **관측치 검사를 prod 검사보다 먼저** 하므로(`rule_engine.py:64` → `:66`), 관측치가 차기 전에는 A7도 A9도 보이지 않고 **`web-1`·`web-2`·`idle-dev` 셋 다 `SKIP_INSUFFICIENT_DATA`** 다.

| 항목 | 기록 |
| --- | --- |
| 실제 기동 시각 · 48개 확보 시각 | ______ · ______ |
| A7 · A9 실측 가능일과 실제 실측일 | 가능 ______ / 실측 ______ |
| 10주차 안에 들어왔는가 · 아니면 어디로 갔는가 | ______ |

### 1-1. ADR-0006 §4 이월 목록 — 집계만 본다

**행별 처분은 [AWS_SMOKE_RESULT.md](AWS_SMOKE_RESULT.md) §5에 적는다.** 이 문서는 그 표를 복제하지 않는다 — 처분을 두 곳에 적으면 10/15(목) 판정 때 어느 쪽이 기록인지 갈린다. 여기는 판정에 쓸 집계만 받는다.

| 항목 | 기록 |
| --- | --- |
| 해소 / 미해소 | 해소 ______ 행 / 미해소 ______ 행 (8행 중) |
| 미해소 행 번호와 사유 | ______ |
| 집계 시점 · 근거 | §5 를 읽은 시각 ______ |

**미해소 0행이 판정 요건인지, 사유 기록으로 충분한지는 PM이 정한다.** 이 문서가 정하지 않는다. → **2026-10-13 주간 SSOT 갱신의 10주차 판정 기준 ⓓ 가 답했다**([`PROJECT_STATUS.md`](PROJECT_STATUS.md) §현재 위치). 기준 문장은 그 자리에만 두므로 여기 옮기지 않는다 — 집계를 적을 때 그 칸을 본다.

---

## 2. B군 — P2 3종 실 AWS 첫 검증 (10주차)

### 2-1. 선행 조건 — 코드가 서 있어야 한다

`executor.py`에 실행 함수가 있는 런북은 **10종 전부**다 — 2026-10-13(화) #459·#466 머지로 `ec2_enable_autoscaling` 이 마지막으로 들어왔고(그 앞은 10/12 #452·#454 의 `ec2_isolate`·`ec2_unisolate`), `executor.py` 머리말의 `[남은 작업]` 목록도 함께 사라졌다. **실행 함수에는 빠진 것이 없다.** `EC2_ISOLATE` 앞단의 **백업 캡처도 섰다** — `run_ec2_isolate_execution` 이 `store_sg_and_tg_mapping_backup` 을 부르고, 백업 캡처가 실패하면 조치를 시작하지 않는다(#452). **발동 경로(B5)까지 섰다** — 2026-10-13(화) [#469](https://github.com/ProjectVigilantis/vigilantis/pull/469) 머지로 **값을 만드는 자리**가 들어왔다(`security/soar.py` 가 `prepare_isolation`(High 선차단)을 두고 `workflows.reserve_auto_isolation`(Medium 1분 만료 예약)을 부르며, `AGENT_WAIT_POLL_SECONDS` 1초 주기 만료 감시 잡을 등록한다). dev 에서 `trigger_source=TriggerSource.PRE_MITIGATION_0_5S` 21곳 · `TIMEOUT_ISOLATION_1M` 4곳이 실제로 값을 싣는다. **그래서 P2 선행 코드 B1–B5 가 전부 dev 에 있고, 남은 것은 코드가 아니라 실 AWS 실측이다**(§2-2 · §8). 참고 — [PROJECT_STATUS.md](PROJECT_STATUS.md) §10주차 리스크 1번은 아직 「값을 만드는 자리가 없다」로 머지 전 상태를 적고 있다. **SSOT 갱신은 팀장 몫이고, 이 표의 근거는 머지된 #469 다.**

**담당은 B군 이슈 카드의 assignee가 기준이다.** 카드 7장을 2026-10-12(월) 발행했고(#443–#449) 아래 담당 칸은 **그 카드의 assignee 실측값**이다. B1–B3·B4·B5 는 같은 날 팀장 지정으로 정해졌다(2026-10-12 15:13 Slack `#회의` — B4·B5 를 안성일에게 요청, 15:15 수락). **B6 은 아직 배정 전이다** — assignee 가 팀장으로 남아 있고, 그것이 「팀장이 담당을 정할 자리」라는 뜻이다. **B7 은 배정 없이 팀장이 직접 처리해 2026-10-13(화) 닫혔다**(assignee 는 팀장 그대로)(SSOT §팀 & 역할 — 「담당 경계가 애매한 작업 — 팀장이 아침에 판단해 분배한다」).

역할·담당 경계의 원천은 [PROJECT_STATUS.md](PROJECT_STATUS.md) §팀 & 역할이며, 이 표만으로 담당 변경이 확정된 것으로 취급하지 않는다 — **위 지정은 SSOT 등재 대기 상태다**(정기 갱신 주체는 PM).

| # | 이슈 | 작업 | 담당(카드 assignee) | PR | 머지 | 비고 |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | [#443](https://github.com/ProjectVigilantis/vigilantis/issues/443) | 백업 `SAVE_CURRENT_SG_AND_TG_MAPPING` 캡처 — `EC2_ISOLATE` 전 SG·TG 매핑 저장 | 김세혁 | [#452](https://github.com/ProjectVigilantis/vigilantis/pull/452) | ☑ | `backup.py` 머리말이 남은 백업 1종으로 적어 둔 자리. **payload 모델도 없었다** — `BackupType` 4종 중 이것만 `packages/schemas/backups.py` 에 모델이 없었다(2026-10-12 확인 · 종전 기재 "계약은 정해져 있고 캡처 함수만 없다"를 정정). 소비처가 읽는 키(`security_group_ids`·`target_group_arn`)도 `_precheck_unisolate` 에만 박혀 있었다 — 지금 원천은 `packages/schemas/backups.py` 의 모델이다. 이것 없이는 `UNISOLATE` 원복 값의 원천이 없었다. **#452 가 `SgAndTgMappingBackup` 모델을 신설해 닫혔다** — 카드(#443)에 올린 「TG 단수/복수」는 **단수 유지**로 정해졌고, 대신 `target_port`(비기본 포트 대상이 TG 기본 포트로 재등록되는 것을 막는다)와 `network_interfaces`(격리가 모든 ENI 를 바꾸므로 원복도 ENI 단위다)가 함께 들어갔다 |
| B2 | [#444](https://github.com/ProjectVigilantis/vigilantis/issues/444) | `execute_ec2_isolate` | 김세혁 | [#452](https://github.com/ProjectVigilantis/vigilantis/pull/452) | ☑ | 10/12(월) 전 머지 기준은 지나갔고 **같은 날 #452 로 들어왔다**(계획은 10/13(화) PR). 순서는 **TG 해제 → ENI SG 교체**다. **부분 격리의 MVP 처분은 (a)로 정해졌다** — `FAILED` + CRITICAL 로그 + 백업 레코드 ID. `FAILED`는 원래 "변경 없는 실패"라 **원클릭 해제가 열리지 않고 복원은 수동이다**(상태 계약 보강은 Post-MVP #435). **판정에 직접 쓰이는 사실이다** — 부분 격리가 나면 P2-1 결과가 `FAILED`로 보여도 자산은 반쯤 바뀐 상태다 |
| B3 | [#445](https://github.com/ProjectVigilantis/vigilantis/issues/445) | `execute_ec2_unisolate` (롤백) | 김세혁 | [#452](https://github.com/ProjectVigilantis/vigilantis/pull/452) | ☑ | 원복 값은 백업 레코드에서만 로드([ADR-0008](adr/0008-backup-record-lifecycle-recovery-integrity.md) · ADR-0004 정책 ③). **TG 재등록의 유일한 자동 경로다** — `provision_smoke_aws.py` 는 초기화 뒤의 `up` 에서 등록을 건너뛰므로(격리 보호) 이 런북이 실패하면 수동 `register_targets` 뿐이고, 다음 `ISOLATE` precheck 가 조건 ③에서 막힌다 |
| B4 | [#446](https://github.com/ProjectVigilantis/vigilantis/issues/446) | `execute_ec2_enable_autoscaling` | **안성일**(확정) | [#459](https://github.com/ProjectVigilantis/vigilantis/pull/459) · [#466](https://github.com/ProjectVigilantis/vigilantis/pull/466) | ☑ | **10/13(화) 머지 — 목표 10/14(수)보다 하루 앞섰다**(#459 10:51 · #466 14:02 KST, 이슈 #446 CLOSED). ASG 상한 4대. 짝 롤백 런북이 없어 **정리가 손으로 간다** — ASG 가 띄우는 인스턴스가 비용 추산 밖이라 검증 직후 정리한다(ADR-0009 §3). `create_auto_scaling_group` 은 DryRun 미지원이라 실행 때 처음 돈다 |
| B5 | [#447](https://github.com/ProjectVigilantis/vigilantis/issues/447) | `PRE_MITIGATION_0_5S` · `TIMEOUT_ISOLATION_1M` 발동 경로 | **안성일**(확정) | [#469](https://github.com/ProjectVigilantis/vigilantis/pull/469) | ☑ | 둘 다 `EC2_ISOLATE`에 걸려 P2와 한 묶음이다. **10/13(화) 15:31 KST 머지 — 목표 10/14(수)보다 하루 앞섰다**(이슈 #447 은 아직 OPEN — CLOSE 판단은 머지 책임자 몫이다). `soar.py` 에 **발동 코드가 들어왔다** — `prepare_isolation`(High 선차단 · `security/soar.py`)과 `workflows.reserve_auto_isolation`(Medium 1분 만료 예약)을 `soar.py` 가 부르고, `AGENT_WAIT_POLL_SECONDS` 1초 주기 감시 잡을 등록하며, 소비처(`agent_dispatcher`·`incidents` 저장소)에 닿는다. 경합은 `lock_incident` 뒤 잠근 행으로 다시 보고, 늦은 승인은 409 `PROPOSAL_NOT_EXECUTABLE` 이다 |
| B6 | [#448](https://github.com/ProjectVigilantis/vigilantis/issues/448) | Medium `실행 예정 시간` 서버 응답 필드 | 배정 대기(assignee 팀장) | ______ | ☐ | API 계약 변경 — 발동 주체(B5)가 서면 함께 연다. 2026-08-27 결정이 함께 확정하라 한 것 셋(필드 null 규칙 · `IncidentListItem` 포함 여부 · 상태 전이 시 갱신·취소 규칙)이 이 카드의 범위다 |
| B7 | [#449](https://github.com/ProjectVigilantis/vigilantis/issues/449) | 단위 테스트(스텁 또는 moto) | **김세혁**(팀장 직접 — 카드 assignee 가 팀장으로 남은 채 처리됐다) | [#454](https://github.com/ProjectVigilantis/vigilantis/pull/454)(일부) · autoscaling 스텁은 B4 의 [#459](https://github.com/ProjectVigilantis/vigilantis/pull/459) 로 들어왔다 | ☑ | **이슈 #449 는 2026-10-13(화) CLOSED** — 3종 모두 스텁까지 섰다(§4 의 확인 강도 행). LocalStack에 `elbv2`·`autoscaling`이 없어 **CI가 실행 경로를 돌지 못한다** — 실 AWS가 첫 실행이다. **`moto` 는 현재 의존성이 아니다**(2026-10-12 확인) — 저장소의 스텁 선례는 둘이다 — `apps/core-api/tests/test_e2e_flow.py`(`aws_client` 를 바꿔 P2 precheck 의 `elbv2`·`autoscaling` 을 스텁한다)와, 같은 이유로 만든 `apps/core-api/services/tests/test_collector_vpc_scope.py`(PR #440 · 10/12(월) 머지 — 수집기의 `elbv2`·`autoscaling` 호출을 `_FakeAws` 한 객체로 스텁한다). 카드에서 스텁을 권했다 |

> B7의 뜻: 이 3종은 **테스트가 초록이어도 실 AWS에서 처음 돈다.** 다른 7종과 확인 강도가 다르며, 그 차이를 §4에 적는다. 머지 기준을 "stub 테스트 통과 + 10주차 실 AWS 실측"으로 미리 합의해 둘 대상이다.

**머지 시점 역산** — A7·A9의 48 관측치 칸과 같은 이유로 둔다. 작업별 담당 확정과 10/9(금) 한글날 휴일을 반영해 실측 가능일을 기록한다. 머지가 밀리면 실측일이 그만큼 밀리므로, 미달이 나도 판정 기록에 자리가 남아야 한다.

| 항목 | 기록 |
| --- | --- |
| P2 선행 코드 실제 머지일(B1–B7) | **실제** — B1–B3 **2026-10-12(월) 머지**([#452](https://github.com/ProjectVigilantis/vigilantis/pull/452) · 후속 [#454](https://github.com/ProjectVigilantis/vigilantis/pull/454)). 계획은 10/13(화) PR 이었고 **하루 앞섰다**. **B4 도 10/13(화) 머지**([#459](https://github.com/ProjectVigilantis/vigilantis/pull/459) · [#466](https://github.com/ProjectVigilantis/vigilantis/pull/466)) — 목표 10/14(수)보다 하루 앞섰다. **B5 도 10/13(화) 머지**([#469](https://github.com/ProjectVigilantis/vigilantis/pull/469) 15:31 KST) — 목표 10/14(수)보다 하루 앞섰다. **그래서 판정 기준 ⓑ 에 코드로 남은 선행은 없다** — 남은 것은 실 AWS 실측뿐이다. **B7 은 팀장이 직접 처리해 2026-10-13(화) 닫혔다**(#449 CLOSED), B6 은 배정 대기다(공개 API·FE 노출 — PR #469 본문이 #448 로 넘긴다) |
| 그 기준 P2 실측 가능일 | **P2-1(격리→해제)의 선행 코드가 전부 섰다** — 실행 함수는 B1–B3 머지(10/12)로, **발동 주체는 B5 머지(10/13 · #469)로** 들어왔다. `EC2_ISOLATE` 는 여전히 AI 메뉴 밖이고(`ai/capabilities.py` `build_secops_capabilities`) 실행 레코드는 발동 경로만 만드는데, **그 발동 경로가 이제 있다.** 환경 전제: 스모크 `up --yes`(ELB 등록 권한) · #452 코드 재배포 · 재스캔(`resolve_isolation_parameters` 가 읽을 `REGISTERED_IN` 관계와 격리 태그 SG). **P2-2 의 선행 코드는 B4 머지(10/13 화)로 섰다** — `EC2_ENABLE_AUTOSCALING` 은 `EC2_ISOLATE` 와 달리 **AI 제안 메뉴에 오르므로**(본편 7종 · `ai/tests/test_capabilities.py` 가 EC2 자산에 이 런북만 제시되는 것을 고정한다) 별도 발동 경로가 필요하지 않다. 남은 전제는 실 AWS 환경뿐이다. **계획** — 10/14(수)–10/15(목)(실 AWS 실측은 김세혁) / **실제 실측일** ______ |
| 10/15(목) 안에 들어왔는가 · 아니면 어디로 갔는가 | ______ |

### 2-2. 실측

| # | 확인할 것 | 근거 칸 | 충족 | 비고 |
| --- | --- | --- | --- | --- |
| P2-1 | `EC2_ISOLATE` → `EC2_UNISOLATE` | `AWS_SMOKE_RESULT.md` §8 | ☐ | |
| P2-2 | `EC2_ENABLE_AUTOSCALING` | §8 | ☐ | 검증 직후 ASG 정리(비용이 예산표 밖) |
| P2-3a/b/c | DryRun 부분 + describe 통과 조건 ①②③④ | §8-1 | ☐ | |
| — | **ADR-0007 §4의 "잠정" 3행이 닫혔는가** | §8-1 확정/수정 열 | ☐ | 닫히지 않으면 그 사실을 판정에 적는다 |

---

## 3. C군 — MVP 확정 범위 대조

**여기서 갈라 적을 것은 "되는가"가 아니라 "어디서 확인했는가"다.** 10/1(목) 시연은 LocalStack 기반이었고(SSOT 확정 결정), 실 AWS에서 처음 돈 것은 **10주차뿐이다**(9주차에는 환경이 서지 않아 스모크가 돌지 않았다 — SSOT 9주차 각주). 둘을 한 칸에 적으면 MVP 완료 판정이 실제보다 세게 읽힌다.

### 3-1. 런북 10종 (SSOT §Action Whitelist 확정본)

**실 AWS 열 현황(2026-10-14 13:00 KST · PR #439 `be1c303` 기준): 6종 ☑ · 4종 ☐.** 미실측 넷 중 **셋은 선택 항목**(P1 3종 · A10 — 미실행이 미달은 아니다)이고, **판정에 걸린 것은 `EC2_ENABLE_AUTOSCALING` 하나**다(10/14 오후 예정).

**☑ 의 뜻**: 그 런북이 실 AWS 에서 한 번 이상 `SUCCESS` 로 끝났고 **그 사실이 `AWS_SMOKE_RESULT.md` 의 어느 칸에 적혀 있다**는 것이다. **서비스 가능을 뜻하지 않는다** — 런북마다 `SUCCESS` 가 어디까지 확인한 것인지가 다르므로 근거 칸에 그 경계를 적는다. 이 표는 결과를 복제하지 않고 **어디서 확인했는가**를 가린다(§3 머리말).

> **§1 A군 표의 「충족」 칸은 그대로 ☐ 로 둔다.** 이 열은 「실 AWS 에서 돌았는가」라는 **사실**이고, §1 의 충족은 **판정**이다. 두 칸이 다른 것을 묻는다 — A1–A5 가 10/12 에 끝났는데도 §1 이 비어 있는 것과 같은 이유다. 판정 기록은 10/15(목) PM 몫이다.

| Runbook ID | 실행 함수 | LocalStack 확인 | 실 AWS 확인 | 근거 |
| --- | --- | --- | --- | --- |
| `RUNBOOK_EC2_RIGHTSIZING` | 있음 | ☐ | **☑** | §4 A7 — `SUCCESS`. **Status Check 2/2 대기 뒤** 성공으로 끝난다 |
| `RUNBOOK_EC2_REVERT_SIZE` | 있음 | ☐ | **☑ 2경로** | §4 A7(사람 승인 원복) · §4 A8(**승인 호출 없이** 생긴 자동 원복 — 다만 **`AUTO_ON_FAILURE` 는 관측값이 아니다**. 공개 API 에 `trigger_source` 가 없어 §4 A8 이 「승인 호출이 없었다」는 사실로 **판단**한 것이라고 적는다). **☑ 는 타입 복귀·`running` 까지다** — `SUCCESS` 가 2/2 `ok` 를 기다리지 않는다(§7 10/14 11:03 · 다운사이징과 **비대칭**). 서비스 복귀 확인이 아니다(#434 1-1) |
| `RUNBOOK_NACL_ADD_DENY` | 있음 | ☐ | **☑** | §4 A6 — `SUCCESS`. **AWS 저장값까지 조회로 확인**(규칙 100 의 `Protocol`·`PortRange`) |
| `RUNBOOK_NACL_RESTORE` | 있음 | ☐ | **☑** | §4 A6 — `SUCCESS`. 백업 fingerprint 대조를 통과하고 **기준선(차단 전 조회)으로 돌아갔다** |
| `RUNBOOK_SG_DELETE_ISOLATED` | 있음 | ☐ | ☐ **미실측(선택)** | §4 A10 빈칸 — 실동작 없음. §3 A4-5 는 `precheck()` 통과까지다 |
| `RUNBOOK_SG_RECREATE` | 있음 | ☐ | ☐ **미실측(선택)** | §4 A10 빈칸. §3 A4-6 은 직접 DryRun 까지다 |
| `RUNBOOK_EBS_DELETE_UNATTACHED` | 있음 | ☐ | ☐ **미실측(선택)** | §4 A10 빈칸. §3 A4-7 은 `precheck()` 통과까지다 |
| `RUNBOOK_EC2_ISOLATE` | **있음**(#452) | 불가 — `elbv2` 없음 | **☑ 3회** | §8 P2-1. **발동이 사람 승인이 아니라 B5 자동 경로**다(선차단 · 1분 자동 격리) — 승인 메뉴 경로는 실 AWS 에서 안 돌았다 |
| `RUNBOOK_EC2_UNISOLATE` | **있음**(#452) | 불가 — `elbv2` 없음 | **☑ 3회** | §8 P2-1. 격리 3회에 해제가 짝으로 돌아 **TG 재등록 경로가 실제로 섰다**(E2E 대본 §반복 조건 ①) |
| `RUNBOOK_EC2_ENABLE_AUTOSCALING` | **있음**(#459·#466) | 불가 — `autoscaling` 없음 | ☐ **10/14(수) 오후 예정** | §8 P2-2 빈칸. **실측과 정리가 한 묶음**이다 — 오늘 구성에서는 `service cleanup` 이 아니라 관리자 CLI 로 넷(ASG·LT·AMI·스냅숏)을 직접 지운다(E2E 대본 §서비스 연결·트래픽 전환) |

### 3-2. 그 밖의 확정 범위 항목

| 항목 | 확인 환경 | 충족 | 근거 |
| --- | --- | --- | --- |
| 4단계 가드레일 순서 고정(① Schema ② Whitelist ③ ARN ④ Dry-Run) | ______ | ☐ | |
| 양방향 회복 — 스펙 JSON 백업 → Status Check 2/2 → 자동 원복 | ______ | ☐ | §4 A8 |
| 보안 축 — 선제 차단 → 관제자 원클릭 해제 | ______ | ☐ | **`B1`·`B2`·`B3`·`B5` 가 모두 머지돼 이 축의 코드 선행은 충족됐다**(B1–B3 2026-10-12 #452 · B5 2026-10-13 #469) — 선제 차단 발동(B5) → 백업(B1)·격리(B2) → 원클릭 해제 `RUNBOOK_EC2_UNISOLATE`(B3)를 함께 확인한다. **머지만으로 충족 처리하지 않고 실측 근거를 기록한다** — 이 행의 ☐ 는 코드가 아니라 실 AWS 확인을 기다린다 |
| 3단계 위험 대응(High 0.5s · Medium·Low 승인 · Medium 1분 자동 격리) | ______ | ☐ | Low는 자동 격리 제외. **`B1`·`B2`·`B5` 가 모두 머지돼 이 항목의 코드 선행은 충족됐다**(B5 2026-10-13 #469) — 선차단·자동 격리 발동(B5)이 백업(B1)·`RUNBOOK_EC2_ISOLATE`(B2)로 이어져야 한다(SSOT §확정 결정 로그 2026-08-25). **머지만으로 충족 처리하지 않고 실측 근거를 기록한다** — 이 행의 ☐ 는 코드가 아니라 실 AWS 확인을 기다린다 |
| Reckon — Incident 상태 축과 수습·종료 | ______ | ☐ | |
| 조치별 절감 예상 — AI는 단가만, 월 절감액·조건·문장은 서버 | ______ | ☐ | |
| AI — CoT 3줄 요약 + Runbook ID 추천(LangGraph·Structured Output) | ______ | ☐ | |
| 위협 — OpenIP · SSH 브루트포스 모의 주입 | ______ | ☐ | |
| FE — 토폴로지 맵 · One-Click · Idempotency Key (mock 계층 없음) | ______ | ☐ | |
| 관제 범위 — 단일 계정 1–2개 리전 · EC2·SG + 런북 대상 리소스 | ______ | ☐ | 스모크는 단일 리전 |

> 범위 밖은 범위 밖으로 둔다 — 근거 조회 · 독립 시계열 분석 화면 · 시간 슬라이드 차트(SSOT §MVP 확정 범위). 대시보드 추이 2축은 그 축이 아니며 이미 섰다.

---

## 4. D군 — 미해소·미검증과 Post-MVP 이월

판정에서 빠지는 것을 **빠뜨리지 않고 적는 자리**다. 12/11(금) 최종 발표 범위는 MVP 마감 판정 뒤에 확정한다(SSOT).

| 항목 | 상태 | 사유 | 처분(이월 / 범위 밖 / 후속 이슈) |
| --- | --- | --- | --- |
| P2 3종 실행 경로의 확인 강도(B7 · #449) | **스텁 단위 테스트만 있다** — 다른 7종의 LocalStack 왕복 겹이 없다 | LocalStack Community에 `elbv2`·`autoscaling`이 없다([ADR-0006](adr/0006-localstack-team-standard-env.md) §4). 스텁은 호출 순서·부분 성공 분기·백업 → 가드레일 ④ 왕복·예외 미전파까지만 고정하고, AWS 응답 모양·IAM 태그 조건·연결 추적 동작은 보지 못한다. `EC2_ENABLE_AUTOSCALING` 도 #459 머지로 **스텁이 생겼다**(`apps/core-api/services/tests/test_execute_ec2_autoscaling.py` · `apps/core-api/tests/test_ec2_autoscaling_workflow.py` · `apps/core-api/tests/test_dispatcher_lane_integration.py` — 셋 다 커밋 `3951963`) — 3종 모두 스텁까지는 섰고, 그 위가 비어 있다는 사실은 그대로다 | 머지 기준 = **스텁 통과 + 10주차(10/12–10/15) 실 AWS 실측**. 초록만으로 "검증됨"으로 세지 않는다. 실측 결과는 [AWS_SMOKE_RESULT.md](AWS_SMOKE_RESULT.md) §8 |

| 항목 | 기록 |
| --- | --- |
| 실 AWS에서 한 번도 돌지 않은 경로 | ______ |
| 테스트만 있고 실행 확인이 없는 경로 | ______ |
| ADR 개정이 필요해진 것 | ______ |

---

## 5. E군 — 판정 기록 (PM)

| 항목 | 기록 |
| --- | --- |
| 참조한 판정 기준 · SSOT 커밋/절 링크 | ______ |
| 기준별 충족 / 분모 | ______ |
| 미달 항목과 처분 | ______ |
| 판정 · 판단자 · 시각 | ______ |
| SSOT 반영 커밋 | ______ |

---

## 6. 판정 전에 닫혀야 하는 것 (10/15 기준 역산)

| 시점 | 무엇 | 담당 | 상태 |
| --- | --- | --- | --- |
| 10/15(목) 오전 | 스모크 결과 기록(A11) — `AWS_SMOKE_RESULT.md` 채움·머지(#439) | 김세혁 실행 · 김승철 대조 | ☐ |
| 10/13(화) | 10주차 전환 갱신 — **MVP 마감 판정 기준 확정**(PR #461 · 계획 10/12(월)에서 하루 늦췄다) | 김세혁 | ☑ |
| 10/12(월) 전 | P2 선행 코드 머지(B1–B7) — 백업·실행 3종·발동 경로·테스트 | §2-1 작업별 담당 확정 후 기록 | ☐ |
| 10/15(목) | P2 검증 기록 · 이월 4행 최종 처분 | 김세혁 · 김승철 | ☐ |
| 10/15(목) | MVP 마감 판정 기록 | 김세혁 | ☐ |
| 10/16(금) | `.env` LocalStack 복귀 — 스모크 환경은 내리지 않는다(ADR-0009 1차 개정 · 정리는 12/11(금) 최종 발표 뒤) | 김세혁 | ☐ |

# MVP 마감 판정 체크리스트 (10/15(목)) — 초안

> **담당**: 김승철 (QA & Scenario) — 2026-10-06(화) 업무 재배정 §팀원 배정 "10/15 마감 판정 체크리스트 초안"
> **판정 주체**: 김세혁(PM). 판정 기록은 SSOT [PROJECT_STATUS.md](PROJECT_STATUS.md)에 PM이 남긴다.
>
> **이 문서는 판정 기준을 정의하지 않는다.** 10/15(목) 판정 기준은 10주차 전환 갱신(10/12(월))에서 PM이 확정하며, 이 문서는 **그 기준에 답할 때 볼 칸과 근거의 자리**다. 확정 전 초안을 확정 정책으로 취급하지 않는다.
> 빈칸·밑줄·미선택 체크박스는 **미확인**이다. 채울 때는 결과 또는 미확인 사유를 적는다.

---

## 0. 판정 대상의 원천

| 항목 | 원천 | 상태 |
| --- | --- | --- |
| 10/15(목)이 무엇을 묻는 날인가 | SSOT §현재 위치 §발표 일정 표 10/15 행 — "실 AWS 스모크 완료 + P2 3종 실 AWS 첫 검증까지. 발표 자리가 아니라 **내부 완료 판정일**" | 확정 |
| 판정 기준 ⓐⓑⓒ… | SSOT 10주차 전환 갱신(10/12(월)) — "10주차 판정 기준 = MVP 마감 판정 기준" | **확정 대기** |
| MVP 범위 | SSOT §MVP 확정 범위 · §Action Whitelist 10종 표 | 확정 |
| 스모크 관측 기록 | [AWS_SMOKE_RESULT.md](AWS_SMOKE_RESULT.md) | 양식 신설 완료 · 값 미기록 |
| 이월 목록 | [ADR-0006](adr/0006-localstack-team-standard-env.md) §4 (8행) | 처분 미기록 |
| 조회 대체 통과 조건 | [ADR-0007](adr/0007-guardrail-dryrun-executor-precheck-contract.md) §4 — 3행이 "잠정안" | **확정 대기** |

> 주차 구간은 평일만 센다(SSOT). 10주차는 10/12(월)–10/15(목) 실작업 4일이고, 앞의 한글날 10/9(금)은 구간 밖이다.

---

## 1. A군 — 실 AWS 스모크 완료 (9주차)

근거는 모두 `AWS_SMOKE_RESULT.md`의 칸이다. 이 표에 결과를 복제하지 않고 **어느 칸이 답했는지** 가리킨다.

| # | 확인할 것 | 근거 칸 | 충족 | 비고 |
| --- | --- | --- | --- | --- |
| A1 | 환경 기동 · TG 대상 `healthy` · Budget $50 알림 | §1 | ☐ | |
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

**48 관측치 게이트**: `rule_engine.MIN_DATAPOINTS = 48`이고 `METRIC_PERIOD_SECONDS` 기본값이 3600초다. 그리고 `evaluate_ec2`는 **관측치 검사를 prod 검사보다 먼저** 하므로(`rule_engine.py:64` → `:67`), 관측치가 차기 전에는 A7도 A9도 보이지 않고 셋 다 `SKIP_INSUFFICIENT_DATA`다.

| 항목 | 기록 |
| --- | --- |
| 실제 기동 시각 · 48개 확보 시각 | ______ · ______ |
| A7 · A9 실측 가능일과 실제 실측일 | 가능 ______ / 실측 ______ |
| 9주차 안에 들어왔는가 · 아니면 어디로 갔는가 | ______ |

### 1-1. ADR-0006 §4 이월 목록 8행 처분

| 행 | 격차 | 처분 | 근거 칸 |
| --- | --- | --- | --- |
| 1 | 가드레일 ④ `DryRun=True` 실제 IAM 권한 검증 | 해소 / 미해소 | §3 A4 |
| 2 | `get_waiter` Status Check `impaired` | 해소 / 미해소 | §4 A8-impaired |
| 3 | CloudWatch 메트릭 수집(지연·해상도) | 해소 / 미해소 | §2-1 |
| 4 | ALB Target Group · ASG 경로(P2 3종) | 해소 / 미해소 | §2-0 · §2-1 · §2-2 · §8 · §8-1 |
| 5 | NACL 생성·삭제의 `DryRun=True` | 해소 / 미해소 | §3 A4-3 · A4-4 |
| 6 | `Protocol` 표기 | 해소 / 미해소 | §4 A6 |
| 7 | `PortRange` | 해소 / 미해소 | §4 A6 |
| 8 | `DryRun`의 대상 존재 검사 | 해소 / 미해소 | §3 A5 |

**미해소 0행이 판정 요건인지, 사유 기록으로 충분한지는 PM이 정한다.** 이 문서가 정하지 않는다.

---

## 2. B군 — P2 3종 실 AWS 첫 검증 (10주차)

### 2-1. 선행 조건 — 코드가 서 있어야 한다

`executor.py`에 실행 함수가 있는 런북은 **7종**이다(`rightsizing` · `revert_size` · `nacl_add_deny` · `nacl_restore` · `ebs_delete_unattached` · `sg_delete_isolated` · `sg_recreate`). **P2 3종은 `precheck`만 있고 실행 함수가 없다** — `executor.py` 머리말 TODO 1번이 그 셋을 적어 둔 자리다.

| # | 작업 | 담당 | PR | 머지 | 비고 |
| --- | --- | --- | --- | --- | --- |
| B1 | `execute_ec2_isolate` | 김세혁 | ______ | ☐ | 10/12(월) 전에 서야 10주차 실측이 가능 |
| B2 | `execute_ec2_unisolate` (롤백) | 김세혁 | ______ | ☐ | [ADR-0008](adr/0008-backup-record-lifecycle-recovery-integrity.md) 백업 수명주기 |
| B3 | `execute_ec2_enable_autoscaling` | 김세혁 | ______ | ☐ | ASG 상한 4대 |
| B4 | 단위 테스트(moto·스텁) | 김세혁 | ______ | ☐ | LocalStack에 `elbv2`·`autoscaling`이 없어 **CI가 실행 경로를 돌지 못한다** — 실 AWS가 첫 실행이다 |

> B4의 뜻: 이 3종은 **테스트가 초록이어도 실 AWS에서 처음 돈다.** 다른 7종과 확인 강도가 다르며, 그 차이를 §4에 적는다.

### 2-2. 실측

| # | 확인할 것 | 근거 칸 | 충족 | 비고 |
| --- | --- | --- | --- | --- |
| P2-1 | `EC2_ISOLATE` → `EC2_UNISOLATE` | `AWS_SMOKE_RESULT.md` §8 | ☐ | |
| P2-2 | `EC2_ENABLE_AUTOSCALING` | §8 | ☐ | 검증 직후 ASG 정리(비용이 예산표 밖) |
| P2-3a/b/c | DryRun 부분 + describe 통과 조건 ①②③④ | §8-1 | ☐ | |
| — | **ADR-0007 §4의 "잠정" 3행이 닫혔는가** | §8-1 확정/수정 열 | ☐ | 닫히지 않으면 그 사실을 판정에 적는다 |

---

## 3. C군 — MVP 확정 범위 대조

**여기서 갈라 적을 것은 "되는가"가 아니라 "어디서 확인했는가"다.** 10/1(목) 시연은 LocalStack 기반이었고(SSOT 확정 결정), 실 AWS에서 처음 돈 것은 9–10주차뿐이다. 둘을 한 칸에 적으면 MVP 완료 판정이 실제보다 세게 읽힌다.

### 3-1. 런북 10종 (SSOT §Action Whitelist 확정본)

| Runbook ID | 실행 함수 | LocalStack 확인 | 실 AWS 확인 | 근거 |
| --- | --- | --- | --- | --- |
| `RUNBOOK_EC2_RIGHTSIZING` | 있음 | ☐ | ☐ | §4 A7 |
| `RUNBOOK_EC2_REVERT_SIZE` | 있음 | ☐ | ☐ | §4 A7 · A8 |
| `RUNBOOK_NACL_ADD_DENY` | 있음 | ☐ | ☐ | §4 A6 |
| `RUNBOOK_NACL_RESTORE` | 있음 | ☐ | ☐ | §4 A6 |
| `RUNBOOK_SG_DELETE_ISOLATED` | 있음 | ☐ | ☐ | §4 A10 (선택) |
| `RUNBOOK_SG_RECREATE` | 있음 | ☐ | ☐ | §4 A10 (선택) |
| `RUNBOOK_EBS_DELETE_UNATTACHED` | 있음 | ☐ | ☐ | §4 A10 (선택) |
| `RUNBOOK_EC2_ISOLATE` | **없음(B1)** | 불가 — `elbv2` 없음 | ☐ | §8 P2-1 |
| `RUNBOOK_EC2_UNISOLATE` | **없음(B2)** | 불가 — `elbv2` 없음 | ☐ | §8 P2-1 |
| `RUNBOOK_EC2_ENABLE_AUTOSCALING` | **없음(B3)** | 불가 — `autoscaling` 없음 | ☐ | §8 P2-2 |

### 3-2. 그 밖의 확정 범위 항목

| 항목 | 확인 환경 | 충족 | 근거 |
| --- | --- | --- | --- |
| 4단계 가드레일 순서 고정(① Schema ② Whitelist ③ ARN ④ Dry-Run) | ______ | ☐ | |
| 양방향 회복 — 스펙 JSON 백업 → Status Check 2/2 → 자동 원복 | ______ | ☐ | §4 A8 |
| 보안 축 — 선제 차단 → 관제자 원클릭 해제 | ______ | ☐ | |
| 3단계 위험 대응(High 0.5s · Medium·Low 승인 · Medium 1분 자동 격리) | ______ | ☐ | Low는 자동 격리 제외 |
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
| | | | |

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
| 10/8(목) | 스모크 결과 기록(A11) — `AWS_SMOKE_RESULT.md` 채움 | 김세혁 실행 · 김승철 대조 | ☐ |
| 10/12(월) | 10주차 전환 갱신 — **MVP 마감 판정 기준 확정**(C3) | 김세혁 | ☐ |
| 10/12(월) 전 | P2 3종 실행 함수 머지(B1–B4) | 김세혁 | ☐ |
| 10/15(목) | P2 검증 기록 · 이월 4행 최종 처분 | 김세혁 · 김승철 | ☐ |
| 10/15(목) | MVP 마감 판정 기록(C4) | 김세혁 | ☐ |
| 10/16(금) | 스모크 환경 `down` · `.env` LocalStack 복귀 | 김세혁 | ☐ |

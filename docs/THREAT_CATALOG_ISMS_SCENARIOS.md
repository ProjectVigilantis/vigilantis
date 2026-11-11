# 고객사 위협 분류표와 위협별 ISMS 보고서 작성 시나리오 (1판)

> **담당**: 박지현 (QA) — 이슈 [#613](https://github.com/ProjectVigilantis/vigilantis/issues/613)
> **쓰는 곳**: ISMS 보고서 서버(안성일 · [#488](https://github.com/ProjectVigilantis/vigilantis/issues/488) 근거 내보내기 · 사고 조사 페이지 · AI 신고 초안)가 위협별 대응 방식과 초안 입력을 정할 때 본다(SSOT §확정 결정 로그 2026-11-06 ISMS 행).
>
> **이 문서는 코드·서버·화면을 정하지 않는다.** 위협마다 「어느 보고서를 · 언제까지 · 무슨 항목으로 · 누가(플랫폼 기록 / AI 문장 / 사람 확인) 채우는가」를 적는 자리다. 공식 서식과 법 조문은 기관 링크로 가리키며 법적 검토의 완결을 주장하지 않는다. 기관 제출은 고객사가 한다(2026-11-06 결정).
> **기록된 사실과 준수·충족 판단은 다르다.** 이 문서의 「플랫폼 기록」은 그 값이 저장돼 있다는 뜻이고, 신고 요건을 채웠다는 뜻이 아니다. 차단·격리 성공이나 인시던트 `RESOLVED`는 사고 처리 전체(원인 제거 · 복구 · 신고)의 완료가 아니다.
> 빈칸 · `[S]` · 「공백」 · 「미확인」은 **미확인**이다 — 「없음」으로 읽지 않는다. `[S]`는 외부 문서를 요약분으로 읽은 행이라 원문으로 재확인한다. **#488 JSON 경로는 PR #616(머지 전) 계약 기준이며 이름과 구조가 리뷰 과정에서 바뀔 수 있다.**

---

## 0. 원천

| 항목 | 원천 | 상태 |
| --- | --- | --- |
| 이 문서의 범위와 완료 조건 | #613 본문 §1·§2 · 완료 조건 6 | 확정(11/11 발행) |
| 제출 목적 5종(ISMS/ISMS-P 인증 · 침해사고 신고 24h · 개인정보 유출 신고·통지 72h · 개인정보위 자료 제출 · 내부 분석) | 안성일 13주차 조사 — #479 코멘트(2026-11-03) §1 | 확정 |
| ISMS 보고서 범위(근거 내보내기 · 사고 조사 · AI 신고 초안) · 제출은 고객사 | SSOT §확정 결정 로그 2026-11-06 ISMS 행 | 확정 |
| 근거 내보내기 **계약**(JSON 최상위 `export` · `incident` · `evidence` · THREAT content = `event` + 선택 `context` · 공격 IP 미마스킹 · 2 MiB) | #488 2026-11-09 코멘트 · **PR #616 `packages/schemas/api/evidence_export.py`(머지 전)** · #613 코멘트 `6785035815`(안성일 · 필드 요약) | 잠정 — 머지 전이라 바뀔 수 있다 |
| GuardDuty 위험도 매핑(7.0↑ HIGH · 4.0–6.9 MEDIUM) · 인시던트는 MEDIUM↑ · 1차 자동 대응 없음 · **C단계(결과 읽기) 미구현** | #588 Q2 · Q3 · PR #593(켜기까지 · 11/11 머지) | 판정 규칙 확정 · 입력은 계획 |
| 플랫폼이 보는 위협 유형과 판정 | `packages/schemas/events.py` — `ThreatEventType` · `RiskReasonCode` · `expected_mode_for` / `apps/core-api/security/risk_evaluator.py` — `_evaluate_open_ip` · `_evaluate_ssh_bruteforce` / `apps/core-api/security/soar.py`(MEDIUM 60초) / ADR-0011(High 목표 3초) | 코드 |
| 기록이 남는 자리(실제 테이블명) | `threat_events` · `incidents`(`IncidentStatus`) · `action_executions`(`RunbookId` · `TriggerSource`) · `evidence_items`(`EvidenceItem` · 현재 SecOps 접수는 THREAT 근거를 남긴다) · `audit_events`(T-2) · `platform_events`(#592 11/11 머지 · 기능별 기록 호출은 #589 B에서 연결) | 코드 |
| 외부 분류 | AWS GuardDuty finding type 문서(EC2 · IAM) · KISA 침해사고 신고 유형 4종(DDoS · 악성코드 감염 · 서버 해킹 · 기타) · MITRE ATT&CK v20 Enterprise IaaS 매트릭스 | 외부 · 2026-11-11 읽음 |

---

## 1. 위협 분류표

### 1-1. A축 — 플랫폼이 지금·곧 보는 위협

| # | 위협 | 원천(신호) | 플랫폼 기록 | 자동 조치 | 보고서 종류(후보) |
| --- | --- | --- | --- | --- | --- |
| A1 | 공개 인그레스 노출 — 전체 개방 `RISK_OPEN_INGRESS_WORLD` · 22/3389 노출 `RISK_SENSITIVE_PORT_EXPOSED` · 전 프로토콜 `RISK_ALL_PROTOCOL_OPEN` · 전 포트 `RISK_ALL_PORTS_EXPOSED` | SG 관측 → `ThreatEventType.OPEN_IP`(중복 키 = SG+프로토콜+포트+CIDR+발생 시각) | `threat_events` → `incidents`(SECOPS · 항상 MEDIUM) · 근거 `evidence_items`(THREAT) · 조치는 승인 뒤 `action_executions`(SG 차단 런북) | **MEDIUM 승인 대기 · SG 대상 자동 격리 없음**(SG를 연결된 EC2로 바꿔 격리하지 않는다) | 사고 아님 → ISMS 통제 증적 · 내부 분석. 침해와 결합되면 「경위」 칸 |
| A2 | SSH 무차별 대입 `RISK_SSH_BRUTEFORCE`(HIGH · MEDIUM 둘 다) · `RISK_SSH_LOW_SIGNAL`(LOW) | `ThreatEventType.SSH_BRUTE_FORCE`(중복 키 = 대상 EC2+공격 IP+횟수+관측 창+발생 시각) | 위와 같음 + 승인/자동 뒤 `action_executions`(NACL 차단 · 격리) | **HIGH 즉시 선차단(목표 3초 · ADR-0011 · 식별자 `PRE_MITIGATION_0_5S` 유지)** · MEDIUM 승인 대기 시작부터 60초 무응답 → 조건 충족 EC2 대상 자동 격리 · LOW 제외 | 시도 = 내부 분석 / 성공 = 침해사고 신고 24h(서버 해킹) — **§3-1** |
| A3 | GuardDuty **EC2** finding 40종(Backdoor 8 · Behavior 2 · CryptoCurrency 2 · DefenseEvasion 3 · Impact 7 · Recon 3 · Trojan 9 · UnauthorizedAccess 6) | 고객 계정 GuardDuty → 주기 조회 — **C단계 미구현 · 저장·내보내기 계약 확정 전의 계획 입력** | 계획: `threat_events` 전부 · `incidents`는 MEDIUM↑ | 없음(1차 · #588 Q2 · #598 보호 태그 선행) | prefix별로 B축에 배정 |
| A4 | GuardDuty **IAM** finding 28종(13 prefix) | 위와 같음 — **수집 여부 미정**(김승철 확인 사항) | — | — | 자격 증명·계정 탈취 → 24h · 유출 경로면 72h |
| A5 | 플랫폼 자체 — 로그인 경로 무차별 대입 · API 남용 | WAF 속도 제한 Count(T-3 #522) · `platform_events` `AUTH_LOGIN` — **스키마는 있지만 기록 호출은 #589 B에서 연결 필요**(지금 확보되는 기록 아님) | `platform_events`(연결 뒤) | Block 전환은 11/16 뒤 | 우리가 신고 주체 — 시나리오는 별도 1건(2판) |

### 1-2. B축 — 고객사(EC2 + IAM)에 들어올 수 있는 위협 상위 유형

| # | 위협 유형 | ATT&CK(IaaS) | GuardDuty 예(기본 심각도) | KISA 유형 | 플랫폼이 보는가 | 보고서(후보) |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | 무차별 대입(SSH · RDP · WinRM) | Credential Access T1110 | `UnauthorizedAccess:EC2/SSHBruteForce` · `RDPBruteForce`(Low · 가해 측이면 High) · `Impact:EC2/WinRMBruteForce` | 서버 해킹 | **직접**(SSH = A2) · RDP/WinRM은 GD(계획) | 내부 분석 → 성공 시 24h |
| B2 | 정찰 · 포트 스캔 · 노출 포트 탐침 | Discovery T1046 | `Recon:EC2/Portscan`(M) · `Recon:EC2/PortProbeUnprotectedPort`(L · 9200/9300은 H) | 서버 해킹(전조) | GD(계획) | 내부 분석 · ISMS 증적 |
| B3 | 설정 오류 노출(전체 개방 SG · 민감 포트) | Initial Access T1190 전조 | — | — | **직접**(A1) | ISMS 증적 |
| B4 | C&C 통신 · 백도어 · 트로이 | Execution T1059 · Persistence | `Backdoor:EC2/C&CActivity.B`(H) · `Trojan:EC2/DGADomainRequest.B`(H) · `BlackholeTraffic`(M) | 악성코드 감염 | GD(계획) | 침해사고 24h — **§3-2** |
| B5 | 암호화폐 채굴(자원 탈취) | Impact T1496 | `CryptoCurrency:EC2/BitcoinTool.B`(H) · `Impact:EC2/BitcoinDomainRequest.Reputation`(H) | 악성코드/서버 해킹 | GD(계획) | 침해사고 24h |
| B6 | DoS **발신**(내 자산이 가해자) | Impact T1498/T1499 | `Backdoor:EC2/DenialOfService.*`(H · 5종) · `Spambot`(M) | DDoS(가해 측) | GD(계획) | 침해사고 24h |
| B7 | DoS **수신**(피해) | Impact T1498/T1499 | — (Shield 영역) | DDoS | **공백** | 침해사고 24h — 플랫폼 밖 근거 |
| B8 | 데이터 유출(DNS 터널 · 외부 반출) | Exfiltration T1048/T1537 | `Trojan:EC2/DNSDataExfiltration`(H) · `Trojan:EC2/DropPoint`(M) | 기타(정보유출) | GD(계획) | 침해사고 24h + 개인정보 포함 시 72h — **§3-3** |
| B9 | 자격 증명 유출 · 인스턴스 역할 자격 반출 | Credential Access T1552 · T1528 | `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS`(H) · `UnauthorizedAccess:EC2/MetadataDNSRebind`(H) · `CredentialAccess:IAMUser/CompromisedCredentials`(H) | 서버 해킹(계정 탈취) | GD(EC2 1종 · 계획) · IAM은 A4 | 24h · 유출 경로면 72h |
| B10 | 계정 탈취 · 권한 상승 · 지속화(IAM 변경) | Valid Accounts T1078 · Account Manipulation T1098 | `Persistence/PrivilegeEscalation:IAMUser/AnomalousBehavior`(M) · `Policy:IAMUser/RootCredentialUsage`(L) · `ConsoleLoginSuccess.B`(M) | 서버 해킹 | A4 | 24h |
| B11 | 탐지 회피 · 로그 끄기 | Defense Impairment T1685 · T1578 | `Stealth:IAMUser/CloudTrailLoggingDisabled`(L) · `DefenseEvasion:IAMUser/AnomalousBehavior`(M) · `DefenseEvasion:EC2/UnusualDNSResolver`(M) | 서버 해킹(은폐) | GD(EC2 · 계획) · IAM은 A4 | 근거 훼손 → 「미확인」 칸 증가 |
| B12 | 랜섬웨어 · 데이터 파괴 | Impact T1486 · T1485 · T1490 | — (EC2 finding 없음 · Malware Protection/S3 영역) | 악성코드(랜섬웨어) | **공백** | 24h · 개인정보면 72h |
| B13 | 피싱 · 악성 도메인 질의 · 드라이브바이 | User Execution T1204 | `Trojan:EC2/PhishingDomainRequest!DNS`(H) · `DriveBySourceTraffic!DNS`(H) `[S]` · `Impact:EC2/MaliciousDomainRequest.Reputation`(H) | 악성코드 | GD(계획) | 내부 분석 → 감염 확인 시 24h |
| B14 | Tor · 악성 IP 통신 | Stealth · C2 성격 | `UnauthorizedAccess:EC2/TorClient` · `TorRelay`(H) · `MaliciousIPCaller.Custom`(M) · `Recon:IAMUser/TorIPCaller`(M) | 서버 해킹 | GD(계획) | 24h |
| B15 | 침투 시험 도구 사용(Kali · Parrot · Pentoo) | — | `PenTest:IAMUser/*`(M) | — | A4 | 내부 분석(승인된 시험이면 기록만) |
| B16 | 고객사 내부자 · 권한 오남용 | Valid Accounts T1078 | — | 기타 | **공백**(고객사 측 · 플랫폼 측은 `platform_events`) | 내부 분석 · 개인정보면 72h |

보고서 종류는 결국 넷으로 접힌다 — **시도·관측 = 내부 분석** / **침해 확인 = 침해사고 신고(24h)** / **유출 경로에 개인정보 = 개인정보 유출 신고·통지(72h)** / **설정 오류·접근 통제 = ISMS 인증 증적**. 1차 상세화는 침해사고 신고다(#613 §3 결정 요청 3 · 안성일 동의 · 서버 1차는 개인정보 유출 신고를 AI 초안 범위에서 제외 — 팀 범위 확정과 별개).

---

## 2. 시나리오 칸 정의

### 2-1. 칸

| 칸 | 내용 |
| --- | --- |
| ⓐ 위협 | §1의 한 행(A·B 번호 · KISA 유형 · ATT&CK) |
| ⓑ 탐지 신호와 시각 | 원천 · 시각 다섯(§2-3) · **사람의 최초 인지는 별도 사람 칸** |
| ⓒ 플랫폼에 남는 기록 | 테이블별로 무엇이 남나 · **#488 JSON에 들어가는 것 / 들어가지 않는 것** |
| ⓓ 보고서 종류와 기한 | 5종 중 어느 것 · 기한 · 걸리는 조건(사람 판단) |
| ⓔ 보고서 항목표 | **`보고서 항목 | 채우는 주체·방식 | JSON 경로 또는 별도 기록 | 부족한 확인 자료`** 4칸 — 주체는 「플랫폼 기록(JSON) / 플랫폼 기록(JSON 밖 · 별도 입력 연결 필요) / AI 문장 / 사람 확인」 넷 중 하나 |
| ⓕ 미확인 기준 | 값이 없을 때 「미확인」으로 남기는 조건(§2-4) |
| ⓖ 추가 확인 · 정정 | 새 사실 → 추가 신고(최초 신고의 추가분임을 밝힘) · 제출본과 작업본 구분 |

### 2-2. #488 JSON 계약 요약 (PR #616 · 머지 전 · 바뀔 수 있음)

| 최상위 | 필드 | 뜻 |
| --- | --- | --- |
| `export` | `version` · `format`(md/json) · `generated_at` · `notice` · `evidence_count` · `missing[]` | 파일 메타. `missing`은 묶음 수준 누락(`no_evidence` · `metric_evidence_absent`) |
| `incident` | `incident_id` · `title` · `subject_arn` · `category` · `status` · `tenant_id` · `created_at` · `updated_at` | 머리말 — 식별·분류·상태만. **위험도 · AI 요약 3줄 · `decision` · 판정 코드는 없다** |
| `evidence[]` | `evidence_id` · `incident_id` · `evidence_type` · `source_type` · `source_id` · `occurred_at` · `collected_at` · `flags{processed, synthetic, excerpt, missing[]}` · `content` | 근거 1건. 현재 SecOps 접수가 만드는 근거는 **THREAT**이다. `EXECUTION` 유형은 계약에 있지만 지금 생성 경로가 없다 |
| THREAT `content` | `event`(유형 · 대상 · `occurred_at` · 중복 키 · `collected_at`(정규화) · `payload`) + 선택 `context`(`captured_at` · `target_status` · `target` · `related_assets[]` · `relation_issues[]` · `log_evidence`) | SSH payload = `source_ip` · `failed_attempt_count` · `window_seconds` / OPEN_IP payload = 프로토콜 · 포트 · CIDR. `context.target.asset`은 접수 때 확보한 자산 사본이지 사건 당시 재구성이 아니다 |
| **들어가지 않는 것** | `action_executions` · `execution_steps` · `audit_events`(승인) · `guardrail_evaluations` · `platform_events` · 외부 알림 발송 이력 · AI 요약·위험도 | 보고서가 쓰려면 **별도 입력 연결이 필요**하거나 **연결 전 사람 확인**이다. #488에 전부 추가하자는 요청은 아니다 |

### 2-3. 시각 다섯 — 서로 대신하지 않는다

| 시각 | 자리 | 뜻 |
| --- | --- | --- |
| 사건·관측 발생 | `evidence[].content.event.occurred_at` · `evidence[].occurred_at` | 입력이 말한 발생 시각 |
| 이벤트 정규화 | `evidence[].content.event.collected_at` | 서버가 위협 이벤트를 받아 정규화한 시각 |
| 근거 저장 | `evidence[].collected_at` | 근거 행을 저장한 시각 |
| 플랫폼 인시던트 생성 | `incident.created_at` | **「플랫폼이 인시던트를 만든 시각」이다. 사람의 최초 인지가 아니다** |
| 파일 생성 | `export.generated_at` | 내보낸 시각 |

**사람의 최초 인지 시각**(신고 기한의 기산점)은 위 어느 것으로도 대신하지 않고 사람 칸에 적는다. 법적 기산점을 이 문서가 정하지 않는다.

### 2-4. 미확인 기준

- `evidence`가 빈 배열이면 관측 자체가 「미확인」 — `export.missing`에 `no_evidence`가 있는지 본다.
- `content.context`가 null · `context.target`이 null(`target_status`가 `not_collected` · `absent` · `missing_collection`) · `log_evidence`가 null이면 대상·로그 칸은 「미확인」.
- `flags.missing[]`은 근거 1건의 누락 사유, `export.missing[]`은 묶음 누락이다. **빈 목록을 자료 전체의 완전성으로 읽지 않는다.** `synthetic=false`가 실데이터 보장이 아니고, `excerpt=true`는 발췌 정책 표시라 실제 생략 여부는 원래 건수와 실린 건수를 대조한다.
- JSON 밖 기록(조치 · 승인 · 발송)은 연결 전까지 「연결 전 사람 확인」.

---

## 3. 시나리오

### 3-1. A2 · SSH 무차별 대입 (B1 · KISA 서버 해킹 · ATT&CK T1110)

**ⓐ 위협** — 외부 IP가 고객사 EC2의 SSH(22)에 짧은 시간 반복 로그인 시도. 플랫폼이 **직접** 보는 유일한 공격 행위다(A1은 설정 노출). 공격자의 `source_ip`와 대상 인스턴스의 IP는 다른 값이다.

**ⓑ 탐지 신호와 시각**

| 항목 | 내용 |
| --- | --- |
| 원천 | 지금: 모의 위협 입력 → `threat_normalizer` → `NormalizedThreatEvent`. T-14 뒤: GuardDuty `UnauthorizedAccess:EC2/SSHBruteForce`(Low · 가해 측이면 High)도 들어올 수 있다 — 매핑은 #588 C단계가 정한다(계획) |
| 신호 값 | `SshBruteForceThreatPayload` = `source_ip` · `failed_attempt_count` · `window_seconds` |
| 판정(`_evaluate_ssh_bruteforce`) | 1회 이하 → LOW(`RISK_SSH_LOW_SIGNAL`, 속도 무관) / **10회 이상 그리고 분당 20회 이상 → HIGH**(`RISK_SSH_BRUTEFORCE`) / 둘 다 미달 → LOW / 한쪽만 충족 → MEDIUM(`RISK_SSH_BRUTEFORCE`) |
| 응답 모드 | HIGH → `PRE_MITIGATION_0_5S` = **즉시 선차단(목표 3초 · ADR-0011)** · MEDIUM/LOW → `AGENT_WAIT`(승인 대기) · MEDIUM은 **승인 대기 시작부터 60초** 무응답이면 조건 충족 EC2 대상 자동 격리(`TIMEOUT_ISOLATION_1M`) · LOW는 자동 격리 없음 |
| 시각 | §2-3의 다섯 + **사람 최초 인지(사람 칸)** |

**ⓒ 플랫폼에 남는 기록**

| 자리 | 남는 것 | #488 JSON |
| --- | --- | --- |
| `threat_events` | 유형 · payload · 중복 키(대상+공격 IP+횟수+창+발생 시각) — 재관측(횟수 상승)은 별 행 | 포함 — `evidence[].content.event` |
| `incidents` | SECOPS · 위험도 · `IncidentStatus`(`ANALYZING` → `AWAITING_APPROVAL` → `ACTION_IN_PROGRESS` → `AWAITING_CLOSURE` → `RESOLVED`/`FAILED`) · AI 요약 3줄 · 근거 선행 `decision`(ADR-0005 1차) | 머리말에 식별·분류·상태만. **위험도 · 요약 · `decision` · 판정 코드는 JSON 밖 기록** |
| `evidence_items` | THREAT 근거(`EvidenceItem` · `source_type`=`threat_event` · `content` = event + context) | 포함 — 본체 |
| `action_executions` | `RUNBOOK_NACL_ADD_DENY`(출발지 차단) · `RUNBOOK_EC2_ISOLATE`(격리) · 해제 `RUNBOOK_NACL_RESTORE` · `RUNBOOK_EC2_UNISOLATE` · 시각 · 결과 · `trigger_source`(실행 **계기** — 승인이나 실행 결과와 별개) | **불포함 — 별도 입력 연결 필요 · 연결 전 사람 확인** |
| T-11 만료 해제 | SUCCESS로 끝난 `RUNBOOK_NACL_ADD_DENY`의 `finished_at`부터 24시간 뒤 조건을 다시 확인해 해제를 **예약**한다(`TriggerSource.NACL_DENY_EXPIRED_24H`). 그 시각에 해제가 성공한다는 보장은 아니다 | 불포함 |
| `audit_events`(T-2) | 승인자 · 시각 · 대상 | 불포함 — 별도 입력 연결 필요 |
| `platform_events`(#592) | 상태 변경 · 반출 기록(파일 조립 뒤 따로 남는 기록 — 파일 내용이 아니다) | 불포함 |
| 외부 알림 outbox(T-9) | 발송 상태(`SLACK_POSTED` 등) | 불포함 |

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| 시도만 — 차단·격리로 끝, 로그인 성공 징후 없음 | **내부 분석**(보고서 없음) · ISMS 접근통제 운영 증적 | — | 플랫폼 기록만으로 닫힘 |
| 성공 징후 — 호스트 로그인 성공 · 후속 행위(B4·B5·B8 finding 동반) | **침해사고 신고**(과기정통부/KISA) | 안 뒤 **24시간** · 추가 사실도 확인 후 24h | 정보통신서비스 제공자 해당 여부는 고객사 판단 |
| 그 호스트가 개인정보처리시스템이고 유출 확인 | **개인정보 유출 신고·통지** | **72시간** | 1천 명 이상 · 민감/고유식별 · 외부 불법 접근 중 하나 — 이 시나리오는 「외부 불법 접근」으로 거의 자동 충족 |

**ⓔ 침해사고 신고 항목표**

| 보고서 항목 | 채우는 주체·방식 | JSON 경로 또는 별도 기록 | 부족한 확인 자료 |
| --- | --- | --- | --- |
| 신고자 · 기관 정보 | 사람 확인 | `incident.tenant_id`는 식별자뿐 · 테넌트 API에 고객사명은 있지만 **신고 담당부서·연락처는 없다** | 담당부서 · 연락처(사람 입력 또는 별도 출처) |
| 사고 유형 | 문서 매핑(B1 → 「서버 해킹」) · AI 문장 | 이 문서 §1 | 성공 여부에 따라 「시도」/「침해」 |
| 발생 시각 | 플랫폼 기록(JSON) | `evidence[].content.event.occurred_at` · `evidence[].occurred_at` | — |
| 플랫폼 인시던트 생성 시각 | 플랫폼 기록(JSON) | `incident.created_at` | 사람 최초 인지와 다르다 |
| **최초 인지 시각(기산점)** | **사람 확인** | — | 사람 칸 · 법적 판단은 고객사 |
| 피해 시스템 | 플랫폼 기록(JSON) + 사람 확인 | `evidence[].content.context.target.asset.resource_id`(인스턴스 ID) · 리전은 `subject_arn` · `private_ip`는 nullable | `context`·`target`이 null이면 미확인 · **공개 IP는 `Ec2Spec`에 없어 사람 확인** · 호스트 역할·보유 정보 |
| 공격 IP · 방법 | 플랫폼 기록(JSON) · AI 문장 | `evidence[].content.event.payload.source_ip` · `.failed_attempt_count` · `.window_seconds` | 판정 코드(`RISK_SSH_BRUTEFORCE`)는 JSON 밖 — 필요하면 별도 입력 |
| 피해 내용 | 사람 확인 | — (호스트 auth 로그는 플랫폼 미수집) | 「미확인」으로 적는다 · 확인된 사실 / 추정 / 미확인 구분은 사고 조사 페이지 |
| 조치 내용 · 결과 | 플랫폼 기록(JSON 밖) → 연결 전 사람 확인 | `action_executions`(런북 · 시각 · 결과) · 승인은 `audit_events` · 계기는 `trigger_source` — **셋을 따로 표시** | 별도 입력 연결 필요 · 호스트 내부 조치는 사람 |
| 추가 보고 여부 | 사람 확인 | — | 조사 진행에 따라 |

**ⓕ 미확인 기준** — §2-4 그대로. 특히 `context`가 null인 SSH 근거는 대상 칸이 전부 미확인이다.

**ⓖ 추가 확인 · 정정** — 격리 뒤 호스트 조사에서 성공이 확인되면 「최초 신고의 추가분」으로 24h 안에 다시 신고(KISA 안내서). 플랫폼 쪽 요구: 제출한 본(스냅숏)과 그 뒤 쌓인 근거를 구분해야 한다 — **담당 협의 전 후보 · #488 확정 범위 아님**(#613 완료 조건 5로 넘김).

**이 시나리오의 공백(플랫폼이 못 채움)** — ① 로그인 성공 여부(호스트 로그) ② 호스트 내부 피해 범위 ③ 그 호스트의 개인정보 보유 여부(자산 태그·고객 선언 없음) ④ 신고 접수 상태 ⑤ 공개 IP · 담당부서·연락처. ③은 T-7 보호 태그와 별도의 「개인정보처리시스템」 표시가 있어야 자동화된다 — #479 §2 후보(담당 협의 전).

### 3-2. B4 · C&C 통신 · 백도어 (GuardDuty HIGH · KISA 악성코드 감염 · ATT&CK Execution/Persistence)

> **이 절의 플랫폼 입력은 전부 계획이다** — T-14 C단계(탐지 결과 읽기)와 저장·내보내기 계약이 확정되기 전이라 JSON 경로를 적지 않는다. 확정되면 §3-1 ⓔ와 같은 4칸표로 바꾼다.

**ⓐ 위협** — 고객사 EC2가 알려진 명령·제어(C&C) 서버의 IP·도메인과 통신한다. A2와 달리 **외부→내부 시도가 아니라 내부→외부 신호**라, 이 finding 하나가 「이미 감염됐다」는 뜻에 가깝다. 플랫폼은 **GuardDuty 경유(A3)** 로만 본다.

**ⓑ 탐지 신호와 시각**

| 항목 | 내용 |
| --- | --- |
| 원천 | GuardDuty `Backdoor:EC2/C&CActivity.B`(IP · High) · `Backdoor:EC2/C&CActivity.B!DNS`(도메인 · High) · 유사: `Trojan:EC2/DGADomainRequest.B`(High) · `Trojan:EC2/BlackholeTraffic`(Medium) |
| 플랫폼 수신(계획) | #588 C단계 — 수집 주기마다 고객 역할로 `ListFindings`(마지막 읽은 갱신 시각 이후) → `GetFindings`(50건씩) → `ThreatEventType`에 더할 유형(제안 `GUARDDUTY_FINDING`) · 페이로드 = 탐지 ID · 탐지 유형 · 심각도 · 대상 자원 ARN · 리전 · 최초·최근 관측 시각 · 중복은 탐지 ID 기준 |
| 판정(확정 규칙) | 심각도 7.0 이상 → HIGH · 4.0–6.9 → MEDIUM · 그 아래 LOW(#588 Q2). 인시던트는 MEDIUM 이상만(Q3) — 이 finding은 High(8.x)라 HIGH 인시던트 |
| 응답 모드 | **자동 조치 없음**(1차 · #588 Q2 · 보호 태그 검사 #598 선행). 인시던트와 조치 후보(격리 · 출발지 차단)까지만 → 관제자 승인(계획) |
| 시각 | GuardDuty `eventFirstSeen` / `eventLastSeen`(발생) · 수집 주기 수신 · 플랫폼 인시던트 생성 · **사람 최초 인지(사람 칸)**. 발생과 플랫폼 생성 사이에 수집 주기만큼 간격이 생긴다 |

**ⓒ 플랫폼에 남는 기록(계획)** — §3-1 ⓒ와 같은 자리. 다른 점 셋: ① 위협 이벤트 payload가 GuardDuty 탐지 요약(원본 finding JSON 전체 보존 여부는 C단계가 정한다 — 보존하지 않으면 「원 출처」는 고객 계정 GuardDuty 콘솔) ② `action_executions`는 승인 뒤에만 생긴다(자동 없음) ③ **GuardDuty finding은 관측이지 침해 증거가 아니다**(#588 Q2 SEC 확인) — 「피해 내용」은 플랫폼 기록만으로 확정하지 않는다.

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| finding 1건 · 호스트 조사 전 | **내부 분석** 시작(사고 조사 페이지 개설) | — | 「추정: 감염」 · 「미확인: 범위」로 기록 |
| 호스트에서 악성 프로세스 · 지속화 확인 | **침해사고 신고**(악성코드 감염) | 안 뒤 **24시간** | 정보통신서비스 제공자 해당 여부 |
| 같은 호스트에 B5(채굴) · B8(유출) finding 동반 | 침해사고 신고 + 유출 경로면 **72h 검토**(§3-3) | — | finding 묶음을 한 인시던트로 볼지는 플랫폼 묶음 규칙(미정) |

**ⓔ 침해사고 신고 항목표(계획)** — §3-1과 같은 항목. 주체·방식만 적고 경로는 비운다:

| 보고서 항목 | 채우는 주체·방식 | JSON 경로 또는 별도 기록 | 부족한 확인 자료 |
| --- | --- | --- | --- |
| 사고 유형 | 문서 매핑(B4 → 「악성코드 감염」) · AI 문장 | 이 문서 §1 | — |
| 공격 IP · 방법 | 플랫폼 기록(계획) · AI 문장 「C&C 통신 관측」 | finding의 원격 IP/도메인 · 탐지 유형 — 경로 미정 | **감염 경로(초기 침입)는 미확인** — 선행 finding(A2 · B13) 대조는 사람 |
| 피해 내용 | 사람 확인 | — | 호스트 포렌식 결과 · 「미확인」 |
| 조치 내용 · 결과 | 플랫폼 기록(JSON 밖) → 연결 전 사람 확인 | 승인 뒤 `action_executions` · 승인 `audit_events` · 계기 `trigger_source` | 호스트 내부 조치(프로세스 종료 · 재구축) |
| 나머지(신고자 · 시각 · 피해 시스템 · 추가 보고) | §3-1과 같다 | §3-1과 같다 | §3-1과 같다 |

**ⓖ 추가 확인 · 정정** — GuardDuty는 같은 탐지를 **갱신**(건수·최근 관측 시각 증가)한다. 플랫폼이 탐지 ID로 중복을 접으면 「최근 관측 시각」만 바뀌므로, 추가 신고 판단은 사람이 finding 갱신을 보고 한다. 제출본과 작업본 구분 요구는 §3-1과 같다.

**이 시나리오의 공백** — ① 감염 경로(초기 침입) ② 호스트 내부 악성 프로세스 · 지속화 여부 ③ 다른 호스트로의 확산(수평 이동 — GuardDuty EC2 finding에 없음) ④ finding 원본 JSON 보존 여부(C단계). ③은 플랫폼 밖이라 공백 유지.

### 3-3. B8 · 데이터 유출 의심 (GuardDuty `Trojan:EC2/DNSDataExfiltration` · KISA 기타(정보유출) · ATT&CK Exfiltration T1048)

> **이 절의 플랫폼 입력도 계획이다**(§3-2와 같은 이유). 서버 1차는 개인정보 유출 신고·통지를 AI 초안 범위에서 제외한다(안성일 · 팀 범위 확정과 별개) — 이 절은 **사람이 채우는 칸이 왜 많은지**를 남기는 자리다.

**ⓐ 위협** — 고객사 EC2가 DNS 질의에 데이터를 실어 밖으로 보낸다(High). 1판 세 시나리오 중 **침해사고 신고(24h)와 개인정보 유출 신고·통지(72h)가 동시에 걸릴 수 있는 유일한 사례**다. 유사: `Trojan:EC2/DropPoint`(Medium · 탈취 자료 집결지 통신) · IAM 쪽 `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS`(High · A4).

**ⓑ 탐지 신호와 시각** — §3-2와 같은 경로(GuardDuty → T-14 C단계(계획) → HIGH 인시던트 · 자동 조치 없음). 다른 점: finding이 말해 주는 것은 「DNS로 나간 데이터가 있다」까지고 **무엇이 나갔는지는 없다.**

**ⓒ 플랫폼에 남는 기록(계획)** — §3-2와 같다. 유출 「항목 · 규모」를 채울 기록이 플랫폼에 **없다**(호스트 파일 · DB 접근 로그 · VPC Flow Logs 원본은 수집 범위 밖).

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| finding 1건 | **내부 분석** + 침해사고 신고 검토 | 24h 시계 시작 여부는 「침해 확인」 시점(사람) | — |
| 호스트에서 반출 확인(무엇이든) | **침해사고 신고**(정보유출) | 안 뒤 **24시간** | — |
| 반출된 것에 개인정보 포함 | **개인정보 유출 신고**(개인정보위 · 전문기관) + **정보주체 통지** | 안 날부터 **72시간**(통지도 원칙 72h) | 신고 대상: 1천 명 이상 **또는** 민감·고유식별정보 **또는** 외부 불법 접근 중 하나 — 이 시나리오는 「외부 불법 접근」으로 거의 자동 충족 · 통지는 규모와 무관 |
| 유출 규모 · 항목이 조사 중 | **확인된 사실로 우선 신고 뒤 추가분 신고**(개인정보 유출등 대응 안내서) | 72h 안 1차 | 「조사 중」을 숨기지 않는다 |

**ⓔ 개인정보 유출 신고·통지 항목표(계획)** — 신고서 항목(유출 항목 · 규모 · 경위 · 시점 · 피해 최소화 조치와 결과 · 정보주체 구제절차 · 담당부서·연락처) 기준:

| 보고서 항목 | 채우는 주체·방식 | JSON 경로 또는 별도 기록 | 부족한 확인 자료 |
| --- | --- | --- | --- |
| 유출 시점 | 플랫폼 기록(계획) + 사람 확인 | finding 최초·최근 관측 · 플랫폼 인시던트 생성 — 경로 미정 | 기산점은 사람 |
| 유출 경위 | 플랫폼 기록(계획) · AI 문장 | 「DNS 터널 의심 — GuardDuty 탐지」 · 원격 도메인/IP · 대상 인스턴스 | 초기 침입 경로는 사람(선행 finding 대조) |
| **유출 항목** | **사람 확인** | — | 호스트 · DB 조사 · 「조사 중」 |
| **유출 규모(정보주체 수)** | **사람 확인** | — | 조사 · 「조사 중」 |
| 피해 최소화 조치 · 결과 | 플랫폼 기록(JSON 밖) → 연결 전 사람 확인 | `action_executions`(격리 · 차단 · 해제 시각 · 결과) | 별도 입력 연결 필요 · 비밀번호 초기화 · 키 폐기 등 호스트 조치는 사람 |
| 정보주체 구제절차 · 통지 방법 | 사람 확인 · AI 서식 문안 | — | 고객사 결정 |
| 담당부서 · 연락처 | 사람 확인 | 테넌트 API에 없다 | 사람 입력 또는 별도 출처 |

**ⓖ 추가 확인 · 정정** — 24h(과기정통부/KISA)와 72h(개인정보위)는 **제출처가 다르다**. 같은 사건의 두 보고서가 서로 다른 「확인 시점」을 가질 수 있어, 제출본마다 「어느 근거 스냅숏으로 썼나」가 남아야 한다 — §3-1 ⓖ의 제출본/작업본 구분 요구가 여기서 가장 커진다(담당 협의 전 후보).

**이 시나리오의 공백** — ① 유출 항목 ② 유출 규모 ③ 개인정보처리시스템 여부(태그·고객 선언) ④ 초기 침입 경로 ⑤ 두 기관 제출본의 스냅숏 구분. ①②는 플랫폼 밖이라 공백 유지 · ③⑤는 §4로.

### 3-4. 세 시나리오에서 같은 것

- 플랫폼이 **JSON으로 채울 수 있는 칸은 「언제 · 어디서 · 무엇을 관측」** 셋이고, 그것도 근거(`evidence`)가 있을 때만이다(지금은 모의 입력 A1·A2). **「무슨 조치」는 JSON 밖 기록**(`action_executions` · `audit_events`)이라 별도 입력 연결 전엔 사람 확인이다. GuardDuty 경유 위협은 전부 계획 입력이다.
- **「피해 내용 · 유출 항목 · 규모 · 침입 경로」는 전부 사람 칸**이고, AI 초안은 이 칸을 「미확인/조사 중」으로 명시한다 — 비우면 「없음」으로 읽힌다(선례 `MVP_CLOSEOUT_CHECKLIST.md` 머리말).
- 시각은 다섯(§2-3)이고 **사람의 최초 인지는 그중 어느 것도 아니다.** 서식에는 다 적고 기산점은 사람이 고른다.
- `trigger_source`(실행 계기) · 승인(`audit_events`) · 실행 결과(`action_executions` 상태)는 **별개로 표시**한다. 차단·격리 성공이나 관제자의 `RESOLVED` 종료 판단을 원인 제거·복구·신고 전체의 완료로 쓰지 않는다.
- 보고서 문장마다 출처를 셋 중 하나로 가른다 — **서버 기록 / AI 문장 / 사람 확인**. 기록된 사실을 제시하되 준수·충족 판정으로 확대하지 않는다.
- 24h와 72h는 제출처 · 조건 · 항목이 다르다. 한 사건에 보고서가 둘 나갈 수 있고, 각각 제출본 스냅숏이 필요하다.

---

## 4. 공백 모음 — 넘길 자리 (전부 **담당 협의 전 후보 · #488 확정 범위 아님**)

| 공백 | 어디로 | 상태 |
| --- | --- | --- |
| 호스트 로그인 성공 여부 · 호스트 내부 피해 범위(§3-1) | #479 §2 후보(시스템·네트워크 로그 분석 모듈 — 안성일 10/30 아이디어 2) | 미제기 |
| 개인정보처리시스템 표시(태그 또는 고객 선언) | #479 §2 후보 | 미제기 |
| 조치·승인·발송 기록을 보고서 입력으로 연결(JSON 밖 → 별도 입력) | #488 후속 또는 AI 신고 초안 설계(안성일) | 미제기 |
| 제출본(스냅숏)과 작업본 구분 | #488 후속 | 미제기 |
| 공개 IP · 신고 담당부서·연락처 출처 | 사람 입력 유지 또는 테넌트 프로필 확장 제안 | 미제기 |
| IAM finding 수집 여부(A4 · B9~B11 · B15) | #588(김승철 확인 사항) | #613 코멘트 `6784418428` §④-2 |
| `platform_events` 로그인 실패 기록 호출(A5) | #589 B | 안성일 #589 코멘트 `6783885796` |
| DoS 수신 · 랜섬웨어 · 고객사 내부자(B7 · B12 · B16) | 2판에서 공백 행 유지 | — |

---

## 변경 이력

| 날짜 | 내용 |
| --- | --- |
| 2026-11-11 | 1판 — §0 원천 · §1 분류표(#613 코멘트 `6784418428` 옮김) · §2 칸 정의 · §3 시나리오 3건(A2 SSH 무차별 대입 · B4 C&C/백도어 · B8 데이터 유출 의심) + §3-4 공통점 · §4 공백 모음. `[S]` 1행(B13)은 원문 재확인 전 |
| 2026-11-11 | PR #614 리뷰(안성일 · #613 코멘트 `6785035815`) 반영 — 테이블명 `action_executions`·`evidence_items` · 실행→근거 연결 삭제 · A1 「SG 대상 자동 격리 없음」 · A2 「즉시 선차단(목표 3초)」·MEDIUM 60초 조건·NACL 24h 예약 의미 · A3·B축 GuardDuty 「계획 입력」 · A5 #589 B 연결 필요 · §2-2 #488 JSON 계약 요약(PR #616 · 머지 전) · §2-3 시각 다섯과 사람 최초 인지 분리 · §2-4 미확인 기준 · ⓔ 항목표를 4칸(주체·JSON 경로/별도 기록·부족 자료)으로 · 공개 IP·담당부서는 사람 확인 · §3-4에 기록≠판정 · `trigger_source`/승인/결과 분리 |

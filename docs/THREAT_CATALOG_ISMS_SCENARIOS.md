# 고객사 위협 분류표와 위협별 ISMS 보고서 작성 시나리오 (1판)

> **담당**: 박지현 (QA) — 이슈 [#613](https://github.com/ProjectVigilantis/vigilantis/issues/613)
> **쓰는 곳**: ISMS 보고서 서버(안성일 · [#488](https://github.com/ProjectVigilantis/vigilantis/issues/488) 근거 내보내기 · 사고 조사 페이지 · AI 신고 초안)가 위협별 대응 방식과 초안 입력을 정할 때 본다(SSOT §확정 결정 로그 2026-11-06 ISMS 행).
>
> **이 문서는 코드·서버·화면을 정하지 않는다.** 위협마다 「어느 보고서를 · 언제까지 · 무슨 항목으로 · 누가(플랫폼/AI/사람) 채우는가」를 적는 자리다. 공식 서식과 법 조문은 기관 링크로 가리키며 법적 검토의 완결을 주장하지 않는다. 기관 제출은 고객사가 한다(2026-11-06 결정).
> 빈칸 · `[S]` · 「공백」은 **미확인**이다. `[S]`는 외부 문서를 요약분으로 읽은 행이라 PR 전에 원문으로 재확인한다.

---

## 0. 원천

| 항목 | 원천 | 상태 |
| --- | --- | --- |
| 이 문서의 범위와 완료 조건 | #613 본문 §1·§2 · 완료 조건 6 | 확정(11/11 발행) |
| 제출 목적 5종(ISMS/ISMS-P 인증 · 침해사고 신고 24h · 개인정보 유출 신고·통지 72h · 개인정보위 자료 제출 · 내부 분석) | 안성일 13주차 조사 — #479 코멘트(2026-11-03) §1 | 확정 |
| ISMS 보고서 범위(근거 내보내기 · 사고 조사 · AI 신고 초안) · 제출은 고객사 | SSOT §확정 결정 로그 2026-11-06 ISMS 행 | 확정 |
| 근거 내보내기 계약(md·JSON · 인시던트 묶음 · 공격 IP 미마스킹) | #488 2026-11-09 코멘트(안성일) | 서버 1차 범위 확정 · 구현 중 |
| GuardDuty 위험도 매핑(7.0↑ HIGH · 4.0–6.9 MEDIUM) · 인시던트는 MEDIUM↑ · 1차 자동 대응 없음 | #588 Q2 · Q3 | 확정(SEC 동의 11/9) |
| 플랫폼이 보는 위협 유형과 판정 | `packages/schemas/events.py` — `ThreatEventType` · `RiskReasonCode` · `expected_mode_for` / `apps/core-api/security/risk_evaluator.py` | 코드 |
| 기록이 남는 자리 | `threat_events` · `incidents`(`IncidentStatus`) · `executions`(`RunbookId`) · `evidence`(`EvidenceItem`) · `audit_events`(T-2) · `platform_events`(#592 · `PlatformEventCategory` · `PlatformEventAction`) | 코드(#592는 11/11 머지) |
| 외부 분류 | AWS GuardDuty finding type 문서(EC2 · IAM) · KISA 침해사고 신고 유형 4종(DDoS · 악성코드 감염 · 서버 해킹 · 기타) · MITRE ATT&CK v20 Enterprise IaaS 매트릭스 | 외부 · 2026-11-11 읽음 |

---

## 1. 위협 분류표

### 1-1. A축 — 플랫폼이 지금·곧 보는 위협

| # | 위협 | 원천(신호) | 플랫폼 기록 | 자동 조치 | 보고서 종류(후보) |
| --- | --- | --- | --- | --- | --- |
| A1 | 공개 인그레스 노출 — 전체 개방 `RISK_OPEN_INGRESS_WORLD` · 22/3389 노출 `RISK_SENSITIVE_PORT_EXPOSED` · 전 프로토콜 `RISK_ALL_PROTOCOL_OPEN` · 전 포트 `RISK_ALL_PORTS_EXPOSED` | SG 관측 → `ThreatEventType.OPEN_IP`(중복 키 = SG+프로토콜+포트+CIDR+시각) | `threat_events` → `incidents`(SECOPS) → `executions`(SG 차단 런북) → `evidence` | MEDIUM(관제자 승인) | 사고 아님 → ISMS 통제 증적 · 내부 분석. 침해와 결합되면 「경위」 칸 |
| A2 | SSH 무차별 대입 `RISK_SSH_BRUTEFORCE` · 임계 미만 `RISK_SSH_LOW_SIGNAL` | `ThreatEventType.SSH_BRUTE_FORCE`(대상 EC2+공격 IP+횟수+관측 창) | 위와 같음 + NACL 차단(T-11 24h 만료) · 격리 | HIGH 0.5초 선차단 · MEDIUM 승인 대기 → 1분 무응답 자동 격리 | 시도 = 내부 분석 / 성공 = 침해사고 신고 24h(서버 해킹) — **§3-1** |
| A3 | GuardDuty **EC2** finding 40종(Backdoor 8 · Behavior 2 · CryptoCurrency 2 · DefenseEvasion 3 · Impact 7 · Recon 3 · Trojan 9 · UnauthorizedAccess 6) | 고객 계정 GuardDuty → 주기 조회(#588 C · 미구현 · 켜기는 PR #593 11/11 머지) | `threat_events` 전부 · `incidents`는 MEDIUM↑ | 없음(1차 · #598 보호 태그 선행) | prefix별로 B축에 배정 |
| A4 | GuardDuty **IAM** finding 28종(13 prefix) | 위와 같음 — **읽을지 미정**(#588 결정 요청) | — | — | 자격 증명·계정 탈취 → 24h · 유출 경로면 72h |
| A5 | 플랫폼 자체 — 로그인 경로 무차별 대입 · API 남용 | WAF 속도 제한 Count(T-3 #522) · `platform_events` 로그인 실패 | `platform_events` | Block 전환은 11/16 뒤 | 우리가 신고 주체 — 시나리오는 별도 1건(2판) |

### 1-2. B축 — 고객사(EC2 + IAM)에 들어올 수 있는 위협 상위 유형

| # | 위협 유형 | ATT&CK(IaaS) | GuardDuty 예(기본 심각도) | KISA 유형 | 플랫폼이 보는가 | 보고서(후보) |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | 무차별 대입(SSH · RDP · WinRM) | Credential Access T1110 | `UnauthorizedAccess:EC2/SSHBruteForce` · `RDPBruteForce`(Low · 가해 측이면 High) · `Impact:EC2/WinRMBruteForce` | 서버 해킹 | **직접**(SSH = A2) · RDP/WinRM은 GD | 내부 분석 → 성공 시 24h |
| B2 | 정찰 · 포트 스캔 · 노출 포트 탐침 | Discovery T1046 | `Recon:EC2/Portscan`(M) · `Recon:EC2/PortProbeUnprotectedPort`(L · 9200/9300은 H) | 서버 해킹(전조) | GD | 내부 분석 · ISMS 증적 |
| B3 | 설정 오류 노출(전체 개방 SG · 민감 포트) | Initial Access T1190 전조 | — | — | **직접**(A1) | ISMS 증적 |
| B4 | C&C 통신 · 백도어 · 트로이 | Execution T1059 · Persistence | `Backdoor:EC2/C&CActivity.B`(H) · `Trojan:EC2/DGADomainRequest.B`(H) · `BlackholeTraffic`(M) | 악성코드 감염 | GD | 침해사고 24h — **§3-2** |
| B5 | 암호화폐 채굴(자원 탈취) | Impact T1496 | `CryptoCurrency:EC2/BitcoinTool.B`(H) · `Impact:EC2/BitcoinDomainRequest.Reputation`(H) | 악성코드/서버 해킹 | GD | 침해사고 24h |
| B6 | DoS **발신**(내 자산이 가해자) | Impact T1498/T1499 | `Backdoor:EC2/DenialOfService.*`(H · 5종) · `Spambot`(M) | DDoS(가해 측) | GD | 침해사고 24h |
| B7 | DoS **수신**(피해) | Impact T1498/T1499 | — (Shield 영역) | DDoS | **공백** | 침해사고 24h — 플랫폼 밖 근거 |
| B8 | 데이터 유출(DNS 터널 · 외부 반출) | Exfiltration T1048/T1537 | `Trojan:EC2/DNSDataExfiltration`(H) · `Trojan:EC2/DropPoint`(M) | 기타(정보유출) | GD | 침해사고 24h + 개인정보 포함 시 72h — **§3-3** |
| B9 | 자격 증명 유출 · 인스턴스 역할 자격 반출 | Credential Access T1552 · T1528 | `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS`(H) · `UnauthorizedAccess:EC2/MetadataDNSRebind`(H) · `CredentialAccess:IAMUser/CompromisedCredentials`(H) | 서버 해킹(계정 탈취) | GD(EC2 1종) · IAM은 A4 결정 전 | 24h · 유출 경로면 72h |
| B10 | 계정 탈취 · 권한 상승 · 지속화(IAM 변경) | Valid Accounts T1078 · Account Manipulation T1098 | `Persistence/PrivilegeEscalation:IAMUser/AnomalousBehavior`(M) · `Policy:IAMUser/RootCredentialUsage`(L) · `ConsoleLoginSuccess.B`(M) | 서버 해킹 | A4 결정 전 | 24h |
| B11 | 탐지 회피 · 로그 끄기 | Defense Impairment T1685 · T1578 | `Stealth:IAMUser/CloudTrailLoggingDisabled`(L) · `DefenseEvasion:IAMUser/AnomalousBehavior`(M) · `DefenseEvasion:EC2/UnusualDNSResolver`(M) | 서버 해킹(은폐) | GD(EC2) · IAM은 A4 | 근거 훼손 → 「미확인」 칸 증가 |
| B12 | 랜섬웨어 · 데이터 파괴 | Impact T1486 · T1485 · T1490 | — (EC2 finding 없음 · Malware Protection/S3 영역) | 악성코드(랜섬웨어) | **공백** | 24h · 개인정보면 72h |
| B13 | 피싱 · 악성 도메인 질의 · 드라이브바이 | User Execution T1204 | `Trojan:EC2/PhishingDomainRequest!DNS`(H) · `DriveBySourceTraffic!DNS`(H) `[S]` · `Impact:EC2/MaliciousDomainRequest.Reputation`(H) | 악성코드 | GD | 내부 분석 → 감염 확인 시 24h |
| B14 | Tor · 악성 IP 통신 | Stealth · C2 성격 | `UnauthorizedAccess:EC2/TorClient` · `TorRelay`(H) · `MaliciousIPCaller.Custom`(M) · `Recon:IAMUser/TorIPCaller`(M) | 서버 해킹 | GD | 24h |
| B15 | 침투 시험 도구 사용(Kali · Parrot · Pentoo) | — | `PenTest:IAMUser/*`(M) | — | A4 결정 전 | 내부 분석(승인된 시험이면 기록만) |
| B16 | 고객사 내부자 · 권한 오남용 | Valid Accounts T1078 | — | 기타 | **공백**(고객사 측 · 플랫폼 측은 `platform_events`) | 내부 분석 · 개인정보면 72h |

보고서 종류는 결국 넷으로 접힌다 — **시도·관측 = 내부 분석** / **침해 확인 = 침해사고 신고(24h)** / **유출 경로에 개인정보 = 개인정보 유출 신고·통지(72h)** / **설정 오류·접근 통제 = ISMS 인증 증적**. 1차 상세화는 침해사고 신고다(#613 §3 결정 요청 3 · 회의 전 제안).

---

## 2. 시나리오 칸 정의

| 칸 | 내용 | 채우는 쪽 |
| --- | --- | --- |
| ⓐ 위협 | §1의 한 행(A·B 번호 · KISA 유형 · ATT&CK) | 문서 |
| ⓑ 탐지 신호와 시각 | 원천 · 발생 시각(`occurred_at`) / 수집 시각(`collected_at`) / 최초 인지(인시던트 생성) 구분 | 플랫폼 |
| ⓒ 플랫폼에 남는 기록 | 테이블·화면별로 무엇이 남나 · #488 내보내기에 담기는 것/안 담기는 것 | 플랫폼 |
| ⓓ 보고서 종류와 기한 | 5종 중 어느 것 · 기한 · 걸리는 조건 | 사람(조건 판단) |
| ⓔ 보고서 항목 | 그 종류의 필수 항목 | 서식 |
| ⓕ 플랫폼 / AI / 사람 | 항목마다 자동(기록) · 반자동(AI 초안) · 사람(확인된 사실 · 추정 · 미확인은 사고 조사 페이지) | — |
| ⓖ AI 초안 입력 | #488 JSON의 어느 필드가 어느 항목으로 | 서버(안성일) |
| ⓗ 추가 확인 · 정정 | 새 사실 → 추가 신고(최초 신고의 추가분임을 밝힘) · 제출본과 작업본 구분 | 사람 + 플랫폼 |

---

## 3. 시나리오

### 3-1. A2 · SSH 무차별 대입 (B1 · KISA 서버 해킹 · ATT&CK T1110)

**ⓐ 위협** — 외부 IP가 고객사 EC2의 SSH(22)에 짧은 시간 반복 로그인 시도. 플랫폼이 **직접** 보는 유일한 공격 행위다(A1은 설정 노출).

**ⓑ 탐지 신호와 시각**

| 항목 | 내용 |
| --- | --- |
| 원천 | 지금: 모의 위협 입력 → `threat_normalizer`(김승철) → `NormalizedThreatEvent`. T-14 뒤: GuardDuty `UnauthorizedAccess:EC2/SSHBruteForce`(기본 Low · 내 인스턴스가 가해 측이면 High)도 같은 유형으로 들어올 수 있다 — 매핑은 #588 C단계가 정한다 |
| 신호 값 | `SshBruteForceThreatPayload` = `source_ip` · `failed_attempt_count` · `window_seconds` |
| 판정(`risk_evaluator`) | 1회 → LOW(`RISK_SSH_LOW_SIGNAL`) / **10회 이상 그리고 분당 20회 이상 → HIGH**(`RISK_SSH_BRUTEFORCE`) / 둘 다 미달 → LOW / 한쪽만 → MEDIUM |
| 응답 모드 | HIGH → `PRE_MITIGATION_0_5S`(0.5초 안 선차단) · MEDIUM/LOW → `AGENT_WAIT`(승인 대기) · MEDIUM 1분 무응답 → `TIMEOUT_ISOLATION_1M`(자동 격리) |
| 시각 셋 | 발생 `occurred_at`(입력) · 수집 `collected_at`(서버 수신) · **최초 인지 = 인시던트 생성(`ANALYZING`)** — 신고 기한 24h의 기산점은 「사실을 안 때」라 셋 중 어느 것인지는 고객사 판단(사람) |

**ⓒ 플랫폼에 남는 기록**

| 자리 | 남는 것 | #488 내보내기 |
| --- | --- | --- |
| `threat_events` | 유형 · payload · 중복 키(대상+공격 IP+횟수+창+시각) — 재관측(횟수 상승)은 별 행 | 근거 원천으로 포함 |
| `incidents` | SECOPS · 위험도 · `IncidentStatus`(`ANALYZING` → `AWAITING_APPROVAL` → `ACTION_IN_PROGRESS` → `AWAITING_CLOSURE` → `RESOLVED`/`FAILED`) · AI 요약 3줄 · 근거 선행 `decision`(ADR-0005 1차) | 머리말 |
| `executions` | `RUNBOOK_NACL_ADD_DENY`(출발지 차단) · `RUNBOOK_EC2_ISOLATE`(격리) · 해제 `RUNBOOK_NACL_RESTORE` · `RUNBOOK_EC2_UNISOLATE` · T-11 자동 해제는 트리거 `TriggerSource.NACL_DENY_EXPIRED_24H`로 구분 — 각 시각·결과 | 조치·결과 |
| `evidence` | `EvidenceItem`(evidence_type · source_type=`threat_event` · content · occurred_at/collected_at) | 본체(코드 펜스) |
| `audit_events`(T-2) | 승인자 · 시각 · 대상 | 포함 여부 서버 결정 |
| `platform_events`(#592) | 상태 변경 · 반출 1행(`INCIDENT_EVIDENCE_EXPORT` 예정) | 반출 기록 자체 |
| 외부 알림 outbox(T-9) | Slack/이메일 발송 상태(`SLACK_POSTED` 등) | 「통지 이력」 칸 후보 |

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| 시도만 — 차단·격리로 끝, 로그인 성공 징후 없음 | **내부 분석**(보고서 없음) · ISMS 접근통제 운영 증적 | — | 플랫폼 기록만으로 닫힘 |
| 성공 징후 — 호스트 로그인 성공 · 후속 행위(B4·B5·B8 finding 동반) | **침해사고 신고**(과기정통부/KISA) | 안 뒤 **24시간** · 추가 사실도 확인 후 24h | 정보통신서비스 제공자 해당 여부는 고객사 판단 |
| 그 호스트가 개인정보처리시스템이고 유출 확인 | **개인정보 유출 신고·통지** | **72시간** | 1천 명 이상 · 민감/고유식별 · 외부 불법 접근 중 하나 — 「외부 불법 접근」은 이 시나리오에서 거의 자동 충족 |

**ⓔ·ⓕ 침해사고 신고 항목과 채우는 쪽**

| 항목 | 플랫폼(자동) | AI 초안 | 사람 |
| --- | --- | --- | --- |
| 신고자 · 기관 정보 | 고객사 프로필(테넌트) | — | 확인 |
| 사고 유형 | 「서버 해킹」(B1 매핑) | 문안 | 확정 |
| 발생 · 인지 시각 | `occurred_at` · 인시던트 생성 시각 | 시각 셋을 문장으로 | **기산점 선택** |
| 피해 시스템 | 인스턴스 ID · 리전 · 공개 IP(자산 기록) | — | 호스트 역할·보유 정보 |
| 공격 IP · 방법 | `source_ip` · 횟수 · 관측 창 · 판정 코드 | 「무차별 대입」 서술 | — |
| 피해 내용 | **공백**(로그인 성공 여부는 호스트 auth 로그 — 플랫폼 미수집) | 「미확인」으로 표기 | 확인된 사실 · 추정 · 미확인 구분(사고 조사 페이지) |
| 조치 내용 · 결과 | `executions` 시각·결과(차단 · 격리 · 해제) | 시간순 문장 | 호스트 내부 조치 추가 |
| 추가 보고 여부 | — | — | 조사 진행에 따라 |

**ⓖ AI 초안 입력(#488 JSON)** — 인시던트 머리말(id · 분류 · 위험도 · 상태 · 시각) + `threat_events` payload + `executions`(런북 · 상태 · 시각) + `evidence` 항목 + 발송 이력. 공격 IP · 사설 IP · 계정명은 마스킹하지 않는다(#488 11/9 결정 — 신고에 필요). md 머리말 「내부 전달용 · 개인정보 포함 가능」 고지 그대로.

**ⓗ 추가 확인 · 정정** — 격리 뒤 호스트 조사에서 성공이 확인되면 「최초 신고의 추가분」으로 24h 안에 다시 신고(KISA 안내서). 플랫폼 쪽 요구: 제출한 본(스냅숏)과 그 뒤 쌓인 근거를 구분해야 한다 — 서버 2차 범위 후보(#488 「후속」에 없음 → #613 완료 조건 5로 넘김).

**이 시나리오의 공백(플랫폼이 못 채움)** — ① 로그인 성공 여부(호스트 로그) ② 호스트 내부 피해 범위 ③ 그 호스트의 개인정보 보유 여부(자산 태그·고객 선언 없음) ④ 신고 접수 상태. ③은 T-7 보호 태그와 별도의 「개인정보처리시스템」 태그가 있어야 자동화된다 — #479 §2 후보.

### 3-2. B4 · C&C 통신 · 백도어 (GuardDuty HIGH · KISA 악성코드 감염 · ATT&CK Execution/Persistence)

**ⓐ 위협** — 고객사 EC2가 알려진 명령·제어(C&C) 서버의 IP·도메인과 통신한다. A2와 달리 **외부→내부 시도가 아니라 내부→외부 신호**라, 이 finding 하나가 「이미 감염됐다」는 뜻에 가깝다. 플랫폼은 **GuardDuty 경유(A3)** 로만 본다.

**ⓑ 탐지 신호와 시각**

| 항목 | 내용 |
| --- | --- |
| 원천 | GuardDuty `Backdoor:EC2/C&CActivity.B`(IP · High) · `Backdoor:EC2/C&CActivity.B!DNS`(도메인 · High) · 유사: `Trojan:EC2/DGADomainRequest.B`(High) · `Trojan:EC2/BlackholeTraffic`(Medium) |
| 플랫폼 수신 | T-14 C단계(#588 · 미구현) — 수집 주기마다 고객 역할로 `ListFindings`(마지막 읽은 갱신 시각 이후) → `GetFindings`(50건씩) → `ThreatEventType`에 더할 유형(제안 `GUARDDUTY_FINDING`) · 페이로드 = 탐지 ID · 탐지 유형 · 심각도 · 대상 자원 ARN · 리전 · 최초·최근 관측 시각 · 중복은 탐지 ID 기준 |
| 판정 | 심각도 7.0 이상 → HIGH · 4.0–6.9 → MEDIUM · 그 아래 LOW(#588 Q2). 인시던트는 MEDIUM 이상만(Q3) — 이 finding은 High(8.x)라 HIGH 인시던트 |
| 응답 모드 | **자동 조치 없음**(1차 · #588 Q2 · 보호 태그 검사 #598 선행). 인시던트와 조치 후보(격리 · 출발지 차단)까지만 → 관제자 승인 |
| 시각 셋 | GuardDuty `eventFirstSeen` / `eventLastSeen`(발생) · 수집 주기 수신(`collected_at`) · 인시던트 생성(최초 인지). **발생과 인지 사이에 수집 주기만큼 간격**이 생긴다 — 24h 기산점 판단 때 이 간격을 적어야 한다 |

**ⓒ 플랫폼에 남는 기록** — §3-1 ⓒ와 같은 자리. 다른 점 셋: ① `threat_events` payload가 GuardDuty 탐지 요약(원본 finding JSON 전체를 저장하는지는 #588 C단계가 정한다 — 저장하지 않으면 「원 출처」는 고객 계정 GuardDuty 콘솔) ② `executions`는 승인 뒤에만 생긴다(자동 없음) ③ GuardDuty finding은 **관측이지 침해 증거가 아니다**(#588 Q2 SEC 확인) — 보고서의 「피해 내용」은 플랫폼 기록만으로 확정하지 않는다.

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| finding 1건 · 호스트 조사 전 | **내부 분석** 시작(사고 조사 페이지 개설) | — | 「추정: 감염」 · 「미확인: 범위」로 기록 |
| 호스트에서 악성 프로세스 · 지속화 확인 | **침해사고 신고**(악성코드 감염) | 안 뒤 **24시간** | 정보통신서비스 제공자 해당 여부 |
| 같은 호스트에 B5(채굴) · B8(유출) finding 동반 | 침해사고 신고 + 유출 경로면 **72h 검토**(§3-3) | — | finding 묶음을 한 인시던트로 볼지는 플랫폼 묶음 규칙(미정) |

**ⓔ·ⓕ 침해사고 신고 항목과 채우는 쪽** — §3-1과 같은 8항목. 다른 칸만:

| 항목 | 플랫폼(자동) | AI 초안 | 사람 |
| --- | --- | --- | --- |
| 사고 유형 | 「악성코드 감염」(B4 매핑) | 문안 | 확정 |
| 공격 IP · 방법 | finding의 원격 IP/도메인 · 탐지 유형 | 「C&C 통신 관측」 서술 — **감염 경로는 미확인으로** | 감염 경로(초기 침입 — A2·B13 등 선행 finding 대조) |
| 피해 내용 | **공백** | 「미확인」 | 호스트 포렌식 결과 |
| 조치 내용 · 결과 | 승인 뒤 `executions`(격리 · 차단 · 해제) · 승인자(`audit_events`) | 시간순 | 호스트 내부 조치(프로세스 종료 · 재구축) |

**ⓖ AI 초안 입력** — §3-1 + GuardDuty 탐지 요약(유형 · 심각도 · 원격 IP/도메인 · 최초·최근 관측 · 건수). 원격 IP·도메인은 공격 IP와 같은 이유로 마스킹하지 않는다(#488 11/9).

**ⓗ 추가 확인 · 정정** — GuardDuty는 같은 탐지를 **갱신**(건수·최근 관측 시각 증가)한다. 플랫폼이 탐지 ID로 중복을 접으면 「최근 관측 시각」만 바뀌므로, 추가 신고 판단은 사람이 finding 갱신을 보고 한다. 제출본과 작업본 구분 요구는 §3-1과 같다.

**이 시나리오의 공백** — ① 감염 경로(초기 침입) ② 호스트 내부 악성 프로세스 · 지속화 여부 ③ 다른 호스트로의 확산(수평 이동 — GuardDuty EC2 finding에 없음) ④ finding 원본 JSON 보존 여부(#588 C단계). ③은 플랫폼 밖이라 공백 유지.

### 3-3. B8 · 데이터 유출 의심 (GuardDuty `Trojan:EC2/DNSDataExfiltration` · KISA 기타(정보유출) · ATT&CK Exfiltration T1048)

**ⓐ 위협** — 고객사 EC2가 DNS 질의에 데이터를 실어 밖으로 보낸다(High). 1판 세 시나리오 중 **침해사고 신고(24h)와 개인정보 유출 신고·통지(72h)가 동시에 걸릴 수 있는 유일한 사례**다. 유사: `Trojan:EC2/DropPoint`(Medium · 탈취 자료 집결지 통신) · IAM 쪽 `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS`(High · A4 결정 전).

**ⓑ 탐지 신호와 시각** — §3-2와 같은 경로(GuardDuty → T-14 C단계 → HIGH 인시던트 · 자동 조치 없음). 다른 점: finding이 말해 주는 것은 「DNS로 나간 데이터가 있다」까지고 **무엇이 나갔는지는 없다.**

**ⓒ 플랫폼에 남는 기록** — §3-2와 같다. 유출 「항목 · 규모」를 채울 기록이 플랫폼에 **없다**(호스트 파일 · DB 접근 로그 · VPC Flow Logs 원본은 수집 범위 밖).

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| finding 1건 | **내부 분석** + 침해사고 신고 검토 | 24h 시계 시작 여부는 「침해 확인」 시점 | — |
| 호스트에서 반출 확인(무엇이든) | **침해사고 신고**(정보유출) | 안 뒤 **24시간** | — |
| 반출된 것에 개인정보 포함 | **개인정보 유출 신고**(개인정보위 · 전문기관) + **정보주체 통지** | 안 날부터 **72시간**(통지도 원칙 72h) | 신고 대상: 1천 명 이상 **또는** 민감·고유식별정보 **또는** 외부 불법 접근 중 하나 — 이 시나리오는 「외부 불법 접근」으로 거의 자동 충족 · 통지는 규모와 무관 |
| 유출 규모 · 항목이 조사 중 | **확인된 사실로 우선 신고 뒤 추가분 신고**(개인정보 유출등 대응 안내서) | 72h 안 1차 | 「조사 중」을 숨기지 않는다 |

**ⓔ·ⓕ 개인정보 유출 신고·통지 항목과 채우는 쪽** — 신고서 항목(유출 항목 · 규모 · 경위 · 시점 · 피해 최소화 조치와 결과 · 정보주체 구제절차 · 담당부서·연락처)을 기준으로:

| 항목 | 플랫폼(자동) | AI 초안 | 사람 |
| --- | --- | --- | --- |
| 유출 시점 | finding 최초·최근 관측 시각 · 인시던트 생성 시각 | 시각 셋을 문장으로 | 기산점 선택 |
| 유출 경위 | 「DNS 터널 의심 — GuardDuty 탐지」 · 원격 도메인/IP · 대상 인스턴스 | 경위 초안(선행 finding A2·B4·B13 대조) | 초기 침입 경로 확정 |
| **유출 항목** | **공백** | 「조사 중」 | 호스트 · DB 조사 |
| **유출 규모(정보주체 수)** | **공백** | 「조사 중」 | 조사 |
| 피해 최소화 조치 · 결과 | `executions`(격리 · 차단 · 해제 시각 · 결과) | 시간순 | 비밀번호 초기화 · 키 폐기 등 호스트 조치 |
| 정보주체 구제절차 · 통지 방법 | — | 서식 문안 | 고객사 결정 |
| 담당부서 · 연락처 | 고객사 프로필 | — | 확인 |

**ⓖ AI 초안 입력** — §3-2와 같다. 추가로 **「개인정보 포함 여부 = 미확인」을 기본값**으로 두고, 사람이 확인 전엔 72h 서식을 「검토용」으로만 낸다. 이유: 플랫폼이 그 호스트의 개인정보 보유 여부를 모른다(§3-1 공백 ③과 같다).

**ⓗ 추가 확인 · 정정** — 24h(과기정통부/KISA)와 72h(개인정보위)는 **제출처가 다르다**. 같은 사건의 두 보고서가 서로 다른 「확인 시점」을 가질 수 있어, 제출본마다 「어느 근거 스냅숏으로 썼나」가 남아야 한다 — §3-1 ⓗ의 제출본/작업본 구분 요구가 여기서 가장 커진다.

**이 시나리오의 공백** — ① 유출 항목 ② 유출 규모 ③ 개인정보처리시스템 여부(태그·고객 선언) ④ 초기 침입 경로 ⑤ 두 기관 제출본의 스냅숏 구분. ①②는 플랫폼 밖이라 공백 유지 · ③⑤는 §4로.

### 3-4. 세 시나리오에서 같은 것

- 플랫폼이 **자동으로 채울 수 있는 칸은 「언제 · 어디서 · 무엇을 관측 · 무슨 조치」** 네 묶음이다. **「피해 내용 · 유출 항목 · 규모 · 침입 경로」는 전부 사람 칸**이고, AI 초안은 이 칸을 「미확인/조사 중」으로 명시해야 한다(「미확인」을 비우면 「없음」으로 읽힌다 — 선례 `MVP_CLOSEOUT_CHECKLIST.md` 머리말).
- 시각은 셋(발생 · 수집 · 인지)이고 기산점은 사람이 고른다. 서식에는 셋을 다 적는다.
- 24h와 72h는 제출처 · 조건 · 항목이 다르다. 한 사건에 보고서가 둘 나갈 수 있고, 각각 제출본 스냅숏이 필요하다.

---

## 4. 공백 모음 — 넘길 자리

| 공백 | 어디로 | 상태 |
| --- | --- | --- |
| 호스트 로그인 성공 여부 · 호스트 내부 피해 범위(§3-1) | #479 §2 후보(시스템·네트워크 로그 분석 모듈 — 안성일 10/30 아이디어 2) | 미제기 |
| 개인정보처리시스템 표시(태그 또는 고객 선언) | #479 §2 후보 | 미제기 |
| 제출본(스냅숏)과 작업본 구분 | #488 후속 | 미제기 |
| IAM finding 읽기 여부(A4 · B9~B11 · B15) | #588 결정 요청 | #613 코멘트 `6784418428` §④-2 |
| DoS 수신 · 랜섬웨어 · 고객사 내부자(B7 · B12 · B16) | 2판에서 공백 행 유지 | — |

---

## 변경 이력

| 날짜 | 내용 |
| --- | --- |
| 2026-11-11 | 1판 — §0 원천 · §1 분류표(#613 코멘트 `6784418428` 옮김) · §2 칸 정의 · §3 시나리오 3건(A2 SSH 무차별 대입 · B4 C&C/백도어 · B8 데이터 유출 의심) + §3-4 공통점 · §4 공백 모음. `[S]` 1행(B13)은 원문 재확인 전 |

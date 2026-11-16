# 고객사 위협 분류표와 위협별 ISMS 보고서 작성 시나리오 (2판)

> **담당**: 박지현 (QA) — 이슈 [#613](https://github.com/ProjectVigilantis/vigilantis/issues/613)
> **쓰는 곳**: ISMS 보고서 서버(안성일 · [#488](https://github.com/ProjectVigilantis/vigilantis/issues/488) 근거 내보내기 · 사고 조사 페이지 · AI 신고 초안)가 위협별 대응 방식과 초안 입력을 정할 때 본다(SSOT §확정 결정 로그 2026-11-06 ISMS 행).
>
> **이 문서는 코드·서버·화면을 정하지 않는다.** 위협마다 「어느 보고서를 · 언제까지 · 무슨 항목으로 · 누가(플랫폼 기록 / AI 문장 / 사람 확인) 채우는가」를 적는 자리다. 공식 서식과 법 조문은 기관 링크로 가리키며 법적 검토의 완결을 주장하지 않는다. 기관 제출은 고객사가 한다(2026-11-06 결정).
> **기록된 사실과 준수·충족 판단은 다르다.** 이 문서의 「플랫폼 기록」은 그 값이 저장돼 있다는 뜻이고, 신고 요건을 채웠다는 뜻이 아니다. 차단·격리 성공이나 인시던트 `RESOLVED`는 사고 처리 전체(원인 제거 · 복구 · 신고)의 완료가 아니다.
> 빈칸 · 「공백」 · 「미확인」은 **미확인**이다 — 「없음」으로 읽지 않는다. 외부 문서 인용은 2026-11-11 원문으로 확인했다(GuardDuty 심각도 · 보호나라 신고 양식). **#488 JSON 경로는 PR #616(2026-11-12 `dev` 머지) 계약 기준이다 — 후속 변경이 있으면 다음 판에서 맞춘다.** AI 신고 초안에 관한 서술은 PR #631(2026-11-13 머지) 계약 기준이고, 작성 모델이 연결되지 않아 실제 초안으로는 확인하지 못했다(§2-5).

---

## 0. 원천

| 항목 | 원천 | 상태 |
| --- | --- | --- |
| 이 문서의 범위와 완료 조건 | #613 본문 §1·§2 · 완료 조건 6 | 확정(11/11 발행) |
| 제출 목적 5종(ISMS/ISMS-P 인증 · 침해사고 신고 24h · 개인정보 유출 신고·통지 72h · 개인정보위 자료 제출 · 내부 분석) | 안성일 13주차 조사 — #479 코멘트(2026-11-03) §1 | 확정 |
| ISMS 보고서 범위(근거 내보내기 · 사고 조사 · AI 신고 초안) · 제출은 고객사 | SSOT §확정 결정 로그 2026-11-06 ISMS 행 | 확정 |
| 근거 내보내기 **계약**(JSON 최상위 `export` · `incident` · `evidence` · THREAT content = `event` + 선택 `context` · 공격 IP 미마스킹 · 2 MiB) | #488 2026-11-09 코멘트 · **PR #616 `packages/schemas/api/evidence_export.py`(2026-11-12 머지)** · #613 코멘트 `6785035815`(안성일 · 필드 요약) | 코드 · 후속 변경은 다음 판에서 |
| AI 신고 초안 **계약**(서버 사실 · AI 서술 · 사람 입력 세 구역 · 인용 범위 검사 · 실행과 감사 기록을 따로 읽음) | #623 · **PR #631(2026-11-13 머지)** — `packages/schemas/api/report_draft.py` `ReportDraftDocument` · `apps/core-api/report_draft.py` `collect_facts` · `apps/core-api/ai/report_draft.py` | 코드 · 대역 테스트. **작성 모델 미연결**(#623 완료 조건 마지막 1개) |
| GuardDuty 위험도 매핑(7.0↑ HIGH · 4.0–6.9 MEDIUM) · 인시던트는 MEDIUM↑ · 1차 자동 대응 없음 · 탐지 결과 읽기(C단계) | #588 Q2 · Q3 · PR #593(켜기 · 11/11) · **PR #615(결과 읽기 · 11/12 머지)** · PR #624(켜지 못한 리전 안내 · 11/13) — `services/guardduty.py` `read_findings` · `guardduty_ingress.py` · `security/risk_evaluator.py` `_evaluate_guardduty` · `GuardDutyFindingThreatPayload` · #613 코멘트 `6815319309`(김승철 · IAM 탐지 확인) | 코드 · **실 계정 확인은 11/16(월) 뒤**(#588 완료 조건 7) |
| 플랫폼이 보는 위협 유형과 판정 | `packages/schemas/events.py` — `ThreatEventType` · `RiskReasonCode` · `expected_mode_for` / `apps/core-api/security/risk_evaluator.py` — `_evaluate_open_ip` · `_evaluate_ssh_bruteforce` / `apps/core-api/security/soar.py`(MEDIUM 60초) / ADR-0011(High 목표 3초) | 코드 |
| 기록이 남는 자리(실제 테이블명) | `threat_events` · `incidents`(`IncidentStatus`) · `action_executions`(`RunbookId` · `TriggerSource`) · `evidence_items`(`EvidenceItem` · 현재 SecOps 접수는 THREAT 근거를 남긴다) · `audit_events`(T-2) · `platform_events`(#592 11/11 머지 · 기능별 기록 호출은 #589 B에서 연결) | 코드 |
| 외부 분류 | AWS GuardDuty finding type 문서(EC2 40종 · IAM 28종 — 심각도는 원문 확인) · KISA 침해사고 **통계 분류** 4종(DDoS · 악성코드 감염 · 서버 해킹 · 기타 — 과기정통부·KISA 2026 상반기 동향) · **보호나라 침해사고 신고 양식 선택지 3종(랜섬웨어 · 디도스 · 그 밖의 해킹 · 「유형을 확인할 수 없으면 그 밖의 해킹」 — `boho.or.kr/kr/report/list.do?menuNo=205034`)** · MITRE ATT&CK v20 Enterprise IaaS 매트릭스 | 외부 · 2026-11-11 읽음 |

---

## 1. 위협 분류표

### 1-1. A축 — 플랫폼이 지금·곧 보는 위협

| # | 위협 | 원천(신호) | 플랫폼 기록 | 자동 조치 | 보고서 종류(후보) |
| --- | --- | --- | --- | --- | --- |
| A1 | 공개 인그레스 노출 — 전체 개방 `RISK_OPEN_INGRESS_WORLD` · 22/3389 노출 `RISK_SENSITIVE_PORT_EXPOSED` · 전 프로토콜 `RISK_ALL_PROTOCOL_OPEN` · 전 포트 `RISK_ALL_PORTS_EXPOSED` | SG 관측 → `ThreatEventType.OPEN_IP`(중복 키 = SG+프로토콜+포트+CIDR+발생 시각) | `threat_events` → `incidents`(SECOPS · 항상 MEDIUM) · 근거 `evidence_items`(THREAT) · 조치는 승인 뒤 `action_executions`(SG 차단 런북) | **MEDIUM 승인 대기 · SG 대상 자동 격리 없음**(SG를 연결된 EC2로 바꿔 격리하지 않는다) | 사고 아님 → ISMS 통제 증적 · 내부 분석. 침해와 결합되면 「경위」 칸 |
| A2 | SSH 무차별 대입 `RISK_SSH_BRUTEFORCE`(HIGH · MEDIUM 둘 다) · `RISK_SSH_LOW_SIGNAL`(LOW) | `ThreatEventType.SSH_BRUTE_FORCE`(중복 키 = 대상 EC2+공격 IP+횟수+관측 창+발생 시각) | 위와 같음 + 승인/자동 뒤 `action_executions`(NACL 차단 · 격리) | **HIGH 즉시 선차단(목표 3초 · ADR-0011 · 식별자 `PRE_MITIGATION_0_5S` 유지)** · MEDIUM 승인 대기 시작부터 60초 무응답 → 조건 충족 EC2 대상 자동 격리 · LOW 제외 | 시도 = 내부 분석 / 성공 = 침해사고 신고 24h(서버 해킹) — **§3-1** |
| A3 | GuardDuty **EC2** finding 40종(Backdoor 8 · Behavior 2 · CryptoCurrency 2 · DefenseEvasion 3 · Impact 7 · Recon 3 · Trojan 9 · UnauthorizedAccess 6) | 고객 계정 GuardDuty(관제 리전의 탐지기) → 주기 조회 `ThreatEventType.GUARDDUTY_FINDING`. 읽는 조건은 셋 — 이 계정의 탐지 · 보관(archive)되지 않은 것 · 마지막 읽은 뒤 갱신된 것. 유형으로 거르지 않는다. 처음 읽는 리전은 하루치만 거슬러 읽는다(`INITIAL_LOOKBACK`) | `threat_events` 전부(`payload` = `finding_id` · `finding_type` · `severity` · `resource_type` · `region` · `finding_region` · `first_seen_at` · `last_seen_at`) · `incidents`는 MEDIUM↑ · 같은 탐지는 한 번만 남고 **첫 관측 값이 남는다**(갱신돼도 기록은 그대로) | **없음** — 자동 격리 대상에서 유형으로 뺐다(`AUTO_ISOLATION_EXCLUDED_EVENT_TYPES`). #598 보호 태그 선행 | prefix별로 B축에 배정 |
| A4 | GuardDuty **IAM** finding 28종(13 prefix) | 위와 같음 — **읽는다**(유형으로 거르지 않음). IAM · STS 같은 글로벌 서비스 탐지는 `finding_region`이 `us-east-1`이어도 받는다 | A3와 같다. 다른 점 하나 — 대상이 인스턴스가 아니라서 `target_arn`이 **탐지 자체의 ARN**이다(`resource_type` 예 `AccessKey`). **어느 사용자 · 액세스 키인지, 어느 IP에서 어떤 API를 몇 번 불렀는지는 남지 않는다** | 없음 | 자격 증명·계정 탈취 → 24h · 유출 경로면 72h. 「대상」 · 「경위」 칸은 GuardDuty 원문을 사람이 붙여야 채워진다 |
| A5 | 플랫폼 자체 — 로그인 경로 무차별 대입 · API 남용 | WAF 속도 제한 Count(T-3 #522) · `platform_events` `AUTH_LOGIN` — **스키마는 있지만 기록 호출은 #589 B에서 연결 필요**(PR #630 · 11/16(월) 기준 머지 전) | `platform_events`(연결 뒤) | Block 전환은 11/16 뒤 | 우리가 신고 주체 — §3-5 마지막 행 |

### 1-2. B축 — 고객사에 들어올 수 있는 위협 상위 유형 (EC2 + IAM · S3와 DB는 공백 행)

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
| B9 | 자격 증명 유출 · 인스턴스 역할 자격 반출 | Credential Access T1552 · T1528 | `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS`(H) · `UnauthorizedAccess:EC2/MetadataDNSRebind`(H) · `CredentialAccess:IAMUser/CompromisedCredentials`(H) | 서버 해킹(계정 탈취) | GD(EC2 · IAM 둘 다) | 24h · 유출 경로면 72h |
| B10 | 계정 탈취 · 권한 상승 · 지속화(IAM 변경) | Valid Accounts T1078 · Account Manipulation T1098 | `Persistence:IAMUser/AnomalousBehavior`(M) · `PrivilegeEscalation:IAMUser/AnomalousBehavior`(M) · `Policy:IAMUser/RootCredentialUsage`(L) · `ConsoleLoginSuccess.B`(M) | 서버 해킹 | GD — 단 `RootCredentialUsage`(L)는 위협 이벤트만 남고 인시던트가 없다 | 24h |
| B11 | 탐지 회피 · 로그 끄기 | Defense Impairment T1685 · T1578 | `Stealth:IAMUser/CloudTrailLoggingDisabled`(L) · `DefenseEvasion:IAMUser/AnomalousBehavior`(M) · `DefenseEvasion:EC2/UnusualDNSResolver`(M) | 서버 해킹(은폐) | GD — 단 `CloudTrailLoggingDisabled`(L)는 위협 이벤트만 남아 **인시던트 목록에 보이지 않는다** | 근거 훼손 → 「미확인」 칸 증가. 이 유형은 `threat_events`에서 찾는다 |
| B12 | 랜섬웨어 · 데이터 파괴 | Impact T1486 · T1485 · T1490 | — (EC2 finding 없음 · Malware Protection/S3 영역) | 악성코드(랜섬웨어) | **공백** | 24h · 개인정보면 72h |
| B13 | 피싱 · 악성 도메인 질의 · 드라이브바이 | User Execution T1204 | `Trojan:EC2/PhishingDomainRequest!DNS`(H) · `Trojan:EC2/DriveBySourceTraffic!DNS`(H) · `Impact:EC2/MaliciousDomainRequest.Reputation`(H) | 악성코드 | GD | 내부 분석 → 감염 확인 시 24h |
| B14 | Tor · 악성 IP 통신 | Stealth · C2 성격 | `UnauthorizedAccess:EC2/TorClient` · `TorRelay`(H) · `MaliciousIPCaller.Custom`(M) · `Recon:IAMUser/TorIPCaller`(M) | 서버 해킹 | GD | 24h |
| B15 | 침투 시험 도구 사용(Kali · Parrot · Pentoo) | — | `PenTest:IAMUser/*`(M) | — | GD | 내부 분석(승인된 시험이면 기록만) |
| B16 | 고객사 내부자 · 권한 오남용 | Valid Accounts T1078 | — | 기타 | **공백**(고객사 측 · 플랫폼 측은 `platform_events`) | 내부 분석 · 개인정보면 72h |
| B17 | S3 데이터 노출 · 유출 | Collection T1530 | — (S3 데이터 이벤트 탐지는 부가 보호 기능) | 기타(정보유출) | **공백** — 플랫폼이 켜는 탐지기는 `S3_DATA_EVENTS`를 꺼 둔다(`DISABLED_FEATURES`) | 개인정보면 72h · 침해 확인 시 24h |
| B18 | DB(RDS) 무단 접근 | Valid Accounts T1078 | — (RDS 로그인 이벤트 탐지는 부가 보호 기능) | 서버 해킹 | **공백** — `RDS_LOGIN_EVENTS`를 꺼 둔다 | 위와 같다 |

「KISA 유형」 칸은 통계 분류 4종이다. **실제 보호나라 신고 양식의 선택지는 셋 — 랜섬웨어 · 디도스 · 그 밖의 해킹**(「침해 유형을 확인할 수 없는 경우 그 밖의 해킹」). 따라서 신고 서식에 옮기면 B12 → 「랜섬웨어」, B6 · B7 → 「디도스」, **나머지 전부 → 「그 밖의 해킹」**이다. 양식의 단계는 유형 선택 → 동의 → 기업·신고자 정보 → 사고현황 → 대응현황이며, 뒤 두 단계의 입력 항목은 폼 안에 있어 원문으로 읽지 못했다(§3 항목표는 안성일 11/3 조사의 「담을 내용」 기준).

「플랫폼이 보는가」 칸의 집계는 **직접 2**(B1 · B3) · **GD 11**(B2 · B4 · B5 · B6 · B8 · B9 · B10 · B11 · B13 · B14 · B15) · **공백 5**(B7 · B12 · B16 · B17 · B18)이다. B17 · B18은 B축의 폭을 EC2 + IAM으로 잡으면서 남긴 자리다(#613 §3 결정 요청 1) — 고객사가 부가 보호 기능을 직접 켜서 탐지가 생기면 유형으로 거르지 않으므로 읽힌다. 이 두 행의 ATT&CK 번호는 2026-11-11 원문 확인 범위에 없었고 다시 열어 보지 않았다. 「GD」는 코드와 테스트 대역으로 확인한 것이고 실 계정 확인은 11/16(월) 뒤다. 심각도 Low 유형은 위협 이벤트로만 남는다(인시던트는 MEDIUM 이상).

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
| ⓔ 보고서 항목표 | **`보고서 항목 | 채우는 주체·방식 | JSON 경로 또는 별도 기록 | 부족한 확인 자료`** 4칸 — 주체는 「플랫폼 기록(JSON) / 플랫폼 기록(#488 JSON 밖 — AI 신고 초안이 읽는 것은 §2-5) / AI 문장 / 사람 확인」 넷 중 하나 |
| ⓕ 미확인 기준 | 값이 없을 때 「미확인」으로 남기는 조건(§2-4) |
| ⓖ 추가 확인 · 정정 | 새 사실 → 추가 신고(최초 신고의 추가분임을 밝힘) · 제출본과 작업본 구분 |

### 2-2. #488 JSON 계약 요약 (PR #616 · 2026-11-12 머지 기준)

| 최상위 | 필드 | 뜻 |
| --- | --- | --- |
| `export` | `version` · `format`(md/json) · `generated_at` · `notice` · `evidence_count` · `missing[]` | 파일 메타. `missing`은 묶음 수준 누락(`no_evidence` · `metric_evidence_absent`) |
| `incident` | `incident_id` · `title` · `subject_arn` · `category` · `status` · `tenant_id` · `created_at` · `updated_at` | 머리말 — 식별·분류·상태만. **위험도 · AI 요약 3줄 · `decision` · 판정 코드는 없다** |
| `evidence[]` | `evidence_id` · `incident_id` · `evidence_type` · `source_type` · `source_id` · `occurred_at` · `collected_at` · `flags{processed, synthetic, excerpt, missing[]}` · `content` | 근거 1건. 현재 SecOps 접수가 만드는 근거는 **THREAT**이다. `EXECUTION` 유형은 계약에 있지만 지금 생성 경로가 없다 |
| THREAT `content` | `event`(유형 · 대상 · `occurred_at` · 중복 키 · `collected_at`(정규화) · `payload`) + 선택 `context`(`captured_at` · `target_status` · `target` · `related_assets[]` · `relation_issues[]` · `log_evidence`) | SSH payload = `source_ip` · `failed_attempt_count` · `window_seconds` / OPEN_IP payload = 프로토콜 · 포트 · CIDR. `context.target.asset`은 접수 때 확보한 자산 사본이지 사건 당시 재구성이 아니다 |
| **들어가지 않는 것** | `action_executions` · `execution_steps` · `audit_events`(승인) · `guardrail_evaluations` · `platform_events` · 외부 알림 발송 이력 · AI 요약·위험도 | 이 파일만으로 보고서를 쓰면 **사람 확인**이다. 이 가운데 `action_executions` · `audit_events`는 AI 신고 초안이 만들 때 따로 읽는다(§2-5). 나머지는 초안에도 없다. #488에 전부 추가하자는 요청은 아니다 |

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
- **대상 식별자는 항상 있다** — THREAT 근거의 `evidence[].content.event.target_arn`은 접수가 필수 값으로 저장하므로(`incident_intake`), `context`가 없어도 「미확인」으로 돌리지 않는다. 기록된 ARN은 그대로 쓴다.
- **사본에서 오는 칸만 사본 기준으로 본다** — `context.target.asset.resource_id` · `private_ip`처럼 접수 때 확보한 자산 사본에서 가져오는 칸은 `content.context`가 null이거나 `context.target`이 null(`target_status`가 `not_collected` · `absent` · `missing_collection`)이거나 그 값이 비어 있을 때만 「미확인」.
- **로그 칸은 로그 기준으로 본다** — `context.log_evidence`가 null이면 로그 칸만 「미확인」. 대상 사본의 유무와 따로 판정한다.
- `flags.missing[]`은 근거 1건의 누락 사유, `export.missing[]`은 묶음 누락이다. **빈 목록을 자료 전체의 완전성으로 읽지 않는다.** `synthetic=false`가 실데이터 보장이 아니고, `excerpt=true`는 발췌 정책 표시라 실제 생략 여부는 원래 건수와 실린 건수를 대조한다.
- #488 JSON 밖 기록 가운데 조치 · 승인은 AI 신고 초안이 서버 사실로 읽는다(§2-5). 기록이 없으면 초안은 「대응 이력 미확인」으로 내며 **「조치 없음」으로 읽지 않는다.** 발송 이력은 초안에도 없어 사람 확인이다.

### 2-5. AI 신고 초안 계약 요약 (#623 · PR #631 · 2026-11-13 머지 기준)

`POST /api/v1/incidents/{incident_id}/report-draft` — 인시던트 한 건의 **침해사고 신고** 작성 보조 초안을 `md`나 JSON으로 돌려준다(`ReportDraftDocument`). 초안은 저장하지 않고 요청마다 새로 만든다. **작성 모델이 아직 연결되지 않아 기본 구성에서는 초안이 만들어지지 않는다**(`get_report_model_client`가 `None`을 돌려준다 — #623 완료 조건의 마지막 1개). 아래는 코드와 대역 테스트 기준이고, 실제 모델로 만든 초안은 확인하지 못했다.

| 구역 | 들어가는 것 | 이 문서의 「주체」와 |
| --- | --- | --- |
| `server_facts` | 원천 넷 — `incident`(분류 · 관제 상태 · 생성/갱신 시각 · 탐지 대상) · `evidence`(#488 JSON에서 **고른 칸만** — 아래) · `action_execution`(조치 종류 · 대상 · 기록된 상태 · 발동 원천 · 예약 시각 · 완료 시각) · `audit_event`(감사 동작 · 행위자 종류 · 발동 원천 · 연결 실행 · 관제 종료 판단). 사실마다 원천 종류 · 레코드 ID · 경로 · 시각 · 해석이 붙고, 값이 없으면 `unknown`과 사유가 붙는다 | 플랫폼 기록 |
| `ai_narrative` | 문장마다 `fact_id` 인용. 구역은 둘 — `occurrence`(관측 사실) · `response`(기록된 대응 상태). 서버는 **인용이 이 요청의 사실 범위 안인지만** 검사한다(`citation_validation` = `scope_only`). 문장이 참이라는 보장이 아니다 | AI 문장 |
| `human_input` | 빈 칸 16 — 사고인지시점 · 인지 근거 · 사고발생시간 확인 · 사고 원인 확인 · 대응 부서 · 대응 연락처 · 피해 범주 · 피해 수량·규모 · 피해 IP·도메인 확인 · 기업명 · 기업 등록 정보 · 기업 주소 · 신고자 이름 · 부서 · 연락처 · 이메일. 체크 안 된 확인 칸 2 — 동의 · 확인 | 사람 확인 |

**`evidence`에서 고르는 칸**(위협 유형별 · `collect_facts`):

| 위협 유형 | 초안에 사실로 들어가는 칸 |
| --- | --- |
| 공통 | 근거 종류 · 이벤트 발생 시각 · 근거 수집 시각 · 탐지 유형 · 탐지 대상 ARN · 대상 수집 상태 · **피해 IP 후보**(접수 때 확보한 EC2 사본의 사설 IP가 있을 때만 — 없으면 `not_collected`) |
| `SSH_BRUTE_FORCE` | 공격 출발지 IP · 관측 실패 횟수 · 관측 구간(초) |
| `OPEN_IP` | 접근 허용 CIDR · 개방 시작 포트 · 개방 종료 포트 |
| `GUARDDUTY_FINDING` | 탐지 ID · 탐지 분류 · 자원 유형 · 탐지기를 읽은 리전 · 탐지에 표시된 리전 · 기록 심각도 · 최초 관측 시각 · 최근 관측 시각(§1-1 A3의 요약 8칸) |

**초안에 들어가지 않는 것** — 인시던트 제목 · 근거의 자유문 · 로그 본문 · 태그(`free_text_omitted`) · 행위자 신원(`identity_omitted`) · 위험도 · AI 요약 3줄 · 판정 코드 · `platform_events` · 외부 알림 발송 이력 · 공인 IP · 도메인. **개인정보 유출 신고·통지는 다루지 않으며**, 신고 의무 · 기한 · 수신처도 판단하지 않는다(초안 머리말 `NOTICE`).

이 계약에서 따라 나오는 것 셋:

1. **조치 · 승인 기록은 이제 초안이 읽는다.** #488 내보내기 파일에는 여전히 없지만(§2-2), 신고 초안은 만들 때 `action_executions`와 `audit_events`를 따로 읽어 서버 사실로 싣는다. 1판이 「JSON 밖 · 연결 전 사람 확인」으로 적은 칸 가운데 **조치 종류 · 기록된 상태 · 발동 원천 · 시각**은 초안에서 플랫폼 기록이 된다. 승인한 **사람이 누구인지**와 호스트 내부 조치는 여전히 사람 칸이다.
2. **초안은 인시던트에서 출발한다.** 인시던트가 없는 관측(심각도 Low 탐지 · 플랫폼이 못 보는 위협)은 초안을 요청할 자리가 없다.
3. **기록된 상태는 판정이 아니다.** 실행 예약은 AWS 실행 시작이 아니고, 시스템 발동은 사람의 승인이 아니며, `RESOLVED`와 조치 성공은 사고 처리 · 신고의 완료가 아니다. `UNVERIFIED`는 「결과 확인 불가」이고 실패가 아니다. 기록이 없으면 「조치 없음」이 아니라 미확인이다.

---

## 3. 시나리오

### 3-1. A2 · SSH 무차별 대입 (B1 · KISA 서버 해킹 · ATT&CK T1110)

**ⓐ 위협** — 외부 IP가 고객사 EC2의 SSH(22)에 짧은 시간 반복 로그인 시도. 플랫폼이 **직접** 보는 유일한 공격 행위다(A1은 설정 노출). 공격자의 `source_ip`와 대상 인스턴스의 IP는 다른 값이다.

**ⓑ 탐지 신호와 시각**

| 항목 | 내용 |
| --- | --- |
| 원천 | 지금: 모의 위협 입력 → `threat_normalizer` → `NormalizedThreatEvent`. GuardDuty `UnauthorizedAccess:EC2/SSHBruteForce`(Low · 가해 측이면 High)도 들어온다 — 이쪽은 `SSH_BRUTE_FORCE`가 아니라 `GUARDDUTY_FINDING` 유형의 별도 위협 이벤트로 남고(PR #615), Low면 인시던트가 없다. 같은 공격이 두 유형으로 따로 남을 수 있다. GuardDuty 쪽 한계(원문): **22번 포트 트래픽만** 보고, VPC Flow Logs 기반 EC2 finding은 **IPv6 미지원** |
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
| `action_executions` | `RUNBOOK_NACL_ADD_DENY`(출발지 차단) · `RUNBOOK_EC2_ISOLATE`(격리) · 해제 `RUNBOOK_NACL_RESTORE` · `RUNBOOK_EC2_UNISOLATE` · 시각 · 결과 · `trigger_source`(실행 **계기** — 승인이나 실행 결과와 별개) | **불포함** — AI 신고 초안은 따로 읽는다(§2-5) |
| T-11 만료 해제 | SUCCESS로 끝난 `RUNBOOK_NACL_ADD_DENY`의 `finished_at`부터 24시간 뒤 조건을 다시 확인해 해제를 **예약**한다(`TriggerSource.NACL_DENY_EXPIRED_24H`). 그 시각에 해제가 성공한다는 보장은 아니다 | 불포함 |
| `audit_events`(T-2) | 승인자 · 시각 · 대상 | 불포함 — AI 신고 초안은 따로 읽되 **행위자 신원은 뺀다** |
| `platform_events`(#592) | 상태 변경 · 반출 기록(파일 조립 뒤 따로 남는 기록 — 파일 내용이 아니다) | 불포함 |
| 외부 알림 outbox(T-9) | 발송 상태(`SLACK_POSTED` 등) | 불포함 |

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| 시도만 — 차단·격리로 끝, 로그인 성공 징후 없음 | **내부 분석**(보고서 없음) · ISMS 접근통제 운영 증적 | — | 플랫폼 기록만으로 닫힘 |
| 성공 징후 — 호스트 로그인 성공 · 후속 행위(B4·B5·B8 finding 동반) | **침해사고 신고**(과기정통부/KISA) | 안 뒤 **24시간** · 추가 사실도 확인 후 24h | 정보통신서비스 제공자 해당 여부는 고객사 판단 |
| 그 호스트가 개인정보처리시스템이고 유출 확인 | **개인정보 유출 신고·통지** | **72시간** | 1천 명 이상 · 민감/고유식별 · 외부 불법 접근 중 하나 — 이 시나리오는 「외부 불법 접근」에 **해당할 수 있다**. 해당 여부와 단서(유출 경로가 확인되고 회수·삭제 등으로 권익 침해 가능성이 현저히 낮으면 신고 생략 가능 · 개인정보 보호법 시행령 제40조 제1항) 적용은 고객사가 판단한다 |

**ⓔ 침해사고 신고 항목표** — 항목의 법령 원천은 정보통신망법 시행령 제58조의2(2024-08-14 시행)다. 24시간 안에 신고할 사항 = ① 발생 일시 · 원인 · 피해 내용 ② 조치사항 등 대응 현황 ③ 대응 담당 부서 · 연락처이고, 추가로 확인된 사실은 확인한 때부터 24시간 안에 보완 신고한다(ⓓ 「추가 사실도 확인 후 24h」의 근거). 조문은 법무법인 해설의 인용으로 확인했고 국가법령정보센터 원문은 열지 않았다. 보호나라 폼의 사고현황 · 대응현황 입력 칸은 읽지 못했다(§1-2) — 아래 표는 이 세 사항을 안성일 11/3 조사의 「담을 내용」으로 푼 것이다.

| 보고서 항목 | 채우는 주체·방식 | JSON 경로 또는 별도 기록 | 부족한 확인 자료 |
| --- | --- | --- | --- |
| 신고자 · 기관 정보 | 사람 확인 | `incident.tenant_id`는 식별자뿐 · 테넌트 API에 고객사명은 있지만 **신고 담당부서·연락처는 없다** | 담당부서 · 연락처(사람 입력 또는 별도 출처) |
| 사고 유형 | 문서 매핑(B1 → 「서버 해킹」) · AI 문장 | 이 문서 §1 | 성공 여부에 따라 「시도」/「침해」 |
| 발생 시각 | 플랫폼 기록(JSON) | `evidence[].content.event.occurred_at` · `evidence[].occurred_at` | — |
| 플랫폼 인시던트 생성 시각 | 플랫폼 기록(JSON) | `incident.created_at` | 사람 최초 인지와 다르다 |
| **최초 인지 시각(기산점)** | **사람 확인** | — | 사람 칸 · 법적 판단은 고객사 |
| 피해 시스템 | 플랫폼 기록(JSON) + 사람 확인 | **대상 ARN `evidence[].content.event.target_arn`(항상 있다)** · 인스턴스 ID `evidence[].content.context.target.asset.resource_id` · `private_ip`(nullable) · 리전은 `subject_arn` | 사본(`context.target`)이 없거나 값이 비면 **인스턴스 ID·사설 IP만** 미확인(ARN은 유지) · **공개 IP는 `Ec2Spec`에 없어 사람 확인** · 호스트 역할·보유 정보 |
| 공격 IP · 방법 | 플랫폼 기록(JSON) · AI 문장 | `evidence[].content.event.payload.source_ip` · `.failed_attempt_count` · `.window_seconds` | 판정 코드(`RISK_SSH_BRUTEFORCE`)는 JSON 밖 — 필요하면 별도 입력 |
| 피해 내용 | 사람 확인 | — (호스트 auth 로그는 플랫폼 미수집) | 「미확인」으로 적는다 · 확인된 사실 / 추정 / 미확인 구분은 사고 조사 페이지 |
| 조치 내용 · 결과 | 플랫폼 기록(#488 JSON 밖 · **AI 신고 초안의 서버 사실** — §2-5) | `action_executions`(런북 · 시각 · 기록된 상태) · 승인은 `audit_events` · 계기는 `trigger_source` — **셋을 따로 표시** | 승인한 사람이 누구인지(초안은 신원을 뺀다) · 호스트 내부 조치는 사람. 근거 내보내기 파일만 쓸 때는 전부 사람 확인 |
| 추가 보고 여부 | 사람 확인 | — | 조사 진행에 따라 |

**ⓕ 미확인 기준** — §2-4 그대로. 대상 ARN(`event.target_arn`)은 항상 기록되므로 유지하고, 사본 칸(인스턴스 ID · 사설 IP)은 사본이나 값이 없을 때, 로그 칸은 `log_evidence`가 없을 때만 미확인이다. `context`가 null이라고 대상 칸 전부를 미확인으로 두지 않는다.

**ⓖ 추가 확인 · 정정** — 격리 뒤 호스트 조사에서 성공이 확인되면 「최초 신고의 추가분」으로 24h 안에 다시 신고(KISA 안내서). 플랫폼 쪽 요구: 제출한 본(스냅숏)과 그 뒤 쌓인 근거를 구분해야 한다 — **담당 협의 전 후보 · #488 확정 범위 아님**(#613 완료 조건 5로 넘김).

**이 시나리오의 공백(플랫폼이 못 채움)** — ① 로그인 성공 여부(호스트 로그) ② 호스트 내부 피해 범위 ③ 그 호스트의 개인정보 보유 여부(자산 태그·고객 선언 없음) ④ 신고 접수 상태 ⑤ 공개 IP · 담당부서·연락처. ③은 T-7 보호 태그와 별도의 「개인정보처리시스템」 표시가 있어야 자동화된다 — #479 §2 후보(담당 협의 전).

### 3-2. B4 · C&C 통신 · 백도어 (GuardDuty HIGH · KISA 악성코드 감염 · ATT&CK Execution/Persistence)

> **탐지 결과 읽기는 구현됐다(PR #615 · 2026-11-12 머지).** 아래 「플랫폼 수신」과 「남는 기록」은 코드 기준이다. 실 계정에서 탐지가 생기고 읽히는지는 11/16(월) 뒤 확인이다(#588 완료 조건 7).

**ⓐ 위협** — 고객사 EC2가 알려진 명령·제어(C&C) 서버의 IP·도메인과 통신한다. A2와 달리 **외부→내부 시도가 아니라 내부→외부 신호**라, 이 finding 하나가 「이미 감염됐다」는 뜻에 가깝다. 플랫폼은 **GuardDuty 경유(A3)** 로만 본다.

**ⓑ 탐지 신호와 시각**

| 항목 | 내용 |
| --- | --- |
| 원천 | GuardDuty `Backdoor:EC2/C&CActivity.B`(IP · High) · `Backdoor:EC2/C&CActivity.B!DNS`(도메인 · High) · 유사: `Trojan:EC2/DGADomainRequest.B`(High) · `Trojan:EC2/BlackholeTraffic`(Medium) |
| 플랫폼 수신 | #588 C단계 — 수집 주기마다 고객 역할로 탐지 목록(마지막 읽은 갱신 시각 이후 · 보관 안 된 것)을 읽고 상세를 50건씩 받는다(`read_findings`) → `ThreatEventType.GUARDDUTY_FINDING` · `payload` = `finding_id` · `finding_type` · `severity` · `resource_type` · `region` · `finding_region` · `first_seen_at` · `last_seen_at` · 대상은 공통 칸 `target_arn`(인스턴스면 인스턴스 ARN) · 중복 키는 계정 + 리전 + 탐지 ID |
| 판정(확정 규칙) | 심각도 7.0 이상 → HIGH · 4.0–6.9 → MEDIUM · 그 아래 LOW(#588 Q2). 인시던트는 MEDIUM 이상만(Q3) — 이 finding은 High(8.x)라 HIGH 인시던트 |
| 응답 모드 | **자동 조치 없음**(1차 · #588 Q2 · 보호 태그 검사 #598 선행) — 자동 격리 대상에서 유형으로 빠져 있다. AI 분석도 이 유형에는 제안할 조치가 없어 조치 후보 없이 닫힌다(김승철 확인 · #613 코멘트 `6815319309`). 대응은 사람이 인시던트를 보고 정한다 |
| 시각 | GuardDuty `eventFirstSeen` / `eventLastSeen`(발생) · 수집 주기 수신 · 플랫폼 인시던트 생성 · **사람 최초 인지(사람 칸)**. 발생과 플랫폼 생성 사이에 수집 주기만큼 간격이 생긴다 |

**ⓒ 플랫폼에 남는 기록** — §3-1 ⓒ와 같은 자리. 다른 점 넷: ① 위협 이벤트 `payload`가 GuardDuty 탐지 **요약 8칸**이다. 원본 finding JSON은 보존하지 않으므로(원격 IP · 도메인 · 프로세스 정보 없음) 「원 출처」는 고객 계정 GuardDuty 콘솔이다. 같은 탐지가 갱신돼도 **첫 관측 값**이 남는다 ② `action_executions`는 승인 뒤에만 생긴다(자동 없음) ③ **GuardDuty finding은 관측이지 침해 증거가 아니다**(#588 Q2 SEC 확인) — 「피해 내용」은 플랫폼 기록만으로 확정하지 않는다 ④ 처음 읽는 리전은 하루치만 거슬러 읽으므로, 연결 하루 전보다 오래된 탐지는 플랫폼 기록에 없다(「미확인」).

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| finding 1건 · 호스트 조사 전 | **내부 분석** 시작(사고 조사 페이지 개설) | — | 「추정: 감염」 · 「미확인: 범위」로 기록 |
| 호스트에서 악성 프로세스 · 지속화 확인 | **침해사고 신고**(악성코드 감염) | 안 뒤 **24시간** | 정보통신서비스 제공자 해당 여부 |
| 같은 호스트에 B5(채굴) · B8(유출) finding 동반 | 침해사고 신고 + 유출 경로면 **72h 검토**(§3-3) | — | finding 묶음을 한 인시던트로 볼지는 플랫폼 묶음 규칙(미정) |

**ⓔ 침해사고 신고 항목표** — §3-1과 같은 항목. GuardDuty 탐지의 JSON 경로는 `evidence[].content.event.payload.*`이고(근거 내보내기 · AI 신고 초안 #623이 같은 경로를 읽는다 — `test_report_draft.py` `test_guardduty_intake_download_preserves_finding_not_open_ip`), 대상은 §3-1과 같은 `evidence[].content.event.target_arn`이다:

| 보고서 항목 | 채우는 주체·방식 | JSON 경로 또는 별도 기록 | 부족한 확인 자료 |
| --- | --- | --- | --- |
| 사고 유형 | 문서 매핑(B4 → 「악성코드 감염」) · AI 문장 | 이 문서 §1 | — |
| 공격 IP · 방법 | 탐지 유형은 플랫폼 기록 · **원격 IP · 도메인은 사람 확인**(GuardDuty 원문) · AI 문장 「C&C 통신 관측」 | 탐지 유형 `evidence[].content.event.payload.finding_type` · 심각도 `…payload.severity` · 원격 IP · 도메인은 플랫폼 기록에 없다 | **감염 경로(초기 침입)는 미확인** — 선행 finding(A2 · B13) 대조는 사람 |
| 피해 내용 | 사람 확인 | — | 호스트 포렌식 결과 · 「미확인」 |
| 조치 내용 · 결과 | 플랫폼 기록(#488 JSON 밖 · AI 신고 초안의 서버 사실 — §2-5) | 승인 뒤 `action_executions` · 승인 `audit_events` · 계기 `trigger_source`. 자동 조치가 없어 승인 전에는 초안이 「대응 이력 미확인」으로 낸다 | 호스트 내부 조치(프로세스 종료 · 재구축) |
| 발생 시각 | 플랫폼 기록(JSON) | `evidence[].content.event.payload.first_seen_at` · `…payload.last_seen_at`(GuardDuty가 본 최초 · 최근 관측 — **첫 읽기 때의 값**) · `evidence[].content.event.occurred_at` | 사람의 최초 인지 시각은 아니다(§2-3). 탐지가 갱신된 뒤의 최근 관측은 GuardDuty 콘솔 |
| 피해 시스템 | 플랫폼 기록(JSON) + 사람 확인 | `evidence[].content.event.target_arn`(인스턴스 ARN) · 자원 종류 `…payload.resource_type` · 리전 `…payload.region` | §3-1과 같다 |
| 나머지(신고자 · 추가 보고) | §3-1과 같다 | §3-1과 같다 | §3-1과 같다 |

**ⓖ 추가 확인 · 정정** — GuardDuty는 같은 탐지를 **갱신**(건수·최근 관측 시각 증가)한다. 플랫폼이 탐지 ID로 중복을 접으면 「최근 관측 시각」만 바뀌므로, 추가 신고 판단은 사람이 finding 갱신을 보고 한다. 제출본과 작업본 구분 요구는 §3-1과 같다.

**이 시나리오의 공백** — ① 감염 경로(초기 침입) ② 호스트 내부 악성 프로세스 · 지속화 여부 ③ 다른 호스트로의 확산(수평 이동 — GuardDuty EC2 finding에 없음) ④ 원격 IP · 도메인 등 finding 원문 칸(플랫폼은 요약 8칸만 보존). ③은 플랫폼 밖이라 공백 유지, ④는 사람이 GuardDuty 콘솔에서 붙인다.

### 3-3. B8 · 데이터 유출 의심 (GuardDuty `Trojan:EC2/DNSDataExfiltration` · KISA 기타(정보유출) · ATT&CK Exfiltration T1048)

> **이 절의 플랫폼 입력은 §3-2와 같다**(GuardDuty 탐지 요약 8칸 · PR #615 · 실 계정 확인은 11/16(월) 뒤). 서버 1차는 개인정보 유출 신고·통지를 AI 초안 범위에서 제외한다(안성일 · 팀 범위 확정과 별개) — 이 절은 **사람이 채우는 칸이 왜 많은지**를 남기는 자리다.

**ⓐ 위협** — 고객사 EC2가 DNS 질의에 데이터를 실어 밖으로 보낸다(High). §3-1 ~ §3-3 세 시나리오 중 **침해사고 신고(24h)와 개인정보 유출 신고·통지(72h)가 동시에 걸릴 수 있는 유일한 사례**다. 유사: `Trojan:EC2/DropPoint`(Medium · 탈취 자료 집결지 통신) · IAM 쪽 `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS`(High · A4).

**ⓑ 탐지 신호와 시각** — §3-2와 같은 경로(GuardDuty → 주기 조회 → HIGH 인시던트 · 자동 조치 없음). 다른 점: finding이 말해 주는 것은 「DNS로 나간 데이터가 있다」까지고 **무엇이 나갔는지는 없다.**

**ⓒ 플랫폼에 남는 기록** — §3-2와 같다. 유출 「항목 · 규모」를 채울 기록이 플랫폼에 **없다**(호스트 파일 · DB 접근 로그 · VPC Flow Logs 원본은 수집 범위 밖).

**ⓓ 보고서 종류와 기한**

| 상황 | 보고서 | 기한 | 조건 |
| --- | --- | --- | --- |
| finding 1건 | **내부 분석** + 침해사고 신고 검토 | 24h 시계 시작 여부는 「침해 확인」 시점(사람) | — |
| 호스트에서 반출 확인(무엇이든) | **침해사고 신고**(정보유출) | 안 뒤 **24시간** | — |
| 반출된 것에 개인정보 포함 | **개인정보 유출 신고**(개인정보위 · 전문기관) + **정보주체 통지** | 안 날부터 **72시간**(통지도 원칙 72h) | 신고 대상: 1천 명 이상 **또는** 민감·고유식별정보 **또는** 외부 불법 접근 중 하나 — 이 시나리오는 「외부 불법 접근」에 **해당할 수 있다** · 해당 여부와 단서(시행령 제40조 제1항 · §3-1 ⓓ) 적용은 고객사 판단 · 통지는 규모와 무관 |
| 유출 규모 · 항목이 조사 중 | **확인된 사실로 우선 신고 뒤 추가분 신고**(개인정보 유출등 대응 안내서) | 72h 안 1차 | 「조사 중」을 숨기지 않는다 |

**ⓔ 개인정보 유출 신고·통지 항목표** — 신고서 항목(유출 항목 · 규모 · 경위 · 시점 · 피해 최소화 조치와 결과 · 정보주체 구제절차 · 담당부서·연락처) 기준:

| 보고서 항목 | 채우는 주체·방식 | JSON 경로 또는 별도 기록 | 부족한 확인 자료 |
| --- | --- | --- | --- |
| 유출 시점 | 플랫폼 기록(JSON) + 사람 확인 | `evidence[].content.event.payload.first_seen_at` · `…payload.last_seen_at` · 플랫폼 인시던트 생성 `incident.created_at` | 기산점은 사람. 관측 시각은 유출이 시작된 시각이 아닐 수 있다 |
| 유출 경위 | 탐지 유형과 대상은 플랫폼 기록(JSON) · AI 문장 「DNS 터널 의심 — GuardDuty 탐지」 · **원격 도메인/IP는 사람 확인** | `evidence[].content.event.payload.finding_type` · `evidence[].content.event.target_arn` | 원격 도메인/IP(GuardDuty 원문) · 초기 침입 경로는 사람(선행 finding 대조) |
| **유출 항목** | **사람 확인** | — | 호스트 · DB 조사 · 「조사 중」 |
| **유출 규모(정보주체 수)** | **사람 확인** | — | 조사 · 「조사 중」 |
| 피해 최소화 조치 · 결과 | 플랫폼 기록(JSON 밖) → 사람 확인 | `action_executions`(격리 · 차단 · 해제 시각 · 기록된 상태) | **개인정보 유출 신고 · 통지는 AI 신고 초안 범위 밖**이라(§2-5) 이 서식에는 사람이 옮겨 적는다 · 비밀번호 초기화 · 키 폐기 등 호스트 조치도 사람 |
| 정보주체 구제절차 · 통지 방법 | 사람 확인 · AI 서식 문안 | — | 고객사 결정 |
| 담당부서 · 연락처 | 사람 확인 | 테넌트 API에 없다 | 사람 입력 또는 별도 출처 |

**ⓖ 추가 확인 · 정정** — 24h(과기정통부/KISA)와 72h(개인정보위)는 **제출처가 다르다**. 같은 사건의 두 보고서가 서로 다른 「확인 시점」을 가질 수 있어, 제출본마다 「어느 근거 스냅숏으로 썼나」가 남아야 한다 — §3-1 ⓖ의 제출본/작업본 구분 요구가 여기서 가장 커진다(담당 협의 전 후보).

**이 시나리오의 공백** — ① 유출 항목 ② 유출 규모 ③ 개인정보처리시스템 여부(태그·고객 선언) ④ 초기 침입 경로 ⑤ 두 기관 제출본의 스냅숏 구분. ①②는 플랫폼 밖이라 공백 유지 · ③⑤는 §4로.

### 3-4. 세 시나리오에서 같은 것

- 플랫폼이 **JSON으로 채울 수 있는 칸은 「언제 · 어디서 · 무엇을 관측」** 셋이고, 그것도 근거(`evidence`)가 있을 때만이다(지금은 모의 입력 A1·A2). **「무슨 조치」는 #488 JSON 밖 기록**(`action_executions` · `audit_events`)이고, AI 신고 초안이 만들 때 따로 읽어 서버 사실로 싣는다(§2-5 · 침해사고 신고에 한함). GuardDuty 경유 위협은 탐지 요약 8칸이 기록되며(PR #615), 실 계정 확인은 11/16(월) 뒤다.
- **「피해 내용 · 유출 항목 · 규모 · 침입 경로」는 전부 사람 칸**이고, AI 초안은 이 칸을 「미확인/조사 중」으로 명시한다 — 비우면 「없음」으로 읽힌다(선례 `MVP_CLOSEOUT_CHECKLIST.md` 머리말).
- 시각은 다섯(§2-3)이고 **사람의 최초 인지는 그중 어느 것도 아니다.** 서식에는 다 적고 기산점은 사람이 고른다.
- `trigger_source`(실행 계기) · 승인(`audit_events`) · 실행 결과(`action_executions` 상태)는 **별개로 표시**한다. 차단·격리 성공이나 관제자의 `RESOLVED` 종료 판단을 원인 제거·복구·신고 전체의 완료로 쓰지 않는다.
- 보고서 문장마다 출처를 셋 중 하나로 가른다 — **서버 기록 / AI 문장 / 사람 확인**. 기록된 사실을 제시하되 준수·충족 판정으로 확대하지 않는다.
- 24h와 72h는 제출처 · 조건 · 항목이 다르다. 한 사건에 보고서가 둘 나갈 수 있고, 각각 제출본 스냅숏이 필요하다.

### 3-5. 전 행 요약 — 누가 무엇을 채우는가

§3-1 ~ §3-3은 세 행을 끝까지 푼 것이고, 이 표는 §1의 **모든 행**에 같은 물음을 한 줄씩 답한다. 행마다 시나리오를 따로 길게 쓰지 않은 이유는, 플랫폼이 넣어 주는 입력의 꼴이 다섯 가지뿐이고 같은 꼴이면 채워지는 칸도 같기 때문이다. 초안에 관한 칸은 §2-5 계약 기준이며 **작성 모델이 연결되기 전에는 초안이 만들어지지 않는다.**

**입력 꼴 다섯**

| 꼴 | 초안의 서버 사실(§2-5) | 대상 |
| --- | --- | --- |
| **O** — `OPEN_IP` | 공통 칸 + 접근 허용 CIDR · 개방 포트 | 보안 그룹 |
| **S** — `SSH_BRUTE_FORCE` | 공통 칸 + 공격 출발지 IP · 실패 횟수 · 관측 구간 | 인스턴스 ARN · 피해 IP 후보는 EC2 사본이 있을 때 |
| **G-EC2** — `GUARDDUTY_FINDING`(인스턴스가 식별됨) | 공통 칸 + 탐지 요약 8칸 | 인스턴스 ARN · 피해 IP 후보는 EC2 사본이 있을 때 |
| **G-기타** — `GUARDDUTY_FINDING`(인스턴스가 아님 · IAM 등) | 공통 칸 + 탐지 요약 8칸 | **탐지 자체의 ARN** — 자원 ARN이 아니다. 피해 IP 후보는 `not_collected` |
| **없음** | 인시던트가 없다 → 초안 경로 없음 | — |

어느 꼴이든 조치를 실행했으면 `action_execution` · `audit_event` 사실이 더해지고, 없으면 「대응 이력 미확인(`no_history`)」으로 나온다. **사람이 채우는 칸의 바탕은 어느 행이든 `human_input` 16칸과 확인 2칸이다** — 아래 「사람이 더 채우는 것」은 그 16칸에 넣을 내용 가운데 이 행에서 플랫폼 기록으로는 알 수 없는 것이다. 「AI 초안 입력」은 모델이 받는 사실(= 그 꼴의 서버 사실)이고, 모델은 사실을 더하지 않는다.

| 행 | 보고서 · 기한 | 플랫폼이 채우는 칸 | 사람이 더 채우는 것 | AI 초안 입력 | 이 행만 다른 점 |
| --- | --- | --- | --- | --- | --- |
| A1 · B3 공개 인그레스 노출 | ISMS 통제 증적 · 내부 분석. 침해가 아니라 신고 대상이 아니다 | 꼴 O + 승인 뒤 차단 조치 기록 | 노출이 의도된 것인지 · 노출 기간에 실제 접근이 있었는지 | 꼴 O | 맞는 산출물은 신고 초안이 아니라 **근거 내보내기(#488)** 다. 초안을 요청하면 침해사고 신고 서식으로 나오므로 「사고 원인 · 피해」 칸은 비워 둔다 |
| A2 · B1 SSH 무차별 대입 | 시도 = 내부 분석 / 성공 확인 = 침해사고 신고 24h | 꼴 S + 선차단 · 격리 조치 기록 | 로그인 성공 여부 · 호스트 내부 피해 | 꼴 S | **§3-1** |
| B1 RDP · WinRM 무차별 대입 | 위와 같다 | 꼴 G-EC2 | 위와 같다 + 공격 출발지(GuardDuty 원문) | 꼴 G-EC2 | 꼴 S와 달리 **공격 출발지 IP가 플랫폼 기록에 없다** · 자동 조치 없음 |
| B2 정찰 · 포트 스캔 | 내부 분석 · ISMS 증적 | 꼴 G-EC2 | 상대 IP · 포트(GuardDuty 원문) · 뒤이은 침입 시도가 있었는지 | 꼴 G-EC2 | 심각도 Low 유형(`PortProbeUnprotectedPort`의 기본)은 인시던트가 없다 → 꼴 없음 |
| B4 C&C · 백도어 · 트로이 | 침해사고 신고 24h | 꼴 G-EC2 | 감염 경로 · 악성 프로세스 · 확산 범위 | 꼴 G-EC2 | **§3-2** |
| B5 암호화폐 채굴 | 침해사고 신고 24h | 꼴 G-EC2 | 감염 경로 · 채굴 프로세스 · 자원 사용 피해 규모(피해 수량 칸) | 꼴 G-EC2 | 「피해 규모」가 요금으로 나타난다 — 플랫폼 비용 화면의 값은 초안 사실에 없다 |
| B6 DoS 발신 | 침해사고 신고 24h(양식 선택지 「디도스」) | 꼴 G-EC2 | 공격 대상(원격 IP) · 트래픽 규모 · 감염 경로 | 꼴 G-EC2 | 내 자산이 가해 측이다. 초안의 「탐지 대상」은 피해자가 아니라 **가해에 쓰인 내 인스턴스** |
| B7 DoS 수신 | 침해사고 신고 24h(「디도스」) | 없음 | 전부 — 근거는 플랫폼 밖(AWS Shield · 로드 밸런서 지표 등) | 꼴 없음 | **공백 행** — 플랫폼이 보지 못한다 |
| B8 데이터 유출 | 침해사고 신고 24h + 개인정보 포함 시 72h | 꼴 G-EC2 | 유출 항목 · 규모 · 개인정보 포함 여부 | 꼴 G-EC2(침해사고 신고 쪽만) | **§3-3**. 72h 쪽(개인정보 유출 신고 · 통지)은 **초안 범위 밖**이라 전부 사람이 쓴다 |
| B9 자격 증명 유출 | 침해사고 신고 24h · 유출 경로면 72h | 꼴 G-기타(`MetadataDNSRebind`처럼 인스턴스가 식별되면 G-EC2) | 어느 사용자 · 액세스 키 · 역할인지 · 어느 IP에서 어떤 API를 불렀는지 · 키 폐기와 교체 | 꼴 G-기타 | 플랫폼 런북(`RunbookId`)에 IAM 조치가 없다 → **대응 기록도 플랫폼에 남지 않는다.** 「대응 이력 미확인」을 「조치 없음」으로 읽지 않는다 |
| B10 계정 탈취 · 권한 상승 | 침해사고 신고 24h | 꼴 G-기타 | 위와 같다 + 바뀐 권한 · 만들어진 사용자 | 꼴 G-기타 | `RootCredentialUsage`(Low)는 인시던트가 없다 → 꼴 없음 |
| B11 탐지 회피 · 로그 끄기 | 단독으로는 내부 분석. 다른 행의 신고에 「근거가 훼손됐다」는 사실로 들어간다 | 꼴 G-기타(`UnusualDNSResolver`는 G-EC2) | 로그가 꺼진 기간 · 그 기간의 미확인 범위 | 꼴 G-기타 | `CloudTrailLoggingDisabled`(Low)는 인시던트가 없어 `threat_events`에서 찾는다. 이 행이 있으면 다른 행의 「미확인」 칸이 늘어난다 |
| B12 랜섬웨어 · 데이터 파괴 | 침해사고 신고 24h(「랜섬웨어」) · 개인정보면 72h | 없음 | 전부 | 꼴 없음 | **공백 행** — 플랫폼이 켜는 탐지기는 EBS 멀웨어 보호와 S3 데이터 이벤트를 꺼 둔다(`DISABLED_FEATURES`) |
| B13 피싱 · 악성 도메인 질의 | 내부 분석 → 감염 확인 시 24h | 꼴 G-EC2 | 질의한 도메인(GuardDuty 원문) · 감염 여부 | 꼴 G-EC2 | 질의 한 번은 감염의 증거가 아니다 — 초안의 「탐지 유형」 해석 문구가 이를 밝힌다 |
| B14 Tor · 악성 IP 통신 | 침해사고 신고 24h(침해 확인 시) | 꼴 G-EC2(`Recon:IAMUser/TorIPCaller`는 G-기타) | 상대 IP(GuardDuty 원문) · 통신 내용 · 감염 경로 | 그 꼴 | — |
| B15 침투 시험 도구 | 내부 분석 | 꼴 G-기타 | **승인된 시험인지** · 시험 주체와 기간 | 꼴 G-기타 | 승인된 시험이면 기록만 남긴다. 승인 여부는 플랫폼 기록에 없다 |
| B16 고객사 내부자 | 내부 분석 · 개인정보면 72h | 없음(플랫폼 사용자의 행위만 `platform_events`에 남는다 — 초안 사실에는 없다) | 전부 | 꼴 없음 | **공백 행** |
| B17 S3 데이터 노출 · 유출 | 개인정보면 72h · 침해 확인 시 24h | 없음(아래) | 전부 | 꼴 없음 | **공백 행** — S3 데이터 이벤트 탐지를 꺼 둔다 |
| B18 DB(RDS) 무단 접근 | 위와 같다 | 없음(아래) | 전부 | 꼴 없음 | **공백 행** — RDS 로그인 이벤트 탐지를 꺼 둔다 |
| A5 플랫폼 자체(로그인 무차별 대입 · API 남용) | 신고 주체가 고객사가 아니라 **플랫폼 운영자** | 없음 — 고객사 인시던트가 아니다 | 전부. 근거는 WAF 로그와 `platform_events`(로그인 기록 호출은 PR #630 · 11/16(월) 기준 머지 전) | 꼴 없음 | 고객사용 초안 경로를 쓸 수 없다. 운영자가 쓰는 별도 문서다 |

B17 · B18에 대해 — 플랫폼은 탐지 유형으로 거르지 않고 읽으므로, 고객사가 부가 보호 기능을 직접 켜서 탐지가 생기면 꼴 G-기타로 들어온다. 부가 기능 없이도 생기는 S3 탐지가 있는지는 원문으로 확인하지 못했다.

**표를 접으면** — 플랫폼이 채우는 칸은 다섯 꼴 어디서나 「언제 · 어디서 · 무엇을 관측했나」와 「플랫폼이 한 조치의 기록」까지다. **원인 · 피해 · 상대 주소 · 인지 시점 · 신고자**는 어느 행에서도 사람이 채운다. 초안 경로가 없는 것이 6행(B7 · B12 · B16 · B17 · B18 · A5)이고, 초안 경로는 있지만 대상이 자원이 아니라 탐지 ARN으로만 남는 것이 IAM 쪽 4행(B9 · B10 · B11 · B15)이다.

---

## 4. 공백 모음 — 넘길 자리 (전부 **담당 협의 전 후보 · #488 확정 범위 아님**)

| 공백 | 어디로 | 상태 |
| --- | --- | --- |
| 호스트 로그인 성공 여부 · 호스트 내부 피해 범위(§3-1) | #479 §2 후보(시스템·네트워크 로그 분석 모듈 — 안성일 10/30 아이디어 2) | 미제기 |
| 개인정보처리시스템 표시(태그 또는 고객 선언) | #479 §2 후보 | 미제기 |
| ~~조치 · 승인 기록을 보고서 입력으로 연결~~ | #623 | **해소** — AI 신고 초안이 `action_executions` · `audit_events`를 따로 읽는다(PR #631 · §2-5) |
| 발송 이력(외부 알림)과 `platform_events`는 초안 사실에 없다 | #623 후속(안성일 확인 필요) | 미제기 |
| 개인정보 유출 신고 · 통지(72h)는 초안 범위 밖 — 서식 전체를 사람이 쓴다 | #479 §2 후보 | 미제기 |
| 작성 모델 미연결 — 기본 구성에서는 초안이 만들어지지 않는다 | #623 완료 조건 마지막 1개(안성일 · 모델 선정) | 진행 중 |
| 제출본(스냅숏)과 작업본 구분 | #488 후속 | 미제기 |
| 공개 IP · 신고 담당부서·연락처 출처 | 사람 입력 유지 또는 테넌트 프로필 확장 제안 | 미제기 |
| ~~IAM finding 수집 여부(A4 · B9~B11 · B15)~~ | #588 | **해소** — 읽는다(#613 코멘트 `6815319309` · PR #615) |
| IAM 탐지의 「누구의 자격 증명 · 어디서 불렀나」(사용자 · 액세스 키 · 호출 IP · API) | 사람 입력(GuardDuty 원문) 또는 서버가 원문 칸을 더 읽는 후속(#588 · 지금 계획 없음) | 미제기 |
| 심각도 Low 탐지는 인시던트가 없다(B10 `RootCredentialUsage` · B11 `CloudTrailLoggingDisabled`) — 인시던트에서 출발하는 보고서에 빠지는지 | #488 · #623(안성일 확인 필요) | 미제기 |
| 플랫폼이 만든 탐지기는 부가 보호 기능이 꺼져 있다 → S3 · RDS 쪽 탐지가 생기지 않을 수 있다 | 공백 행 유지(B17 · B18 · §3-5) | — |
| IAM 쪽 위협(B9 · B10)에 쓸 플랫폼 조치가 없다(`RunbookId`에 IAM 조치 없음) → 대응 기록이 플랫폼에 남지 않는다 | #479 §2 후보 | 미제기 |
| `platform_events` 로그인 실패 기록 호출(A5) | #589 B | 안성일 #589 코멘트 `6783885796` |
| DoS 수신 · 랜섬웨어 · 고객사 내부자(B7 · B12 · B16) | 공백 행 유지(§3-5) | — |
| 플랫폼 자체 침해(A5)의 신고 문서 — 고객사용 초안 경로를 쓸 수 없다 | 운영 문서(담당 미정) | 미제기 |

---

## 변경 이력

| 날짜 | 내용 |
| --- | --- |
| 2026-11-11 11:31 | 1판 — §0 원천 · §1 분류표(#613 코멘트 `6784418428` 옮김) · §2 칸 정의 · §3 시나리오 3건(A2 SSH 무차별 대입 · B4 C&C/백도어 · B8 데이터 유출 의심) + §3-4 공통점 · §4 공백 모음. `[S]` 1행(B13)은 원문 재확인 전 |
| 2026-11-11 16:15 | PR #614 1차 리뷰(안성일 · #613 코멘트 `6785035815`) 반영 — 테이블명 `action_executions`·`evidence_items` · 실행→근거 연결 삭제 · A1 「SG 대상 자동 격리 없음」 · A2 「즉시 선차단(목표 3초)」·MEDIUM 60초 조건·NACL 24h 예약 의미 · A3·B축 GuardDuty 「계획 입력」 · A5 #589 B 연결 필요 · §2-2 #488 JSON 계약 요약(PR #616 · 당시 머지 전) · §2-3 시각 다섯과 사람 최초 인지 분리 · §2-4 미확인 기준 · ⓔ 항목표를 4칸(주체·JSON 경로/별도 기록·부족 자료)으로 · 공개 IP·담당부서는 사람 확인 · §3-4에 기록≠판정 · `trigger_source`/승인/결과 분리 |
| 2026-11-11 17:08 | 외부 인용 원문 확인 — GuardDuty EC2 페이지에서 `DriveBySourceTraffic!DNS` High 등 인용 심각도 전부 일치(`[S]` 해제) · 보호나라 신고 양식 선택지 3종(랜섬웨어 · 디도스 · 그 밖의 해킹) 확인 → §0 · §1-2 주석 추가 · §3-1 GuardDuty SSH 한계(22번 포트 · IPv6 미지원) |
| 2026-11-13 | PR #614 2차(안성일 `5855391165`)·3차(김세혁 `5867138412`) 리뷰 반영 — §2-4 미확인 기준을 셋으로(대상 ARN `event.target_arn`은 항상 기록 → 유지 / 사본 칸은 사본·값 없을 때 / 로그 칸은 `log_evidence` 없을 때) · §3-1 ⓔ 피해 시스템 행에 `target_arn` 경로 추가 · ⓕ 「`context` null → 대상 전부 미확인」 삭제 · B10 두 유형으로 분리 · ⓔ 머리에 시행령 제58조의2 신고 사항 3가지 · ⓓ·§3-3 72h 「거의 자동 충족」 → 「해당할 수 있다 · 단서 적용은 고객사 판단」 · PR #616 머지 반영(§0 · §2-2) · 변경 이력에 시각. **PR #615(GuardDuty 탐지 읽기 · 11/12 머지)와 #613 김승철 A4 답(IAM finding 읽음)은 2판 입력** |
| 2026-11-13(2판 작업 ① · ②) | GuardDuty 결과 읽기 구현(PR #615 · #624)과 김승철 IAM 탐지 확인(#613 코멘트 `6815319309`) 반영 — §0 원천 · A3 「계획 입력」 → 구현 · A4 「수집 여부 미정」 → 읽음(대상 = 탐지 ARN) · B축 「GD(계획)」 → 「GD」 · B9 · B10 · B11 · B15 · 집계 문단(직접 2 · GD 11 · 공백 3) · §3-2 수신 · 기록 · 응답 모드 · 공백 · §4 공백 4행. ② §3-1 원천 · §3-2 ⓔ · §3-3 ⓑⓒⓔ의 「계획」을 걷고 GuardDuty JSON 경로(`evidence[].content.event.payload.*`)를 채움 — 1판이 플랫폼 기록으로 적었던 원격 IP · 도메인은 사람 확인으로 정정 |
| 2026-11-16(2판 작업 ③) | AI 신고 초안 계약(PR #631 · 11/13 머지) 반영 — §0 원천 1행 · **§2-5 신설**(세 구역 · 유형별로 고르는 칸 · 초안에 없는 것) · 「조치 · 승인 기록은 사람 확인」을 「초안이 서버 사실로 읽는다」로 고침(§2-2 · §2-4 · §3-1 ⓒⓔ · §3-2 ⓔ · §3-4) · §3-3은 초안 범위 밖임을 명시. **§3-5 신설 — 전 행 요약표**(입력 꼴 다섯 · 행마다 보고서 · 플랫폼이 채우는 칸 · 사람이 채우는 것 · AI 초안 입력). B축에 **B17(S3) · B18(DB) 공백 행** 추가 → 공백 5. §4에 해소 1 · 추가 5행. 제목 2판. 작성 모델이 연결되지 않아 초안 서술은 코드와 대역 테스트 기준이다 |

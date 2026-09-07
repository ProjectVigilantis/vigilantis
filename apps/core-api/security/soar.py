# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 보안 위협 대응(SOAR)의 자리 파일입니다. 구 security-soar 앱이 Lambda 핸들러 형태를
# 벗고 흡수된 자리이며(ADR-0001), 그 뒤 실제 일은 아래 모듈로 나뉘어 구현됐습니다.
# 이 파일에는 구현이 없습니다.
#
# [현재 소재]
# 1. 위협 이벤트 수신·정형화 — security/threat_normalizer.py
#    (MockThreatEventInput → NormalizedThreatEvent. 입력은 severity·response_mode를
#     받지 않습니다 — 위험도는 판정기가 냅니다.)
# 2. 초기 위험 판정 — security/risk_evaluator.py
#    (initial_risk_level·response_mode·reason_codes. 판정 규칙 확정은 PR #206.)
# 3. Incident 생성 — incident_intake.py (Issue #254)
# 4. 차단·격리 실행과 원클릭 해제 — POST /api/v1/actions/execute →
#    workflows.py(상태 전이·트랜잭션) → dispatcher.py(디스패치) →
#    services/aws/executor.py(AWS 호출). 해제는 별도 경로가 아니라 롤백 런북
#    3종이며 같은 실행 계약을 씁니다(ADR-0004).
#
# [Post-MVP]
# 0.5초 실환경 선제 차단이 필요해지면 이 자리가 Lambda/EventBridge로 나갑니다
# (ADR-0001 Post-MVP 전환 경로). MVP 범위에서 PRE_MITIGATION_0_5S로 발동하는 런북은
# RUNBOOK_EC2_ISOLATE 하나뿐이고 그 런북은 P2입니다 — 출처는 SSOT의 Action Whitelist
# 표이며, 런북별 trigger_source 매핑은 코드에 없습니다(TriggerSource는 실행 레코드에
# 붙는 값입니다).
# ==============================================================================

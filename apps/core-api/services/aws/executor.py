# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# Boto3 기반 AWS 제어 모듈입니다. ai/whitelist.py의 확정 10종 Runbook
# (ADR-0002 본편 7 + ADR-0004 롤백 3)만 실행하며, 조치 전 원본 상태 스냅샷을 백업합니다.
#
# [수행해야 할 작업]
# 1. 확정 10종 Runbook 실행 함수 (예: RUNBOOK_EC2_RIGHTSIZING —
#    변경 전 SpecSnapshot 저장 후 인스턴스 타입 변경)
# 2. 실행 전 AWS Dry-Run(DryRun=True) 유효성 검증 연동
# 3. 롤백 3종 실행도 executor 경유 — 트리거 판단·감시는 rollback.py 담당
#    (예: REVERT_SIZE 실행은 executor 경유, Status Check 실패 감지·발동은 rollback.py)
# ==============================================================================

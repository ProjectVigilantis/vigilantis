# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# Boto3 기반 EC2/SG 제어 모듈입니다. 허용 Runbook(RUNBOOK_EC2_DOWNSIZE,
# RUNBOOK_IP_BLOCK)만 실행하며, 조치 전 스펙 스냅샷을 백업합니다.
#
# [수행해야 할 작업]
# 1. downsize_ec2(arn, target_spec): 변경 전 SpecSnapshot 저장 후 인스턴스 타입 변경
# 2. block_ip(sg_id, cidr): Security Group 인바운드 규칙 차단
# 3. 실행 전 AWS Dry-Run(DryRun=True) 유효성 검증 연동
# ==============================================================================

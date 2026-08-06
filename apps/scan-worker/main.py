# ==============================================================================
# [파일 설명]
# 이 파일은 Vigilantis Scan Worker 서비스의 메인 실행 파일 (main.py)입니다.
# AWS Step Functions 및 ECS Fargate 환경에서 실행되는 분산 스캐너로, 24/7 클라우드
# 자산 관제 및 Terraform (.tfstate) vs 실제 AWS 리소스 간 Drift를 감지합니다.
#
# [수행해야 할 작업]
# 1. AWS SDK (Boto3)를 활용한 Multi-Account/Region 리소스 상시 관제 스캔 로직 작성
# 2. Terraform plan / show JSON 파싱 및 tfstate와 실제 인프라 상태 비교 Drift 감지 구현
# 3. 감지된 Drift 및 보안 위협 이벤트를 Core API 및 EventBridge로 전송하는 파이프라인 연동
# 4. Fargate 태스크 파라미터 수신 및 주기적 스캔 스케줄링 처리
# ==============================================================================

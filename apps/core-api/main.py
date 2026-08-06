# ==============================================================================
# [파일 설명]
# 이 파일은 Vigilantis Core API 백엔드 서비스의 메인 엔트리포인트 (main.py)입니다.
# FastAPI 기반 RESTful API를 제공하며, GitOps PR 엔진과 Boto3 엔진을 통한 Dual-Path
# 조치 실행 및 PostgreSQL/Redis 연동 State Sync를 관리합니다.
#
# [수행해야 할 작업]
# 1. FastAPI 앱 객체 초기화 및 CORS, OpenTelemetry 미들웨어 설정
# 2. Dual-Path 조치 실행 API 엔드포인트 구현 (GitOps PR 생성 및 Boto3 즉시 실행)
# 3. 헬스 체크, Drift 감지 결과 조회, Runbook ID 기반 자동 조치 및 Auto-Rollback 트리거 라우터 연결
# 4. DB (PostgreSQL) 및 캐시 (Redis) 세션 관리 및 에러 핸들러 구성
# ==============================================================================

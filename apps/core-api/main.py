# ==============================================================================
# [파일 설명]
# Vigilantis Core API — MVP 단일 FastAPI 백엔드 메인 엔트리포인트입니다.
# routers(assets/incidents/actions) 등록, APScheduler 주기 스캔 기동, DB 세션
# 초기화를 담당합니다. (마이크로서비스/Lambda/Step Functions 분리는 Post-MVP)
#
# [수행해야 할 작업]
# 1. FastAPI 앱(app) 생성, CORS 및 전역 예외 핸들러 구성
# 2. routers.assets / routers.incidents / routers.actions 라우터 등록
# 3. services.scheduler(APScheduler) 기동으로 EC2/SG 주기 수집·판별 트리거
# 4. db.session 초기화 및 /health 헬스체크 엔드포인트 제공
# ==============================================================================

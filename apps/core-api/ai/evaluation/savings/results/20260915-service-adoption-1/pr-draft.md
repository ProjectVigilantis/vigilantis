# ✨ [FEAT] #347 - 조치별 단가 추정과 절감 예상 저장·조회 구현

**head:** `feat/AI-347-action-savings` · **base:** `dev`

현재 로컬 초안이며 게시하지 않았다. 후보는 v0.9.1이고, 가격 기준·요약 재통과 뒤 첫 PR 버전 v1.0.0을 확정한다.

## 개요

EC2 다운사이징 후보에 월 절감 예상과 산출 근거를 제공한다. 서버가 정한 현재·목표 사양에 대해 AI가 시간당 단가 두 개를 추정하고, 서버가 월 금액을 계산해 저장한다. 상세 API는 저장본을 반환한다.

현재 최종 생성 검증에서 60회 중 2회가 절감액 과대 추정 한도를 넘어 채택을 보류했다. 가격 합격 후 기존·새 요약 짝 비교와 승인 스냅샷 갱신이 남아 있다.

**리뷰 요청**: @SehyeokKim — DB migration과 기존 후보 호환성, 단가 추정 실패 시 후보 보존 흐름.

**리뷰 요청**: @yoogh3546 — 상세 API `recommendations[].ai_savings_estimate`의 상태·금액 문자열·null 해석과 화면 소비 계약.

## 변경 사항

- **서비스:** 요약 → 추천 → 후보 계약 검사 → 단가 추정의 3호출을 연결한다. 유효한 RIGHTSIZING에만 단가를 요청하며, 단가 호출만 실패하면 후보를 보존한다.
- **계약·계산:** 시간당 단가와 근거를 받아 서버가 `(현재 단가 − 목표 단가) × 730`을 계산한다. USD 월 금액은 소수 2자리, 단가는 6자리 문자열로 응답한다. Linux·공유·온디맨드·인스턴스 컴퓨팅만 비교하는 가정을 명시한다.
- **저장·조회:** nullable JSONB 컬럼과 migration을 추가하고 후보·Workflow·Mapper·상세 응답에 전달한다. `ESTIMATED`, `UNAVAILABLE`, `INVALID`, 기존 후보의 null을 구분한다. 조회 시 AI를 재호출하지 않는다.
- **평가 도구:** 기존 실험 호출 경로를 `ai/evaluation/savings/legacy.py`로 보존하고 단가 채점·단계별 호출 기록·저장 결과 재생 검증을 추가한다. 기존 `finops_eval.py --estimate`는 최대 3호출로 계산한다.
- **실험 결과:** v0.9.1 서비스 생성 60회는 가격 PASS 58·과대 추정 2(+10.77%, +13.85%). 승인 v2 재생성과 합쳐 실제 300호출이며, 중단 조건에 따라 요약 판정 240호출은 미실행이다. 상세 기록은 `apps/core-api/ai/evaluation/savings/results/20260915-service-adoption-1/`에 보관한다.

공개 응답 소비 예시는 같은 폴더의 `saved-output-verification.json`에 있다. FE 타입과 화면 구현은 후속 소비 작업이며, API에는 필드가 추가된다. 이 계약 변경은 머지 후 PM의 SSOT API 갱신 대상이다.

## 테스트

- [ ] `pytest` 전체 통과 — 현재 CI와 같은 인자의 로컬 실행은 **2,140 passed / 1 failed / 2 skipped**. 실패는 승인 v2와 미채택 v0.9.1의 스냅샷 불일치. 두 skip은 기존 E2E 무조건 보류다. PostgreSQL·LocalStack 의존 테스트의 환경 skip은 없다.
- [x] Docker Compose로 로컬 기동 확인 — 검증용 `api`를 `run --build`로 기동해 `/health` 200, OpenAPI의 신규 필드, 단일 worker와 자동 분석·스캔 비활성 상태 확인.
- [x] (`apps/web` 변경 시) 프론트 lint·build·test — (해당 없음) 프론트 파일 변경 없음.
- [x] (API 변경 시) FE↔BE 계약/Mock 영향 확인 — 추천 응답에 nullable 필드 추가. 현재 FE `RecommendationItem`에는 해당 필드가 없으므로 소비 타입·표시는 후속 구현 대상이다. 서버 응답 예시 제공.
- [x] 실제 생성 내용 6건을 DB에 저장하고 새 앱 두 개에서 동일 응답 조회 — 합성 식별자만 UUID로 대응한 재생 검증이며 추가 모델 호출 0회. 온라인 생성→저장 연속 실행과 실제 AWS 사전검사는 포함하지 않음.
- [x] 변경 Python 파일 lint 비교 — 현재 HEAD 대비 추가 진단 0건. 기존 진단은 별도 기록.
- [ ] 최종 후보 가격·요약 품질 기준 재통과 및 v1.0.0 승인 스냅샷 확정.

## 관련 이슈

Refs #347

게시 전 확인: 열린 PR #344와 `db/models.py`가 겹치고 두 작업 모두 Alembic migration을 추가한다. 병합 순서에 맞춰 migration head를 정리한다.

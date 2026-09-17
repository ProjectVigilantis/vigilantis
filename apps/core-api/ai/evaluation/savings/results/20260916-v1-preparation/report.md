# v1.0.0 정리·summary 이관 검증

2026-09-16, `feat/AI-347-action-savings` 작업본. `origin/dev`의
`b8087c449a90f509ceed17bffe22ad6b6d95be9e`를 fast-forward로 통합한 뒤 검증했다.
커밋·푸시·PR 게시 전의 로컬 검증 기록이다.

## 변경과 유지한 기준

단가 전용 B 요청을 서비스의 v1.0.0 후보로 정리했다. AI의 현재·목표 단가에 서버가
비교 조건과 월 730시간 산식을 결합하고, 설명의 출처를 `SERVER_TEMPLATE`으로 저장한다.
모든 후보의 가드레일 검사가 끝난 뒤 통과한 RIGHTSIZING 후보만 단가를 요청한다.
실패·비가용 추정은 0달러로 바꾸지 않으며 통과한 조치 후보 자체를 폐기하지 않는다.

#324 동결에 따라 그 작업본에서 준비한 FinOps `summary/`와 `common/` 이관을 #347에 편입했다.
SecOps의 입력·프롬프트·호출 순서·평가 결과는 가져오지 않았다. 기존 실험 스크립트의
재채점을 위해 필요한 옛 import와 CLI 진입점은 호환 경로로 남겼다.

- FinOps 요약 승인 v2, 요약·추천의 2회 요청, 단가 B 요청, 판정자, 고정 입력 6건의 지문이 모두 같다.
- summary 승인 스냅샷은 이동 전과 바이트가 같다.
- 기존 savings 결과 파일 195개는 해시가 같다. 과거 기록의 버전·소스 해시를 현재 코드에 맞춰 다시 쓰지 않았다.
- 동결된 #324 작업본 409개 파일과 Git 상태·HEAD가 그대로다.
- 상세 비교는 [verification.json](verification.json)과 [migration.json](migration.json)에 보존했다.

## 이번 무료 검증

| 확인 | 결과 | 보장 범위 |
| --- | --- | --- |
| `uv sync --locked --all-packages` | 성공 | 잠금 파일의 의존성 설치 |
| Python 전체 회귀 | **2,230 passed, 2 skipped**, 138.77초 | CI와 같은 테스트 디렉터리, 실제 로컬 PostgreSQL·LocalStack 사용 |
| 변경 경계 집중 회귀 | **134 passed** | summary 경로·지문, 추정 계약·호출·저장 경계 |
| [대표 서비스 재검증](../20260916-v1-free-validation-2/free-service.json) | **6/6, 새 조회 24회**, 실제 모델 0호출 | SDK 응답 대역 → LocalStack 가드레일 → PostgreSQL 저장 → 새 앱·세션 조회 |
| Compose 이미지 빌드·기동 | 성공 | 새 DB에 migration `c7a9e1d83f24`까지 적용, API 시작, 외부 모델 키 없음 |
| Compose HTTP | 모두 200 | `/health`, `/api/v1/incidents`, `/openapi.json`; 추천 응답에 추정 필드 존재 |
| 변경 Python 64개 Ruff | 기존 진단 216개, dev 대비 추가 0개 | 이관 전 경로와 규칙·메시지별 비교. 전체 lint 무경고를 뜻하지 않음 |

전체 테스트의 skip 2건은 `tests/test_e2e_scenario.py`의 미작성 T1·T2 본문이다.
DB·LocalStack 부재로 건너뛴 테스트는 없다. 입력 사실을 DB에 심는 서비스 검증은
위협 접수나 수집부터 이어지는 전 구간 E2E를 대신하지 않는다.

대표 서비스 재검증은 실제 OpenAI SDK의 HTTP 전송을 대역으로 처리했다. 모델 형식·요청 순서와
B 요청의 일치, 가드레일 이후 호출, DB 저장과 조회 시 재호출 0회를 확인한다.
합성 응답의 가격 PASS를 새 모델 정확도 실측으로 세지 않는다.
전송은 18회 모두 대역이며, 실제 모델 호출은 **0회**다.

첫 무료 시도는 Windows 샌드박스에서 결과 JSON의 원자적 파일 교체가 `WinError 5`로 거절되어
A1·A7·A11 완료 후 중단됐다. [해당 기록](../20260916-v1-free-validation/)을 보존하고
호스트에서 새 디렉터리로 다시 실행했다. 두 시도가 만든 LocalStack 인스턴스 10개는 모두
종료됐고 임시 서비스 DB는 남지 않았다. Compose 검증용 별도 프로젝트·볼륨도 정리했다.

## 기존 유료 근거와 남은 판단

[B 반복 실측](../20260916-numeric-output-1/report.md)은 60/60 가격 PASS,
[실제 모델 서비스 검증](../20260916-numeric-service-1/report.md)은 6/6 경로·18호출이다.
당시 단가 진단 이탈·금액 하한 사례도 그대로 남겼다. 이번 경로 이동은 새 유료 실험이 아니다.
기존 승인 예산은 **80/80회**, 이번 추가 호출은 **0회**다.

AI가 금액·근거를 추천 호출에서 함께 만든다는 SSOT의 기존 계약과 달리,
현재 구현은 별도 단가 요청 후 서버가 금액·설명을 만든다. 이 변경의 채택은 #347 PR에서
검토하고, 합의 후 FE 표시와 PM 소유 SSOT 갱신을 연결한다. 현재 FE 표시는 이 검증의 완료 범위에 없다.

확인 시점에 열린 PR #344의 migration `95956d08c917`과 이번 `c7a9e1d83f24`는 모두
`b5e2d7a4c19f` 다음이다. 둘째로 병합되는 PR에서 실제 병합 순서에 맞춰 revision을 연결하고
단일 head·업그레이드를 재확인해야 한다. 현재 #347 단독 새 DB 업그레이드는 통과했다.

## 재현

저장소 루트에서 로컬 PostgreSQL·LocalStack을 켠 뒤 실행한다. 무료 검증 출력 경로는 새 디렉터리여야 한다.

```powershell
uv sync --locked --all-packages
uv run --no-sync python -m pytest tests apps/core-api/services/tests apps/core-api/ai/tests packages/schemas/tests apps/core-api/db/tests apps/core-api/tests apps/core-api/security/tests -q -rs
uv run --no-sync python scripts/finops_eval.py --estimate --repeats 10
uv run --no-sync python scripts/finops_judge.py --help
uv run --no-sync python apps/core-api/ai/evaluation/savings/verify_service.py --out <새-결과-디렉터리>
```

Compose 확인에는 모델 키를 비우고 `SCAN_ENABLED=false`, `DISPATCH_ENABLED=false`인 격리 프로젝트를 썼다.
검증 값은 [docker-verification.json](docker-verification.json)에 있다.
게시 정리(2026-09-16): 원본 `docker-startup.log`는 로컬에 보존하고 커밋 대상에서 제외했다.
당시 검증 수치와 실험 원자료는 변경하지 않았다.

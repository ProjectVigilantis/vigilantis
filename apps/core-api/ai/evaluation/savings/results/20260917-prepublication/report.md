# 최신 dev 반영 후 게시 전 검증

2026-09-17, `feat/AI-347-action-savings`. #358이 병합된 dev `65e47d87aea1eafd152c89af10a76781f3d75716`를 fast-forward로 반영한 작업본이다.
추가 유료 모델 호출은 **0회**다.

## 자동 테스트

- `uv sync --locked --all-packages --offline`: exit 0.
- 별도 로컬 PostgreSQL·LocalStack 기동과 시드 후 CI와 같은 7개 경로 실행.
- 전체 pytest: **2,259 passed / 0 skipped / 0 failures / 0 errors**, 75.35초, exit 0. [JUnit](pytest.xml).
- #358에서 활성화한 T1·T2를 포함한다. 테스트 대역과 로컬 DB를 통한 흐름 검증이며, 실제 AWS 조치나 새 모델 가격 평가를 뜻하지 않는다.
- 전체 테스트 실행 시 #347 Python 62개는 [앞선 검증본](../20260916-publication-cleanup/verification.json)과 해시가 같았다. 이후 `test_savings_numeric.py` 말미의 빈 줄 하나만 정리했고 AST 동일성을 확인했다. 다른 61개 파일은 바이트 동일하며 전체 테스트를 반복하지 않았다.
- 앞선 무료 서비스 검증은 6/6 경로·새 앱 조회 24회·조회 모델 호출 0회다. 이번에는 별도로 반복하지 않았다.
- 변경 파일 Ruff도 같은 소스에 대한 앞선 결과(dev 대비 추가 0개, 기존 185개)를 유지한다. 전체 lint 통과를 뜻하지 않는다.

## 범위 확인

- `cases.py`와 Golden AI 테스트는 현재 dev와 동일하다.
- 확인 시점 열린 PR 3개와 #347 변경 경로의 직접 겹침은 0개다.
- summary 이관·실험 기록·절감 예상 기능의 3개 커밋으로 게시한다. 이관 커밋만 분리한 사본의 관련 테스트 94개도 통과했다.
- 이전 실험 원자료·검증 수치는 당시 기록으로 보존한다. 과거 `20260916-numeric-service-1/pytest.xml` 3행의 출력 공백도 유지했다. 해당 원자료 한 곳을 제외한 diff 공백 검사는 통과했다.

명령 결과·원천 해시 연결·비교한 PR head는 [verification.json](verification.json)에 있다. GitHub CI는 PR 게시 후 확인한다.

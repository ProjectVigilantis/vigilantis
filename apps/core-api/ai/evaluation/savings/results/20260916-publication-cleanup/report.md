# 게시 전 정리·무료 재검증

2026-09-16, `feat/AI-347-action-savings` 미커밋 작업본. #348을 포함한 dev
`a29bcce6e4a249194aec703d8fd4763095573a7d`에서 아래 Python 검증을 실행했다. 이후 #360의 FE 의존성 파일 두 개만 바뀐 dev `a4d0e684d73cb73ba2a28cabd603e1c2063ec1d6`를 fast-forward로 반영했다. Python·DB·테스트 실행 코드는 동일하다. 추가 유료 모델 호출은 **0회**다.

## 정리한 범위

- dev 대비 추가 Ruff 진단 61개를 정리했다. 사용하지 않는 `noqa`를 지우고, 독립 CLI의 경로 등록 뒤 import와 과거 import 호환, 기존 Enum 표현을 유지하는 예외에는 이유를 붙였다.
- Python 11개 중 AST 변화는 `Counter` 입력을 같은 내용의 `dict.fromkeys`로 만드는 1곳이다. 프롬프트·요청·산식·공개 계약은 유지한다.
- Docker 시작 로그는 바이트 그대로 로컬 보존하고 게시 대상에서 제외했다. 과거 보고서의 해당 링크만 보존 위치 설명으로 바꿨다.
- 로컬 PR 본문의 관련 이슈는 #347·#324로 정리하고, 변경 파일·실험 근거·계약 리뷰 포인트를 유지했다.

## 검증

| 확인 | 결과 | 증거 범위 |
| --- | --- | --- |
| 전체 pytest | **2,257 passed / 2 skipped**, 139.35초 | CI 7개 경로, 실제 로컬 PostgreSQL·LocalStack. [JUnit](pytest.xml) |
| LocalStack 재확인 | **1 passed**, 환경 skip 0 | [단독 JUnit](pytest-localstack.xml), 이후 전체 재실행도 통과 |
| 무료 서비스 | **6/6 경로**, SDK 대역 18회·새 앱 조회 24회·조회 모델 호출 0 | [free-service.json](free-service.json). 검증용 LocalStack 자원 정리 확인 |
| Ruff | dev 대비 추가 **0개**, 기존 **185개** | 변경 Python 62개. 정리한 11개에는 진단 0개. [동일 규칙 비교](lint-vs-dev.json) |
| 요청·자료 보존 | 요청/입력 지문 유지, 과거 B 채점 60/60 동일, 동결 #324 파일 409개 동일 | [preservation.json](preservation.json) |
| CLI | `finops_eval --estimate`, `finops_judge --help`, `savings_eval --help` 모두 exit 0 | 모델 요청 없는 진입점 확인 |
| 열린 PR 파일 | 확인 시점 열린 PR과 직접 겹침 0 | [PR head·파일 목록](pr-overlap.json). 결합 동작 검증을 뜻하지 않음 |

첫 전체 실행은 2,256 passed / 3 skipped였다. 기존 T1·T2 외에 LocalStack health 요청의
`OSError`를 테스트가 “미기동”으로 처리해 1건을 건너뛰었다. 후속 health 요청 3회는 200,
같은 코드의 단독 실행 및 전체 재실행은 통과했다. 최초 예외의 정확한 원인은 미확인이다.
최초 JUnit·로그는 로컬에 보존하고 수치·skip 사유·해시는 [verification.json](verification.json)에 기록했다.
테스트의 skip 조건이나 제한 시간은 바꾸지 않았다.

최종 skip 2건은 dev의 기존 T1·T2 미작성 본문이며 환경 skip은 0이다. 무료 서비스는
SDK 응답 대역과 실제 로컬 DB·LocalStack 경계를 확인한다. 새 모델 가격 정확도 실측으로 합산하지 않는다.

Ruff 규칙은 `E4,E7,E9,F,I,UP,B,C4,RUF100,TRY004`, Python 3.11 기준이다.
기존 185개가 남아 Ruff 전체 명령의 종료 코드는 1이며 전체 lint 통과로 표시하지 않는다.

이전 savings 기록 211개 중 209개는 바이트 동일, 시작 로그 1개는 로컬로 옮겨 원본 보존,
보고서 1개는 로그 링크를 게시 제외 설명으로 바꿨다. 당시 수치와 모델 원자료는 유지했다.
`cases.py`와 Golden 테스트는 dev와 동일하다.

이번에는 Docker 이미지를 다시 빌드하지 않았다. 기존 [빌드·기동](../20260916-cases-deferred-validation/docker-verification.json)과
[migration 왕복](../20260916-cases-deferred-validation/migration-chain.json) 증거는 당시 실행 기록으로 유지한다.
현재 테스트와 무료 서비스는 새 DB migration을 실행했다. GitHub CI, 실제 AWS 실행·청구 절감액,
수집부터의 전 구간 E2E는 이번 로컬 검증 범위에 포함하지 않는다.

# cases 이관 보류·#344 통합 무료 검증

2026-09-16, `feat/AI-347-action-savings`의 미커밋 작업본. dev `ae428a618923581924697652eef7e086dd7af6c0`
(#344·#355 병합 포함)을 fast-forward로 반영한 뒤 검증했다. 이후 #354의 문서·주석 변경을
포함한 dev `a322387eb7464fb9408edf7506bf7e8a781314dc`까지 통합했다. #354에서 바뀐 Python 2개는
AST가 동일하며 #347 변경 Python 62개도 해시가 같아 아래 실행 증거를 유지한다.
추가 유료 호출은 **0회**다.

## 이번 조정

- `ai/evaluation/cases.py`와 `tests/test_golden_ai_dataset.py`를 dev와 동일하게 복원했다.
- `summary/cases.py`는 기존 `EvalCase`·`finops_cases`를 다시 공개한다. 입력 변환 구현의 실제 이동은
  **#353 병합 후 #324 재개 때** 진행한다. 나머지 FinOps summary·공통 평가 이관은 #347에 유지한다.
- #347 migration `c7a9e1d83f24`의 부모를 병합된 #344의 `95956d08c917`로 연결했다.
- 모델 프롬프트·출력 스키마·호출 순서·산식은 바꾸지 않았다.

## 검증 결과

| 확인 | 결과 | 범위 |
| --- | --- | --- |
| `uv sync --locked --all-packages` | 성공 | 잠금 파일과 의존성 일치 |
| [전체 pytest](pytest.xml) | **2,238 passed / 2 skipped**, 90.25초 | CI와 같은 7개 테스트 경로, 실제 로컬 PostgreSQL·LocalStack 사용 |
| [Migration](migration-chain.json) | 단일 head `c7a9e1d83f24`, 새 DB upgrade·downgrade·재upgrade 통과 | #344 인덱스를 유지하며 추정 컬럼만 제거·복원. 임시 DB 정리 확인 |
| [무료 서비스](free-service.json) | **6/6 경로·새 조회 24회**, 조회 모델 재호출 0 | SDK 응답 대역 → LocalStack 가드레일 → PostgreSQL 저장 → 새 앱·세션 조회 |
| [Compose](docker-verification.json) | 빌드·새 DB migration·API 기동, HTTP 200 | health·인시던트 목록·OpenAPI의 추정 필드. 모델 키·스캔·디스패처 없이 확인 |
| 요청·입력 경계 | 동일 | summary 승인 v2 요청·스냅샷, B 단가 요청, 판정자, 고정 6사례 입력 지문 |
| 보존 | 기존 결과 **203개**, 동결 #324 파일 **409개** 불변 | #324 HEAD·Git 상태도 동일. 역사 기록을 현재 코드에 맞춰 고치지 않음 |
| [열린 PR 파일 대조](pr-overlap.json) | #348·#351·#353·#357과 직접 변경 경로 겹침 **0개** | 확인 시점의 PR head 기준. 의미상 통합 접점은 아래에 별도 기재 |

전체 테스트의 skip 2건은 기존 `tests/test_e2e_scenario.py`의 미작성 T1·T2 본문이다.
DB·LocalStack 부재 skip, 테스트 실패·수집 오류는 0건이다. summary 입력 회귀와 Golden 소비 경로,
실제 SDK 직렬화 요청 지문 검사는 이번 전체 실행에 포함된다.

무료 서비스의 18회 전송은 모두 SDK MockTransport다. 합성 응답의 가격 PASS는 새로운 모델 정확도
실측이 아니다. LocalStack 인스턴스 6개는 종료됐고, 임시 DB와 이번 Compose 프로젝트·볼륨도 정리했다.
기존 개발용 DB·LocalStack 컨테이너는 유지했다.

### 정적 검사 범위

[Ruff 기록](lint-vs-dev.json)에 명령·규칙·파일별 진단을 남겼다. 이번에는
`E4,E7,E9,F,I,UP,B,C4,RUF100,TRY004`를 명시해 변경 Python 62개를 검사했다.
현재 진단은 246개, 같은 조건의 이번 수정 전 백업은 266개이며 **이번 범위 조정으로 늘어난 진단은 0개**다.
dev 대비로는 61개가 추가된 상태이므로 전체 lint 통과로 표기하지 않는다.
대부분 CLI 경로 설정 뒤 import, 호환용 `import *`, Enum·스타일 제안이며, 선택하지 않은 규칙의
`noqa`를 지적하는 진단도 포함한다. 이전 보고서는 정확한 규칙 선택을 기록하지 않았으므로
그 보고서의 216개·추가 0개와 이번 수치를 직접 비교하지 않는다. CI에 백엔드 Ruff job은 아직 없다.

## #353과의 연결

#347은 #353 병합을 기다리지 않고 올릴 수 있다. 기존 `cases.py`를 그대로 두었으므로 #353의 ARN
공통화는 원래 파일에 적용한다. `summary/cases.py`는 그 구현을 함께 사용한다.

새로 추가한 `savings/service_validation.py`의 `mapped_graph()`에는 검증용 ARN 조립 1곳이 남는다.
#353의 `tests/test_arn_assembly_single_source.py`가 적용되면 이 경로도 `schemas.arns.build_arn`을
사용해야 한다. **둘 중 나중에 병합되는 PR에서 이 1곳을 연결하고 #353 정적 검사를 실행한다.**
이 변경을 위해 #353의 미병합 구현을 #347에 복제하지 않았다. #353과의 결합 검증은 아직 수행하지 않았다.

FE #351의 추정값 표시와 PM의 SSOT 계약 갱신은 합의 후 후속 작업이다. 실제 AWS 실행·청구액,
수집/접수부터의 전 구간 E2E, GitHub CI는 이번 로컬 검증으로 보장하지 않는다.
기존 유료 승인 예산은 **80/80회**, 이번 추가 유료 호출은 **0회**다.

## 재현

저장소 루트에서 로컬 PostgreSQL·LocalStack을 켜고 실행한다. 서비스 결과 디렉터리는 새 경로여야 한다.

```powershell
uv sync --locked --all-packages
$env:AWS_ENDPOINT_URL='http://localhost:4566'
$env:AWS_ACCESS_KEY_ID='test'
$env:AWS_SECRET_ACCESS_KEY='test'
$env:AWS_DEFAULT_REGION='ap-northeast-2'
uv run --no-sync python -m pytest tests apps/core-api/services/tests apps/core-api/ai/tests packages/schemas/tests apps/core-api/db/tests apps/core-api/tests apps/core-api/security/tests -q -rs
uv run --no-sync python apps/core-api/ai/evaluation/savings/verify_service.py --out <새-결과-디렉터리>
```

종합 증거와 소스 지문은 [verification.json](verification.json)에 있다. 이전
[v1 준비 보고서](../20260916-v1-preparation/report.md)와 당시 원자료는 그대로 보존했다.

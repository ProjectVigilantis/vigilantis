# #347 가드레일 이후 절감 예상 — 구현과 무료 검증

2026-09-16. `feat/AI-347-action-savings`, HEAD `3e482a4aed9bdae3308d1eabc58676963e1265c2` 위의 미커밋 변경.
유료 모델 호출 0회. 이 기록은 호출 이동·요청 동일성·로컬 저장 검증이며 단가 품질 승인이나 온라인 모델 검증이 아니다.

## 구현 결과

현재 순서:

`FinOps 요약·후보 2호출 → 출력 계약 검사 → 모든 후보의 실행 가드레일 → PASS RIGHTSIZING만 단가 1호출 → 서버 계산 → Incident 잠금·후보 저장 → commit·이벤트`

- `ai/agent.py`: 단가 노드·조건부 간선을 제거했다. FinOps 문구·후보 출력은 승인 v2로 복원했다. 서버의 대상·목표 타입 검사와 SecOps 그래프는 보존했다.
- `ai/savings.py`: `estimate_candidate_savings()`가 후보와 입력 스냅샷의 대상·목표 타입을 대조한 뒤 기존 v0.9.1 요청을 보낸다. AI가 두 단가를 추정하고 서버가 `(현재 − 목표) × 730`을 Decimal HALF_UP으로 계산한다.
- `agent_dispatcher.py`: 기존 모델 클라이언트와 FinOps 입력 스냅샷을 좁은 callable에 결속한다. 그래프 단계의 미실행 추정을 INVALID로 만드는 검사는 제거했다. Claim 회수 상한은 `max(FinOps 2 + 단가 1, SecOps 3)`으로 계산한다.
- `workflows.py`: 가드레일 결과를 모두 얻은 뒤 해당 후보만 보강한다. 그래프 초안에 실린 기존 추정은 저장하지 않는다. 가드레일과 모델 호출 중 DB 트랜잭션은 없으며 저장·잠금은 호출 뒤다.
- 처리 가능한 호출 오류는 INVALID/MISSING_ESTIMATE, 응답 내용·저장 계약 위반은 INVALID와 해당 사유, 모델의 미산출은 UNAVAILABLE로 보존한다. 금액이 없으면 null이다. 후보 상태·실행 파라미터·가드레일 판정·검증 명령은 추정 실패의 영향을 받지 않는다. 예상하지 못한 구현 오류는 숨기지 않는다.
- 기존 JSONB 저장·mapper·상세 API·migration을 재사용했다. 이번 순서 변경을 위한 추가 migration은 없다.

동기 처리다. PASS 후보도 단가 응답 뒤 저장된다. 비동기 보강, 중단점 재개, 프로세스 중단 시 후보 선저장은 구현하지 않았다.

## 현재 증거

| 항목 | 결과와 한계 |
| --- | --- |
| CI와 같은 7개 pytest 수집 경로 | **2,152 passed / 0 failed / 0 errors / 2 skipped**, 52.92초. [최종 JUnit](pytest-final.xml). skip은 `test_e2e_scenario.py`의 기존 T1·T2 무조건 보류다. GitHub CI 실행 결과는 아니다. |
| 호출 순서·미호출 | 가드레일 전체 완료 → 단가 → 잠금 순서, 미호출 대상, 다른 후보로 결과가 섞이지 않음, 그래프 추정 무시를 검증했다. |
| 실패·DB·조회 | 실제 PostgreSQL에 ESTIMATED·INVALID·UNAVAILABLE 및 호출 실패 결과를 저장했다. 통과 후보·검증 명령 보존, 새 앱/새 Session의 반복 조회, 조회 후 모델 재호출 0회를 확인했다. 이 대표 저장 테스트의 가드레일 1~3은 실제, 4는 대역이다. 실제 AWS 사전 조건 성공을 뜻하지 않는다. |
| LocalStack 회귀 | 최종 전체 실행에서 변경 파일의 조건부 skip 0건. 기존 SecOps의 실제 가드레일 4단계·저장·조회 통합 테스트도 실행됐다. |
| SDK 요청 | 6개 고정 입력에서 실제 OpenAI SDK 3.0.0 + MockTransport로 그래프 2회와 독립 단가 1회 요청을 대조했다. 외부 API 호출 0회. |
| 변경 파일 lint | Ruff 기본 검사: HEAD의 동일 파일 198건, 현재 196건, 코드·메시지·파일별 비교에서 신규 경고 0건. 기존 스타일·타입 표기 경고는 남아 있다. 변경 Python 파일의 `--isolated --select E9,F` 검사는 통과했다. |
| diff·기록 보존 | `git diff --check` 통과. 작업 시작 때 기록한 과거 결과·실행기 140파일의 SHA-256이 모두 동일하다. |
| CLI 예산 안내 | `finops_eval.py --estimate --repeats 10`은 60실행·최대 120호출을 출력했다. 단가만 같은 규모는 60호출, 전체 분석은 통과 RIGHTSIZING당 최대 3호출이다. `savings_eval.py`는 모델을 부르지 않는 채점기다. |

초기 실행에는 Windows sandbox의 pytest 임시 폴더 접근 오류가 있었다. 호스트의 고유 임시 경로로 실행해 해소했다. 첫 전체 실행은 2,151 passed / 3 skipped였고 추가 1건은 LocalStack health 조회의 OSError 분기였다. 제품 코드 수정 없이 최종 전체 실행에서 재현되지 않았다. 최초 오류 종류는 기록되지 않아 원인을 단정하지 않는다. [최종 health 진단](localstack-health-probe.json)은 연결 오류 0건이다.

## 승인 기준선과 단가 요청

[요청 스냅샷](../../request_snapshot.json)은 코드 수정 전에 다음 두 원천에서 수집했다.

1. 승인 v2: 현재 HEAD의 `ai/agent.py`를 별도 모듈로 로드하고, 기존 승인 snapshot의 지문과 일치함을 먼저 확인했다.
2. 단가 v0.9.1: 이동 전 서비스 그래프의 실제 세 번째 SDK 요청을 수집했다.

고정 A1·A7·A11·A12·A14·A16 각각에 대해 SDK 요청 전체의 순서 보존 JSON 지문을 기록했다. 이동 후 요청이 모두 일치했다. 범위는 시스템 문구·입력·capabilities·중간 합성 요약·출력 스키마의 title/description/필드 순서·모델 설정이다. 검증 설정은 `gpt-5.6-luna`, `reasoning_effort=low`, temperature 미지정이다. 제품 기본 설정 파일은 바꾸지 않았다.

- 승인 v2 지문: `1e2e5c45cd1b1d250ebf371ba65699827bf3c42f88f6d77080515a07c22e1cc7`
- 단가 v0.9.1 지문: `f47b8c94781c712e81a7af1995944f1e86e6972623c3aa04c870f8a03ae23bb6`

`finops_prompt_fingerprint()`는 승인 v2의 기존 정렬 직렬화를 유지한다. 새 `finops_request_fingerprint()`는 스키마 생성 순서를 따로 감시하고 CLI 메타에 남긴다. 단가 지문은 독립이다. 단가 문구·스키마 순서를 바꿔도 요약 지문이 움직이지 않는 것과, 요약 스키마 순서 변경은 새 요청 지문이 감지하는 것을 테스트했다. 승인 snapshot을 현재 출력에 맞춰 덮어쓰지 않았다.

## 남은 일

- **단가 품질**: v0.9.1은 최근 비교 29/30, 이동 전 서비스 편입 58/60으로 아직 미승인이다. 호출 이동으로 이 결과를 새 합격으로 바꾸지 않는다. 품질 개선 후보를 정한 뒤 새 유료 라운드의 대상·최대 호출 수·비용·시간을 제시해야 한다.
- **온라인 대표 흐름**: 실제 모델 생성 → 가드레일 이후 실제 단가 생성 → DB 저장 → 새 앱 조회의 연속 검증은 미실행이다. 이번 결과는 모델 대역·SDK MockTransport와 실제 로컬 저장을 구분한다.
- **#324 통합**: 별도 작업 트리의 공통 계측·summary 이관은 여전히 미커밋이다. 여기서는 flat 경로를 유지했다. `agent.py`, `agent_dispatcher.py`, `test_agent_dispatcher.py`, `finops_eval.py` 접점을 통합할 때 #324를 재사용한다.
- **#344 migration**: 조회 시 OPEN·미병합. #347은 `c7a9e1d83f24 → b5e2d7a4c19f`, #344는 별도 index migration을 추가하므로 게시 전 순서·head를 다시 대조해야 한다.
- **SSOT·FE 접점**: AI 단가·서버 계산 및 공개 필드는 PM 갱신 절차에 연결할 항목이다. FE 화면은 이번 범위 밖이다.
- 커밋·푸시·PR 생성·Issue 수정은 수행하지 않았다. `v1.0.0`, #347 전체 완료, PR 준비 완료는 아직 판정하지 않는다.

조회한 원격은 `origin/dev=4c9c90fcc25b4f7f0df9c3f30b972068c1f98b1a`, `origin/main=62a4b4ecaa8a9323ace0f4ca4bb13703e9c813b9`다. 작업 HEAD 이후 dev의 추가 6커밋은 FE 변경이며 이번 작업 트리에 병합하지 않았다.

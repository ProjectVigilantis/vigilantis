# B 서비스 편입·대표 온라인 검증

사용자는 B의 60/60 결과 뒤, 서버 설명 계약 정리와 서비스 경로 검증에 “진행해”라고 지시했다.
원래의 유료 호출 **80회 승인** 중 A 2회·B 60회로 62회를 사용했다.
이번 라운드는 **서비스 구성 1개 × 6사례 × 최대 3호출 = 최대 18회**, 누적 최대 80회다.

## 변경과 검증

- 서비스 `ai/rate_estimator.py`는 B의 상태·두 단가 출력, 프롬프트와 숫자 제약을 유지한다.
- 서버가 비교 문맥·월 절감액·설명을 구성하고 `basis.explanation_source=SERVER_TEMPLATE`를 저장한다.
- 출처 필드가 없는 과거 설명은 `MODEL_GENERATED`로 읽는다. 비교 가정의 단가 출처 `MODEL_KNOWLEDGE`와 구분한다.
- FinOps 요약·추천은 승인 v2 요청 그대로이며 단가 호출은 전체 후보 가드레일 후 PASS RIGHTSIZING에만 적용한다.
- 가격 충분성은 앞선 B 60회가 근거다. 이번 6회는 온라인 생성·서비스 저장·재조회 경로의 대표 검증이다.

무료 단계는 좁은 회귀 → 전체 CI 대상 pytest → 설치된 SDK MockTransport로 전체 6사례,
실제 PostgreSQL 일회용 DB의 migration·commit·조회와 LocalStack Describe/DryRun을 수행한다.
서비스 입력은 실제 Dispatcher가 저장된 ASSET·RULE·METRIC에서 재구성하고 고정 입력과 대조한다.

## 데이터·격리

공개 저장소 Golden의 더미 사례 A1·A7·A11·A12·A14·A16을 쓴다.
기존 더미 인스턴스 ID는 LocalStack에 없어 전체 가드레일에서 정상 거절되므로,
같은 현재 사양의 임시 LocalStack 인스턴스를 사례마다 생성한다. 대상 ARN·인스턴스 ID·계정 ID만 일대일 대응한다.
Incident·Evidence ID도 DB 계약에 맞는 UUID로 대응한다. 가격 조건과 단가 참조는 바꾸지 않는다.
요청의 ARN을 원래 Golden 식별자로 정규화하면 B 실측의 단가 SDK 요청 지문과 정확히 같아야 한다.

OpenAI 목적지는 `https://api.openai.com/v1/chat/completions`다.
요약·추천에는 공개 Golden의 더미 자산·CPU 근거·태그·메뉴가, 단가에는 리전·사양·비교 가정이 전달된다.
참조 단가·실제 운영 자산·실제 AWS 자격증명은 전송하지 않는다.
인증은 기존 승인된 기본 체크아웃 `.env`의 OpenAI 키를 인증 헤더에만 사용한다.
키·원문 응답·내부 추론은 보존하지 않고 수용된 요약·후보·추정값·API 응답·호출 usage와 지문을 남긴다.

AWS 클라이언트는 `http://localhost:4566`과 더미 자격증명에 고정한다.
DB는 localhost의 고유 일회용 DB이며 Alembic으로 구성한다.
이 실행에서 생성한 인스턴스만 종료하고 일회용 DB만 정리한다. 실제 AWS 조치 실행은 포함하지 않는다.

## 유료 실행·중단

모델은 `gpt-5.6-luna / low`, temperature 미지정이다. SDK 재시도 0, 래퍼 시도 1, timeout 30초다.
18호출 비용은 **약 0.01–0.03 USD**, 시간은 **약 1–3분**으로 예상한다. 비용·시간은 보장 상한이 아니다.
비용 계산은 2026-09-16 확인한 [공식 Luna 가격](https://developers.openai.com/api/docs/models/gpt-5.6-luna)의
100만 토큰당 입력 0.20 / 캐시 입력 0.02 / 출력 1.20 USD를 사용한다. 실제 청구액·미보고 캐시 쓰기는 별도다.

- 실제 요청 전에 STARTED를 남기고 총 18회 한도를 검사한다. 호출 재개·표본 교체·자동 재실행은 없다.
- 각 사례는 요약 → 추천 → 실제 4단계 가드레일 → 단가 → 저장 순서여야 한다.
- 모델/AWS 호출 중 DB 트랜잭션이 열려 있으면 중단한다.
- 호출 오류·미제안·거절·미산출·계약 오류·가격 이탈·저장/조회 불일치가 처음 발생하면 중단한다.
- 성공한 사례는 새로운 앱 2개·새 DB 세션에서 총 4회 조회하고, 대기 스캔을 다시 돌려도 추가 모델 호출이 없어야 한다.
- 전체 6사례가 성공해야 서비스 대표 검증 통과다. DB/API 성공으로 실제 AWS 권한·다운사이징 실행·실제 절감을 보장하지 않는다.

## 실행 명령

worktree 루트에서 `.venv/Scripts/python.exe -X utf8 apps/core-api/ai/evaluation/savings/service_validation.py`는 무료 검증이다.
`--execute-approved`는 무료 검증·소스·과거 장부·승인 기록을 대조한 뒤 온라인 실행한다.
온라인 STARTED 뒤에는 같은 라운드의 무료 재고정과 유료 재실행을 모두 거절한다.

팀 SSOT 갱신·FE 표시·#324 공통 계측 이관과 현재 dev 통합, 커밋·게시 판정은 별도 추적한다.

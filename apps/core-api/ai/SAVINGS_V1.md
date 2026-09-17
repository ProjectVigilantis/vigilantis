# 조치별 절감 예상 V1.0.0

V1은 AI가 현재·목표 시간당 단가만 추정하고, 서버가 비교 문맥·월 절감액·설명을 구성한다.
서비스 구현은 [rate_estimator.py](rate_estimator.py), 저장·조회 계약은
[schemas/savings.py](../../../packages/schemas/savings.py)다.
SSOT의 AI 금액·근거 동시 생성 계약에서 책임을 변경하는 제안이며, 팀 채택은 PR 리뷰에서 합의한다.

## 서비스 계약

요약·추천 2호출 → 모든 후보의 가드레일 → PASS RIGHTSIZING 단가 1호출 → 서버 계산 →
후보·추정 결과 함께 저장 → commit 이후 `INCIDENT_UPDATED` 순서다.
FE는 알림의 `incident_id`로 `GET /api/v1/incidents/{id}`를 조회한다.
`recommendations[].ai_savings_estimate`는 저장값이며, 조회는 모델을 호출하지 않는다.
추정 완료 전에는 추천 저장·분석 완료·갱신 알림도 기다린다. 모델·AWS 호출 중 DB 트랜잭션은 열지 않는다.

| 값 | 생성 주체·의미 |
| --- | --- |
| `basis.current_hourly_rate`, `target_hourly_rate` | AI 추정 단가, USD 숫자 문자열. 저장·응답은 소수 6자리 |
| `basis.target_arn`, `region`, `current_instance_type`, `target_instance_type` | 서버 분석 스냅샷·목표 타입 규칙 |
| `basis.assumptions` | 월 730시간·Linux·공유형·온디맨드·인스턴스 컴퓨팅만 비교하는 가정 |
| `amount` | 서버가 `(현재 단가 − 목표 단가) × 730`을 Decimal HALF_UP으로 센트 반올림, 소수 2자리 문자열 |
| `basis.explanation`, `explanation_source` | 서버가 비교 조건·계산 방법·한계를 설명하며 출처는 `SERVER_TEMPLATE` |
| `basis.assumptions.pricing_source` | `MODEL_KNOWLEDGE`: 실시간 가격 조회가 아닌 모델 지식 기반 추정 |

스토리지·네트워크·세금·할인·크레딧은 제외한다. 비교 가정은 고객의 실제 청구 조건을 확인한 사실이 아니다.
서버 설명에는 모델이 그 단가를 고른 이유가 담기지 않는다. 금액·설명은 실행 파라미터에 넣지 않는다.

| 상태 | 금액·근거 | 사유 |
| --- | --- | --- |
| `ESTIMATED` | `amount`와 `basis` 있음 | null |
| `UNAVAILABLE` | 모두 null | `MODEL_UNAVAILABLE` |
| `INVALID` | 모두 null | `MISSING_ESTIMATE`·`INVALID_ESTIMATE`·`CONTEXT_MISMATCH` |
| 필드 전체 null | 기존 후보 또는 추정 비대상 | 해당 없음 |

정상 금액 `"0.00"`과 미산출은 다르다. 실패해도 추천 후보와 가드레일 PASS는 유지한다.
예전 저장 JSONB에 설명 출처가 없으면 `MODEL_GENERATED`로 읽는다.

재시도는 [공통 모델 클라이언트](openai_client.py)의 정책을 따른다. 기본값은 시도당 timeout 30초,
최초 포함 최대 3회이며 타임아웃·연결 오류·HTTP 408/409/429/5xx에만 재시도한다.
일반 대기는 약 0.75~1초 → 1.5~2초, 유효한 `Retry-After`가 60초 이내이면 우선 적용한다.
인증·요청 오류, 거절, SDK 파싱 실패와 수용 검증 실패에는 재시도하지 않는다.
SDK 파싱 실패를 포함해 사용 가능한 응답을 얻지 못하면 `MISSING_ESTIMATE`, 단가 누락·범위·산식 등
수용 실패면 `INVALID_ESTIMATE`, 후보와 서버 비교 문맥이 다르면 `CONTEXT_MISMATCH`다.
최종 실패 저장 후 별도 재추정을 예약하지 않는다. 일반 코드 예외·DB 오류를 추정 실패로 숨기지 않는다.
재시도는 단가 요청만 반복하며 전체 알림 지연이 30초 이내라는 보장은 없다.

## 선정 실험 요약

2026-09-15~16에 `gpt-5.6-luna`, reasoning `low`, temperature 미지정으로 비교했다.
서울 EC2 사양 변경 2종(`t3.xlarge → t3.medium`, `t3.large → t3.small`),
고정 6사례 A1·A7·A11·A12·A14·A16을 사용했다.
아래 가격 PASS는 실험 당시 참조 가격에 대한 판정이며 현재 AWS 가격이나 실제 청구 절감액을 뜻하지 않는다.

참조는 2026-09-15에 확보한 [AWS 서울 EC2 가격표](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/20260910195514/ap-northeast-2/index.csv)다.
Linux·Shared·OnDemand 컴퓨팅 단가 기준으로 사용한 값은 다음과 같다.

| 변경 | 현재 / 목표 단가(USD/시간) | 참조 월 절감액(730시간) |
| --- | --- | --- |
| t3.xlarge → t3.medium | 0.208000 / 0.052000 | $113.88 |
| t3.large → t3.small | 0.104000 / 0.026000 | $56.94 |

| 후보·실험 | 관측 결과 | 판단 |
| --- | --- | --- |
| v0.3~v0.8: 추천·가격 분리, 출력 순서, 단가 쌍·가격표 | 가격 이탈·리전 불일치 | 탐색 |
| 단가 쌍 확대 | 52/60 PASS, 과대 1·숫자 파싱 거부 7 | 미채택 |
| v0.9.0: AI 금액 제거·서버 계산 | 60/60 가격 PASS, 단가 진단 이탈 22·금액 하한 적용 16 | 서비스 후보 |
| v0.9.1: 서비스 편입·설명 지시 조정 | 58/60 PASS, 과대 2 | 미채택 |
| v0.9.0·v0.9.1 동시 비교 | 각각 29/30, 과대 +20.00%·과소 −27.18% 잔존 | 출력 축소 실험으로 진행 |
| A: 식별자 재출력 제거, 모델 설명 유지 | 2번째 호출에서 +10.77% 과대 | 사전 조건에 따라 중단 |
| B: `status`·두 단가만 출력, 서버 설명 | **60/60 가격 PASS**, 미산출·계약 오류 0 | V1 요청으로 선택 |
| B 실제 모델 서비스 검증 | **6/6 경로**, 모델 18호출, 새 앱 GET 24/24 일치, 조회 재호출 0 | 모델 → LocalStack → PostgreSQL → 조회 확인 |

후기 가격 기준은 과대 +10%·과소 −20%, 최소 절대 허용폭 $1, 센트 HALF_UP 경계다.
산출률 95% 이상, 허용 밖 추정·계약 오류 0건을 요구했다. 개별 단가 ±10%는 별도 진단이다.
기존 자료에 이 비대칭 기준을 다시 적용한 재채점은 새 모델 실험이 아니다.

B의 60건에도 **단가 진단 이탈 13건·금액 하한 적용 5건**이 있었다.
서비스 6건은 각각 2건·1건이며 새 60회 가격 평가로 합산하지 않는다.
A가 조기 중단되었으므로 설명 제거의 인과 효과는 확정할 수 없다.
고정 6사례의 결과를 다른 리전·사양의 정확성이나 미래 응답의 무실패로 확대하지 않는다.
마지막 승인 80회는 A 2 + B 60 + 서비스 18로 소진했다. V1 정리에서 추가 유료 호출은 없다.

## 유지·재검증 기준

실험 B(`candidate-B-v1`), 서비스 편입판 `v0.10.0`, V1.0.0의 단가 요청은 같다.
요청 기준은 [SDK 경계 테스트](tests/test_savings_request_boundary.py)에 작은 고정 지문으로 남겼다.
상세 원자료·비교 후보·실험 실행 도구는 저자의 별도 보관 자료이며 CI 실행에 필요하지 않다.

- 단가 요청 지문: `b52d6a31b0b7d0a632c8b61a58c6ad7de559ee2d8c6bbc0bb163f7db30e5bbc2`
- summary 승인 v2 지문: `1e2e5c45cd1b1d250ebf371ba65699827bf3c42f88f6d77080515a07c22e1cc7`
- 경로·구조 정리는 기존 SDK 요청 지문과 단가 수용·저장·조회 회귀 테스트로 무료 검증한다.
- 프롬프트·출력 스키마·모델·입력 조건을 바꾸면 별도로 승인받아 가격 품질을 다시 측정한다.
- 요약·추천 요청을 바꾸면 [summary 재통과 절차](evaluation/summary/baseline.md)를 적용한다.

자동 테스트의 합성 응답은 계산·계약·호출·저장 흐름을 검증한다. 실제 모델의 가격 품질은 위 실험의 관측 범위다.
FE 표시와 SSOT 갱신은 계약 합의 후 담당 후속 작업으로 연결한다.

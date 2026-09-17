# #347 절감 예상 저장·조회 계약

2026-09-16 최초 PR 후보 v1.0.0. B의 검증된 단가 요청과 서버 설명·저장·조회 경로를 사용한다.
팀 SSOT의 2026-09-07 문구는 아직 AI가 금액·근거를 함께 생성하는 설명이므로,
PR에서 합의할 계약 변경은 아래의 **AI 단가 추정 / 서버 금액 계산 / 서버 설명 작성 / 가드레일 후 별도 호출**이다.
SSOT의 갱신 완료나 FE 화면 반영을 뜻하지 않는다.

## API와 값의 출처

`GET /api/v1/incidents/{id}`의 각 `recommendations[].ai_savings_estimate`에 저장본을 반환한다.

| 필드 | 소유자·의미 |
| --- | --- |
| `status` | 서버의 수용 판정. `ESTIMATED`, `UNAVAILABLE`, `INVALID` |
| `amount` | AI 추정 두 단가의 차이 × 730을 서버가 Decimal HALF_UP으로 계산한 USD/월 예상값. 소수 2자리 문자열 |
| `basis.current_hourly_rate`, `basis.target_hourly_rate` | 모델 지식 기반 추정 단가. 소수 6자리 문자열이며 실시간 가격 조회가 아님 |
| `basis.target_arn`, `region`, `current_instance_type`, `target_instance_type` | 서버의 분석 시점 자산 스냅샷과 목표 타입 규칙. 모델 출력에서 받지 않음 |
| `basis.assumptions` | 월 730시간·Linux·공유형·온디맨드·인스턴스 컴퓨팅만이라는 비교 가정. 실제 자산의 OS·청구 조건을 확인했다는 뜻이 아님 |
| `basis.assumptions.pricing_source` | 항상 `MODEL_KNOWLEDGE`. 설명 작성 주체와 별개 |
| `basis.explanation` | 비교 조건·계산식·추정 한계를 사람이 읽도록 서술한 저장 문자열 |
| `basis.explanation_source` | 신규 B 결과는 `SERVER_TEMPLATE`. 기존 출처 필드 없는 모델 작성 결과는 `MODEL_GENERATED`로 읽음 |

서버 설명은 모델의 내부 추론·단가 선택 이유·정확한 조건 이해를 입증하지 않는다.
가격이 부정확해도 산술과 출력 계약을 만족할 수 있으며, 품질 평가는 별도다.
실행 `parameters`·`display_parameters`·검증된 실행 명령에는 이 추정값을 넣지 않는다.

## 상태·호환성

- `ESTIMATED`: `amount`와 `basis`가 있고 `reason=null`. 0은 두 유효 단가가 실제로 같을 때만 허용한다.
- `UNAVAILABLE`: 모델이 단가를 산출하지 못함. `amount=null`, `basis=null`, `reason=MODEL_UNAVAILABLE`.
- `INVALID`: 수용 실패. 금액·근거는 null이고 `MISSING_ESTIMATE`, `INVALID_ESTIMATE`, `CONTEXT_MISMATCH`로 구분한다.
  공급자 호출 오류는 `MISSING_ESTIMATE`로 저장하고 오류 종류는 원문 없이 로그에 남긴다.
- 기존 후보와 비대상 후보의 `ai_savings_estimate=null`은 유지한다.
- 과거 JSONB에 설명 출처가 없으면 읽기 시 `MODEL_GENERATED` 기본값을 적용한다. 과거 설명을 서버 작성으로 재분류하지 않는다.
  기존 행을 수정하는 데이터 migration은 필요 없다. 신규 결과는 출처를 JSONB에 명시해 저장한다.

## 실행 순서와 조회

Dispatcher의 요약·추천 2호출 → Workflow의 모든 후보 가드레일 → PASS RIGHTSIZING만 단가 1호출
→ 서버 수용·계산·설명 구성 → 후보·가드레일·추정값 commit 순서다.
모델·AWS 호출 중 DB 트랜잭션은 닫는다. 추정 실패에도 실행 가능한 후보와 PASS 결과를 보존한다.
거절·무제안·비대상·SecOps에서는 단가를 호출하지 않는다.

조회는 저장본을 사용하며 새 앱·세션에서도 모델을 호출하지 않는다. 프로세스 중간 재개·후보 선저장은 이 계약에 없다.

## 응답 예시

아래는 계약 설명용 합성 단가다. AWS 실제 가격의 증거가 아니다.

```json
{
  "status": "ESTIMATED",
  "currency": "USD",
  "period": "MONTH",
  "amount": "56.94",
  "basis": {
    "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0",
    "region": "ap-northeast-2",
    "current_instance_type": "t3.large",
    "target_instance_type": "t3.small",
    "current_hourly_rate": "0.104000",
    "target_hourly_rate": "0.026000",
    "assumptions": {
      "hours": 730,
      "operating_system": "LINUX",
      "tenancy": "SHARED",
      "purchase_option": "ON_DEMAND",
      "included_cost": "INSTANCE_COMPUTE_ONLY",
      "pricing_source": "MODEL_KNOWLEDGE"
    },
    "explanation": "모델 지식 기반 단가 차이에 월 730시간을 곱한 예상값입니다. 실제 요금 조회 결과가 아닙니다.",
    "explanation_source": "SERVER_TEMPLATE"
  },
  "reason": null
}
```

전체 응답·미산출·재시작 후 조회의 검증 기록은 [서비스 검증](results/20260916-numeric-service-1/report.md)에 둔다.
기존 FE의 표시 구현은 이 변경에 포함하지 않는다. 새 필드를 소비할 때 AI 추정 단가와 서버 작성 설명을 구분해야 한다.

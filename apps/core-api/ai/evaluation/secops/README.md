# SecOps AI 응답 평가

Incident에 저장된 위협·자산·로그 근거를 사용해 조치 제안과 요약의 품질을 측정한다. 서비스와 평가 도구는 같은 입력 조립 함수를 사용한다. 현재 공개 평가 기준선은 **v2.0.7**(10사례 세트)이다. 근거 선행 1회 호출 그래프의 v2.0.7 프롬프트를 2026-11-12 22차(C01~C10 × 10회)로 측정했고, 열 사례 모두 비율 기준(사례마다 10회 중 8회 이상 PASS)을 충족해 재고정했다. 최종 99/100 PASS이며 전원 PASS가 아니다(남은 FAIL 1건 C04/6 `risk_reason`). 재고정은 측정 판정이며, 팀 승인은 이 기준선을 담은 PR의 리뷰로 따로 정한다. 같은 10사례 세트에 직전 승인판 v2.0.5를 적용한 21차(89/100, C10 0/10) FAIL, 9사례 세트의 승인판이던 입력 보강 v2.0.5(20차 83/90), C01~C07 7사례 세트에서만 유효했던 v2.0.2·v1.0.0은 `prompt_snapshot.json`의 `history`에 남는다. 합격 기준과 재검증 방법은 [품질 기준](baseline.md), 1–22차 결과와 한계는 [실험 요약](results/README.md), 프롬프트 보완 이유는 [품질 개선 근거](defects.md)에 있다.

## 서비스 경로와 평가 범위

모의 관측 → 정규화·초기 위험 판정 → Incident의 THREAT context 보존 → Dispatcher 입력 조립 → 근거 선행 1회 호출(근거 3줄 + decision) → 코드 실행 계획 조립 → 최종 출력 검증 순서다. 정상 그래프의 모델 호출은 건당 1회이고, 대비책(`CANDIDATE_FIRST`, `--variant`)은 후보 생성 → 요약의 건당 2회다. 위험 재평가 노드는 ADR-0005 1차 개정(#583)으로 없어졌고 출력 `reviewed_risk_level`은 null이다 — 위험 등급은 서버 초기 판정 하나다.

합성 SSH 10사례를 평가한다. C01~C07, C01 입력에 합성 선행 EC2 격리 기록 SUCCESS·FAILED를 더한 C08·C09, 그리고 C01의 관측과 서버 초기 등급(전체 200행·실패 120회/300초·HIGH·발췌 12행)을 그대로 두고 출발지만 127.0.0.1로 바꾼 C10이다. 서버 초기 위험도를 그대로 두고, 관측 사실·위험 이유·조치 범위가 서로 일치하는지 확인한다. C01~C09는 후보와 무제안을 모두 허용하고 C10은 무제안만 허용한다 — 메뉴에 NACL 조치가 있어도 서브넷 경계의 NACL은 루프백 통신에 적용되지 않는다. 어느 쪽이든 판단 이유는 관측 근거로 설명해야 한다. 제안의 존재가 승인·실행·차단 효과나 위협 해소를 뜻하지 않는다.

SSH 입력에는 서버 초기 판정과 별도로 `current_rule_risk_basis`가 붙을 수 있다. 저장된 관측을 현재 위험 규칙으로 해석한 분기(단발, 횟수와 빈도 모두 충족, 횟수만 충족, 빈도만 충족, 둘 다 미충족)이며 접수 당시의 판정 근거를 보존한 값이 아니다. 현재 규칙의 등급·사유 코드가 저장된 초기 판정과 같을 때만 붙고, 초기 등급·사유·대응 모드와 타이머는 바꾸지 않는다. 값은 내부 enum이며 모델에는 한국어 라벨만 보낸다. 선행 실행 기록이 이 EC2를 영향 대상으로 한 EC2 격리 SUCCESS이면 NACL 조치의 목적 문구가 같은 서브넷의 다른 호스트 보호로 바뀐다. 기록된 실행 상태를 옮긴 것이며 현재 격리 상태나 차단 효과를 확인하지 않는다.

## 재현 자료

| 자료 | 역할 |
| --- | --- |
| [inputs.json](inputs.json) | C01–C10의 동결 그래프 입력(현재 규칙 위험 근거 포함, C08·C09는 합성 선행 격리 기록 포함)과 Golden·로그 원천 지문 |
| [sources/C10/](sources/C10/) | C01 위협·로그에서 출발지만 127.0.0.1로 바꿔 동결한 C10 평가 전용 파생 원천 |
| [service-fixtures.json](service-fixtures.json) | 동결된 자산 적재·모의 위협 접수 자료 |
| [inventory.json](inventory.json) | 분석 당시의 대상 자산·연결 자원 문맥 |
| [answers.json](answers.json) | 위험 등급과 후보·무제안의 허용 결과 |
| [rubric.json](rubric.json) | 의미 기준과 내부 식별자 노출 검사 |
| [prompt_snapshot.json](prompt_snapshot.json) | 승인판(후보가 있으면 후보 포함) 프롬프트 지문과 측정한 입력·요청 지문, 공개 보완·측정 근거 |

CI의 입력 로딩과 DB 비교는 동결 fixture로 수행한다. 현재 Golden·corpus의 내용이나 해시는 CI 수집 조건이 아니다. 계측 CLI는 실행 준비와 보고 시점에 원본 드리프트를 별도로 검사한다. 서비스와 동결 입력의 불일치가 있으면 원인을 확인한 뒤 갱신 여부를 결정한다.

## 실행

저장소 루트에서 실행한다. 기본 입력 확인과 호출 상한 추정은 모델·DB·AWS를 호출하지 않는다.

```powershell
uv run --no-sync python scripts/secops_eval.py --check-inputs
uv run --no-sync python scripts/secops_eval.py --estimate --repeats 3
```

실측 원자료와 의미 검토 파일은 Git에서 제외된 프로젝트 기록 폴더에 저장한다. 아래 `$recordDir`를 해당 폴더의 절대 경로로 바꾼다. 공개 소스·fixture 폴더를 결과 출력 위치로 사용하지 않는다. 파일은 매 실행 새 이름을 사용하며 도구는 기존 결과를 덮어쓰지 않는다.

`--run`은 유료 호출이다. 실행 전에 입력·프롬프트·답지·rubric과 호출 수·예상 비용·시간을 확정하고 해당 라운드의 실행 승인을 받는다. 인증은 `OPENAI_API_KEY` 환경변수 또는 인증 키만 읽는 `--key-env-file`로 제공한다. 모델·추론 강도를 생략하면 서비스 코드 기본값을 사용한다.

```powershell
$recordDir = "<Git에서 제외된 프로젝트 기록 폴더의 절대 경로>"
uv run --no-sync python scripts/secops_eval.py --run --repeats 3 --output "$recordDir/round-run.json"
uv run --no-sync python scripts/secops_eval.py --review-template "$recordDir/round-run.json" --output "$recordDir/round-review.json"
```

검토 파일에 실제 출력 인용·판정 이유·검토자와 사람/assistant 구분을 작성한 뒤 집계한다. 검토 양식 생성과 집계에는 유료 호출이 없다.

```powershell
uv run --no-sync python scripts/secops_eval.py --report "$recordDir/round-run.json" --review "$recordDir/round-review.json" --output "$recordDir/round-report.json"
```

`--report`의 종료 코드는 `quality_pass`(실행 전체가 전원 PASS)이면 0, 아니면 2다. 기준선 판정의 비율 기준은 보고서의 `case_acceptance`로 따로 읽는다. 현재 고정 세트(C01–C10) 전체 × 정확히 10회일 때(`case_acceptance.measurement.planned_fixed_set_x10`) 사례마다 PASS 8회 이상인지 내며, 오류·기록 누락·검토 누락도 PASS가 아닌 회차로 분모에 남는다. 그래서 `case_acceptance.threshold_met`이 true여도 FAIL이 한 건이라도 있으면 종료 코드는 2다(22차가 이 경우다: 비율 기준 충족, FAIL 1건). `threshold_met`은 산술 판정이고, 관측 완전성은 `case_acceptance.measurement.fully_observed`, 기준선 후보 여부는 `case_acceptance.baseline_candidate`로 본다. 기존 최상위 `baseline_candidate`는 전원 PASS 지표로 그대로다. 집계 성공, 요청 호환성, 모델 품질, 기능 회귀는 각각의 결과로 읽는다. 변경 종류별 재검증 범위는 [품질 기준](baseline.md#프롬프트입력rubric이-바뀌면-무엇을-다시-돌리는가)을 따른다.

## 기록의 범위

저장소에는 반복 검증에 필요한 코드·fixture·현재 규격과 공개 실험 요약을 둔다. 상세 라운드 기록·허용된 구조화 출력·검토 원자료는 프로젝트 기록으로 보존하고, 실행 로그·JUnit·개인 작업 과정은 게시하지 않는다. 공개 규격과 테스트는 비공개 보관 자료 없이 사용할 수 있어야 한다.

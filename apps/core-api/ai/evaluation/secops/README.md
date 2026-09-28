# SecOps AI 응답 평가

Incident에 저장된 위협·자산·로그 근거를 사용해 위험 재평가, 조치 제안과 요약의 품질을 측정한다. 서비스와 평가 도구는 같은 입력 조립 함수를 사용한다. 현재 공개 평가 기준선은 **v1.0.0**이며, v0.5.6의 9차 품질 통과와 최종 기능 회귀를 마친 내용을 그대로 확정했다. 합격 기준과 재검증 방법은 [품질 기준](baseline.md), 1–9차 결과와 한계는 [실험 요약](results/README.md), 프롬프트 보완 이유는 [품질 개선 근거](defects.md)에 있다.

## 서비스 경로와 평가 범위

모의 관측 → 정규화·초기 위험 판정 → Incident의 THREAT context 보존 → Dispatcher 입력 조립 → 위험 재평가 → 후보 생성·계약 검증 → 요약 → 최종 출력 검증 순서다. 정상 그래프의 모델 호출은 건당 3회다.

합성 SSH 7사례를 평가한다. 초기 위험도와 AI의 재평가를 구분하고, 관측 사실·위험 이유·조치 범위가 서로 일치하는지 확인한다. 후보와 무제안은 모두 허용하지만 각각의 판단 이유는 관측 근거로 설명해야 한다. 제안의 존재가 승인·실행·차단 효과나 위협 해소를 뜻하지 않는다.

## 재현 자료

| 자료 | 역할 |
| --- | --- |
| [inputs.json](inputs.json) | C01–C07의 동결 그래프 입력과 Golden·로그 원천 지문 |
| [service-fixtures.json](service-fixtures.json) | 동결된 자산 적재·모의 위협 접수 자료 |
| [inventory.json](inventory.json) | 분석 당시의 대상 자산·연결 자원 문맥 |
| [answers.json](answers.json) | 위험 등급과 후보·무제안의 허용 결과 |
| [rubric.json](rubric.json) | 의미 기준과 내부 식별자 노출 검사 |
| [prompt_snapshot.json](prompt_snapshot.json) | 승인판·후보 프롬프트 지문과 공개 보완·측정 근거 |

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

`--report`는 품질 충족이면 종료 코드 0, 미충족이면 2를 반환한다. 집계 성공, 요청 호환성, 모델 품질, 기능 회귀는 각각의 결과로 읽는다. 변경 종류별 재검증 범위는 [품질 기준](baseline.md#프롬프트입력rubric이-바뀌면-무엇을-다시-돌리는가)을 따른다.

## 기록의 범위

저장소에는 반복 검증에 필요한 코드·fixture·현재 규격과 공개 실험 요약을 둔다. 상세 라운드 기록·허용된 구조화 출력·검토 원자료는 프로젝트 기록으로 보존하고, 실행 로그·JUnit·개인 작업 과정은 게시하지 않는다. 공개 규격과 테스트는 비공개 보관 자료 없이 사용할 수 있어야 한다.

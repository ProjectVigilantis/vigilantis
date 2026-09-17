# FinOps summary 실험

평가 기준과 실행 순서는 [baseline.md](baseline.md), 결함 정의는 [summary_defects.md](summary_defects.md), 승인 판·결과·지문은 [summary_prompt_snapshot.json](summary_prompt_snapshot.json)에 있다.

기존 명령은 저장소 루트의 scripts/finops_eval.py와 scripts/finops_judge.py를 사용한다. 구현은 cli.py와 judge_cli.py, 채점은 이 디렉터리에 둔다. 공통 계측은 ../common/을 재사용한다.

입력 변환 구현은 `../cases.py`에 유지한다. 이 디렉터리의 `cases.py`는 `EvalCase`와
`finops_cases`를 다시 공개하므로 #353의 ARN 공통화는 기존 파일에 적용할 수 있다.
실제 구현 이동은 #353 병합 후 #324 재개 때 진행한다.

새 원자료·판정 결과는 results/에 라운드별로 보존한다. 현재 저장소에 별도 summary 원자료가 없는 과거 판은 snapshot과 기준선의 기록 범위까지만 확인할 수 있다.

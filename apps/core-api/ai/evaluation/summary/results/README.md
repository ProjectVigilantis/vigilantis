# Summary 결과

새 생성 원자료와 판정 결과의 저장 위치다. 예:

    uv run --no-sync python scripts/finops_eval.py --repeats 10 --json apps/core-api/ai/evaluation/summary/results/<round>.raw.json

실제 실행은 과금되므로 라운드별 승인이 필요하다. 승인 기록과 이전 판 지표는 ../summary_prompt_snapshot.json에 보존돼 있다. 존재하지 않는 과거 원자료를 재구성해 넣지 않는다.

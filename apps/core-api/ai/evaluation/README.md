# AI 평가 자료

AI 평가 도구·기준선·실험 기록을 기능별로 관리하는 위치다.
`docs/AI_SUMMARY_BASELINE.md`에 있던 FinOps summary 기준선 본문을 프로젝트 공통 문서에서
분리하는 것에서 시작해, 앞으로 진행할 실험의 평가 코드·판정 기준·결과를 함께 찾을 수 있도록
`ai/evaluation/` 아래에 모은다. 운영 앱은 이 패키지를 import하지 않는다.

| 영역 | 책임 | 안내 |
| --- | --- | --- |
| `common/` | 모델 호출의 사용량·실패 단계, 구조화 값 재현성, 입력 지문·경로 | 도메인별 판정 기준은 포함하지 않음 |
| `summary/` | FinOps 요약·추천 평가, 승인 v2 스냅샷과 재통과 절차 | [기준선](summary/baseline.md) |

첫 이관 대상은 기존 FinOps summary 자료다. 기준선 본문은 `summary/baseline.md`로 옮기고,
`docs/AI_SUMMARY_BASELINE.md`에는 기존 링크를 위한 이동 안내만 남긴다.
새 summary 실험의 생성 원자료와 판정 결과는 [summary/results/](summary/results/README.md)에
라운드별로 보존한다. 다른 평가에서도 재사용할 계측은 `common/`으로 분리한다.

summary와 공통 계측은 #324에서 준비한 이관을 #347에 편입했다.
단, `cases.py` 구현과 Golden 테스트의 기존 import는 유지한다. `summary/cases.py`는
기존 구현을 다시 공개하는 경로이며, 실제 구현 이동은 #324 재개 때 진행한다.
#324의 SecOps 입력·프롬프트·호출 순서·평가 결과는 이 변경에 포함하지 않는다.
summary의 승인 판·기존 지표·스냅샷과 프롬프트·판정자·입력 지문을 유지한다.

저장소 루트에서 다음 명령은 모델을 호출하지 않는다.

```powershell
uv run --no-sync python scripts/finops_eval.py --estimate
uv run --no-sync python scripts/finops_judge.py --help
```

`scripts/finops_eval.py`와 `scripts/finops_judge.py`는 CLI 진입점이며 구현은 `summary/`에 있다.
새 코드는 도메인 경로를 직접 import한다. 과거 실험 스크립트의 재채점 경로를 위해
`ai.evaluation`, `ai.evaluation.cases`, `ai.evaluation.judge`와 `finops_eval`의 기존 helper import만 유지한다.
`ai.evaluation.cases`는 기존 구현을 유지하고, 나머지 옛 경로는 이동한 구현을 연결한다.
공통 입력 지문 계산은 `common/fingerprints.py`를 사용하며, 보류한 `cases.py`의 기존 함수도
호환을 위해 유지한다. 두 경로는 같은 고정 입력 지문을 내야 한다.

유료 실행은 라운드별 승인 대상이다. 완료된 라운드의 원자료·승인·요청 지문·실패 기록은
이동 후 코드에 맞춰 다시 쓰지 않는다. 당시 소스 해시 검사는 당시 보존 소스를 기준으로 읽고,
현재 코드의 보장은 새 라운드의 검증 기록으로 확인한다.

절감 예상 V1의 계약·선정 실험 요약은 [SAVINGS_V1.md](../SAVINGS_V1.md)에 있다.

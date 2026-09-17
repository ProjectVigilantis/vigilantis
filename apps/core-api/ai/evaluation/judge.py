"""과거 실험 스크립트의 import 호환 경로. 구현은 common/summary에 있다."""

# 과거 모듈이 노출한 이름을 유지하는 호환 계층이며 새 구현은 summary에 둔다.
from ai.evaluation.summary.judge import *  # noqa: F403

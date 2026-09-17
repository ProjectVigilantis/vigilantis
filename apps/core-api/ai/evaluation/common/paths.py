"""오프라인 평가 명령이 공유하는 저장소 경로."""
from pathlib import Path

EVALUATION_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EVALUATION_ROOT.parents[3]

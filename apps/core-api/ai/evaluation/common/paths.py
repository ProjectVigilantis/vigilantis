"""Repository paths shared by offline evaluation commands."""
from pathlib import Path

EVALUATION_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EVALUATION_ROOT.parents[3]

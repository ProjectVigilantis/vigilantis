"""SDK 대역 + 로컬 PostgreSQL·LocalStack의 무료 서비스 검증. 실제 모델 호출은 없다."""

# 독립 CLI 실행을 위해 아래 저장소 경로 등록 뒤 내부 패키지를 import한다.
# ruff: noqa: E402

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[5]
sys.path[:0] = [str(ROOT / path) for path in ("apps/core-api", "packages")]

import httpx
from openai import OpenAI

from ai.evaluation.savings.numeric_validation import make_client, read
from ai.evaluation.savings.service_validation import (
    ROUND,
    MeasuredClient,
    run_cases,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="아직 존재하지 않는 결과 디렉터리")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    audit = MeasuredClient(False, expected=read(ROUND / "preflight.json"))
    audit.path = args.out / "free-service.json"
    started = perf_counter()
    logging.disable(logging.CRITICAL)
    try:
        with OpenAI(
            api_key="offline-only", base_url="https://unit.test/v1", max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(audit.mock_response),
                event_hooks={"request": [audit.verify_wire]},
            ),
        ) as sdk:
            audit.client = make_client(sdk)
            run_cases(audit)
    except Exception as exc:
        audit.record.update(status="STOPPED", error_type=type(exc).__name__)
        raise
    finally:
        audit.record.update(
            ended_at=datetime.now(UTC).isoformat(),
            elapsed_seconds=round(perf_counter() - started, 3), live_model_calls=0,
        )
        audit.persist()
    assert len(audit.record["cases"]) == 6
    print(json.dumps({"status": audit.record["status"], "service_paths": 6,
                      "fresh_api_reads": 24, "live_model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

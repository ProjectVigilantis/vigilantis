# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# Golden Dataset(FinOps) 자산 적재 스크립트 — `datasets/golden/finops/input/`의
# `AssetInventory`를 DB에 넣고 rule_engine 판정까지 돌려 `GET /api/v1/assets`가
# 골든 데이터를 그대로 서빙하게 만든다.
#
# 실행 (repo 루트, PostgreSQL 기동 + Alembic head 적용 후):
#   PowerShell: uv run python scripts/load_golden_assets.py
#   bash      : uv run python scripts/load_golden_assets.py
#   정답 대조까지: ... load_golden_assets.py --verify
#
# 왜 이 스크립트가 필요한가:
#   골든은 `pytest` 입력으로만 쓰였고 화면까지 나가는 경로가 없어, FE가 자산 화면을
#   `apps/web/src/app/api/v1/_mock/data.ts`로 따로 채워 왔다. 경로가 없어서가 아니라
#   **경로가 알려져 있지 않아서**다 — 아래 세 함수는 전부 이미 있던 프로덕션 함수다.
#
#       AssetInventory ─→ collector.persist_inventory ─→ rule_engine.run_rule_engine
#                                                              ↓
#                                                    GET /api/v1/assets
#
#   FE는 `NEXT_PUBLIC_API_BASE_URL`을 이 백엔드로 걸면 mock 없이 실 API를 본다
#   (`apps/web/src/lib/api/client.ts`).
#
# 안전 가드: 기본적으로 **로컬 DB에만** 적재한다. `DATABASE_URL`의 호스트가
#   `localhost`·`127.0.0.1`·`db`(compose 서비스명)가 아니면 즉시 종료하며, `--force`
#   로만 넘길 수 있다. `_is_local`의 집합에는 `::1`도 있지만 호스트 파싱이 `:`로 먼저
#   자르므로 IPv6 대괄호 표기(`[::1]`)는 실제로는 걸러진다 — **틀릴 때 닫히는 쪽으로
#   틀리는** 가드다(자격증명 없는 원격 URL도 같은 이유로 거부된다).
#
# 멱등성: 자산은 ARN 기준 upsert라 다시 돌려도 자산 수가 늘지 않는다. 다만 실행마다
#   CollectionRun과 판정이 **누적**된다 — 화면은 리전별 최신 run과 자산별 최신 판정만
#   보므로 응답은 같다.
#
# 이 스크립트의 run 모양을 수집 계약으로 읽지 말 것 (PR #263 리뷰 반영):
#   `persist_inventory`가 **인벤토리 1건마다 CollectionRun 1건**을 연다. 골든 입력이
#   전부 같은 리전(ap-northeast-2)이라 **한 리전에 파일 수만큼 run**이 생긴다 —
#   프로덕션 수집은 `collector.collect_region`이 **리전당 1회 1건**을 연다. 응답이
#   같아 보이는 것은 조회단이 리전별 **최신 run**만 접기 때문이고(`routers/assets.py`의
#   `latest_collection_run_per_region`, #231 / #259), run 테이블의 모양은 다르다.
#   여기서 본 run 개수를 수집 계약으로 오해하면 안 된다.
#
# 적재 건수·판정 분포는 여기 적지 않는다:
#   골든이 한 건만 늘어도 낡고, 이 헤더의 어떤 설명도 그 숫자에 기대지 않는다. 실제
#   값은 `--verify` 출력이 원천이다(같은 표가 PR #266 → #275 원복 → #280 재착륙으로
#   세 번 낡았다). 남기는 것은 분포가 아니라 **커버리지**다.
#
#   골든이 채우는 분기 — `verdict` 4종 전부 · `skip_reason_code` 6종 중 5종
#   (`SKIP_UNSUPPORTED_STATE`만 비어 있다 — EBS 전이·비정상·미상, #276의 정답지
#   편입 대기). 이 문장은 `apps/core-api/tests/test_golden_assets_api.py`가 CI에서
#   강제하므로 손으로 맞출 필요가 없다. mock과 달리 값의 출처가
#   `tests/test_golden_dataset.py`가 지키는 정답지다.
#
# 담지 못하는 것: 골든 FinOps 입력에는 NACL·Launch Template·ASG·ALB Target Group이
#   없다(자산 유형 EC2·SG·EBS 3종 / 계약 7종). 토폴로지 뷰가 요구하는 나머지 자산
#   4종은 여전히 mock이 필요하다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Windows 콘솔(cp949)은 em dash 등 출력 시 UnicodeEncodeError로 죽는다 — UTF-8로 강제
sys.stdout.reconfigure(encoding="utf-8")

# import 경로: services(core-api) + schemas(packages) — 통합 테스트와 동일한 부트스트랩
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT / "apps" / "core-api"), str(_REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

GOLDEN_INPUT_DIR = _REPO_ROOT / "datasets" / "golden" / "finops" / "input"
GOLDEN_EXPECTED_DIR = _REPO_ROOT / "datasets" / "golden" / "finops" / "expected"


# ------------------------------------------------------------------ 골든 읽기
def load_inventories() -> list[Any]:
    """골든 FinOps 입력 전량을 `AssetInventory`로 파싱한다. DB가 필요 없다.

    `$schema`는 편집기 자동완성용 키라 계약 필드가 아니다 — 넘기기 전에 뺀다.
    (`AssetInventory`는 `extra=forbid`가 아니라 조용히 무시되지만, 무시에
    기대지 않고 명시적으로 뺀다.)
    """
    from schemas.assets import AssetInventory

    inventories = []
    for path in sorted(GOLDEN_INPUT_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("$schema", None)
        inventories.append(AssetInventory.model_validate(raw))
    if not inventories:
        raise FileNotFoundError(f"골든 입력이 없다: {GOLDEN_INPUT_DIR}")
    return inventories


def load_expected() -> dict[str, dict]:
    """골든 FinOps 정답을 `{asset_arn: 판정}`으로 편다.

    파일 경계는 대조에 쓰이지 않는다 — `GET /assets`가 전 리전·전 파일을 한 목록으로
    돌려주므로, 대조도 ARN 하나를 열쇠로 삼는 편이 응답 모양과 맞는다.
    """
    by_arn: dict[str, dict] = {}
    for path in sorted(GOLDEN_EXPECTED_DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for evaluation in doc["evaluations"]:
            by_arn[evaluation["asset_arn"]] = evaluation
    return by_arn


# ------------------------------------------------------------------ DB 적재
def load_into_db(db, inventories: Iterable[Any]) -> dict:
    """골든 인벤토리를 적재하고 판정까지 돌린다. commit은 이 함수가 한다.

    리전 단위로 CollectionRun을 열고 SUCCESS로 닫는다 — 닫지 않으면 라우터가
    `COLLECTING`을 돌려줘 화면이 "수집 중"에서 멈춘다(`routers/assets.py`
    `_COLLECTION_STATUS`).
    """
    from db.repositories import assets as assets_repo
    from schemas.collections import CollectionRunStatus
    from services.collector import persist_inventory
    from services.rule_engine import run_rule_engine

    persisted = []
    for inventory in inventories:
        stats = persist_inventory(inventory, db)
        db.commit()
        assets_repo.finish_collection_run(
            db,
            stats["collection_run_id"],
            CollectionRunStatus.SUCCESS,
            finished_at=datetime.now(timezone.utc),
        )
        db.commit()
        persisted.append(stats)

    evaluated = run_rule_engine(db)
    db.commit()
    return {
        "assets": sum(stats["total"] for stats in persisted),
        "runs": len(persisted),
        "counts": evaluated.get("counts", {}),
    }


# ------------------------------------------------------------------ 실행부
def _is_local(database_url: str) -> bool:
    host = database_url.rsplit("@", 1)[-1].split("/")[0].split(":")[0]
    return host in {"localhost", "127.0.0.1", "::1", "db"}


def _verify(db) -> int:
    """적재 결과를 골든 정답과 대조한다. 어긋난 건수를 돌려준다."""
    from db.repositories import assets as assets_repo

    expected = load_expected()
    actual = assets_repo.latest_rule_evaluation_by_asset(db)
    assets = {asset.asset_id: asset.arn for asset in assets_repo.list_assets(db)}
    by_arn = {assets[asset_id]: row for asset_id, row in actual.items() if asset_id in assets}

    mismatched = 0
    for arn, want in expected.items():
        got = by_arn.get(arn)
        if got is None:
            print(f"  MISSING {want['case_id']} {arn}")
            mismatched += 1
            continue
        pair = (got.evaluation_status, got.verdict, got.skip_reason_code)
        wanted = (want["evaluation_status"], want["verdict"], want["skip_reason_code"])
        if pair != wanted:
            print(f"  DIFF {want['case_id']}: 정답{wanted} != 실제{pair}")
            mismatched += 1
    print(f"  대조 {len(expected)}건 중 어긋남 {mismatched}건")
    return mismatched


def main() -> int:
    parser = argparse.ArgumentParser(description="골든 FinOps 자산을 DB에 적재한다")
    parser.add_argument("--verify", action="store_true", help="적재 후 골든 정답과 대조")
    parser.add_argument(
        "--force", action="store_true", help="원격 DATABASE_URL에도 적재(기본은 로컬만)"
    )
    args = parser.parse_args()

    from config import get_settings
    from db.session import get_session_factory

    database_url = get_settings().DATABASE_URL
    target = database_url.rsplit("@", 1)[-1]
    if not _is_local(database_url) and not args.force:
        print(f"중단: DATABASE_URL이 로컬이 아니다({target}). 의도한 것이면 --force")
        return 2

    inventories = load_inventories()
    print(f"골든 입력 {len(inventories)}개 파싱 완료 → 적재 대상 {target}")

    with get_session_factory()() as db:
        result = load_into_db(db, inventories)
        print(f"자산 {result['assets']}건 적재 · CollectionRun {result['runs']}건")
        print(f"판정 분포: {dict(Counter(result['counts']))}")
        if args.verify:
            print("골든 정답 대조:")
            if _verify(db) > 0:
                return 1

    print("완료. FE는 NEXT_PUBLIC_API_BASE_URL을 이 백엔드로 걸면 실 API를 본다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
#   골든이 채우는 분기 — `verdict` 4종 전부 · `skip_reason_code` 6종 전부
#   (마지막 값이던 `SKIP_UNSUPPORTED_STATE`를 EBS 전이·비정상·미상 케이스 E4~E8이
#   채웠다, #276). 이 문장은 `apps/core-api/tests/test_golden_assets_api.py`가 CI에서
#   강제하므로 손으로 맞출 필요가 없다. mock과 달리 값의 출처가
#   `tests/test_golden_dataset.py`가 지키는 정답지다.
#
# 담지 못하는 것: 골든 FinOps 입력에는 NACL·Launch Template·ASG·ALB Target Group이
#   없다(자산 유형 EC2·SG·EBS 3종 / 계약 7종). 토폴로지 뷰가 요구하는 나머지 자산
#   4종은 여전히 mock이 필요하다.
#
# --------------------------------------------------------------------------
# `--bind-a1-to-seed` — 게이트 전용 옵션 (#301, 2026-09-10 PM 확정)
# --------------------------------------------------------------------------
# 골든 A1의 **식별자만**(`arn`·`instance_id`) LocalStack에 실재하는 시드 인스턴스
# (`vigilantis-seed-idle`)의 것으로 바꿔 적재한다. 이름·타입·메트릭·태그는 골든 그대로라
# 화면도 판정도 골든이다 — 바뀌는 것은 "이 자산이 AWS에서 누구인가" 하나다.
#
# 왜 필요한가: 9/11 게이트 T1은 자산 화면과 인시던트를 **골든 A1로 고정**하면서
#   (`docs/E2E_GATE_0911.md` §1-2) 6번에서 **실제 인스턴스 타입 변경**을 요구한다(§3-1).
#   그런데 골든 A1은 LocalStack에 없다 — 실행 1단계 `stop_instances`가
#   `InvalidInstanceID.NotFound`로 즉사한다(`services/aws/executor.py`의
#   `execute_rightsizing` ①).
#
#   **가드레일이 이것을 막아 주지 못한다**(2026-09-10 실측). ③ ARN Match는 DB 조회라
#   통과하고(`ai/guardrails.py`의 `run_arn_match`), ④ AWS Dry-Run은 LocalStack이 DryRun
#   플래그를 **대상 존재 검사보다 먼저** 처리해 `DryRunOperation`으로 통과한다. 그래서
#   4단계 전부 통과 → 승인 버튼이 열리고 → **승인 직후 실행에서 처음 깨진다.**
#   실 AWS는 DryRun에서 걸러 줄 것으로 보이나 미측정이다 — 로컬에서 ④는 "대상이
#   실재하는가"를 보증하지 않는다. 이 격차는 `docs/adr/0006` §4 **8행**으로 이월했고,
#   실 AWS 스모크에서 존재하지 않는 ID로 1회 재는 것이 해소 조건이다.
#
# 대상이 `vigilantis-seed-idle`인 이유: 타입이 **t3.xlarge로 골든 A1과 같다.** 판정서 R6의
#   `t3.xlarge → t3.small`이 문구 그대로 성립한다. 시드 쪽 `Environment=production` 태그는
#   적재되지 않으므로(태그도 골든 것을 쓴다) `SKIP_PROD_PROTECTED`로 흡수되지 않는다.
#
# ⚠️ 적재 뒤 스캔이 돌면 같은 ARN이 **시드 spec으로 덮여** 판정이 뒤집힌다. 대본이
#   `SCAN_ENABLED=false`로 앱을 띄우는 이유가 이것이다(대본 §2-⑦).
# ⚠️ 시드 ARN은 LocalStack 재기동마다 바뀐다. 그래서 ARN을 이 파일에 적지 않고 **매 실행
#   시 이름 태그로 조회**한다 — 대본에 ARN을 박아 두면 다음 기동에서 낡는다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Windows 콘솔(cp949)은 em dash 등 출력 시 UnicodeEncodeError로 죽는다 — UTF-8로 강제
sys.stdout.reconfigure(encoding="utf-8")

# import 경로: services(core-api) + schemas(packages) — 통합 테스트와 동일한 부트스트랩
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT / "apps" / "core-api"), str(_REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

GOLDEN_INPUT_DIR = _REPO_ROOT / "datasets" / "golden" / "finops" / "input"
GOLDEN_EXPECTED_DIR = _REPO_ROOT / "datasets" / "golden" / "finops" / "expected"

# 게이트 T1의 조치 대상 — 골든 케이스 A1과, 그 자리에 세울 LocalStack 시드 인스턴스.
# 시드 이름은 `scripts/seed_localstack.py`의 INSTANCES 첫 행이며, 그 행이 골든 A1과 같은
# 타입인 것이 이 짝의 전제다(타입 일치는 `_resolve_seed_binding`이 실행 시 다시 잰다).
GOLDEN_A1_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f00001"
SEED_INSTANCE_NAME = "vigilantis-seed-idle"


@dataclass(frozen=True)
class SeedBinding:
    """골든 A1 → 시드 실물 인스턴스 바인딩. 식별자 셋이 전부다."""

    old_arn: str
    new_arn: str
    new_instance_id: str


def _rebind_inventory(raw: dict, binding: SeedBinding) -> int:
    """인벤토리 원문에서 A1의 식별자만 바꾼다. 바꾼 레코드 수."""
    changed = 0
    for instance in raw.get("ec2_instances", []):
        if instance.get("arn") != binding.old_arn:
            continue
        instance["arn"] = binding.new_arn
        instance["instance_id"] = binding.new_instance_id
        changed += 1
    return changed


def _require_single_rebind(
    binding: Optional[SeedBinding], rebound: int, what: str, where: Path
) -> None:
    """바인딩이 정확히 1건을 바꿨는지 확인한다. 0건이면 **조용한 무효화**라 멈춘다.

    골든이 개정돼 A1의 ARN이 바뀌면 치환이 아무 것도 못 잡고, 그 결과는 "바인딩을 걸었는데
    실행이 깨지는" 게이트 당일의 사고로만 드러난다.
    """
    if binding is None or rebound == 1:
        return
    sys.exit(
        f"중단: {what}에서 A1({binding.old_arn}) 레코드를 {rebound}건 찾았다 — 1건이어야 한다.\n"
        f"  골든이 개정됐다면 이 스크립트의 GOLDEN_A1_ARN을 함께 고칠 것({where})"
    )


def _rebind_expected(doc: dict, binding: SeedBinding) -> int:
    """정답지 원문에서 A1의 열쇠(ARN)만 바꾼다. 바꾼 레코드 수.

    입력과 정답을 **같은 실행 안에서 함께** 바꾼다 — 한쪽만 바뀌면 `--verify`가 바인딩
    자체를 어긋남으로 세어 대조가 무의미해진다.
    """
    changed = 0
    for evaluation in doc.get("evaluations", []):
        if evaluation.get("asset_arn") != binding.old_arn:
            continue
        evaluation["asset_arn"] = binding.new_arn
        changed += 1
    return changed


# ------------------------------------------------------------------ 골든 읽기
def load_inventories(binding: Optional[SeedBinding] = None) -> list[Any]:
    """골든 FinOps 입력 전량을 `AssetInventory`로 파싱한다. DB가 필요 없다.

    `$schema`는 편집기 자동완성용 키라 계약 필드가 아니다 — 넘기기 전에 뺀다.
    (`AssetInventory`는 `extra=forbid`가 아니라 조용히 무시되지만, 무시에
    기대지 않고 명시적으로 뺀다.)

    바인딩은 **파싱 전 원문에서** 적용한다. 골든 파일 자체는 건드리지 않는다 — 바인딩은
    게이트 당일의 실행 조건이지 정답지의 개정이 아니다.
    """
    from schemas.assets import AssetInventory

    inventories = []
    rebound = 0
    for path in sorted(GOLDEN_INPUT_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("$schema", None)
        if binding is not None:
            rebound += _rebind_inventory(raw, binding)
        inventories.append(AssetInventory.model_validate(raw))
    if not inventories:
        raise FileNotFoundError(f"골든 입력이 없다: {GOLDEN_INPUT_DIR}")
    _require_single_rebind(binding, rebound, "골든 입력", GOLDEN_INPUT_DIR)
    return inventories


def load_expected(binding: Optional[SeedBinding] = None) -> dict[str, dict]:
    """골든 FinOps 정답을 `{asset_arn: 판정}`으로 편다.

    파일 경계는 대조에 쓰이지 않는다 — `GET /assets`가 전 리전·전 파일을 한 목록으로
    돌려주므로, 대조도 ARN 하나를 열쇠로 삼는 편이 응답 모양과 맞는다.
    """
    by_arn: dict[str, dict] = {}
    rebound = 0
    for path in sorted(GOLDEN_EXPECTED_DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if binding is not None:
            rebound += _rebind_expected(doc, binding)
        for evaluation in doc["evaluations"]:
            by_arn[evaluation["asset_arn"]] = evaluation
    _require_single_rebind(binding, rebound, "골든 정답", GOLDEN_EXPECTED_DIR)
    return by_arn


def _golden_instance(arn: str) -> Optional[dict]:
    """골든 입력에서 그 ARN의 EC2 레코드 원문. 없으면 None.

    타입·이름을 상수로 적지 않는 이유 — 골든이 바뀌면 함께 움직여야 한다.
    """
    for path in sorted(GOLDEN_INPUT_DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for instance in doc.get("ec2_instances", []):
            if instance.get("arn") == arn:
                return instance
    return None


def _stale_a1_assets(db, binding: SeedBinding) -> list[str]:
    """이번 바인딩과 다른 ARN으로 이미 적재돼 있는 A1 자산들의 ARN.

    ARN이 아니라 **이름으로** 세는 이유: 바인딩 없이 적재한 이력(골든 ARN)만이 아니라
    **이전 LocalStack 기동의 바인딩**도 잡아야 하기 때문이다. 시드 인스턴스 ID는 재기동마다
    바뀌는데 DB는 그대로 남으므로, 골든 ARN만 보면 "A1이 두 장 뜨는" 같은 상태를 그냥
    지나친다 — 그중 실물이 없는 쪽을 고르면 승인 직후 실행이 깨진다.

    같은 바인딩으로 다시 적재하는 것은 막지 않는다(그 행은 upsert로 덮인다).
    """
    from db.repositories import assets as assets_repo

    golden_name = (_golden_instance(binding.old_arn) or {}).get("name")
    return [
        asset.arn
        for asset in assets_repo.list_assets(db)
        if asset.arn != binding.new_arn
        and (asset.arn == binding.old_arn or (golden_name and asset.name == golden_name))
    ]


def _resolve_seed_binding() -> SeedBinding:
    """LocalStack에서 살아있는 시드 인스턴스를 찾아 바인딩을 만든다. (게이트 전용)

    실 AWS에는 실행하지 않는다 — `scripts/seed_localstack.py`와 같은 가드다.
    ARN 조립은 **수집기의 것을 그대로 쓴다**(`services/collector._arn`). 문자열이 한 글자만
    갈라져도 시드 스캔이 돌 때 같은 인스턴스가 자산 두 행이 된다.

    막을 때는 적재 전에 막는다 — 절반만 적재된 DB로 게이트를 시작하는 것이 가장 나쁘다.
    """
    from services.aws.client import account_id, aws_client, endpoint_url, regions
    from services.collector import _arn

    endpoint = endpoint_url()
    if not endpoint or "amazonaws.com" in endpoint:
        sys.exit(
            "중단: --bind-a1-to-seed는 LocalStack 전용이다 — AWS_ENDPOINT_URL을 확인할 것\n"
            "  예) AWS_ENDPOINT_URL=http://localhost:4566"
        )

    region = GOLDEN_A1_ARN.split(":")[3]
    if region not in regions():
        sys.exit(
            f"중단: 골든 A1의 리전({region})이 관제 대상 리전에 없다({regions()}) — "
            "AWS_REGIONS/AWS_REGION을 확인할 것"
        )

    ec2 = aws_client("ec2", region)
    found = [
        instance
        for reservation in ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [SEED_INSTANCE_NAME]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]},
            ]
        )["Reservations"]
        for instance in reservation["Instances"]
    ]
    if not found:
        sys.exit(
            f"중단: 살아있는 시드 인스턴스({SEED_INSTANCE_NAME})가 없다 — "
            "`uv run python scripts/seed_localstack.py`를 먼저 돌릴 것"
        )
    if len(found) > 1:
        ids = ", ".join(instance["InstanceId"] for instance in found)
        sys.exit(
            f"중단: 동명 시드 인스턴스가 여러 대다({ids}) — 어느 것을 조치할지 고를 수 없다.\n"
            "  `docker compose restart localstack` 후 시드를 다시 돌릴 것"
        )

    instance = found[0]
    want = (_golden_instance(GOLDEN_A1_ARN) or {}).get("instance_type")
    got = instance.get("InstanceType")
    if want is not None and got != want:
        sys.exit(
            f"중단: 시드 인스턴스 타입이 골든 A1과 다르다(시드 {got} != 골든 {want}).\n"
            "  이전 시연으로 이미 다운사이징된 상태일 수 있다. 시드 스크립트는 이름으로 기존\n"
            "  인스턴스를 재사용하므로 재실행만으로는 되돌아오지 않는다 —\n"
            "  `docker compose restart localstack` 후 `uv run python scripts/seed_localstack.py`."
        )

    return SeedBinding(
        old_arn=GOLDEN_A1_ARN,
        new_arn=_arn("instance", instance["InstanceId"], region, account_id(region)),
        new_instance_id=instance["InstanceId"],
    )


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


def _verify(db, binding: Optional[SeedBinding] = None) -> int:
    """적재 결과를 골든 정답과 대조한다. 어긋난 건수를 돌려준다."""
    from db.repositories import assets as assets_repo

    expected = load_expected(binding)
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
    parser.add_argument(
        "--bind-a1-to-seed",
        action="store_true",
        help="골든 A1의 식별자를 LocalStack 시드 실물 인스턴스의 것으로 치환해 적재(게이트 전용)",
    )
    args = parser.parse_args()

    from config import get_settings
    from db.session import get_session_factory

    database_url = get_settings().DATABASE_URL
    target = database_url.rsplit("@", 1)[-1]
    if not _is_local(database_url) and not args.force:
        print(f"중단: DATABASE_URL이 로컬이 아니다({target}). 의도한 것이면 --force")
        return 2

    binding = _resolve_seed_binding() if args.bind_a1_to_seed else None
    inventories = load_inventories(binding)
    print(f"골든 입력 {len(inventories)}개 파싱 완료 → 적재 대상 {target}")
    if binding is not None:
        print(f"골든 A1 → 시드 실물 바인딩: {binding.new_instance_id}")
        print(f"  조치 대상 ARN: {binding.new_arn}")
        print("  ↑ 인시던트 수동 생성의 대상 ARN에 이 값을 쓴다(대본 §1-2 · R3)")

    with get_session_factory()() as db:
        stale = _stale_a1_assets(db, binding) if binding is not None else []
        if stale:
            # 바인딩 없이 적재한 이력이거나, 이전 LocalStack 기동의 바인딩이 남아 있다.
            # 그대로 두면 화면에 A1이 두 장 뜨고 그중 실물이 없는 쪽을 고르면 실행이
            # 승인 직후 깨진다 — 조용히 지나가는 대신 여기서 멈춘다.
            print(f"중단: 이번 바인딩과 다른 A1 자산이 DB에 남아 있다({', '.join(stale)})")
            print("  DB를 비우고 다시 적재할 것 —")
            print("  docker compose down -v && docker compose up -d db localstack")
            # `alembic.ini`가 apps/core-api/에 있어 저장소 루트의
            # `uv run alembic upgrade head`는 `No 'script_location' key found`로
            # 죽는다. `-c`로 ini를 짚는다(대본 §2-②와 같은 명령) — 이 자리는
            # 접속 주소 블록 뒤라 셸에 DATABASE_URL이 이미 잡혀 있다.
            print("  uv run alembic -c apps/core-api/alembic.ini upgrade head")
            print("  uv run python scripts/seed_localstack.py")
            return 2
        result = load_into_db(db, inventories)
        print(f"자산 {result['assets']}건 적재 · CollectionRun {result['runs']}건")
        print(f"판정 분포: {dict(Counter(result['counts']))}")
        if args.verify:
            print("골든 정답 대조:")
            if _verify(db, binding) > 0:
                return 1

    print("완료. FE는 NEXT_PUBLIC_API_BASE_URL을 이 백엔드로 걸면 실 API를 본다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

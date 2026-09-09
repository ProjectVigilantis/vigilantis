# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 골든 FinOps 데이터셋이 `GET /api/v1/assets`까지 실제로 도는지 지키는 회귀입니다.
#
# 왜 이 파일이 필요한가:
#   골든은 `tests/test_golden_dataset.py`가 `evaluate_*`를 **직접 호출**해 검증한다.
#   그 경로는 라우터·DB·직렬화를 지나지 않으므로, 골든이 아무리 정확해도 **화면이
#   그것을 받을 수 있는지는 아무것도 말해 주지 않는다.** 실제로 그 공백 때문에 FE가
#   자산 화면을 별도 mock(`apps/web/src/app/api/v1/_mock/data.ts`)으로 채워 왔다.
#
#   이 파일이 잇는 구간은 전부 프로덕션 코드다 — 새로 만든 경로가 아니다.
#       collector.persist_inventory → rule_engine.run_rule_engine → routers/assets
#
#   적재부는 `scripts/load_golden_assets.py`가 단일 원천이고 여기서는 경로로 불러
#   쓴다. 사본을 두면 스크립트와 테스트가 갈려 "스크립트로는 되는데 CI는 모른다"가
#   된다 — `test_seed_dataset_verdicts.py`가 시드 스크립트에 쓰는 것과 같은 방식이다.
#
# 이 파일이 보지 않는 것: 판정 규칙 자체(`test_rule_engine.py`)와 골든 정답의
#   정합(`tests/test_golden_dataset.py`). 여기는 **경계를 넘는가**만 본다.
# ==============================================================================

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_script_module():
    """`scripts/load_golden_assets.py`를 경로로 로드(패키지가 아니라 스크립트다)."""
    path = REPO_ROOT / "scripts" / "load_golden_assets.py"
    spec = importlib.util.spec_from_file_location("load_golden_assets", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def golden():
    return _load_script_module()


@pytest.fixture()
def loaded(db, golden):
    """골든 전량을 테스트 세션에 적재하고 판정까지 돌린 상태."""
    return golden.load_into_db(db, golden.load_inventories())


def _items(client_pg):
    response = client_pg.get("/api/v1/assets")
    assert response.status_code == 200, response.text
    return response.json()


def test_golden_inventory_reaches_the_assets_api(client_pg, loaded, golden):
    """골든이 적재되면 자산 목록이 mock 없이 채워진다 — 화면 1단계의 전제.

    건수를 상수로 박지 않는다. 골든이 늘면 화면도 같이 늘어야 하므로, 기대값은
    골든 자신에서 파생시킨다.
    """
    body = _items(client_pg)

    assert body["collection_status"] == "READY"
    assert body["last_collected_at"] is not None
    assert len(body["items"]) == loaded["assets"]

    expected_arns = set(golden.load_expected())
    served_arns = {item["arn"] for item in body["items"]}
    assert expected_arns <= served_arns, (
        "골든 정답이 가리키는 자산이 응답에 없다 — "
        f"누락 {sorted(expected_arns - served_arns)}"
    )


def test_api_verdicts_match_the_golden_answers(client_pg, loaded, golden):
    """판정이 DB·직렬화를 지나도 골든 정답 그대로다.

    `tests/test_golden_dataset.py`는 `evaluate_*` 반환값을 본다. 그 값이 DB 컬럼과
    응답 DTO를 지나며 바뀌지 않는지는 여기서만 드러난다 — 실제로 이 경계에는
    `EvaluationStatus`·`Verdict`·`SkipReasonCode`가 각각 다른 모듈에 있다.
    """
    expected = golden.load_expected()
    by_arn = {item["arn"]: item for item in _items(client_pg)["items"]}

    mismatched = []
    for arn, want in expected.items():
        got = by_arn[arn]
        actual = (got["evaluation_status"], got["verdict"], got["skip_reason_code"])
        wanted = (want["evaluation_status"], want["verdict"], want["skip_reason_code"])
        if actual != wanted:
            mismatched.append(f"{want['case_id']}: 정답{wanted} != 응답{actual}")

    assert not mismatched, "\n".join(mismatched)


# 예외 목록(UNCOVERED_SKIP_REASONS)은 비었으므로 지웠다 — 골든이 `SkipReasonCode` 6종을
# 전부 만든다(마지막 값이던 SKIP_UNSUPPORTED_STATE를 골든 E4~E8이 채웠다, #276).
# 계약에 값이 추가되면 아래 등식이 바로 실패한다. 그때 케이스를 당장 만들 수 없으면
# 예외 목록을 되살린다 — 형태는 git 이력에 있다(이 줄 이전 판).


def test_golden_fills_every_verdict_and_skip_reason(client_pg, loaded):
    """화면의 배지·사유 분기를 골든만으로 전부 눌러 볼 수 있다.

    시연에서 mock을 못 벗는 흔한 이유가 "그 분기를 만들 데이터가 없어서"다. 골든이
    분기를 전부 덮는 한 자산 화면은 mock이 필요 없다.

    계약에 값이 추가되면 이 테스트가 먼저 실패한다 — 그때 고칠 것은 이 파일이 아니라
    `datasets/golden/finops/`다(사유 코드를 만드는 판정 케이스를 추가한다). 부분집합
    비교(`skips <= enum`)로 완화하지 않는다 — 항상 참이라 이 단언이 하는 일이 없어진다.
    """
    from schemas.rules import SkipReasonCode, Verdict

    items = _items(client_pg)["items"]
    verdicts = {item["verdict"] for item in items if item["verdict"]}
    skips = {item["skip_reason_code"] for item in items if item["skip_reason_code"]}

    known = {s.value for s in SkipReasonCode}
    assert verdicts == {v.value for v in Verdict}, (
        f"골든이 못 만드는 verdict가 있다: {sorted({v.value for v in Verdict} - verdicts)}"
    )
    assert skips == known, (
        f"골든이 못 만드는 Skip 사유가 있다: {sorted(known - skips)} / "
        f"계약에 없는 사유가 골든에 있다: {sorted(skips - known)}"
    )

    # 한 분기에 표본이 몰려 화면이 단조로워지지 않는지 — 분포도 함께 남긴다
    distribution = Counter(str(item["verdict"]) for item in items)
    assert distribution["COST_CANDIDATE"] >= 1 and distribution["SKIP"] >= 1


# ------------------------------------------------------------------------------
# 토폴로지 뷰(#146)가 그릴 수 있는가 — 노드와 엣지 (#271 ③)
# ------------------------------------------------------------------------------
#
# 위 세 테스트는 **판정**이 화면까지 가는지를 본다. 관계 그래프는 다른 축이다 —
# 자산이 전부 있어도 엣지가 없으면 토폴로지는 그릴 것이 없다.
#
# 이 가드가 없어서 실제로 하나가 조용히 비어 있었다: `ATTACHED_TO` 는 골든에
# EBS 가 들어온 뒤(#264 · #276)에도 **한 번도 파생되지 않았다.** 004 의 볼륨이
# 부착 대상으로 `i-0a1b2c3d4e5f00041` 을 적는데 그 EC2 가 골든 어디에도 없었고,
# 관계는 `persist_inventory` 가 **같은 인벤토리 안에서만** 잇기 때문이다. 아무도
# 세지 않으니 아무도 몰랐다.


def test_golden_covers_every_asset_type(client_pg, loaded):
    """골든만으로 계약의 자산 유형이 전부 뜬다 — 토폴로지가 그릴 **노드**.

    `Verdict`·`SkipReasonCode` 를 등식으로 잠근 위 테스트와 같은 이유다. 부분집합
    비교로 완화하지 않는다 — 계약에 유형이 추가되면 여기서 먼저 실패해야 하고,
    고칠 곳은 이 파일이 아니라 `datasets/golden/finops/input/` 이다.
    """
    from schemas.api.assets import AssetType

    served = {item["asset_type"] for item in _items(client_pg)["items"]}
    known = {a.value for a in AssetType}
    assert served == known, (
        f"골든이 못 만드는 자산 유형이 있다: {sorted(known - served)} / "
        f"계약에 없는 유형이 응답에 있다: {sorted(served - known)}"
    )


def test_golden_derives_every_relation_type(client_pg, loaded):
    """골든만으로 계약의 관계 6종이 전부 **파생**된다 — 토폴로지가 그릴 **엣지**.

    자산을 넣는 것과 관계가 서는 것은 다르다. 관계는 입력에 적는 값이 아니라
    `collector.persist_inventory` 가 같은 인벤토리 안의 참조에서 파생시키는 것이라
    (subnet_id→NACL · instance_id→ASG/TG · attached_instance_ids→EBS · ASG→LT),
    **한 파일에 짝이 다 들어 있어야** 선다. 파일을 갈라 담으면 조용히 0건이 된다.
    """
    from schemas.api.assets import RelationType

    derived = {
        relationship["relation_type"]
        for item in _items(client_pg)["items"]
        for relationship in item["relationships"]
    }
    known = {r.value for r in RelationType}
    assert derived == known, (
        f"골든이 못 만드는 관계가 있다: {sorted(known - derived)} — "
        "관계는 같은 인벤토리 파일 안에서만 파생된다(양쪽 자산이 한 파일에 있어야 한다) / "
        f"계약에 없는 관계가 응답에 있다: {sorted(derived - known)}"
    )


def test_golden_relations_point_at_served_assets(client_pg, loaded):
    """엣지의 반대쪽 끝이 응답 안에 있다 — 그래프가 끊기지 않는다.

    `target_arn` 이 응답에 없는 자산을 가리키면 화면은 **그릴 수 없는 엣지**를 받는다.
    관계 개수가 맞아도 이 방향은 드러나지 않으므로 따로 센다.
    """
    items = _items(client_pg)["items"]
    served = {item["arn"] for item in items}
    dangling = sorted(
        {
            f"{item['arn']} -{relationship['relation_type']}-> {relationship['target_arn']}"
            for item in items
            for relationship in item["relationships"]
            if relationship["target_arn"] not in served
        }
    )
    assert not dangling, f"응답에 없는 자산을 가리키는 관계가 있다: {dangling}"

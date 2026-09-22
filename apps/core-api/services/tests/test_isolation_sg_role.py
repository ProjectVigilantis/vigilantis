# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 격리용 SG 의 자리 태그가 수집 → 적재 → 판정까지 닿는지 지킨다. (Issue #359)
#
# 격리 SG 는 EC2_ISOLATE 전까지 미부착이 정상이라, 태그가 판정에 닿지 않으면 첫 회차부터
# SG_DELETE_ISOLATED 후보(UNUSED)로 올라온다. 구멍은 네 계층(수집·계약·적재·판정)에 하나씩
# 있었으므로 계층마다 한 번씩 본다 — 판정 함수만 보면 수집·적재가 태그를 버려도 통과한다.
#
#   1) evaluate_sg          — 순서: default → 전체개방(THREAT) → 격리 태그 → 미부착
#   2) collect_region       — AWS 응답의 Tags 가 SecurityGroupAsset.tags 로 온다(스텁 클라이언트)
#   3) persist → rule engine — sg_spec 에 태그가 적재되고 DB 판정이 SKIP_WHITELISTED 다.
#      재수집으로 태그가 바뀌면 판정이 따라가고, 태그 키가 없는 옛 행은 종전 판정이다
#   4) 단일 원천            — 태그 키 문자열이 코드에 한 곳(schemas.asset_roles)뿐이다
# ==============================================================================

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from db import models  # noqa: E402
from schemas.api.assets import AssetItem  # noqa: E402
from schemas.asset_roles import ROLE_ISOLATION, ROLE_TAG_KEY  # noqa: E402
from schemas.assets import AssetInventory, OpenPort, SecurityGroupAsset  # noqa: E402
from services import collector as C  # noqa: E402
from services.collector import persist_inventory  # noqa: E402
from services.rule_engine import SkipReason, Verdict, evaluate_sg, run_rule_engine  # noqa: E402

# db·pg_engine 픽스처는 services/tests/conftest.py 가 등록한다

ISOLATION = {ROLE_TAG_KEY: ROLE_ISOLATION}
REGION = "ap-northeast-2"
ACCOUNT = "123456789012"


# --- 1) 판정 함수 -------------------------------------------------------------

# (name, attached, open_to_world, tags) -> (verdict, skip_reason)
ROLE_CASES = [
    # 격리 태그 + 미부착 → 삭제 후보가 아니다
    ("smoke-isolation", False, False, ISOLATION, Verdict.SKIP, SkipReason.SKIP_WHITELISTED),
    # 격리 태그라도 전체개방이면 위협이다 — 태그가 빼는 것은 "미사용이니 지우자" 하나뿐
    ("smoke-isolation", False, True, ISOLATION, Verdict.THREAT, None),
    ("smoke-isolation", True, True, ISOLATION, Verdict.THREAT, None),
    # 격리 발동 뒤(부착) — 삭제 후보가 아닌 것은 같다
    ("smoke-isolation", True, False, ISOLATION, Verdict.SKIP, SkipReason.SKIP_WHITELISTED),
    # 태그 없는 미부착 SG 는 종전대로 후보다(vigilantis-smoke-unused)
    ("smoke-unused", False, False, {}, Verdict.UNUSED, None),
    ("smoke-unused", False, False, None, Verdict.UNUSED, None),
    # 정확일치만 — 다른 자리·대소문자 차이·이름만 isolation 은 추정이라 빼지 않는다
    ("smoke-other", False, False, {ROLE_TAG_KEY: "bastion"}, Verdict.UNUSED, None),
    ("smoke-upper", False, False, {ROLE_TAG_KEY: "Isolation"}, Verdict.UNUSED, None),
    ("smoke-key", False, False, {"Vigilantis:Role": ROLE_ISOLATION}, Verdict.UNUSED, None),
    ("x-isolation", False, False, {"Name": "x-isolation"}, Verdict.UNUSED, None),
    # default SG 경로 불변
    ("default", False, False, ISOLATION, Verdict.SKIP, SkipReason.SKIP_WHITELISTED),
]


@pytest.mark.parametrize("name,attached,openw,tags,exp_v,exp_s", ROLE_CASES)
def test_isolation_role_verdicts(name, attached, openw, tags, exp_v, exp_s):
    verdict, skip = evaluate_sg(name, attached, openw, tags)
    assert (verdict, skip) == (exp_v, exp_s)


def test_evaluate_sg_without_tags_argument_keeps_old_behavior():
    """태그 인자는 선택이다 — 기존 3인자 호출(골든·외부 호출자)의 판정이 바뀌지 않는다."""
    assert evaluate_sg("orphan", False, False) == (Verdict.UNUSED, None)


# --- 2) 수집 — AWS 응답의 Tags 가 계약까지 온다 ----------------------------------


class _Pages:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return iter(self._pages)


class _FakeEc2:
    """collect_region 이 부르는 ec2 호출만 흉내 낸다. 인스턴스 0대라 CloudWatch 는 안 부른다."""

    def __init__(self, sgs):
        self._sgs = sgs

    def get_paginator(self, name):
        pages = {
            "describe_instances": [{"Reservations": []}],
            "describe_volumes": [{"Volumes": []}],
            "describe_launch_templates": [{"LaunchTemplates": []}],
        }
        return _Pages(pages[name])

    def describe_security_groups(self, **_kw):
        return {"SecurityGroups": self._sgs}

    def describe_network_interfaces(self, **_kw):
        return {"NetworkInterfaces": []}

    def describe_network_acls(self, **_kw):
        return {"NetworkAcls": []}


class _Unsupported:
    """Community 미지원 서비스(autoscaling·elbv2) — 수집기가 degrade 하는 경로."""

    def get_paginator(self, _name):
        raise ClientError({"Error": {"Code": "InternalFailure", "Message": "n/a"}}, "Describe")


def _sg(group_id, name, tags=None):
    raw = {"GroupId": group_id, "GroupName": name, "Description": name, "VpcId": "vpc-1",
           "IpPermissions": []}
    if tags is not None:
        raw["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
    return raw


def test_collector_carries_sg_tags(monkeypatch):
    sgs = [
        _sg("sg-iso", "smoke-isolation", {"Name": "smoke-isolation", **ISOLATION}),
        _sg("sg-unused", "smoke-unused", {"Name": "smoke-unused"}),
        _sg("sg-bare", "bare"),  # Tags 키 자체가 없는 응답(태그 0개인 SG)
    ]
    ec2 = _FakeEc2(sgs)
    monkeypatch.setattr(
        C, "aws_client", lambda svc, _region: ec2 if svc == "ec2" else _Unsupported()
    )
    monkeypatch.setattr(C, "_account_id", lambda _region: ACCOUNT)

    inv = C.collect_region(REGION, cfg={"lookback_days": 14, "period_seconds": 3600})

    tags = {g.group_id: g.tags for g in inv.security_groups}
    assert tags["sg-iso"] == {"Name": "smoke-isolation", ROLE_TAG_KEY: ROLE_ISOLATION}
    assert tags["sg-unused"] == {"Name": "smoke-unused"}
    assert tags["sg-bare"] == {}


# --- 3) 적재 → 판정 — DB 왕복 ----------------------------------------------------


def _inventory() -> AssetInventory:
    def sg(gid, name, tags, open_=False):
        return SecurityGroupAsset(
            arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{gid}",
            group_id=gid, name=name, region=REGION, vpc_id="vpc-1", attached=False,
            open_to_world=[OpenPort(protocol="tcp", from_port=22, to_port=22)] if open_ else [],
            tags=tags,
        )

    return AssetInventory(
        account_id=ACCOUNT, region=REGION, mode="localstack",
        collected_at=datetime.now(timezone.utc), lookback_days=14, period_seconds=3600,
        security_groups=[
            sg("sg-iso", "smoke-isolation", ISOLATION),
            sg("sg-iso-open", "smoke-isolation-open", ISOLATION, open_=True),
            sg("sg-unused", "smoke-unused", {}),
        ],
    )


def test_isolation_tag_survives_persist_and_rule_engine(db):
    res = persist_inventory(_inventory(), db)
    run_rule_engine(db, collection_run_id=res["collection_run_id"])
    db.flush()
    db.expire_all()  # identity map 을 비우고 DB 행에서 다시 읽는다

    def row(resource_id):
        asset = db.execute(
            select(models.Asset).where(models.Asset.resource_id == resource_id)
        ).scalar_one()
        ev = db.execute(
            select(models.RuleEvaluation).where(models.RuleEvaluation.asset_id == asset.asset_id)
        ).scalar_one()
        return asset, ev

    iso, iso_ev = row("sg-iso")
    assert iso.spec["tags"] == ISOLATION
    assert (iso_ev.verdict, iso_ev.skip_reason_code) == ("SKIP", "SKIP_WHITELISTED")

    _, open_ev = row("sg-iso-open")
    assert (open_ev.verdict, open_ev.skip_reason_code) == ("THREAT", None)

    unused, unused_ev = row("sg-unused")
    assert unused.spec["tags"] == {}
    assert (unused_ev.verdict, unused_ev.skip_reason_code) == ("UNUSED", None)

    # 조회 계약(SgSpec 은 extra="forbid")이 새 키를 받아야 GET /assets 가 500 이 나지 않는다
    item = AssetItem.model_validate(
        {
            "arn": iso.arn, "resource_id": iso.resource_id, "asset_type": iso.asset_type,
            "resource_role": "PRIMARY", "name": iso.name, "account_id": iso.account_id,
            "region": iso.region, "state": iso.state, "spec": iso.spec, "relationships": [],
            "evaluation_status": iso_ev.evaluation_status, "verdict": iso_ev.verdict,
            "health_score": iso_ev.health_score, "skip_reason_code": iso_ev.skip_reason_code,
            "collected_at": iso.collected_at,
        }
    )
    assert item.spec.tags == ISOLATION


def _evaluation(db, resource_id, run_id):
    asset = db.execute(
        select(models.Asset).where(models.Asset.resource_id == resource_id)
    ).scalar_one()
    ev = db.execute(
        select(models.RuleEvaluation).where(
            models.RuleEvaluation.asset_id == asset.asset_id,
            models.RuleEvaluation.collection_run_id == run_id,
        )
    ).scalar_one()
    return asset, ev


def _single_sg_inventory(tags) -> AssetInventory:
    inv = _inventory()
    sg = next(g for g in inv.security_groups if g.group_id == "sg-iso")
    return inv.model_copy(update={"security_groups": [sg.model_copy(update={"tags": tags})]})


@pytest.mark.parametrize(
    "before,after,exp_before,exp_after",
    [
        # 태그를 떼면 다음 회차에 삭제 후보로 돌아온다 — 한 번 뺀 판정이 굳지 않는다
        (ISOLATION, {}, ("SKIP", "SKIP_WHITELISTED"), ("UNUSED", None)),
        # 자리가 바뀌어도(다른 role) 같다
        (ISOLATION, {ROLE_TAG_KEY: "bastion"}, ("SKIP", "SKIP_WHITELISTED"), ("UNUSED", None)),
        # 나중에 태그를 달면 다음 회차부터 빠진다
        ({}, ISOLATION, ("UNUSED", None), ("SKIP", "SKIP_WHITELISTED")),
    ],
)
def test_role_change_is_reflected_on_recollection(db, before, after, exp_before, exp_after):
    """재수집이 태그를 덮어써서 판정이 따라간다(이슈 #359 완료 기준 — 재수집 반영)."""
    run1 = persist_inventory(_single_sg_inventory(before), db)["collection_run_id"]
    run_rule_engine(db, collection_run_id=run1)
    run2 = persist_inventory(_single_sg_inventory(after), db)["collection_run_id"]
    run_rule_engine(db, collection_run_id=run2)
    db.flush()
    db.expire_all()

    _, ev1 = _evaluation(db, "sg-iso", run1)
    asset, ev2 = _evaluation(db, "sg-iso", run2)
    assert (ev1.verdict, ev1.skip_reason_code) == exp_before
    assert (ev2.verdict, ev2.skip_reason_code) == exp_after
    assert asset.spec["tags"] == after


def test_row_saved_before_tags_is_judged_as_before(db):
    """#359 이전에 적재된 SG 행(spec 에 tags 키 없음)은 종전 판정(UNUSED) 그대로다."""
    run = persist_inventory(_single_sg_inventory({}), db)["collection_run_id"]
    asset = db.execute(
        select(models.Asset).where(models.Asset.resource_id == "sg-iso")
    ).scalar_one()
    asset.spec = {k: v for k, v in asset.spec.items() if k != "tags"}  # 옛 행 모양
    db.flush()
    assert "tags" not in asset.spec

    run_rule_engine(db, collection_run_id=run)
    db.flush()
    db.expire_all()
    _, ev = _evaluation(db, "sg-iso", run)
    assert (ev.verdict, ev.skip_reason_code) == ("UNUSED", None)


def test_sg_spec_contract_reads_rows_saved_before_tags():
    """#359 이전에 적재된 SG 행(spec 에 tags 없음)도 조회 계약을 통과한다."""
    from schemas.api.assets import SgSpec

    old = SgSpec.model_validate(
        {"description": None, "vpc_id": None, "attached": False, "open_to_world": []}
    )
    assert old.tags == {}


# --- 4) 단일 원천 ----------------------------------------------------------------


def test_role_tag_strings_live_in_one_module():
    """태그를 다는 스크립트와 읽는 규칙이 같은 상수를 본다 — 문자열 원천은 하나다(#342 와 같은 결).

    파이썬 코드에서 태그 키 문자열 리터럴이 schemas/asset_roles.py 밖에 나오면 실패한다.
    테스트 파일·문서는 대상이 아니다(설명·기대값으로 적는 것은 원천이 아니다).
    """
    literal = re.compile(r"""["']vigilantis:role["']""")
    roots = [REPO_ROOT / "apps" / "core-api", REPO_ROOT / "packages", REPO_ROOT / "scripts"]
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if "tests" in parts or ".venv" in parts or path.name.startswith("test_"):
                continue
            if literal.search(path.read_text(encoding="utf-8")):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())  # Windows 구분자 무관
    assert offenders == ["packages/schemas/asset_roles.py"]

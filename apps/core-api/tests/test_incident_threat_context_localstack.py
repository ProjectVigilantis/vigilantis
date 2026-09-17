"""#362: LocalStack 실수집 자산과 골든 위협을 PostgreSQL·공개 API에서 연결한다.

필드 거부·누락·쿼리 수는 schema/API 테스트의 몫이다. 이 테스트는 FE가 사용할
subject_arn이 실수집 자산에 연결되고, 두 유형의 문맥이 분석 전부터 보이는지 확인한다.
"""

import json
import os
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from config import get_aws_settings, get_collector_settings
from mock_threat_source import parse_observation
from services import collector
from threat_ingress import receive_threat

GOLDEN = Path(__file__).resolve().parents[3] / "datasets/golden/secops/input"


@pytest.fixture
def localstack_inventory(monkeypatch):
    endpoint = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    try:
        with urllib.request.urlopen(f"{endpoint}/_localstack/health", timeout=2) as response:
            assert response.status == 200
    except OSError:
        pytest.skip(f"LocalStack({endpoint}) 미기동 — 시드 자산 통합 테스트 skip")
    monkeypatch.setenv("AWS_ENDPOINT_URL", endpoint)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")
    monkeypatch.setenv("AWS_REGIONS", "ap-northeast-2")
    get_aws_settings.cache_clear()
    get_collector_settings.cache_clear()
    try:
        yield collector.collect_region("ap-northeast-2")
    finally:
        get_aws_settings.cache_clear()
        get_collector_settings.cache_clear()


def test_golden_threat_contexts_join_collected_assets_before_analysis(
    localstack_inventory, db, client_pg,
):
    inventory = localstack_inventory
    instance = next(asset for asset in inventory.ec2_instances if asset.name == "vigilantis-seed-idle")
    group = next(asset for asset in inventory.security_groups if asset.name == "vigilantis-seed-open-ssh")
    assert group.group_id in instance.security_group_ids
    assert any(rule.from_port == rule.to_port == 22 for rule in group.open_to_world)
    collector.persist_inventory(inventory, db)
    db.commit()

    expected = {}
    for name, target, public_field, input_field in (
        ("evt_ssh_bruteforce_001", instance.arn, "source_ip", "source_ip"),
        ("evt_open_ip_001", group.arn, "exposed_cidr", "source_cidr"),
    ):
        raw = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
        raw.update(event_id=str(uuid.uuid4()), target_arn=target, occurred_at=datetime.now(UTC).isoformat())
        result = receive_threat(db, parse_observation(raw))
        assert result.created
        expected[result.incident_id] = (
            target, {"event_type": raw["event_type"], public_field: raw[input_field]},
        )

    assets = client_pg.get("/api/v1/assets")
    assert assets.status_code == 200
    types_by_arn = {asset["arn"]: asset["asset_type"] for asset in assets.json()["items"]}
    assert types_by_arn[instance.arn] == "EC2"
    assert types_by_arn[group.arn] == "SG"
    listing = client_pg.get("/api/v1/incidents")
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == len(expected)
    for item in listing.json()["items"]:
        target, context = expected[item["incident_id"]]
        assert item["subject_arn"] == target
        assert item["threat_context"] == context
        assert item["status"] == "ANALYZING"
        detail = client_pg.get(f"/api/v1/incidents/{item['incident_id']}")
        assert detail.status_code == 200
        assert detail.json()["threat_context"] == context
        assert detail.json()["recommendations"] == detail.json()["summary_lines"] == []

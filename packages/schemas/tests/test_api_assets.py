"""GET /api/v1/assets 외부 DTO 계약 테스트.

확정 계약(4장)의 교차 필드 불변식을 검증한다:
판정 상태 ↔ verdict/skip 사유, 자산 유형 ↔ spec/role, "Z" 시각 직렬화.
"""

import pytest
from pydantic import ValidationError

from schemas.api import (
    AssetItem,
    AssetsResponse,
    CollectionStatus,
    EvaluationStatus,
    RelationType,
    Verdict,
)


def make_ec2_item(**over):
    base = {
        "arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123",
        "resource_id": "i-0123",
        "asset_type": "EC2",
        "resource_role": "PRIMARY",
        "name": "batch-dev",
        "account_id": "123456789012",
        "region": "ap-northeast-2",
        "state": "running",
        "spec": {
            "instance_type": "t3.large",
            "availability_zone": "ap-northeast-2a",
            "vpc_id": "vpc-0123",
            "subnet_id": "subnet-0123",
            "private_ip": "10.0.1.10",
        },
        "relationships": [
            {
                "relation_type": "SECURED_BY",
                "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-0123",
            }
        ],
        "evaluation_status": "COMPLETED",
        "health_score": 2.8,
        "verdict": "COST_CANDIDATE",
        "skip_reason_code": None,
        "collected_at": "2026-08-12T09:00:00Z",
    }
    base.update(over)
    return base


def make_nacl_item(**over):
    base = {
        "arn": "arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-0123",
        "resource_id": "acl-0123",
        "asset_type": "NACL",
        "resource_role": "RUNBOOK_SUPPORT",
        "account_id": "123456789012",
        "region": "ap-northeast-2",
        "spec": {"vpc_id": "vpc-0123", "is_default": False, "associated_subnet_ids": ["subnet-0123"]},
        "evaluation_status": "NOT_APPLICABLE",
        "collected_at": "2026-08-12T09:00:00Z",
    }
    base.update(over)
    return base


def test_ec2_item_roundtrip_and_z_serialization():
    item = AssetItem.model_validate(make_ec2_item())
    assert item.verdict == Verdict.COST_CANDIDATE
    dumped = item.model_dump_json()
    assert '"2026-08-12T09:00:00Z"' in dumped
    assert AssetItem.model_validate_json(dumped) == item


def test_sg_threat_and_ebs_unused_valid():
    sg = AssetItem.model_validate(make_ec2_item(
        arn="arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-0123",
        resource_id="sg-0123", asset_type="SG", state=None, name="demo-open-ssh",
        spec={"description": "demo", "vpc_id": "vpc-0123", "attached": True,
              "open_to_world": [{"protocol": "tcp", "from_port": 22, "to_port": 22, "ipv6": False}]},
        relationships=[], health_score=None, verdict="THREAT",
    ))
    assert sg.spec.open_to_world[0].from_port == 22

    ebs = AssetItem.model_validate(make_ec2_item(
        arn="arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-0123",
        resource_id="vol-0123", asset_type="EBS", resource_role="RUNBOOK_SUPPORT",
        state=None, spec={"volume_type": "gp3", "size_gib": 100, "availability_zone": "ap-northeast-2a",
                          "encrypted": True, "attached_instance_ids": []},
        relationships=[], health_score=None, verdict="UNUSED",
    ))
    assert ebs.verdict == Verdict.UNUSED


@pytest.mark.parametrize("over", [
    {"verdict": None},                                            # COMPLETED인데 verdict 없음
    {"verdict": "SKIP", "skip_reason_code": None},                # SKIP인데 사유 없음
    {"verdict": "COST_CANDIDATE", "skip_reason_code": "SKIP_ACTIVE"},  # SKIP 아닌데 사유 있음
    {"evaluation_status": "PENDING"},                             # PENDING인데 verdict·score 잔존
    {"health_score": 150.0},                                      # 범위 밖
    {"asset_type": "SG", "resource_id": "sg-1", "spec": {"attached": True}},  # SG인데 health_score 잔존
    {"resource_role": "RUNBOOK_SUPPORT"},                         # EC2 role 불일치
    {"unknown_field": 1},                                         # extra 거부
])
def test_ec2_item_contract_violations(over):
    with pytest.raises(ValidationError):
        AssetItem.model_validate(make_ec2_item(**over))


def test_spec_must_match_asset_type():
    # EC2에 NACL spec 형태를 주면 spec 검증에서 거부된다
    with pytest.raises(ValidationError):
        AssetItem.model_validate(make_ec2_item(spec={"is_default": True}))


def test_nacl_valid_and_must_stay_not_applicable():
    nacl = AssetItem.model_validate(make_nacl_item())
    assert nacl.evaluation_status == EvaluationStatus.NOT_APPLICABLE
    assert nacl.verdict is None

    with pytest.raises(ValidationError):
        AssetItem.model_validate(make_nacl_item(evaluation_status="COMPLETED", verdict="UNUSED"))
    with pytest.raises(ValidationError):
        AssetItem.model_validate(make_nacl_item(resource_role="PRIMARY"))


def test_relation_types_complete():
    assert {r.value for r in RelationType} == {
        "SECURED_BY", "ATTACHED_TO", "MEMBER_OF", "USES", "REGISTERED_IN", "PROTECTED_BY",
    }
    with pytest.raises(ValidationError):
        AssetItem.model_validate(make_ec2_item(
            relationships=[{"relation_type": "OWNS", "target_arn": "arn:x"}]
        ))


def test_assets_response_states():
    ready = AssetsResponse.model_validate({
        "collection_status": "READY",
        "last_collected_at": "2026-08-12T09:00:00Z",
        "items": [],
    })
    assert ready.collection_status == CollectionStatus.READY

    empty = AssetsResponse.model_validate({"collection_status": "NOT_COLLECTED", "last_collected_at": None, "items": []})
    assert empty.last_collected_at is None

    with pytest.raises(ValidationError):
        AssetsResponse.model_validate({
            "collection_status": "NOT_COLLECTED",
            "last_collected_at": "2026-08-12T09:00:00Z",
            "items": [],
        })

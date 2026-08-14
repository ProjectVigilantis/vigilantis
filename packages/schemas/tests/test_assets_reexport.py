"""schemas.assets 호환 계약 테스트 (Issue #48).

축소 AssetType(2종)을 공개 7종 Enum 재노출로 통합 — 기존 collector 소비 경로
(`from schemas.assets import ...`)가 그대로 동작해야 한다.
"""

from schemas import assets as internal_assets
from schemas.api import assets as public_assets


def test_asset_type_is_public_enum_reexport():
    # 복제가 아니라 동일 객체 재노출이어야 한다
    assert internal_assets.AssetType is public_assets.AssetType
    assert internal_assets.AssetType.EC2.value == "EC2"
    assert internal_assets.AssetType.SG.value == "SG"
    assert len(internal_assets.AssetType) == 7


def test_legacy_models_still_default_to_ec2_sg():
    ec2 = internal_assets.Ec2Asset(
        arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-1",
        instance_id="i-1", region="ap-northeast-2",
    )
    sg = internal_assets.SecurityGroupAsset(
        arn="arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-1",
        group_id="sg-1", region="ap-northeast-2", attached=True,
    )
    assert ec2.asset_type is internal_assets.AssetType.EC2
    assert sg.asset_type is internal_assets.AssetType.SG


def test_top_level_package_reexport_unchanged():
    import schemas

    assert schemas.AssetType is public_assets.AssetType

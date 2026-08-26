"""백업 레코드 payload 계약 (스펙 JSON 백업 모듈, ADR-0004 정책 ③).

payload는 만드는 시점과 읽는 시점이 멀다 — 원복이 필요한 순간에 필드가 비어
있으면 자산은 이미 바뀐 뒤다. 그 사이를 지키는 계약이 여기 있다.
"""

import pytest
from pydantic import ValidationError

from schemas.backups import BackupType, InstanceSpecBackup

MINIMAL = {"instance_id": "i-0abc", "instance_type": "t3.xlarge", "state": "running"}


def test_backup_types_match_the_runbook_spec_vocabulary():
    """런북 명세서 safety_and_rollback.backup_action과 같은 어휘여야 한다 —
    executor의 백업 조회가 이 문자열로 레코드를 찾는다."""
    assert {t.value for t in BackupType} == {
        "SAVE_INSTANCE_SPEC_JSON",
        "SAVE_SG_FULL_RULES_JSON",
        "SAVE_CURRENT_SG_AND_TG_MAPPING",
        "RECORD_NACL_RULE_INDEX",
    }


def test_minimal_spec_only_needs_the_revert_inputs():
    spec = InstanceSpecBackup(**MINIMAL)
    assert spec.instance_type == "t3.xlarge"
    assert spec.image_id is None and spec.ebs_optimized is None


@pytest.mark.parametrize("field", ["instance_id", "instance_type", "state"])
def test_revert_inputs_are_required(field):
    """이 셋이 없으면 원복이 불가능하다 — payload를 만들 수 있으면 안 된다."""
    payload = {**MINIMAL}
    payload.pop(field)
    with pytest.raises(ValidationError):
        InstanceSpecBackup(**payload)


@pytest.mark.parametrize("field", ["instance_id", "instance_type", "state"])
def test_revert_inputs_reject_blank_values(field):
    with pytest.raises(ValidationError):
        InstanceSpecBackup(**{**MINIMAL, field: ""})


def test_unknown_keys_are_rejected():
    """SG 목록은 SAVE_CURRENT_SG_AND_TG_MAPPING의 몫이다. 스펙 백업에 섞이면
    격리 해제가 잘못된 레코드에서 SG를 복원할 여지가 생긴다."""
    with pytest.raises(ValidationError):
        InstanceSpecBackup(**MINIMAL, security_group_ids=["sg-1"])


def test_dump_is_plain_json_for_the_jsonb_column():
    """BackupRecord.payload(JSONB)에 그대로 들어간다 — 직렬화 불가 값이 없어야 한다."""
    import json

    dumped = InstanceSpecBackup(
        **MINIMAL, ebs_optimized=True, availability_zone="ap-northeast-2a"
    ).model_dump(mode="json")
    assert json.loads(json.dumps(dumped))["instance_type"] == "t3.xlarge"
    assert dumped["ebs_optimized"] is True


def test_precheck_reads_instance_type_from_the_dumped_payload():
    """executor._precheck_revert_size가 읽는 키 이름을 고정한다."""
    assert "instance_type" in InstanceSpecBackup(**MINIMAL).model_dump(mode="json")

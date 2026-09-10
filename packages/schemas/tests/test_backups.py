"""백업 레코드 payload 계약 (스펙 JSON 백업 모듈, ADR-0004 정책 ③).

payload는 만드는 시점과 읽는 시점이 멀다 — 원복이 필요한 순간에 필드가 비어
있으면 자산은 이미 바뀐 뒤다. 그 사이를 지키는 계약이 여기 있다.
"""

import pytest
from pydantic import ValidationError

from schemas.backups import BackupType, InstanceSpecBackup, NaclRuleIndexBackup

MINIMAL = {"instance_id": "i-0abc", "instance_type": "t3.xlarge", "state": "running"}


def test_backup_types_match_the_runbook_spec_vocabulary():
    """ADR-0004 롤백 공통 정책 ③의 backup_action과 같은 어휘여야 한다 —
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


# ------------------------------------------------- NACL 규칙 index (ADR-0008 §5)

NACL_RULE = {
    "rule_number": 100,
    "egress": False,
    "cidr_block": "198.51.100.0/24",
    "protocol": "6",
    "rule_action": "deny",
}


def test_nacl_rule_index_keeps_exactly_the_five_required_items():
    """ADR-0008 §5가 fingerprint 3항목을 부가에서 필수로 승격했다 — 5항목 전부다."""
    record = NaclRuleIndexBackup(**NACL_RULE)
    assert set(record.model_dump(mode="json")) == {
        "rule_number",
        "egress",
        "cidr_block",
        "protocol",
        "rule_action",
    }


@pytest.mark.parametrize("field", list(NACL_RULE))
def test_every_nacl_rule_item_is_required(field):
    """부가 항목이 하나도 없다. 어느 하나가 비면 NACL_RESTORE는 삭제 대상을
    특정하지 못하고, 특정하지 못한 채 삭제하면 남의 규칙을 지운다."""
    payload = {k: v for k, v in NACL_RULE.items() if k != field}
    with pytest.raises(ValidationError):
        NaclRuleIndexBackup(**payload)


def test_nacl_protocol_is_stored_as_an_aws_number():
    """이름 표기는 계약이 아니다.

    이 값이 대조할 상대는 describe_network_acls의 Protocol이고 실 AWS는 거기에
    번호를 돌려준다. 이름으로 저장하면 fingerprint가 영영 맞지 않는다.
    """
    with pytest.raises(ValidationError):
        NaclRuleIndexBackup(**{**NACL_RULE, "protocol": "tcp"})


def test_nacl_rule_action_cannot_be_allow():
    """이 백업 종류를 만드는 런북은 NACL_ADD_DENY 하나뿐이고 그 조치는 deny만 넣는다.
    allow가 실린 payload는 이 경로가 만든 레코드가 아니므로 읽는 쪽에서 걸려야 한다."""
    with pytest.raises(ValidationError):
        NaclRuleIndexBackup(**{**NACL_RULE, "rule_action": "allow"})


def test_nacl_rule_index_rejects_unknown_keys():
    """읽는 쪽이 dict.get으로 더듬지 않게 하려는 계약이다 — 여분 키를 받으면
    "이 payload에 무엇이 들어 있는가"가 다시 열린 질문이 된다."""
    with pytest.raises(ValidationError):
        NaclRuleIndexBackup(**{**NACL_RULE, "port_range": {"From": 22, "To": 22}})

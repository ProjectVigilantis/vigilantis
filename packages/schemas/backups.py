# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 백업 레코드 payload 계약입니다. (스펙 JSON 백업 모듈, ADR-0004 롤백 공통 정책 ③)
#
# 원복 파라미터의 유일한 원천은 DB 백업 레코드다 — 요청 페이로드에서 원복 값을
# 받지 않는다. 그래서 payload는 "만든 쪽과 읽는 쪽이 다른 시점에 사는" 계약이며,
# 자유 dict로 두면 원복 시점에야 필드 부재가 드러난다. 그때는 이미 자산이 바뀐
# 뒤라 되돌릴 방법이 없다. 여기서 형태를 고정한다.
#
#   생산: apps/core-api/services/aws/backup.py (조치 직전 AWS 조회)
#   저장: db.models.BackupRecord.payload (JSONB, 생성 후 불변)
#   소비: apps/core-api/services/aws/executor.py precheck 롤백 4종
#
# BackupType은 ADR-0004 롤백 공통 정책 ③의 backup_action과 같은 어휘다.
# backup_action이 NONE인 런북(롤백 4종·NACL_RESTORE 등)은 여기 값을 갖지 않는다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .runbook_parameters import Ipv4Cidr, NaclProtocolNumber, RuleNumber


@unique
class BackupType(str, Enum):
    """백업 종류 4종 — ADR-0004 롤백 공통 정책 ③의 backup_action 어휘."""

    SAVE_INSTANCE_SPEC_JSON = "SAVE_INSTANCE_SPEC_JSON"        # RIGHTSIZING
    SAVE_SG_FULL_RULES_JSON = "SAVE_SG_FULL_RULES_JSON"        # SG_DELETE_ISOLATED
    SAVE_CURRENT_SG_AND_TG_MAPPING = "SAVE_CURRENT_SG_AND_TG_MAPPING"  # EC2_ISOLATE
    RECORD_NACL_RULE_INDEX = "RECORD_NACL_RULE_INDEX"          # NACL_ADD_DENY


class InstanceSpecBackup(BaseModel):
    """`SAVE_INSTANCE_SPEC_JSON` payload — RIGHTSIZING 변경 직전 인스턴스 스펙.

    `RUNBOOK_EC2_REVERT_SIZE`가 읽는 값이다(ADR-0004 §Decision). 원복이
    필요한 순간은 인스턴스가 부팅에 실패한 뒤이므로, **원복에 필요한 값은 전부
    여기 있어야 한다** — 그 시점에 AWS를 다시 조회해서 얻을 수 있는 것은 이미
    바뀐 값뿐이다.

    필수 3종의 근거
      - `instance_type` — 되돌릴 타입. precheck `_precheck_revert_size`가 읽는다.
      - `state` — 변경 전 실행 상태. 타입 변경은 중지 상태에서만 되므로 원복
        절차가 "다시 켜야 하는가"를 이 값으로 판단한다.
      - `instance_id` — 레코드만 보고 대상을 특정할 수 있어야 한다.

    나머지는 원복 판단의 근거다(관제자에게 "무엇이 그대로가 아니었는지" 설명).
    AWS가 돌려주지 않을 수 있는 값이라 없다고 조치를 막지는 않는다 — 백업이
    없어서 못 되돌리는 것과, 부가 정보가 비어 있는 것은 다른 사건이다.

    그중 `public_ip_address`·`elastic_ip_association_id`는 **원복 값이 아니라 한계
    고지의 근거다**(ADR-0008 §5). 타입 변경은 정지를 거치므로 EIP가 붙어 있지 않으면
    퍼블릭 IPv4가 바뀌고, 원복해도 원래 주소로 돌아오지 않는다. 조치 이전 주소를
    여기 남겨 두지 않으면 조치 후에는 영영 알 수 없어, "무엇이 안 돌아왔는지"를
    관제자에게 사실대로 말할 수 없다.

    SG 목록은 일부러 담지 않는다. ENI SG 복원의 원천은
    `SAVE_CURRENT_SG_AND_TG_MAPPING`이며(EC2_ISOLATE), 스펙 백업에 SG를 함께
    두면 격리 해제가 잘못된 레코드에서 SG를 복원할 여지가 생긴다.
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1)
    instance_type: str = Field(min_length=1)
    state: str = Field(min_length=1)

    image_id: Optional[str] = None
    architecture: Optional[str] = None
    # 타입 호환성의 근거 — ebs / instance-store
    root_device_type: Optional[str] = None
    # 일부 타입이 강제하는 속성이라 원복 시 함께 되돌려야 할 수 있다
    ebs_optimized: Optional[bool] = None
    availability_zone: Optional[str] = None
    vpc_id: Optional[str] = None
    subnet_id: Optional[str] = None
    # 조치 이전 퍼블릭 IPv4. 원복해도 돌아오지 않으므로 복원 값이 아니라 고지 근거다.
    public_ip_address: Optional[str] = None
    # 값이 있으면 EIP가 붙어 있었다는 뜻이라 주소가 유지된다 — 위 고지의 반대 근거다.
    elastic_ip_association_id: Optional[str] = None


class NaclRuleIndexBackup(BaseModel):
    """`RECORD_NACL_RULE_INDEX` payload — `NACL_ADD_DENY`가 넣은 deny 규칙 1건의 좌표.

    `RUNBOOK_NACL_RESTORE`가 읽는 값이다. 다른 백업 3종과 성격이 다르다 — 조치
    **이전** 상태가 아니라 조치가 **만들어 낼** 규칙을 적는다. NACL 규칙 삽입은
    기존 값을 덮지 않고 빈 슬롯에 넣는 조치라, 되돌리는 일이 "옛 값 복원"이 아니라
    "우리가 넣은 그 규칙만 삭제"이기 때문이다.

    5항목 전부가 필수다(ADR-0008 §5). 앞의 둘은 규칙 슬롯을 특정하고, 뒤의 셋은
    **규칙 fingerprint**로 `NACL_RESTORE`의 통과 조건이 된다.

      - `rule_number`·`egress` — 삭제할 슬롯
      - `cidr_block`·`protocol`·`rule_action` — 그 슬롯에 있는 규칙이 우리 것인지

    fingerprint가 부가 정보가 아니라 필수인 이유가 이 백업의 존재 이유이기도 하다.
    `rule_number`는 **재사용되는 슬롯 번호**다. 우리 규칙이 삭제된 뒤 같은 번호에
    제3자의 다른 deny 규칙이 들어오면, 슬롯만 보는 대조는 그 규칙을 우리 것으로
    오인해 삭제한다 — 그리고 삭제는 되돌릴 수 없다.

    `protocol`은 AWS 표기(번호 문자열)다. `NaclAddDenyParameters.protocol`의 이름
    표기가 아니다 — 이 값이 대조할 상대가 `describe_network_acls`의 `Protocol`이라,
    같은 축의 값을 저장해야 대조가 성립한다(schemas.runbook_parameters
    `NACL_PROTOCOL_NUMBERS`의 실측 주석).

    `rule_action`을 "deny"로 못 박는다. 이 백업 종류를 만드는 런북은 `NACL_ADD_DENY`
    하나뿐이고(ADR-0004 롤백 공통 정책 ③의 backup_action 표), 그 조치가 넣는 규칙은
    항상 deny다. "allow"가 실린 payload는 이 경로가 만든 레코드가 아니라는 뜻이므로
    모델 검증에서 걸리는 편이 옳다 — 그때 `NACL_RESTORE`는 삭제하지 않고 판정 불가로
    남긴다(ADR-0008 §5의 "백업 payload에 항목이 없다" 칸과 같은 처분).
    """

    model_config = ConfigDict(extra="forbid")

    rule_number: RuleNumber
    egress: bool
    cidr_block: Ipv4Cidr
    protocol: NaclProtocolNumber
    rule_action: Literal["deny"]

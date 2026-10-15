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

from datetime import datetime
from enum import Enum, unique
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .runbook_parameters import (
    Ipv4Cidr,
    NaclProtocolNumber,
    NetworkInterfaceId,
    RuleNumber,
    SecurityGroupId,
    TargetGroupArn,
    VpcId,
)


@unique
class BackupType(str, Enum):
    """백업 종류 4종 — ADR-0004 롤백 공통 정책 ③의 backup_action 어휘."""

    SAVE_INSTANCE_SPEC_JSON = "SAVE_INSTANCE_SPEC_JSON"  # RIGHTSIZING
    SAVE_SG_FULL_RULES_JSON = "SAVE_SG_FULL_RULES_JSON"  # SG_DELETE_ISOLATED
    SAVE_CURRENT_SG_AND_TG_MAPPING = "SAVE_CURRENT_SG_AND_TG_MAPPING"  # EC2_ISOLATE
    RECORD_NACL_RULE_INDEX = "RECORD_NACL_RULE_INDEX"  # NACL_ADD_DENY


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

    image_id: str | None = None
    architecture: str | None = None
    # 타입 호환성의 근거 — ebs / instance-store
    root_device_type: str | None = None
    # 일부 타입이 강제하는 속성이라 원복 시 함께 되돌려야 할 수 있다
    ebs_optimized: bool | None = None
    availability_zone: str | None = None
    vpc_id: str | None = None
    subnet_id: str | None = None
    # 조치 이전 퍼블릭 IPv4. 원복해도 돌아오지 않으므로 복원 값이 아니라 고지 근거다.
    public_ip_address: str | None = None
    # 값이 있으면 EIP가 붙어 있었다는 뜻이라 주소가 유지된다 — 위 고지의 반대 근거다.
    elastic_ip_association_id: str | None = None


# CreateSecurityGroup API의 GroupName·Description 상한이 255자다. AWS가 거절할 값을
# 백업에 담아 두면 원복 시점에야 드러나는데, 그때는 SG가 이미 지워진 뒤다.
_SgText = Annotated[str, Field(min_length=1, max_length=255)]

# describe_security_groups가 돌려주는 규칙 1건. **모델로 더 쪼개지 않는다.**
# 이 목록은 우리가 해석할 값이 아니라 authorize_security_group_*에 **그대로 되붓는**
# 값이라, 필드를 우리가 다시 적으면 AWS가 늘린 필드(전송 규칙·접두 목록 등)가 백업에서
# 조용히 떨어져 나가 원복된 SG가 원본보다 좁아진다. extra="forbid"를 여기까지 밀면
# 그 손실이 검증 실패로 드러나지도 않는다 — 규칙이 통째로 사라진다.
SgIpPermission = dict[str, Any]


class SgFullRulesBackup(BaseModel):
    """`SAVE_SG_FULL_RULES_JSON` payload — `SG_DELETE_ISOLATED` 삭제 직전 SG 전체.

    `RUNBOOK_SG_RECREATE`가 읽는 값이다. 이 백업은 **원복에 필요한 전부**여야 한다 —
    SG를 지우고 나면 그 이름도 설명도 규칙도 AWS에 다시 물을 수 없고, `SG_RECREATE`는
    복원 대상 SG ID를 파라미터로 받지도 않는다(`SgRecreateParameters`).

    필수 5항목의 근거
      - `group_name`·`description`·`vpc_id` — `create_security_group` 호출의 인자 3종이다.
        하나라도 없으면 그룹 자체를 만들 수 없다.
      - `ingress_permissions`·`egress_permissions` — 되붓을 규칙 목록. **빈 목록을
        허용한다.** 규칙이 0개인 SG가 실재하고(미부착 SG는 대개 그렇다), 빈 목록과
        "백업이 규칙을 놓쳤다"는 모델 검증이 아니라 캡처 시점에 갈린다.

    `group_id`는 부가다(ADR-0008 §5 신설). 원복은 AWS가 새로 발급한 ID를 받으므로 원본
    ID는 복원 값이 아니라 **한계 고지의 근거**다 — 원본 ID를 참조하던 다른 SG 규칙과 ENI
    연결은 돌아오지 않으므로(ADR-0008 §참조 무결성), 관제자에게 "무엇을 손수 다시 이어야
    하는가"를 말하려면 조치 이전 ID가 남아 있어야 한다. `InstanceSpecBackup`의
    `public_ip_address`와 같은 성격이다.

    그리고 원복 실행이 **자기 참조 규칙을 고쳐 붓는 근거**이기도 하다. 규칙의
    `UserIdGroupPairs`에 원본 자기 ID가 있으면 그대로 주입할 수 없다 — 그 SG는 이미
    없다. 무엇을 새 ID로 바꿔야 하는지는 이 값과 대조해야만 알 수 있다.
    """

    model_config = ConfigDict(extra="forbid")

    group_name: _SgText
    description: _SgText
    vpc_id: VpcId
    ingress_permissions: list[SgIpPermission]
    egress_permissions: list[SgIpPermission]

    group_id: SecurityGroupId | None = None


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


# 등록 포트의 범위 — RegisterTargets의 Port 계약(1–65535).
_TargetPort = Annotated[int, Field(ge=1, le=65535)]


class EniSecurityGroups(BaseModel):
    """ENI 1개의 조치 직전 SG 목록 — `SgAndTgMappingBackup.network_interfaces`의 원소."""

    model_config = ConfigDict(extra="forbid")

    network_interface_id: NetworkInterfaceId
    # ENI에는 SG가 최소 1개 붙는다(AWS 계약). 빈 목록은 캡처가 무언가를 놓쳤다는 뜻이고,
    # 그 값으로 원복하면 modify_network_interface_attribute가 파라미터 오류로 거절한다.
    security_group_ids: list[SecurityGroupId] = Field(min_length=1)


# ASG 이름 상한(CreateAutoScalingGroup 255자)
_AsgName = Annotated[str, Field(min_length=1, max_length=255)]


class ScaleInProtectionBackup(BaseModel):
    """격리 직전에 **관측한** ASG scale-in 보호 상태 — 보조 정보이며 SG/TG 원복 값이 아니다.

    `SgAndTgMappingBackup.scale_in_protection`에 실린다. 세 상태를 섞지 않는다.

      - `MANAGED` — 이 인스턴스가 #446 `EC2_ENABLE_AUTOSCALING` 실행이 만든 스모크 ASG의
        **실제 멤버**임을 ASG 소유 태그 ↔ 저장된 실행 계획 대조로 확인했고, 보호 이전 값을
        읽었다(`previous_protected`). 보호 쓰기·복원 대상은 이 상태뿐이다.
      - `NOT_MANAGED` — 조회가 성공했고 대상이 ASG 멤버가 아니거나 우리가 만든 스모크 ASG가
        아니다. 격리는 그대로 두고 ASG에는 손대지 않는다.
      - `UNKNOWN` — 조회·대조를 끝내지 못했다. **보호되지 않았다는 뜻이 아니다.** 쓰기도
        복원도 하지 않는다. 이 필드가 없는 옛 백업도 같은 처분이다(`None` = 모름).
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["MANAGED", "NOT_MANAGED", "UNKNOWN"]
    asg_name: _AsgName | None = None
    previous_protected: bool | None = None
    # ASG 소유 태그가 가리킨 #446 실행 — 저장된 계획과 대조한 값
    autoscaling_execution_id: str | None = Field(
        default=None, min_length=1, max_length=64
    )
    reason: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _shape_by_status(self) -> ScaleInProtectionBackup:
        managed_fields = (
            self.asg_name,
            self.previous_protected,
            self.autoscaling_execution_id,
        )
        if self.status == "MANAGED":
            if any(value is None for value in managed_fields):
                raise ValueError(
                    "MANAGED에는 asg_name·previous_protected·autoscaling_execution_id가 모두 필요합니다"
                )
        elif any(value is not None for value in managed_fields):
            raise ValueError(
                "MANAGED가 아닌 상태는 ASG 좌표나 이전 보호 값을 갖지 않습니다"
            )
        return self


ScaleInProtectionReason = Literal[
    "CORE_STATE_UNVERIFIED",  # 복원 전에 ENI SG·TG 등록이 복원된 현물을 다시 읽어 확인하지 못했다
    "WRITTEN",  # 쓰기 응답을 받았고 조회로 값을 확인했다
    "ALREADY_PROTECTED",  # 이미 보호돼 있어 쓰지 않았다
    "RESTORED",  # 복원 쓰기 후 이전 값을 조회로 확인했다
    "ALREADY_RESTORED",  # 복원할 필요가 없었다(이미 이전 값)
    "NO_PROTECTION_WRITE",  # 우리가 보호를 쓴 기록이 없어 복원하지 않았다
    "PRIOR_STATE_UNKNOWN",  # 이전 값을 모른다 — 쓰지 않았다
    "MEMBERSHIP_DRIFT",  # 같은 인스턴스·ASG 멤버십이 확인되지 않았다 — 쓰지 않았다
    "LOOKUP_FAILED",  # 조회를 못 했다
    "WRITE_REJECTED",  # AWS가 쓰기를 거절했다(4xx)
    "WRITE_UNCERTAIN",  # 쓰기 응답을 알 수 없고 값도 확인하지 못했다
    "EXECUTION_INTERRUPTED",  # 격리/해제 실행이 끊겨 이 단계에 도달한 기록이 없다
]


class ScaleInProtectionResult(BaseModel):
    """ASG scale-in 보호 쓰기(`PROTECT`)·복원(`RESTORE`) 1회의 **별도 관측 결과**.

    격리/해제 실행의 SUCCESS·단계 effect와 섞이지 않는 자리다 — 이 결과가 FAILED·UNKNOWN이어도
    TG/ENI 격리 성공이나 해제 경로를 바꾸지 않는다. 잔여(`residual`)는 "보호가 의도대로
    되지 않았거나 확인되지 않았다"를 뜻하며 사람이 확인할 몫이다.

    저장 위치는 `ActionExecution.scale_in_protection_result`(실행당 1개, 갱신 가능)다 — PROTECT는
    격리 실행에, RESTORE는 해제 실행에 남는다. 원복 근거인 SG/TG 백업은 불변으로 두고 이 값은
    그 위에 쌓지 않는다.

    `write_may_have_begun`은 이 시도의 AWS 쓰기가 **시작됐을 수 있다**는 표지다. 의미는 한 번의
    시도 안의 "의도 → 최종 결과"이며, 최종 결과(확정 거절 포함)가 의도를 덮어쓴다 — 단계를 남긴
    실행은 다시 돌리지 않으므로 시도 사이로 이어 붙이지 않는다. 쓰기 직전에 남기는
    `UNKNOWN`·`WRITE_UNCERTAIN` 의도 기록에서 참이 되며, **호출이 실제로 나갔다는 증거가
    아니다**(기록 후 호출 전에 죽었을 수 있다). 복원은 이 값이 참이고 확정 거절
    (`FAILED`·`WRITE_REJECTED`)이 아닌 PROTECT 기록이 있을 때만 쓴다.

    `observed_at`은 이 결과를 만든(기록 직전) 시각이다 — 의도 기록과 최종 기록은 각자 시각을 갖는다.
    """

    model_config = ConfigDict(extra="forbid")

    phase: Literal["PROTECT", "RESTORE"]
    outcome: Literal["APPLIED", "NOT_ATTEMPTED", "FAILED", "UNKNOWN"]
    reason: ScaleInProtectionReason
    # 격리 직전에 ASG를 확정하지 못한 UNKNOWN 백업의 잔여 기록은 ASG 이름이 없다
    asg_name: _AsgName | None = None
    instance_id: str = Field(min_length=1)
    write_may_have_begun: bool
    # 마지막으로 **관측한** 보호 값. 관측하지 못했으면 None
    observed_protected: bool | None = None
    residual: bool
    observed_at: datetime

    @property
    def may_have_changed_protection(self) -> bool:
        """이 기록이 "우리 쓰기가 값을 바꿨을 수 있다"의 근거인가 — 확정 거절은 근거가 아니다."""
        rejected = self.outcome == "FAILED" and self.reason == "WRITE_REJECTED"
        return self.write_may_have_begun and not rejected

    @model_validator(mode="after")
    def _consistent(self) -> ScaleInProtectionResult:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at은 시간대가 있어야 합니다")
        if self.outcome == "APPLIED" and self.residual:
            raise ValueError("APPLIED는 잔여가 없습니다")
        if self.outcome in ("FAILED", "UNKNOWN") and not self.residual:
            raise ValueError("FAILED·UNKNOWN은 잔여를 남깁니다")
        return self


class SgAndTgMappingBackup(BaseModel):
    """`SAVE_CURRENT_SG_AND_TG_MAPPING` payload — `EC2_ISOLATE` 격리 직전 SG·TG 매핑.

    `RUNBOOK_EC2_UNISOLATE`가 읽는 값이다. 격리는 ENI의 SG를 격리 SG로 **덮어쓰므로**
    원래 SG는 조치 뒤 AWS 어디에도 남지 않는다 — 원복 값은 전부 여기 있어야 한다.

    필수 항목(ADR-0008 §5)
      - `security_group_ids` — **주 ENI(DeviceIndex 0)의** 원래 SG. 가드레일 ④
        `_precheck_unisolate`가 이 키를 읽어 DryRun·현존 확인을 한다(ADR-0007 §4).
      - `target_group_arn` — 재등록할 Target Group.
      - `target_port` — 등록 포트. **원복 값이다** — 없으면 비기본 포트로 등록돼 있던
        대상이 TG 기본 포트로 재등록돼 원상 복구가 아니다(ADR-0008 §참조 무결성).

    **신설: `network_interfaces`** — ENI가 여럿인 인스턴스의 ENI별 원래 SG. 격리는
    인스턴스의 **모든** ENI를 격리 SG로 바꾸므로(보조 ENI를 남기면 그쪽으로 트래픽이
    그대로 든다), 원복도 ENI 단위여야 한다. 주 ENI 하나의 목록(`security_group_ids`)만
    있으면 보조 ENI는 주 ENI의 SG로 "복원"되어 원본보다 넓거나 좁은 SG가 붙는다.
    순서는 DeviceIndex 오름차순이고 **첫 원소가 주 ENI다** — 아래 검증이 그 첫 원소와
    `security_group_ids`가 같은지 본다(두 자리가 다른 값을 말하면 원천이 둘이 된다).
    """

    model_config = ConfigDict(extra="forbid")

    security_group_ids: list[SecurityGroupId] = Field(min_length=1)
    target_group_arn: TargetGroupArn
    target_port: _TargetPort
    network_interfaces: list[EniSecurityGroups] = Field(min_length=1)
    # 보조 관측(SG/TG 원복 값 아님). None = 이 필드가 없는 옛 백업 — "모름"이며 보호되지
    # 않았다는 뜻이 아니다. 이 값이 어떻든 SG/TG 격리·해제 계약은 같다.
    scale_in_protection: ScaleInProtectionBackup | None = None

    @model_validator(mode="after")
    def _primary_matches_and_unique(self) -> SgAndTgMappingBackup:
        primary = self.network_interfaces[0]
        if list(primary.security_group_ids) != list(self.security_group_ids):
            raise ValueError(
                "security_group_ids는 주 ENI(network_interfaces[0])의 SG 목록과 같아야 합니다"
            )
        ids = [eni.network_interface_id for eni in self.network_interfaces]
        if len(ids) != len(set(ids)):
            raise ValueError("network_interfaces에 같은 ENI가 두 번 있습니다")
        return self

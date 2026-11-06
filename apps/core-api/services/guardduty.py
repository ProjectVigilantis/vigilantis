# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 고객 계정의 GuardDuty 켜기 — 연결 확인(tenant_verification.verify_connection)의 마지막 단계입니다.
# (T-14 B안 · #588 B)
#
#   - 리전 하나에 탐지기를 보고, 없으면 만든다(`CreateDetector(Enable=True)`). 있으면 상태만 본다.
#     GuardDuty 는 리전 서비스라 탐지기가 계정 · 리전마다 하나다. 리전을 도는 것은 호출부 몫이다.
#   - 정지된 탐지기는 다시 켜지 않는다(#588 Q1) — 고객이 일부러 정지했을 수 있고, 켜려면 `UpdateDetector`
#     쓰기 권한이 더 든다. 끄는 작업(`DeleteDetector` · `UpdateDetector`)은 부르지 않는다.
#   - 처음 켤 때 GuardDuty 가 서비스 연결 역할(AWSServiceRoleForAmazonGuardDuty)을 만든다 — 호출자에게
#     `iam:CreateServiceLinkedRole` 이 필요하다. 이 모듈은 IAM 을 직접 부르지 않는다.
#   - 부가 보호 기능(Features)은 넘기지 않는다 — 켜기 범위 밖이다(#588 「하지 않는 것」).
#   - 권한은 스모크 스크립트의 GuardDuty 정책(build_guardduty_policy)이 거울이다. 여기 호출을 더하면
#     tests/test_provision_smoke_aws.py 의 두 방향 대조가 정책을 따라오게 한다.
#   - AWS 오류는 삼키지 않는다 — 판정(묻지 못함 · 거절)은 tenant_verification 이 한다.
# ==============================================================================

from __future__ import annotations

from typing import Final

from botocore.exceptions import ClientError

from services.aws.client import session_client
from services.aws.errors import aws_error_code

#: 탐지기가 있지만 정지돼 있다 — AWS 오류가 아니라 우리가 내린 판정이라 따로 이름을 둔다.
DETECTOR_DISABLED_REASON: Final[str] = "DetectorDisabled"

_ENABLED: Final[str] = "ENABLED"


def _first_detector(guardduty) -> str | None:
    # 계정 · 리전마다 탐지기는 하나다. 첫 쪽에 없으면 없다.
    detector_ids = guardduty.list_detectors()["DetectorIds"]
    return detector_ids[0] if detector_ids else None


def ensure_detector(session, region: str) -> str | None:
    """그 리전의 탐지기를 켜진 상태로 둔다. 켜져 있으면 None, 켤 수 없으면 사유 코드.

    없으면 만든다. 동시에 들어온 다른 확인이 먼저 만들었으면 만들기가 거절되므로 한 번 더 본다."""
    guardduty = session_client(session, "guardduty", region)
    detector_id = _first_detector(guardduty)
    if detector_id is None:
        try:
            guardduty.create_detector(Enable=True)
            return None
        except ClientError as exc:
            # 「이미 있다」는 BadRequestException 이다. 그 밖의 거절은 그대로 올린다.
            if aws_error_code(exc) != "BadRequestException":
                raise
            detector_id = _first_detector(guardduty)
            if detector_id is None:
                raise
    status = guardduty.get_detector(DetectorId=detector_id)["Status"]
    return None if status == _ENABLED else DETECTOR_DISABLED_REASON

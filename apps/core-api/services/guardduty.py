# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 고객 계정의 GuardDuty 켜기 — 연결 확인(tenant_verification.verify_connection)의 마지막 단계입니다.
# (T-14 B안 · #588 B) 그리고 탐지 결과 읽기 — 테넌트 수집 주기가 리전마다 부릅니다(#588 C · read_findings).
#
#   - 리전 하나에 탐지기를 보고, 없으면 만든다(`CreateDetector(Enable=True)`). 있으면 상태만 본다.
#     GuardDuty 는 리전 서비스라 탐지기가 계정 · 리전마다 하나다. 리전을 도는 것은 호출부 몫이다.
#   - 정지된 탐지기는 다시 켜지 않는다(#588 Q1) — 고객이 일부러 정지했을 수 있고, 켜려면 `UpdateDetector`
#     쓰기 권한이 더 든다. 끄는 작업(`DeleteDetector` · `UpdateDetector`)은 부르지 않는다.
#   - 처음 켤 때 GuardDuty 가 서비스 연결 역할(AWSServiceRoleForAmazonGuardDuty)을 만든다 — 호출자에게
#     `iam:CreateServiceLinkedRole` 이 필요하다. 이 모듈은 IAM 을 직접 부르지 않는다.
#   - 만들 때 부가 보호 기능(Features)을 모두 `DISABLED` 로 넘긴다. 생략하면 꺼진 채 남지 않고
#     `RUNTIME_MONITORING` 을 뺀 부가 기능이 모두 켜진다(CreateDetector API 문서) — 켜기 범위 밖이고
#     (#588 「하지 않는 것」), 끄는 권한이 없어 플랫폼이 나중에 끌 수도 없다. 이 목록을 지우지 않는다.
#     `RUNTIME_MONITORING` 은 넣지 않는다 — `EKS_RUNTIME_MONITORING` 과 함께 넘기면 거절되고(같은 문서),
#     기본으로 켜지지 않는 유일한 기능이다. 이미 있는 탐지기는 건드리지 않는다.
#   - 권한은 고객 온보딩 템플릿(scripts/onboarding_template.py)과 스모크 스크립트의 GuardDuty 정책
#     (build_guardduty_policy)이 거울이다. 여기 호출을 더하면 tests/test_provision_smoke_aws.py ·
#     tests/test_onboarding_template.py 의 대조가 정책을 따라오게 한다.
#   - AWS 오류는 삼키지 않는다 — 판정(묻지 못함 · 거절)은 tenant_verification 이 한다.
#
# 결과 읽기(read_findings · #588 C)
#   - 연결 확인과 달리 수집 주기 안에서 부른다 — 세션을 받지 않고 tenant_scope 의 클라이언트(aws_client)를 쓴다.
#   - `ListFindings`(갱신 시각 ≥ 커서 · 이 계정 · 보관 처리 안 된 것, 갱신 시각 오름차순) → `GetFindings`(50건씩).
#     오름차순이라 상한(READ_LIMIT)에서 끊겨도 커서가 다음 회차에 이어 읽는다. 커서와 같은 시각은 다시 읽고
#     중복 억제가 접는다(같은 밀리초의 탐지를 잃지 않게 「초과」가 아니라 「이상」).
#   - 탐지기가 없으면 None — 읽을 것이 없다(켜기는 연결 확인의 몫이다). 정지된 탐지기는 그대로 읽는다.
#   - 계정 조건은 위임 관리자 계정이 멤버 계정 탐지까지 돌려주는 것을 막는다(정규화가 한 번 더 본다).
#   - 페이지네이터를 쓰지 않는다 — 권한 거울(tests/test_provision_smoke_aws.py _guardduty_calls)이 호출 이름을
#     소스에서 읽는다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Final

from botocore.exceptions import ClientError

from services.aws.client import aws_client, session_client
from services.aws.errors import aws_error_code

#: 탐지기가 있지만 정지돼 있다 — AWS 오류가 아니라 우리가 내린 판정이라 따로 이름을 둔다.
DETECTOR_DISABLED_REASON: Final[str] = "DetectorDisabled"

_ENABLED: Final[str] = "ENABLED"

#: 만들 때 끈 채로 넘기는 부가 보호 기능(botocore DetectorFeature 에서 RUNTIME_MONITORING 을 뺀 것).
DISABLED_FEATURES: Final[tuple[str, ...]] = (
    "S3_DATA_EVENTS",
    "EKS_AUDIT_LOGS",
    "EBS_MALWARE_PROTECTION",
    "RDS_LOGIN_EVENTS",
    "LAMBDA_NETWORK_LOGS",
    "EKS_RUNTIME_MONITORING",
    "AI_PROTECTION",
    "AI_ANALYST",
)


#: GetFindings 한 번에 넘길 수 있는 탐지 ID 수(API 상한).
GET_FINDINGS_MAX: Final[int] = 50
#: 한 리전 한 회차에 읽는 탐지 수 상한. 넘치면 커서가 다음 회차에 이어 읽는다.
READ_LIMIT: Final[int] = 500


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
            guardduty.create_detector(
                Enable=True,
                Features=[{"Name": name, "Status": "DISABLED"} for name in DISABLED_FEATURES],
            )
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


def _epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def read_findings(region: str, *, account_id: str, updated_since: datetime) -> list[dict] | None:
    """그 리전에서 updated_since 이후 갱신된 이 계정의 탐지(GetFindings 원문). 탐지기가 없으면 None.

    고르는 것은 갱신 시각 오름차순이지만 돌려주는 순서는 GetFindings 의 것이다. 최대 READ_LIMIT 건 — 남은 것은 다음 회차가 커서로 이어 읽는다."""
    guardduty = aws_client("guardduty", region)
    detector_id = _first_detector(guardduty)
    if detector_id is None:
        return None
    criteria = {"Criterion": {
        "updatedAt": {"GreaterThanOrEqual": _epoch_ms(updated_since)},
        "accountId": {"Equals": [account_id]},
        "service.archived": {"Equals": ["false"]},
    }}
    order = {"AttributeName": "updatedAt", "OrderBy": "ASC"}
    finding_ids: list[str] = []
    token = None
    while len(finding_ids) < READ_LIMIT:
        page = guardduty.list_findings(
            DetectorId=detector_id, FindingCriteria=criteria, SortCriteria=order,
            MaxResults=GET_FINDINGS_MAX, **({"NextToken": token} if token else {}),
        )
        finding_ids += page.get("FindingIds", [])
        token = page.get("NextToken")
        if not token:
            break
    finding_ids = finding_ids[:READ_LIMIT]
    findings: list[dict] = []
    for start in range(0, len(finding_ids), GET_FINDINGS_MAX):
        findings += guardduty.get_findings(
            DetectorId=detector_id, FindingIds=finding_ids[start:start + GET_FINDINGS_MAX],
        ).get("Findings", [])
    # GetFindings 는 순서를 지키지 않는다. 갱신 시각으로 다시 맞추는 것은 받는 쪽(guardduty_ingress)이다
    return findings

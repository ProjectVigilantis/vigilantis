"""SDK 재전송 뒤의 결과 분류 (Issue #434 2-1).

두 축을 따로 검증한다.
  ① **SDK 전송 동작** — 설치된 botocore가 읽기 제한 시간 뒤 같은 요청을 실제로 다시 보내고,
     마지막 ClientError의 ResponseMetadata에 RetryAttempts를 싣는가. 실제 boto3 클라이언트의
     before-send 훅으로 재현하며 네트워크에 나가지 않는다(이슈 본문의 재현 코드와 같다).
  ② **결과 분류** — 재시도가 있었던 오류는 4xx여도 "변경 없음"이 아니라 UNKNOWN이고, 그
     effect가 executor 단계 기록으로 이어져 종료 판정(실자산 대조)으로 가는가.

가짜(stub) 성공과 SDK 성공을 섞지 않는다 — ①이 깨지면 ②의 전제가 사라지므로 ①을 먼저 둔다.
"""

import sys
from pathlib import Path

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from services.aws import executor as ex  # noqa: E402

E = ExecutionEffect
S = ExecutionStepStatus

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
SG = "sg-0abc123456789def0"
SG_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{SG}"
ACL = "acl-0abc123456789def0"
ACL_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-acl/{ACL}"


def _error_body(code: str) -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8"?><Response><Errors><Error>'
        + f"<Code>{code}</Code><Message>{code}</Message></Error></Errors>".encode()
        + b"<RequestID>req-x</RequestID></Response>"
    )


class _Raw:
    def __init__(self, body: bytes):
        self.body = body

    def stream(self, **_):
        yield self.body


def lossy_client(error_code: str, *, status: int = 400, lost_first: int = 1):
    """첫 요청(들)은 "적용됐는데 응답이 유실된" 것으로, 그다음은 AWS 오류 응답으로 답하는
    실제 boto3 EC2 클라이언트. (클라이언트, 보낸 요청 목록) 짝."""
    client = boto3.client(
        "ec2",
        region_name=REGION,
        aws_access_key_id="x",
        aws_secret_access_key="x",
        config=Config(retries={"total_max_attempts": 5, "mode": "standard"}),
    )
    sent: list[str] = []

    def before_send(request, **_):
        sent.append(request.url)
        if len(sent) <= lost_first:
            raise ReadTimeoutError(endpoint_url=request.url)
        return AWSResponse(request.url, status, {}, _Raw(_error_body(error_code)))

    client.meta.events.register("before-send", before_send)
    return client, sent


# ------------------------------------------------------------------ ① SDK 전송


def test_sdk_resends_after_a_lost_response_and_reports_retry_attempts():
    """읽기 제한 시간 뒤 botocore는 같은 삭제 요청을 다시 보낸다 — 멱등성을 보지 않는다.

    두 번째 응답(NotFound)이 마지막 오류가 되고 RetryAttempts=1이 실린다. 이 값이 ②의 입력이다.
    """
    client, sent = lossy_client("InvalidGroup.NotFound")

    with pytest.raises(ClientError) as raised:
        client.delete_security_group(GroupId=SG)

    assert len(sent) == 2
    meta = raised.value.response["ResponseMetadata"]
    assert raised.value.response["Error"]["Code"] == "InvalidGroup.NotFound"
    assert (meta["HTTPStatusCode"], meta["RetryAttempts"]) == (400, 1)


def test_first_attempt_error_carries_zero_retry_attempts():
    """재전송이 없었으면 RetryAttempts=0이다 — 이 경우만 마지막 응답이 유일한 사실이다."""
    client, sent = lossy_client("DependencyViolation", lost_first=0)

    with pytest.raises(ClientError) as raised:
        client.delete_security_group(GroupId=SG)

    assert len(sent) == 1
    assert raised.value.response["ResponseMetadata"]["RetryAttempts"] == 0


# ------------------------------------------------------------------ ② 결과 분류


def client_error(code: str, *, status: int = 400, retries: int = 0) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status, "RetryAttempts": retries},
        },
        "Op",
    )


@pytest.mark.parametrize(
    "exc",
    [
        client_error("InvalidGroup.NotFound", retries=1),  # 삭제가 적용된 뒤의 재전송
        client_error("NetworkAclEntryAlreadyExists", retries=1),  # 삽입이 적용된 뒤의 재전송
        client_error("RequestLimitExceeded", status=503, retries=2),  # 적용 뒤 재전송이 스로틀링
        client_error("IncorrectInstanceState", retries=1),
    ],
)
def test_error_after_a_retry_is_unknown_not_not_applied(exc):
    """재시도가 있었던 오류는 마지막 응답 하나로 '변경 없음'을 단정할 수 없다 — UNKNOWN."""
    assert ex._effect_for(exc) is E.UNKNOWN


@pytest.mark.parametrize(
    "exc",
    [
        client_error("InvalidGroup.NotFound"),
        client_error("NetworkAclEntryAlreadyExists"),
        client_error("RequestLimitExceeded", status=503),
        client_error("DependencyViolation"),
    ],
)
def test_first_attempt_rejection_is_still_not_applied(exc):
    """재전송이 없었던 4xx·스로틀링은 기존대로 작업 이전 반려다 — NOT_APPLIED."""
    assert ex._effect_for(exc) is E.NOT_APPLIED


def test_retry_attempts_reader_tolerates_missing_metadata():
    assert ex.retry_attempts({}) == 0
    assert ex.retry_attempts({"ResponseMetadata": {}}) == 0
    assert ex.retry_attempts({"ResponseMetadata": {"RetryAttempts": "1"}}) == 0
    assert ex.retry_attempts({"ResponseMetadata": {"RetryAttempts": 3}}) == 3
    assert ex.retry_attempts(None) == 0


# ------------------------------------------ ①+② SDK가 낸 오류가 단계 기록으로 이어진다


def test_sg_delete_after_lost_response_is_recorded_unknown(monkeypatch):
    """적용 뒤 응답 유실 → 재전송 NotFound. 단계는 FAILED/UNKNOWN이라 실자산 판정으로 간다.

    전에는 NOT_APPLIED("변경 없이 실패")로 확정돼 실제로 지워진 SG의 재생성 경로가 닫혔다.
    """
    client, _sent = lossy_client("InvalidGroup.NotFound")
    monkeypatch.setattr(ex, "aws_client", lambda service, region=None, **_: client)
    recorded = []

    outcome = ex.execute_sg_delete_isolated(SG_ARN, record_step=recorded.append)

    assert not outcome.succeeded
    [step] = [s for s in recorded if s.status is not S.IN_PROGRESS]
    assert (step.step_type, step.status, step.effect) == (
        ex.STEP_DELETE_SECURITY_GROUP,
        S.FAILED,
        E.UNKNOWN,
    )
    assert "InvalidGroup.NotFound" in step.error_summary
    assert step.aws_request_id == "req-x"


def test_nacl_deny_after_lost_response_is_recorded_unknown(monkeypatch):
    """적용 뒤 응답 유실 → 재전송 AlreadyExists. deny가 실제로 들어가 있을 수 있다 — UNKNOWN."""
    client, _sent = lossy_client("NetworkAclEntryAlreadyExists")
    monkeypatch.setattr(ex, "aws_client", lambda service, region=None, **_: client)
    recorded = []

    outcome = ex.execute_nacl_add_deny(
        ACL_ARN,
        rule_number=100,
        cidr_block="203.0.113.10/32",
        protocol="tcp",
        record_step=recorded.append,
    )

    assert not outcome.succeeded
    [step] = [s for s in recorded if s.status is not S.IN_PROGRESS]
    assert (step.status, step.effect) == (S.FAILED, E.UNKNOWN)


def test_sg_delete_rejected_on_the_first_attempt_stays_not_applied(monkeypatch):
    """재전송 없는 DependencyViolation은 기존대로 '변경 없이 실패'다 — 회귀 방지."""
    client, _sent = lossy_client("DependencyViolation", lost_first=0)
    monkeypatch.setattr(ex, "aws_client", lambda service, region=None, **_: client)
    recorded = []

    ex.execute_sg_delete_isolated(SG_ARN, record_step=recorded.append)

    [step] = [s for s in recorded if s.status is not S.IN_PROGRESS]
    assert (step.status, step.effect) == (S.FAILED, E.NOT_APPLIED)

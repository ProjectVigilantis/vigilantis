"""AI 후보 → 가드레일 ④ AWS Dry-Run 경계의 LocalStack 통합 테스트. (Issue #285)

`workflows._candidate_precheck`가 이 카드로 처음 생긴 **프로덕션 AI_CANDIDATE 배선**이다
(그전까지 executor.precheck을 부르는 곳은 원복 경로 하나였다). 그 함수가 하는 일은 셋이고
셋 다 실물이 있어야 확인된다 — target_arn 해석, 조회로 채우는 값(RIGHTSIZING의
current_instance_type), 실행 파라미터 조립.

test_agent_dispatcher.py는 이 함수를 대역으로 바꿔 오케스트레이션만 본다. 판정 분기 전수는
services/tests/test_precheck_dispatch.py가, 실물 판정은 test_precheck_localstack.py가 맡는다.
여기서 보는 것은 **후보 계약의 값이 실행 파라미터로 옮겨져 실제로 판정까지 가는가** 하나다.

로컬 실행 전제: LocalStack 기동 + scripts/seed_localstack.py 완료. 미기동 시 전체 skip.
"""

import os
import sys
import urllib.request
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.setdefault("AWS_ENDPOINT_URL", ENDPOINT)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import workflows  # noqa: E402
from schemas.agents import RunbookCandidateDraft  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws.client import account_id, aws_client, default_region  # noqa: E402


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


@pytest.fixture(scope="module")
def fx():
    ec2 = aws_client("ec2")
    region, account = default_region(), account_id()

    def arn(resource_type, resource_id):
        return f"arn:aws:ec2:{region}:{account}:{resource_type}/{resource_id}"

    instance = next(
        i
        for r in ec2.describe_instances()["Reservations"]
        for i in r["Instances"]
        if i["State"]["Name"] == "running"
    )
    volume = ec2.describe_volumes(
        Filters=[{"Name": "status", "Values": ["available"]}]
    )["Volumes"][0]["VolumeId"]
    return {
        "arn": arn,
        "instance_id": instance["InstanceId"],
        "instance_type": instance["InstanceType"],
        "volume": volume,
    }


def _draft(runbook_id: str, target_arn: str, parameters: dict) -> RunbookCandidateDraft:
    """그래프가 내는 후보와 같은 모양 — evidence_ids 첫 항목이 evidence_id가 된다."""
    return RunbookCandidateDraft.model_validate(
        {
            "runbook_id": runbook_id,
            "target_arn": target_arn,
            "parameters": parameters,
            "evidence_ids": ["3f5b8c1e-0000-4000-8000-000000000001"],
        }
    )


def test_rightsizing_candidate_reaches_a_pass_judgement(fx):
    """조회로 채우는 current_instance_type이 붙어야 실행 파라미터가 선다."""
    outcome = workflows._candidate_precheck(
        _draft(
            "RUNBOOK_EC2_RIGHTSIZING",
            fx["arn"]("instance", fx["instance_id"]),
            {"target_instance_type": "t3.medium"},
        )
    )

    assert outcome.passed is True, outcome.verification_summary
    assert outcome.reason_code is None
    assert outcome.verification_summary.startswith("DRY_RUN")


def test_candidate_without_lookups_reaches_a_pass_judgement(fx):
    """조회값이 없는 런북은 후보 값과 대상 자원 ID만으로 실행 파라미터가 선다."""
    outcome = workflows._candidate_precheck(
        _draft("RUNBOOK_EBS_DELETE_UNATTACHED", fx["arn"]("volume", fx["volume"]), {})
    )

    assert outcome.passed is True, outcome.verification_summary
    assert outcome.reason_code is None


def test_missing_instance_is_a_judgement_not_an_exception(fx):
    """조회 실패는 배선 오류가 아니라 AWS 판정이다 — 예외로 새면 거절이 기록되지 않는다."""
    outcome = workflows._candidate_precheck(
        _draft(
            "RUNBOOK_EC2_RIGHTSIZING",
            fx["arn"]("instance", "i-0ffffffffffffffff"),
            {"target_instance_type": "t3.medium"},
        )
    )

    assert outcome.passed is False
    assert outcome.reason_code is PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND


def test_unparseable_target_arn_is_a_judgement_not_an_exception():
    """③ ARN Match가 먼저 거르는 방어적 경로 — 그래도 예외로 새지 않는다."""
    outcome = workflows._candidate_precheck(
        _draft("RUNBOOK_EC2_RIGHTSIZING", "not-an-arn", {"target_instance_type": "t3.medium"})
    )

    assert outcome.passed is False
    assert outcome.reason_code is PrecheckReasonCode.PRECHECK_PARAM_INVALID

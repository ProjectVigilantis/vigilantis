# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 실행 계열 하네스의 **LocalStack 실물 검증**입니다. (Issue #136)
#
# ── 이 파일이 하지 않는 것 (중복 방지) ──────────────────────────────────────
# precheck 판정 자체는 보지 않는다. 이미 두 파일이 전수로 덮고 있다.
#   apps/core-api/services/tests/test_precheck_dispatch.py (김세혁) — boto3 스텁 단위 전수
#   apps/core-api/services/tests/test_precheck_localstack.py (김세혁, #129 / PR #147)
#     — 실물 LocalStack 통합. P0 7종 통과 + **P2 3종 `PRECHECK_AWS_ERROR` 실측까지 이미 있다**
#     (`test_isolate_fails_locally_on_the_pro_only_service` 외 2건).
#   `backup_loader` 미배선 RuntimeError 도 test_precheck_dispatch.py:397 이 덮는다.
#
# ── 이 파일이 하는 것 ────────────────────────────────────────────────────────
# **하네스가 실물에서 도는가**만 본다. conftest 의 `seeded_*` 픽스처와
# execution_harness 의 P2 표·FakeBackupLoader 가 실제 LocalStack 을 상대로 성립하는지다.
# 이게 없으면 그 픽스처들은 아무 테스트도 쓰지 않는 죽은 코드로 머지된다.
#
# LocalStack 이 없으면 통째로 skip 된다. 떠 있는데 시드 자산만 없으면 FAIL 이다 —
# 그건 환경 미구성이 아니라 시드와 테스트가 어긋난 것이라서다.
#
# 실행 방법:
#   docker compose up -d localstack
#   AWS_ENDPOINT_URL=http://localhost:4566 uv run python scripts/seed_localstack.py
#   uv run pytest tests/test_execution_harness_localstack.py -q
# ==============================================================================

from __future__ import annotations

import os
import urllib.request

import pytest

# 엔드포인트 해석은 팀 선례를 그대로 따른다 — services/tests/test_collector_raw.py:24 와
# services/tests/test_precheck_localstack.py:23 이 같은 두 줄을 쓴다. CI 의 pytest 스텝에는
# AWS_ENDPOINT_URL 이 걸려 있지 않고(시드 스텝에만 있다), 이 두 줄이 그 자리를 메운다.
# ADR-0006 §3(단일 스위치)과 어긋나지 않는다: 스위치 값을 새로 만드는 것이 아니라
# **기본값을 채운 뒤 실제 기동 여부로만 판단**하며, 값이 이미 있으면 건드리지 않는다.
ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.setdefault("AWS_ENDPOINT_URL", ENDPOINT)

from execution_harness import (  # noqa: E402
    P2_LOCAL_FAIL_CASES,
    P2_LOCAL_FAIL_RUNBOOKS,
    arn_for,
    make_backup_loader,
)
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402
from services.aws import executor as ex  # noqa: E402

# 시드가 만드는 인스턴스 이름(scripts/seed_localstack.py INSTANCES).
# idle-dev 를 쓰는 이유: non-prod 대형 타입이라 RIGHTSIZING 후보로 성립하는 유일한 한 대다.
SEED_TARGET = "vigilantis-seed-idle-dev"

SEED_INSTANCE_NAMES = {
    "vigilantis-seed-idle",
    "vigilantis-seed-normal",
    "vigilantis-seed-spike",
    "vigilantis-seed-idle-dev",
}

_P2_RUNBOOKS = sorted(P2_LOCAL_FAIL_RUNBOOKS, key=lambda r: r.value)


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason=f"LocalStack({ENDPOINT}) 미기동 — 하네스 실물 검증 skip"
)


@pytest.fixture()
def p2_precheck_params(seeded_instance, seeded_account_id, seeded_region):
    """P2 런북별 (target_arn, parameters, backup_loader) — 시드 실물 ID 로 조립한다.

    예시 ARN 을 쓰지 않는 이유: precheck 가 실제로 AWS 를 조회하므로 존재하지 않는
    인스턴스면 P2 여부와 무관하게 조회 실패로 떨어져, 무엇이 검증된 것인지 갈린다.
    """

    def _build(runbook_id: RunbookId):
        instance = seeded_instance(SEED_TARGET)
        target_arn = instance.arn(seeded_account_id, seeded_region)
        target_group_arn = (
            f"arn:aws:elasticloadbalancing:{seeded_region}:{seeded_account_id}"
            ":targetgroup/vigilantis-tg/0123456789abcdef"
        )
        group_id = instance.security_group_ids[0]

        if runbook_id is RunbookId.RUNBOOK_EC2_ISOLATE:
            return target_arn, {
                "instance_id": instance.instance_id,
                "target_group_arn": target_group_arn,
                "isolation_group_id": group_id,
                "evidence_id": "ev-1",
            }, None
        if runbook_id is RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING:
            return target_arn, {
                "instance_id": instance.instance_id,
                "min_size": 1,
                "max_size": 2,
                "evidence_id": "ev-1",
            }, None
        if runbook_id is RunbookId.RUNBOOK_EC2_UNISOLATE:
            loader, record = make_backup_loader(
                runbook_id,
                target_arn,
                payload={
                    "security_group_ids": [group_id],
                    "target_group_arn": target_group_arn,
                },
            )
            return target_arn, {
                "instance_id": instance.instance_id,
                "backup_record_id": record.backup_record_id,
                "evidence_id": "ev-1",
            }, loader
        raise AssertionError(f"P2 런북이 아니다 — {runbook_id.value}")

    return _build


# ------------------------------------------------------------------ 시드 조회 픽스처
def test_seed_provides_every_instance_the_scenarios_need(seeded_instances):
    """시드 인스턴스 4대가 태그 조회로 전부 잡힌다.

    이름이 아니라 `vigilantis:seed` 태그로 찾으므로, 시드가 이름을 바꿔도 조회는 살아 있고
    **여기서만 이름 대조가 깨진다.** 조회 자체가 죽는 것보다 어디가 어긋났는지 드러난다.
    """
    assert set(seeded_instances) == SEED_INSTANCE_NAMES


def test_seeded_instance_arn_round_trips_through_the_parser(
    seeded_instance, seeded_account_id, seeded_region
):
    """하네스가 만든 ARN 을 executor.parse_arn 이 그대로 되읽는다.

    가드레일 ③ ARN Match(#177)가 대조할 문자열이 이것이라, 형식이 어긋나면 ③ 구현 후
    하네스를 쓰는 테스트가 통째로 거절된다. 계정·리전도 **실행 환경 실측값**이어야 한다 —
    스키마 조립용 예시 계정(123456789012)을 실물 테스트에 흘리면 ③에서 걸린다.
    """
    instance = seeded_instance(SEED_TARGET)
    target_arn = instance.arn(seeded_account_id, seeded_region)
    parsed = ex.parse_arn(target_arn)

    assert parsed is not None
    assert (parsed.resource_type, parsed.resource_id) == ("instance", instance.instance_id)
    assert (parsed.account_id, parsed.region) == (seeded_account_id, seeded_region)
    assert target_arn == arn_for(
        "instance", instance.instance_id, account_id=seeded_account_id, region=seeded_region
    )


# ------------------------------------------------------------------ P2 표 ↔ 실측 결속
@pytest.mark.parametrize("runbook_id", _P2_RUNBOOKS, ids=lambda r: r.value)
def test_p2_table_missing_service_appears_in_the_measured_summary(
    runbook_id, p2_precheck_params, p2_local_fail_expected
):
    """하네스 P2 표의 `missing_service` 가 실제 거절 요약에 등장한다.

    그 값은 손으로 적은 것이라 실물과 갈릴 수 있다. 판정(passed·reason_code) 자체는
    services/tests/test_precheck_localstack.py 가 이미 보므로 여기서 다시 단언하지 않고,
    **표를 실측에 묶는 일만** 한다.
    """
    if not p2_local_fail_expected(runbook_id):
        pytest.skip("실 AWS 모드 — P2 3종은 통과해야 하므로 이 전제가 적용되지 않는다")

    expected = {
        case.missing_service for case in P2_LOCAL_FAIL_CASES if case.runbook_id is runbook_id
    }
    target_arn, params, loader = p2_precheck_params(runbook_id)
    summary = ex.precheck(runbook_id, target_arn, params, backup_loader=loader).verification_summary

    assert any(service in summary for service in expected), (
        f"{runbook_id.value} 요약에 {expected} 가 없다 — 하네스 표가 낡았다: {summary}"
    )


def test_fake_backup_loader_satisfies_the_real_precheck_path(
    p2_precheck_params, p2_local_fail_expected
):
    """FakeBackupLoader 가 실제 executor 조회 경로에서 레코드를 내준다.

    로더가 제 역할을 못 하면 판정이 `PRECHECK_PARAM_INVALID`(백업 레코드 없음)로 **먼저**
    떨어져, elbv2 부재까지 가지 못한다. 즉 `PRECHECK_AWS_ERROR` 에 도달했다는 것 자체가
    로더가 Protocol 을 만족했다는 증거다 — 대역의 계약 준수를 실물로 확인하는 자리다.
    """
    runbook_id = RunbookId.RUNBOOK_EC2_UNISOLATE
    if not p2_local_fail_expected(runbook_id):
        pytest.skip("실 AWS 모드 — 이 판정 경로가 성립하지 않는다")

    target_arn, params, loader = p2_precheck_params(runbook_id)
    outcome = ex.precheck(runbook_id, target_arn, params, backup_loader=loader)

    assert outcome.reason_code is not PrecheckReasonCode.PRECHECK_PARAM_INVALID, (
        f"백업 레코드 조회 단계에서 떨어졌다 — FakeBackupLoader 가 계약을 못 맞춘다: "
        f"{outcome.verification_summary}"
    )

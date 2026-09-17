# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# assets_repo.find_dangling_arns 회귀 — 조인 키가 assets.arn 과 어긋나거나 자산 리전이
# 자기 ARN 과 다르면 잡는다. (Issue #342)
#
# 이 파일이 지키는 핵심은 **매달렸다는 사실과 이상하다는 판단을 가르는 것**이다(#353 리뷰).
# 위협 접수는 미등록 대상도 받고 가드레일 ③ 은 거절한 후보를 기록으로 남긴다 — 그 자리는
# 매달린 것이 정상이라, 같은 통에 담으면 정상 보존분이 매 회차 같은 경고로 반복되며 새
# 어긋남을 덮는다. 집계 단위가 행이 아니라 **(종류, ARN)** 이라는 것도 함께 잠근다.
# ==============================================================================

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import models  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from schemas.api.assets import AssetType, RelationType  # noqa: E402
from schemas.api.incidents import IncidentCategory, RiskLevel  # noqa: E402
from schemas.collections import CollectionRunStatus  # noqa: E402
from schemas.events import ThreatEventType  # noqa: E402
from schemas.guardrails import (  # noqa: E402
    GuardrailDecision,
    GuardrailStep,
    GuardrailValidationContext,
)
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402

NOW = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)

UNMANAGED = "arn:aws:ec2:ap-northeast-2:1:instance/i-not-ours"


def _run(db, region="ap-northeast-2"):
    return assets_repo.start_collection_run(
        db, account_id="1", region=region, mode="localstack",
        lookback_days=14, period_seconds=3600,
    )


def _asset(db, arn, region, asset_type=AssetType.EC2):
    return assets_repo.upsert_asset(
        db, arn=arn, asset_type=asset_type, resource_id=arn.rsplit("/", 1)[-1],
        account_id="1", region=region, spec={}, collection_run_id=_run(db, region).collection_run_id,
        collected_at=NOW,
    )


def _incident(db, arn, category):
    # SECOPS 는 위협 이름·초기 위험도·사유 코드 1개 이상이 필수이고 FINOPS 는 그 축이 전부
    # null 이어야 한다(models.py ck_incidents_category_risk_shape).
    secops = category is IncidentCategory.SECOPS
    inc = models.Incident(
        subject_arn=arn,
        category=category,
        title="SSH 무차별 대입" if secops else None,
        initial_risk_level=RiskLevel.HIGH if secops else None,
        initial_risk_reason_codes=["SSH_BRUTE_FORCE"] if secops else [],
    )
    db.add(inc)
    db.flush()
    return inc


def _candidate(db, incident, arn, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY):
    # 한 Incident 에 같은 런북 후보를 둘 둘 수 없다(uq_runbook_candidates_active).
    cand = models.RunbookCandidate(
        incident_id=incident.incident_id, runbook_id=runbook_id,
        target_arn=arn, parameters={},
    )
    db.add(cand)
    db.flush()
    return cand


def _guardrail(db, candidate, failed_step):
    """가드레일 평가 1건. `failed_step=None` 이면 네 단계 전체 PASS 다."""
    db.add(models.GuardrailEvaluation(
        validation_context=GuardrailValidationContext.AI_CANDIDATE,
        candidate_id=candidate.candidate_id,
        result=GuardrailDecision.PASS if failed_step is None else GuardrailDecision.FAIL,
        failed_step=failed_step, steps=[], validated_at=NOW,
    ))
    db.flush()


def _kinds(found):
    return {f.kind for f in found}


def _of_kind(found, kind):
    return [f for f in found if f.kind == kind]


def test_consistent_assets_and_relationships_have_no_findings(db):
    """리전 정합 자산 + 그 자산을 정확히 가리키는 관계 → 0건."""
    a = _asset(db, "arn:aws:ec2:ap-northeast-2:1:instance/i-ok", "ap-northeast-2")
    sg = _asset(db, "arn:aws:ec2:ap-northeast-2:1:security-group/sg-ok", "ap-northeast-2",
                AssetType.SG)
    db.add(models.AssetRelationship(
        source_asset_id=a.asset_id, relation_type=RelationType.SECURED_BY.value,
        target_arn=sg.arn,
    ))
    db.flush()
    assert assets_repo.find_dangling_arns(db) == []


def test_preserved_observations_do_not_count_as_investigation(db):
    """미등록 대상 관측을 **정상 보존**한 것은 조사 대상에서 빠진다(#353 리뷰: 안성일).

    안성일 님이 실측한 사례 그대로다 — 미등록 대상의 위협 1건을 정상 접수하면
    `ThreatEvent` 와 `Incident` 가 각각 남고, 종전 구현은 그 둘을 매 회차 같은 경고로
    올렸다. 위협 접수는 미등록 대상도 받는 자리이므로 이것은 어긋남이 아니다.
    """
    db.add(models.ThreatEvent(
        source_event_id="evt-1", event_type=ThreatEventType.SSH_BRUTE_FORCE,
        target_arn=UNMANAGED, payload={}, deduplication_key="dedup-1", occurred_at=NOW,
    ))
    _incident(db, UNMANAGED, IncidentCategory.SECOPS)
    db.flush()

    found = assets_repo.find_dangling_arns(db)

    # 대상이 하나이므로 ARN 1건으로 묶이고, 보인 자리 둘은 상세에 남는다
    assert _kinds(found) == {assets_repo.KIND_UNMANAGED_OBSERVATION}
    assert len(found) == 1
    assert set(found[0].sources) == {"threat_events.target_arn", "incidents.subject_arn"}

    summary = assets_repo.summarize_dangling(found)
    assert summary["total"] == 1
    assert summary["investigate"] == 0  # 경고로 올리지 않는다


def test_a_real_missing_reference_is_separated_from_preserved_observations(db):
    """정상 보존분과 **실제 참조 누락**이 같은 점검에서 갈린다 — 집계가 구분된다.

    관계는 수집이 만든 자산끼리의 연결이라 대상이 없으면 조립이 어긋난 것이고, FINOPS
    Incident 는 등록 자산의 판정에서만 생기므로 매달릴 수 없다.
    """
    db.add(models.ThreatEvent(
        source_event_id="evt-1", event_type=ThreatEventType.SSH_BRUTE_FORCE,
        target_arn=UNMANAGED, payload={}, deduplication_key="dedup-1", occurred_at=NOW,
    ))
    a = _asset(db, "arn:aws:ec2:ap-northeast-2:1:instance/i-ok", "ap-northeast-2")
    db.add(models.AssetRelationship(
        source_asset_id=a.asset_id, relation_type=RelationType.SECURED_BY.value,
        target_arn="arn:aws:ec2:ap-northeast-2:1:security-group/sg-missing",
    ))
    _incident(db, "arn:aws:ec2:ap-northeast-2:1:instance/i-gone", IncidentCategory.FINOPS)
    db.flush()

    found = assets_repo.find_dangling_arns(db)
    broken = _of_kind(found, assets_repo.KIND_BROKEN_REFERENCE)

    assert {f.value for f in broken} == {
        "arn:aws:ec2:ap-northeast-2:1:security-group/sg-missing",
        "arn:aws:ec2:ap-northeast-2:1:instance/i-gone",
    }
    assert len(_of_kind(found, assets_repo.KIND_UNMANAGED_OBSERVATION)) == 1

    summary = assets_repo.summarize_dangling(found)
    assert summary["total"] == 3
    assert summary["investigate"] == 2  # 보존한 관측 1건은 빠진다


@pytest.mark.parametrize(
    "failed_step",
    [GuardrailStep.SCHEMA_CHECK, GuardrailStep.ACTION_WHITELIST, GuardrailStep.ARN_MATCH],
)
def test_candidate_not_past_arn_match_is_expected_but_others_investigate(db, failed_step):
    """후보는 **상태가 아니라 가드레일 ③ 을 통과했는지**로 가른다(#353 리뷰·재리뷰: 안성일).

    - ①·②·③ 중 어디서 넘어졌든 ③ 은 그 대상을 관리 자산으로 인정한 적이 없다 — 실패
      단계 뒤는 전부 NOT_RUN 이다. 가드레일이 제대로 거절한 기록이니 **정상 보존**이다.
      종전 구현은 ③ 실패만 이렇게 보고 ①·②에서 거절된 후보를 조사 대상으로 올렸다.
    - ③ 을 통과한 뒤 ④(DryRun)에서 거절된 후보가 매달렸다면 **어긋남**이다. `REJECTED`
      전체를 정상으로 치면 이것까지 빠진다.
    - 네 단계를 전부 통과한 후보가 매달렸다면 ③ 이 인정한 참조가 끊긴 것이니 **조사 대상**이다.
    - 평가 기록이 없는 후보는 판단할 근거가 없으니 **조사 대상**으로 남긴다.
    """
    inc = _incident(db, UNMANAGED, IncidentCategory.SECOPS)
    rejected_early = _candidate(db, inc, UNMANAGED)
    _guardrail(db, rejected_early, failed_step)

    passed_three = "arn:aws:ec2:ap-northeast-2:1:instance/i-passed-three"
    later = _candidate(db, inc, passed_three, runbook_id=RunbookId.RUNBOOK_NACL_RESTORE)
    _guardrail(db, later, GuardrailStep.AWS_DRY_RUN)

    all_passed = "arn:aws:ec2:ap-northeast-2:1:instance/i-all-passed"
    passed_all = _candidate(db, inc, all_passed, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE)
    _guardrail(db, passed_all, None)

    unevaluated = "arn:aws:ec2:ap-northeast-2:1:instance/i-no-evaluation"
    _candidate(db, inc, unevaluated, runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED)
    db.flush()

    found = assets_repo.find_dangling_arns(db)

    rejected = _of_kind(found, assets_repo.KIND_GUARDRAIL_REJECTED)
    assert [f.value for f in rejected] == [UNMANAGED]

    broken = _of_kind(found, assets_repo.KIND_BROKEN_REFERENCE)
    assert {f.value for f in broken} == {passed_three, all_passed, unevaluated}


def test_execution_axis_counts_one_arn_once_and_always_investigates(db):
    """실행·단계·백업 3컬럼은 **한 값의 사본 3개**다 — 1건으로 센다(#353 리뷰: 김세혁).

    행으로 세면 어긋남 1건이 3건으로 부풀어 요약의 숫자가 "어긋난 대상 수"도 "어긋난
    행 수"도 아니게 된다. 그리고 이 축은 가드레일 ③ 을 통과한 후보나 앞선 원본 실행에서만
    나오므로, 나오면 예외 없이 버그다 — 관측·제안 축과 다른 통에 담는다.
    """
    inc = _incident(db, UNMANAGED, IncidentCategory.SECOPS)
    execution = models.ActionExecution(
        incident_id=inc.incident_id, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
        target_arn=UNMANAGED, trigger_source=TriggerSource.USER_APPROVAL,
    )
    db.add(execution)
    db.flush()
    db.add(models.ExecutionStep(
        execution_id=execution.execution_id, sequence=1, affected_arn=UNMANAGED,
        step_type="APPLY", aws_operation="CreateNetworkAclEntry",
    ))
    db.add(models.BackupRecord(
        execution_id=execution.execution_id, target_arn=UNMANAGED,
        backup_type="SAVE_NACL_RULE_INDEX", payload={},
    ))
    db.flush()

    found = assets_repo.find_dangling_arns(db)
    axis = _of_kind(found, assets_repo.KIND_EXECUTION_INTEGRITY)

    assert len(axis) == 1  # 3행이 아니라 대상 1건
    assert set(axis[0].sources) == {
        "action_executions.target_arn",
        "execution_steps.affected_arn",
        "backup_records.target_arn",
    }
    assert assets_repo.KIND_EXECUTION_INTEGRITY in assets_repo.INVESTIGATE_KINDS


def test_relation_to_an_unobserved_type_is_explained_by_a_degraded_run(db):
    """그 회차가 대상 유형을 관측하지 못했으면 누락이 아니라 **미관측**이다.

    LocalStack Community 는 `autoscaling`·`elbv2` 가 라이선스 밖이라 회차가 매번 PARTIAL 로
    끝난다(ADR-0006). 그 조건에서 만들어진 ASG 관계를 누락으로 세면 팀 표준 환경에서
    조사 대상이 매 회차 올라온다. 반대로 SUCCESS 로 끝난 회차의 같은 관계는 누락이다.
    """
    a = _asset(db, "arn:aws:ec2:ap-northeast-2:1:instance/i-ok", "ap-northeast-2")
    degraded = _run(db)
    degraded.status = CollectionRunStatus.PARTIAL
    clean = _run(db)
    clean.status = CollectionRunStatus.SUCCESS
    db.flush()

    db.add(models.AssetRelationship(
        source_asset_id=a.asset_id, relation_type=RelationType.MEMBER_OF.value,
        target_arn="arn:aws:autoscaling:ap-northeast-2:1:autoScalingGroup/asg-unseen",
        collection_run_id=degraded.collection_run_id,
    ))
    db.add(models.AssetRelationship(
        source_asset_id=a.asset_id, relation_type=RelationType.SECURED_BY.value,
        target_arn="arn:aws:ec2:ap-northeast-2:1:security-group/sg-missing",
        collection_run_id=clean.collection_run_id,
    ))
    db.flush()

    found = assets_repo.find_dangling_arns(db)

    unobserved = _of_kind(found, assets_repo.KIND_UNOBSERVED_RELATION)
    assert [f.value for f in unobserved] == [
        "arn:aws:autoscaling:ap-northeast-2:1:autoScalingGroup/asg-unseen"
    ]
    assert assets_repo.KIND_UNOBSERVED_RELATION not in assets_repo.INVESTIGATE_KINDS

    broken = _of_kind(found, assets_repo.KIND_BROKEN_REFERENCE)
    assert [f.value for f in broken] == [
        "arn:aws:ec2:ap-northeast-2:1:security-group/sg-missing"
    ]


def test_region_mismatch_is_its_own_kind(db):
    """자산 region 컬럼이 자기 ARN 의 리전과 다르면 매달림과 **별개 종류**로 잡힌다 —
    #261 리전 스코프가 둘을 따로 읽어 어긋나면 필터에서 새거나 빠진다."""
    _asset(db, "arn:aws:ec2:us-east-1:1:instance/i-bad", "ap-northeast-2")  # region≠arn
    found = assets_repo.find_dangling_arns(db)
    mism = _of_kind(found, assets_repo.KIND_REGION_MISMATCH)
    assert [f.value for f in mism] == ["arn:aws:ec2:us-east-1:1:instance/i-bad"]
    assert mism[0].detail == "region=ap-northeast-2"
    assert assets_repo.KIND_REGION_MISMATCH in assets_repo.INVESTIGATE_KINDS


def test_region_mismatch_skips_assets_marked_absent(db):
    """소멸 표시된 자산의 리전 불일치는 올리지 않는다(#353 nit 3: 김세혁).

    #261 이 말하는 해는 살아 있는 자산에서만 생기고(`list_assets` 가 소멸분을 기본
    제외하며 판정이 그 목록을 쓴다), 다시 관측되면 `upsert_asset` 이 region 과
    absent_since 를 함께 다시 쓴다. 남겨 두면 고칠 수도 사라지지도 않는 경고가 된다.
    """
    asset = _asset(db, "arn:aws:ec2:us-east-1:1:instance/i-gone-bad", "ap-northeast-2")
    assert _of_kind(assets_repo.find_dangling_arns(db), assets_repo.KIND_REGION_MISMATCH)

    asset.absent_since = NOW
    db.flush()
    assert _of_kind(assets_repo.find_dangling_arns(db), assets_repo.KIND_REGION_MISMATCH) == []


def test_optional_types_match_the_collector_map():
    """미관측 판정에 쓰는 유형 집합이 수집기의 지도와 어긋나지 않는다.

    원천은 collector 의 `_UNOBSERVED_TYPES_BY_FAILURE` 다(#332 의 fail-closed 지도).
    누가 흡수 조회를 새로 더하고 한쪽만 고치면 미관측이 누락으로, 또는 그 반대로 뒤집힌다.
    """
    from services.collector import _UNOBSERVED_TYPES_BY_FAILURE

    from_collector = {t for types in _UNOBSERVED_TYPES_BY_FAILURE.values() for t in types}
    assert from_collector == {t.value for t in assets_repo._COLLECTION_OPTIONAL_TYPES}

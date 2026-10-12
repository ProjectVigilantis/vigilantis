"""EC2_ISOLATE 시스템 공급 값 — workflows.resolve_isolation_parameters. (MVP 체크리스트 §2-1)

`isolation_group_id`·`target_group_arn`은 AI가 정하지 않는다(`Ec2IsolateCandidateParameters`가
빈 이유). 수집기가 적재한 자산 DB에서 시스템이 찾으며, **정확히 1개일 때만** 채운다.

두 층으로 나눈다.

  ① 선택 규칙 — 실제 PostgreSQL 필요(미기동 시 skip). 관계 `REGISTERED_IN` 1개 · 같은 VPC ·
     자리 태그 정확일치 · 소멸 표시 제외 · 0개/2개 이상이면 짐작하지 않고 거절.
  ② 두 변환 지점의 일치 — DB 불필요. 가드레일 ④(_candidate_precheck — 미리 조회)와 실행
     폴백(_execution_params)이 **같은 함수의 같은 값**을 쓰는가. 갈리면 ④가 통과시킨 SG·TG와
     실제로 바꾸는 SG·TG가 다를 수 있다.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import workflows  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from schemas.api.assets import AssetType, RelationType  # noqa: E402
from schemas.asset_roles import ROLE_ISOLATION, ROLE_TAG_KEY  # noqa: E402
from schemas.candidates import CandidateStatus, RunbookCandidateData  # noqa: E402
from schemas.precheck import (  # noqa: E402
    PrecheckOutcome,
    PrecheckReasonCode,
    VerificationMethod,
    build_verification_summary,
)
from schemas.runbook_parameters import (  # noqa: E402
    Ec2IsolateCandidateParameters,
    Ec2IsolateParameters,
)
from schemas.runbooks import RunbookId  # noqa: E402

R = PrecheckReasonCode

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
VPC = "vpc-0aaa111122223333a"
OTHER_VPC = "vpc-0bbb444455556666b"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
ISOLATION_SG = "sg-0150150150150150a"
SECOND_ISOLATION_SG = "sg-0250250250250250b"
WEB_SG = "sg-0aaa111122223333a"


def sg_arn(group_id: str) -> str:
    return f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{group_id}"


def tg_arn(name: str) -> str:
    return f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{name}/0123456789abcdef"


TG = tg_arn("vigilantis-smoke-tg")
OTHER_TG = tg_arn("vigilantis-other-tg")


# ============================================================ ① 선택 규칙 (PostgreSQL)


class Seed:
    def __init__(self, db):
        self.db = db
        self.run = assets_repo.start_collection_run(
            db, account_id=ACCOUNT, region=REGION, mode="localstack",
            lookback_days=3, period_seconds=3600,
        )

    def asset(self, arn, kind, resource_id, spec, *, absent=False):
        row = assets_repo.upsert_asset(
            self.db, arn=arn, asset_type=kind, resource_id=resource_id,
            account_id=ACCOUNT, region=REGION, spec=spec,
            collection_run_id=self.run.collection_run_id,
            collected_at=datetime.now(timezone.utc),
        )
        if absent:
            row.absent_since = datetime.now(timezone.utc)
            self.db.flush()
        return row

    def instance(self, *, vpc=VPC, registered_in=(TG,)):
        row = self.asset(INSTANCE_ARN, AssetType.EC2, INSTANCE, {"vpc_id": vpc, "tags": {}})
        assets_repo.replace_relationships(
            self.db, row.asset_id,
            [(RelationType.REGISTERED_IN, arn) for arn in registered_in]
            + [(RelationType.SECURED_BY, sg_arn(WEB_SG))],
            collection_run_id=self.run.collection_run_id,
        )
        return row

    def target_group(self, arn=TG, *, absent=False):
        return self.asset(arn, AssetType.ALB_TARGET_GROUP, arn.split("/")[1], {}, absent=absent)

    def group(self, group_id, *, vpc=VPC, role=ROLE_ISOLATION, absent=False):
        tags = {ROLE_TAG_KEY: role} if role else {}
        return self.asset(
            sg_arn(group_id), AssetType.SG, group_id,
            {"vpc_id": vpc, "tags": tags, "attached": False}, absent=absent,
        )


@pytest.fixture
def seed(db):
    return Seed(db)


def resolve(seed):
    return workflows.resolve_isolation_parameters(seed.db, INSTANCE_ARN)


def test_one_registration_and_one_isolation_group_resolve(seed):
    seed.instance()
    seed.target_group()
    seed.group(ISOLATION_SG)
    seed.group(WEB_SG, role=None)  # 자리 태그가 없는 SG는 후보가 아니다

    result = resolve(seed)

    assert result.resolved
    assert (result.target_group_arn, result.isolation_group_id) == (TG, ISOLATION_SG)
    assert result.as_lookups() == {"target_group_arn": TG, "isolation_group_id": ISOLATION_SG}


@pytest.mark.parametrize(
    "registered_in, code",
    [((), R.PRECHECK_TARGET_NOT_FOUND), ((TG, OTHER_TG), R.PRECHECK_PARAM_INVALID)],
    ids=["no-target-group", "two-target-groups"],
)
def test_target_group_must_be_exactly_one(seed, registered_in, code):
    seed.instance(registered_in=registered_in)
    seed.group(ISOLATION_SG)

    result = resolve(seed)

    assert result.reason_code is code
    assert result.target_group_arn is None and result.isolation_group_id is None


def test_registration_to_a_vanished_target_group_is_not_counted(seed):
    """소멸 표시된 TG를 가리키는 관계는 세지 않는다 — 남은 하나가 정답이다."""
    seed.instance(registered_in=(TG, OTHER_TG))
    seed.target_group(OTHER_TG, absent=True)
    seed.group(ISOLATION_SG)
    assert resolve(seed).target_group_arn == TG


@pytest.mark.parametrize(
    "groups, code",
    [((), R.PRECHECK_TARGET_NOT_FOUND), ((ISOLATION_SG, SECOND_ISOLATION_SG), R.PRECHECK_PARAM_INVALID)],
    ids=["no-isolation-group", "two-isolation-groups"],
)
def test_isolation_group_must_be_exactly_one(seed, groups, code):
    seed.instance()
    for group_id in groups:
        seed.group(group_id)

    result = resolve(seed)

    assert result.reason_code is code
    assert result.isolation_group_id is None


def test_isolation_group_in_another_vpc_is_ignored(seed):
    """다른 VPC의 SG는 이 인스턴스의 ENI에 붙일 수 없다 — 세면 2개로 거절되거나 엉뚱한 SG가 된다."""
    seed.instance()
    seed.group(ISOLATION_SG)
    seed.group(SECOND_ISOLATION_SG, vpc=OTHER_VPC)
    assert resolve(seed).isolation_group_id == ISOLATION_SG


def test_vanished_isolation_group_is_ignored(seed):
    seed.instance()
    seed.group(ISOLATION_SG, absent=True)
    seed.group(SECOND_ISOLATION_SG)
    assert resolve(seed).isolation_group_id == SECOND_ISOLATION_SG


def test_role_tag_must_match_exactly(seed):
    """rule_engine._is_isolation_sg와 같은 정확일치 — 대소문자를 보정하지 않는다."""
    seed.instance()
    seed.group(ISOLATION_SG, role=ROLE_ISOLATION.upper())
    assert resolve(seed).reason_code is R.PRECHECK_TARGET_NOT_FOUND


def test_unknown_or_vanished_instance_is_not_found(seed):
    assert resolve(seed).reason_code is R.PRECHECK_TARGET_NOT_FOUND
    row = seed.instance()
    seed.group(ISOLATION_SG)
    row.absent_since = datetime.now(timezone.utc)
    seed.db.flush()
    assert resolve(seed).reason_code is R.PRECHECK_TARGET_NOT_FOUND


def test_instance_without_vpc_is_refused(seed):
    seed.instance(vpc=None)
    seed.group(ISOLATION_SG)
    assert resolve(seed).reason_code is R.PRECHECK_PARAM_INVALID


# ============================================================ ② 두 변환 지점의 일치 (DB 불필요)


RESOLVED = workflows.IsolationParameters(
    target_group_arn=TG, isolation_group_id=ISOLATION_SG
)
UNRESOLVED = workflows.IsolationParameters(
    reason_code=R.PRECHECK_PARAM_INVALID, detail="격리 SG 2개"
)


@pytest.fixture
def resolver(monkeypatch):
    """resolve_isolation_parameters를 대역으로 — 몇 번, 무엇으로 불렸는지 남긴다."""
    calls: list = []

    def fake(db, target_arn):
        calls.append(target_arn)
        return fake.result

    fake.result = RESOLVED
    fake.calls = calls
    monkeypatch.setattr(workflows, "resolve_isolation_parameters", fake)
    return fake


def candidate() -> RunbookCandidateData:
    return RunbookCandidateData(
        candidate_id="11111111-1111-1111-1111-111111111111",
        incident_id="22222222-2222-2222-2222-222222222222",
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=INSTANCE_ARN,
        parameters=Ec2IsolateCandidateParameters(),
        evidence_ids=["ev-1"],
        status=CandidateStatus.PENDING_VALIDATION,
    )


@pytest.fixture
def precheck(monkeypatch):
    """executor.precheck 대역 — ④가 실제로 받은 실행 파라미터를 남긴다(AWS 미호출)."""
    seen: list = []

    def fake(runbook_id, target_arn, parameters, *, backup_loader=None):
        seen.append(parameters)
        return PrecheckOutcome(
            passed=True,
            verification_summary=build_verification_summary(
                VerificationMethod.MIXED, verified=["테스트 대역"], unverified=["AWS 전부"]
            ),
        )

    monkeypatch.setattr(workflows.executor, "precheck", fake)
    fake.seen = seen
    return fake


def guardrail_parameters(resolver, precheck):
    """④ 경로 — 가드레일 전에 미리 뽑고(_prefetch_candidate_backups), 그 값으로 판정한다."""
    loader = workflows._prefetch_candidate_backups(db=None, candidates=[candidate()])
    command = SimpleNamespace(
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=INSTANCE_ARN,
        parameters=Ec2IsolateCandidateParameters(),
        evidence_ids=["ev-1"],
    )
    return workflows._candidate_precheck(command, backup_loader=loader)


def execution_parameters(monkeypatch):
    """실행 폴백 — validated_command 없이 후보에서 옮긴다(_execution_params)."""
    row = SimpleNamespace(candidate_id="cand-1")
    monkeypatch.setattr(workflows.incidents_repo, "get_candidate", lambda db, cid: row)
    monkeypatch.setattr(workflows.mappers, "to_candidate_data", lambda r: candidate())
    execution = SimpleNamespace(
        execution_id="exec-1",
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=INSTANCE_ARN,
        validated_command=None,
        candidate_id="cand-1",
    )
    return workflows._ec2_isolate_params(None, execution)


def test_guardrail_and_execution_fallback_use_the_same_resolved_values(
    monkeypatch, resolver, precheck
):
    outcome = guardrail_parameters(resolver, precheck)
    assert outcome.passed
    (judged,) = precheck.seen

    executed = execution_parameters(monkeypatch)

    assert isinstance(judged, Ec2IsolateParameters) and isinstance(executed, Ec2IsolateParameters)
    assert judged == executed
    assert (executed.target_group_arn, executed.isolation_group_id) == (TG, ISOLATION_SG)
    # 두 지점 모두 같은 함수를 같은 대상으로 불렀다
    assert resolver.calls == [INSTANCE_ARN, INSTANCE_ARN]


def test_unresolved_values_are_a_guardrail_rejection_not_a_guess(resolver, precheck):
    resolver.result = UNRESOLVED
    outcome = guardrail_parameters(resolver, precheck)

    assert outcome.passed is False
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert precheck.seen == []  # AWS 판정까지 가지 않는다


def test_unresolved_values_leave_the_execution_without_parameters(monkeypatch, resolver):
    resolver.result = UNRESOLVED
    assert execution_parameters(monkeypatch) is None


def test_isolate_candidate_without_prefetched_values_is_a_wiring_error(precheck):
    """미리 뽑지 않은 채 ④를 부르면 거절이 아니라 예외다(ADR-0007 §1) — 거절로 삼키면
    배선 누락이 "격리 SG 없음" 판정으로 둔갑한다."""
    command = SimpleNamespace(
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        target_arn=INSTANCE_ARN,
        parameters=Ec2IsolateCandidateParameters(),
        evidence_ids=["ev-1"],
    )
    with pytest.raises(RuntimeError):
        workflows._candidate_precheck(command, backup_loader=None)
    loader = workflows._PrefetchedBackupLoader({})
    with pytest.raises(RuntimeError):
        workflows._candidate_precheck(command, backup_loader=loader)


def test_result_shape_is_values_or_reason_never_both():
    with pytest.raises(ValueError):
        workflows.IsolationParameters(target_group_arn=TG)
    with pytest.raises(ValueError):
        workflows.IsolationParameters(
            target_group_arn=TG, isolation_group_id=ISOLATION_SG,
            reason_code=R.PRECHECK_PARAM_INVALID, detail="x",
        )
    with pytest.raises(ValueError):
        UNRESOLVED.as_lookups()

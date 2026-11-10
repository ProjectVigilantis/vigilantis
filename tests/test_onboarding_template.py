"""고객 온보딩 템플릿(scripts/onboarding_template.py · packages/iac/onboarding/)의 회귀 테스트.

작업 거울은 스모크 앱 정책 테스트(test_provision_smoke_aws.py)의 것을 그대로 쓴다. 코드가 부르는 작업이
늘면 두 정책이 함께 깨진다.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


onboarding = _load("onboarding_template", REPO_ROOT / "scripts" / "onboarding_template.py")
smoke_tests = _load("_smoke_policy_mirror", REPO_ROOT / "tests" / "test_provision_smoke_aws.py")
smoke = smoke_tests.smoke

TEMPLATE = onboarding.build_template()
POLICY = onboarding.build_customer_policy()


def _granted(policy=POLICY) -> list[str]:
    return [a for s in policy["Statement"] for a in s["Action"]]


def _is_granted(action: str) -> bool:
    return any(smoke_tests.fnmatch.fnmatchcase(action, p) for p in _granted())


def test_repository_file_is_the_generated_template():
    assert onboarding.TEMPLATE_PATH.read_text(encoding="utf-8") == onboarding.render(), (
        "packages/iac/onboarding/vigilantis-access.json이 낡았다: uv run python scripts/onboarding_template.py")


def test_parameters_match_the_quick_create_link():
    source = (REPO_ROOT / "apps" / "core-api" / "tenant_admin.py").read_text(encoding="utf-8")
    link_params = set(re.findall(r'"param_(\w+)"', source))
    assert link_params == set(onboarding.PARAMETERS) == set(TEMPLATE["Parameters"])


def test_link_parameters_are_not_no_echo():
    """빠른 생성 링크는 NoEcho 파라미터의 param_ 값을 버린다. 걸면 고객이 ExternalId를 손으로 넣어야 한다."""
    assert [name for name, spec in TEMPLATE["Parameters"].items() if spec.get("NoEcho")] == []


def test_role_trusts_only_the_platform_principal_with_the_external_id():
    (statement,) = TEMPLATE["Resources"]["AccessRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"AWS": {"Ref": "PlatformPrincipalArn"}}
    assert statement["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}


def test_role_name_carries_the_prefix_the_platform_may_assume():
    # 플랫폼 태스크 역할은 vigilantis-access* 이름만 AssumeRole 한다(provision_platform_aws.task_role_policy)
    pattern = TEMPLATE["Parameters"]["RoleName"]["AllowedPattern"]
    assert pattern.startswith("^vigilantis-access")
    assert re.match(pattern, TEMPLATE["Parameters"]["RoleName"]["Default"])


def test_reads_and_cost_match_the_smoke_mirror():
    # 스모크 테스트가 READ_ACTIONS를 수집기 호출에, COST_ACTIONS를 비용 코드에 비춘다
    assert onboarding.READ_ACTIONS == smoke.READ_ACTIONS
    assert onboarding.COST_ACTIONS == smoke.COST_ACTIONS and onboarding.COST_REGION == smoke.COST_REGION


def test_policy_grants_every_aws_operation_the_code_calls():
    missing = sorted(a for a in smoke_tests._direct_actions() if not _is_granted(a))
    assert not missing, f"온보딩 정책에 없는 작업: {missing}"


def test_policy_grants_every_authorization_dependency():
    missing = sorted(a for a in smoke_tests._active_dependencies() if not _is_granted(a))
    assert not missing, missing


def test_policy_grants_only_required_mutations_and_dependencies():
    allowed = smoke_tests._direct_actions() | smoke_tests._active_dependencies() | set(onboarding.COST_ACTIONS)
    extra = sorted(a for a in _granted() if a not in onboarding.READ_ACTIONS and a not in allowed)
    assert not extra, f"코드가 부르지 않는 조치 권한: {extra}"


def test_policy_never_grants_destructive_or_out_of_scope_actions():
    wildcards = [a for a in _granted() if "*" in a and a not in onboarding.READ_ACTIONS]
    assert not wildcards, wildcards
    assert not [a for a in smoke_tests._NEVER_GRANTED if _is_granted(a)]
    assert not _is_granted("iam:PassRole")


def test_new_resources_need_the_execution_tags():
    for action in ("ec2:CreateLaunchTemplate", "autoscaling:CreateAutoScalingGroup", "autoscaling:CreateOrUpdateTags"):
        for statement in (s for s in POLICY["Statement"] if action in s["Action"]):
            assert statement["Condition"]["Null"][f"aws:RequestTag/{onboarding.EXECUTION_TAG_KEY}"] == "false", action


def test_asg_name_prefix_matches_the_executor():
    source = (REPO_ROOT / "apps" / "core-api" / "services" / "aws" / "executor.py").read_text(encoding="utf-8")
    assert f'f"{onboarding.ASG_PREFIX}{{instance_id}}"' in source


def test_inline_policy_fits_the_iam_limit():
    size = len(re.sub(r"\s", "", json.dumps(POLICY)))
    assert size <= onboarding.INLINE_POLICY_MAX_CHARS, size

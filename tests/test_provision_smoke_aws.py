# scripts/provision_smoke_aws.py 검증 — 실 AWS 스모크 환경(ADR-0009)이 코드로 지키는 약속.
#
#   ① 앱 IAM 정책은 코드가 부르는 AWS 작업의 거울이다. 실행 경로를 더한 PR이 정책을
#      빠뜨리면 여기서 깨진다 — 스모크 당일 AccessDenied로 처음 드러나지 않게. 반대로
#      코드가 부르지 않는 조치 권한이 정책에 끼어도 깨진다.
#   ② 실 AWS 전용 가드 — LocalStack 엔드포인트가 잡혀 있으면 AWS를 부르기 전에 멈춘다.
#   ③ 실 AWS에서만 드러나는 배치 조건 두 개(NACL 허용 번호·RIGHTSIZING 대상 타입).
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다. AWS를 부르지 않는다.

from __future__ import annotations

import fnmatch
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "provision_smoke_aws.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("provision_smoke_aws", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass가 자기 모듈을 sys.modules에서 찾는다 — 등록하지 않으면 정의가 깨진다
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_script()  # 스크립트가 apps/core-api·packages를 sys.path에 넣는다

from schemas.rightsizing_policy import rightsizing_target_type  # noqa: E402
from schemas.runbook_parameters import RuleNumber  # noqa: E402
from services.aws import executor, rollback  # noqa: E402

REGION = "ap-northeast-2"
VPC_ID = "vpc-0123456789abcdef0"
POLICY = smoke.build_app_policy("123456789012", REGION, VPC_ID)

# boto3 클라이언트 이름 → IAM 서비스 접두
_IAM_SERVICE = {"ec2": "ec2", "elbv2": "elasticloadbalancing", "autoscaling": "autoscaling"}


def _iam_action(operation: str) -> str:
    """"ec2.modify_instance_attribute" → "ec2:ModifyInstanceAttribute"."""
    service, name = operation.split(".", 1)
    return f"{_IAM_SERVICE[service]}:{''.join(part.capitalize() for part in name.split('_'))}"


def _code_operations() -> set[str]:
    """코드가 부르는 AWS 작업 — precheck 명세(RUNBOOK_SPECS)와 실행·판정의 작업 상수."""
    operations = {op for spec in executor.RUNBOOK_SPECS.values() for op in spec.operations}
    for module in (executor, rollback):
        operations |= {
            value for name, value in vars(module).items()
            if name.startswith("_OP_") and isinstance(value, str)
        }
    return operations


def _granted() -> list[str]:
    return [action for statement in POLICY["Statement"] for action in statement["Action"]]


def _is_granted(action: str) -> bool:
    return any(fnmatch.fnmatchcase(action, pattern) for pattern in _granted())


def test_policy_grants_every_aws_operation_the_code_calls():
    missing = sorted(a for a in {_iam_action(op) for op in _code_operations()} if not _is_granted(a))
    assert not missing, f"앱 정책에 없는 작업 — build_app_policy에 더할 것: {missing}"


def test_policy_grants_no_change_the_code_does_not_make():
    needed = {_iam_action(op) for op in _code_operations()}
    extra = sorted(a for a in _granted() if a not in smoke.READ_ACTIONS and a not in needed)
    assert not extra, f"코드가 부르지 않는 조치 권한: {extra}"


def test_every_change_statement_is_fenced():
    """조회 문장만 Resource "*"를 쓰고, 그것도 리전 조건으로 묶인다. 조치 문장은 리전이
    박힌 ARN이며, 특정 VPC 하나를 가리키지 않는 한 태그·VPC 조건이 붙는다."""
    for statement in POLICY["Statement"]:
        resources = statement["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        if set(statement["Action"]) <= set(smoke.READ_ACTIONS):
            assert statement["Condition"] == {"StringEquals": {"aws:RequestedRegion": REGION}}
            continue
        assert "*" not in resources, statement["Sid"]
        assert all(f":{REGION}:" in r for r in resources), statement["Sid"]
        pinned = all(r.endswith(VPC_ID) for r in resources)
        new_resource_only = statement["Sid"] in {"SnapshotsOfSmokeVolumes", "LaunchTemplatesForAutoscaling"}
        assert "Condition" in statement or pinned or new_resource_only, statement["Sid"]


def test_nacl_allow_rule_sits_at_the_top_of_what_ai_can_pick():
    """허용 규칙이 AI가 고를 수 있는 번호의 맨 끝이어야 어떤 차단 규칙도 먼저 평가된다."""
    adapter = TypeAdapter(RuleNumber)
    assert adapter.validate_python(smoke.NACL_ALLOW_ALL_RULE) == smoke.NACL_ALLOW_ALL_RULE
    with pytest.raises(ValidationError):
        adapter.validate_python(smoke.NACL_ALLOW_ALL_RULE + 1)


def test_non_production_instance_is_a_rightsizing_candidate():
    """production은 SKIP_PROD_PROTECTED로 빠지므로 FinOps 경로는 non-prod 대상에 달려 있다."""
    candidates = [s for s in smoke.INSTANCES if s.environment != "production"]
    assert candidates
    assert all(rightsizing_target_type(s.instance_type) for s in candidates)


def test_refuses_localstack_endpoint_before_calling_aws():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "AWS_ENDPOINT_URL": "http://localhost:4566"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "status", "--account", "000000000000"],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode != 0
    assert "실 AWS 전용" in result.stderr

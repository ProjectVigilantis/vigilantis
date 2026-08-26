"""ADR-0007 §Context 실측 표 ↔ 코드 ↔ 실제 AWS 정합 (Issue #130, ADR-0007 §6).

ADR-0007 §Context 표는 실측으로 만들어졌다. 그 표가 근거를 유지하려면 표·코드·실제
응답 셋이 함께 움직여야 한다 — 어느 하나만 바뀌면 여기서 실패한다.

앞 세 테스트는 문서와 코드만 보므로 어디서나 돈다. 뒤 두 테스트는 실제 AWS 호출이라
LocalStack 미기동 시 skip한다.
"""

import importlib.util
import os
import re
import sys
import urllib.request
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

# 엔드포인트를 여기서 고정한다. 비워 두면 skipif는 localhost 헬스체크로 통과하는데
# 실측은 실 AWS로 나간다 — DryRun이 듣지 않는 NACL 2종이 실제 계정의 규칙을 건드린다.
# (test_precheck_localstack.py와 같은 방식)
ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.setdefault("AWS_ENDPOINT_URL", ENDPOINT)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

from schemas.runbooks import ALLOWED_RUNBOOK_IDS  # noqa: E402

ADR_PATH = REPO_ROOT / "docs" / "adr" / "0007-guardrail-dryrun-executor-precheck-contract.md"


def _load_probe_module():
    """scripts/는 패키지가 아니라 파일 경로로 읽는다."""
    path = REPO_ROOT / "scripts" / "probe_dryrun.py"
    spec = importlib.util.spec_from_file_location("probe_dryrun", path)
    module = importlib.util.module_from_spec(spec)
    # @dataclass가 클래스의 모듈을 sys.modules에서 되찾으므로 실행 전에 등록해야 한다
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe_dryrun = _load_probe_module()


def _adr_rows():
    """ADR-0007 §Context 실측 표를 (작업, 런북들, 판정)으로 읽는다."""
    lines = ADR_PATH.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("### 실측 — 확정 10종"))
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            # 표가 끝났다. 계속 읽으면 뒤쪽의 다른 5열 표까지 먹는다
            if rows:
                break
            continue
        cells = [re.sub(r"[`*]", "", cell).strip() for cell in line.split("|")[1:-1]]
        if len(cells) != 5 or cells[0].startswith("---") or cells[0] == "AWS 작업":
            continue
        operation, runbooks, _exception, _changed, verdict = cells
        rows.append((operation, tuple(r.strip() for r in runbooks.split("·")), verdict))
    return tuple(rows)


# ------------------------------------------------------------------ 문서 ↔ 코드
def test_code_matrix_matches_the_adr_table():
    """표와 코드가 갈리면 실측 근거가 어느 쪽에 붙은 것인지 알 수 없게 된다."""
    code = tuple(
        (row.operation, row.runbooks, row.verdict) for row in probe_dryrun.TARGET_API_MATRIX
    )
    assert code == _adr_rows()


def test_every_whitelisted_runbook_is_covered():
    """런북이 늘면 실측 대상도 늘어야 한다 — ADR-0007 §6 머지 조건의 강제 지점."""
    # 표는 RUNBOOK_ 접두를 뺀 짧은 이름을 쓴다
    covered = {runbook for row in probe_dryrun.TARGET_API_MATRIX for runbook in row.runbooks}
    missing = sorted(
        runbook_id
        for runbook_id in ALLOWED_RUNBOOK_IDS
        if not any(runbook_id.endswith(short) for short in covered)
    )
    assert not missing, f"실측 대상이 없는 런북: {missing}"


# ------------------------------------------------------------------ 판정 규약
@pytest.mark.parametrize(
    "exception,changed,expected",
    [
        ("DryRunOperation", False, probe_dryrun.DRY_RUN),
        # 예외가 났어도 자원이 바뀌었으면 DryRun이 아니다 — NACL 2종이 여기서 갈린다
        ("DryRunOperation", True, probe_dryrun.DESCRIBE_FALLBACK),
        ("없음", False, probe_dryrun.DESCRIBE_FALLBACK),
        ("없음", True, probe_dryrun.DESCRIBE_FALLBACK),
        ("ParamValidationError", False, probe_dryrun.DESCRIBE_FALLBACK),
        ("UnauthorizedOperation", False, probe_dryrun.DESCRIBE_FALLBACK),
    ],
)
def test_verdict_requires_both_the_exception_and_an_unchanged_resource(
    exception, changed, expected
):
    assert probe_dryrun.verdict_for(exception, changed) == expected


# ------------------------------------------------------------------ 코드 ↔ 실제 AWS
def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def probe_results():
    """실측 1회분. 14개 작업 전수를 AWS에 물어보는 값비싼 호출이라 모듈당 한 번만 돈다."""
    return probe_dryrun.run_all()


@pytest.mark.skipif(not _localstack_up(), reason="LocalStack(4566) 미기동 — 실측 skip")
def test_probe_reproduces_the_matrix(probe_results):
    """실측이 표와 어긋나면 ADR과 코드를 함께 갱신해야 한다(ADR-0007 §6)."""
    mismatched = [
        f"{r.operation}: 표={r.expected} 실측={r.observed}"
        f"({r.exception}, 자원변경={r.resource_changed})"
        for r in probe_results
        if not r.matches
    ]
    assert not mismatched, "\n".join(mismatched)


@pytest.mark.skipif(not _localstack_up(), reason="LocalStack(4566) 미기동 — 실측 skip")
def test_nacl_operations_are_the_ones_that_actually_mutate(probe_results):
    """예외만 보면 놓치는 자리 — 이 두 작업은 DryRun을 무시하고 실제로 수행한다.

    ADR-0006 §4 5행의 근거이자, 조회 대체가 선택이 아니라 필수인 이유다.
    """
    mutating = {r.operation for r in probe_results if r.resource_changed}
    assert mutating == {"ec2.create_network_acl_entry", "ec2.delete_network_acl_entry"}

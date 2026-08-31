# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 실행 계열(런북 precheck · 원클릭 실행 · 원복) 회귀 테스트의 공통 픽스처입니다.
# (Issue #136)
#
# 데이터·헬퍼 본체는 execution_harness.py 에 있다. 이 파일은 그것을 픽스처로 감싸고,
# LocalStack 환경에 의존하는 픽스처만 직접 정의한다.
#
# ── 계층: 이 파일은 DB·FastAPI 앱에 의존하지 않는다 ──────────────────────────
# apps/core-api/tests/conftest.py 에 pg_engine·db·client·client_pg 4종이 있지만
# 여기로 가져오지 않는다. 네 가지 이유다.
#
#   ① pytest conftest 는 디렉터리 스코프다. tests/ 와 apps/core-api/tests/ 는
#      형제라 픽스처가 상속되지 않는다 — "재사용"은 자동이 아니라 복사다.
#   ② 끌어오는 유일한 문법인 pytest_plugins 선언은 rootdir conftest 에서만 허용된다.
#      저장소 루트에 conftest.py 가 없으므로 이 파일은 비-rootdir 이고, 선언하면 에러다.
#   ③ 복사하면 DB 접속 포트 해석(환경변수 > .env 의 POSTGRES_PORT > 5432)이 세 번째
#      사본이 된다. 한 곳만 어긋나면 통합 테스트가 조용히 skip 되고 pytest 는 초록불이
#      난다 — #92 가 CI 에서, #111 이 로컬에서 각각 막은 바로 그 사각지대다.
#   ④ 필요가 없다. precheck() 는 동기 함수이고(executor.py:975) backup_loader 는
#      Protocol 이라 가짜로 채워진다. 이 디렉터리는 DB 없이 돈다.
#
# DB 왕복이 실제로 필요한 실행 검증(ActionExecution 저장·멱등 재생)은 이 계층이 아니라
# apps/core-api/tests/ 에 둔다 — 거기에 db 픽스처와 _execution 헬퍼가 이미 있다
# (test_backup_workflow.py:82).
#
# ── 제공하는 것 ──────────────────────────────────────────────────────────────
#   A. LocalStack 시드 자산 재사용 — 조회만 한다(시드 실행은 CI·개발자 몫, 멱등)
#   B. Incident → RunbookCandidate → 실행 요청 조립 헬퍼 + idempotency_key 규약
#   C. 원복 계열 백업 레코드(backup_record_id) 입력 구성
#   D. ADR-0007 P2 3종 "로컬 FAIL = 정상" 전제를 가드레일 문맥별로 표현
# ==============================================================================

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

import pytest

# execution_harness 는 이 디렉터리의 형제 모듈이다. pytest 기본 임포트 모드(prepend)는
# 이 경로를 알아서 넣어 주지만, 그 기본값에 기대지 않는다 — `--import-mode=importlib` 로
# 돌리면 넣어 주지 않아 수집이 통째로 멈춘다. 임포트 모드는 테스트가 고를 문제가 아니다.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from execution_harness import (  # noqa: E402
    P2_LOCAL_FAIL_CASES,
    SEED_HINT,
    SEED_TAG_KEY,
    SEED_TAG_VALUE,
    P2LocalFailCase,
    SeededInstance,
    discover_seeded_instances,
    expects_local_precheck_fail,
    localstack_reachable,
    make_backup_loader,
    make_backup_record,
    make_candidate,
    make_execute_request,
    make_idempotency_key,
    make_precheck_parameters,
)


# ==============================================================================
# A. LocalStack 시드 자산 (scripts/seed_localstack.py 결과 재사용 — 조회만)
# ==============================================================================


@pytest.fixture(scope="session")
def aws_mode() -> str:
    """'localstack' | 'aws'. ADR-0006 §3 단일 스위치(AWS_ENDPOINT_URL)의 해석 결과다.

    환경변수를 직접 읽지 않는다 — 엔드포인트 해석의 단일 원천은 services/aws/client 다.
    """
    from services.aws.client import deployment_mode

    return deployment_mode()


@pytest.fixture(scope="session")
def localstack_endpoint(aws_mode) -> str:
    """LocalStack 엔드포인트. 실 AWS 모드거나 미기동이면 실제 대상을 적어 skip 한다.

    skip 메시지에 접속 대상을 적는 것은 #111 conftest 의 선례다 — 주소를 바꿔 놓고
    기본값을 출력하면 "왜 skip 됐는지"를 사람이 처음부터 다시 조사하게 된다.
    """
    from services.aws.client import endpoint_url

    if aws_mode != "localstack":
        pytest.skip("AWS_ENDPOINT_URL 미설정(실 AWS 모드) — 시드 자산 기반 테스트 skip")
    endpoint = endpoint_url()
    if not localstack_reachable(endpoint):
        pytest.skip(f"LocalStack({endpoint}) 미기동 — {SEED_HINT}")
    return endpoint


@pytest.fixture(scope="session")
def seeded_account_id(localstack_endpoint) -> str:
    """실행 환경의 실제 계정 ID. 가드레일 ③이 대조하는 ARN 이 이 값으로 조립된다."""
    from services.aws.client import account_id

    return account_id()


@pytest.fixture(scope="session")
def seeded_region(localstack_endpoint) -> str:
    from services.aws.client import default_region

    return default_region()


@pytest.fixture(scope="session")
def seeded_instances(localstack_endpoint, seeded_region) -> Mapping[str, SeededInstance]:
    """시드된 EC2 인스턴스 {Name 태그: SeededInstance}. 시드는 하지 않고 조회만 한다.

    시드 실행을 픽스처에 넣지 않는 이유: CI 는 pytest 앞 단계에서 이미 시드하고
    (.github/workflows/ci.yml), 테스트가 자산을 만들기 시작하면 "무엇이 전제이고
    무엇이 이 테스트가 만든 것인지"가 섞인다.
    """
    from services.aws.client import aws_client

    found = discover_seeded_instances(aws_client("ec2", seeded_region))
    if not found:
        pytest.skip(f"시드 인스턴스 없음({SEED_TAG_KEY}={SEED_TAG_VALUE}) — {SEED_HINT}")
    return found


@pytest.fixture()
def seeded_instance(seeded_instances):
    """이름으로 시드 인스턴스 하나. 없으면 skip 이 아니라 FAIL 이다.

    LocalStack 이 떠 있는데 특정 자산만 없는 것은 "환경 미구성"이 아니라 시드와
    테스트가 어긋난 것이다 — skip 으로 넘기면 초록불 뒤에 숨는다(#92 와 같은 형태).
    """

    def _pick(name: str) -> SeededInstance:
        instance = seeded_instances.get(name)
        if instance is None:
            pytest.fail(
                f"시드 인스턴스 '{name}' 없음. 조회된 것: {sorted(seeded_instances)} — "
                "scripts/seed_localstack.py 의 INSTANCES 와 이름이 어긋났는지 확인할 것"
            )
        return instance

    return _pick


# ==============================================================================
# B. Incident → RunbookCandidate → 실행 요청 조립
# ==============================================================================


@pytest.fixture()
def candidate_factory():
    """make_candidate 그대로. 픽스처로도 쓸 수 있게 감싼다."""
    return make_candidate


@pytest.fixture()
def precheck_parameters_factory():
    return make_precheck_parameters


@pytest.fixture()
def idempotency_key_factory():
    return make_idempotency_key


@pytest.fixture()
def execute_request_factory():
    return make_execute_request


# ==============================================================================
# C. 원복 계열 백업 레코드 (backup_record_id)
# ==============================================================================


@pytest.fixture()
def backup_record_factory():
    return make_backup_record


@pytest.fixture()
def backup_loader_factory():
    """원복 계열 런북용 로더. `backup_loader_factory(runbook_id, target_arn)` → (loader, record)."""
    return make_backup_loader


# ==============================================================================
# D. ADR-0007 P2 3종 — "로컬 FAIL 은 정상" 전제
# ==============================================================================


@pytest.fixture(params=P2_LOCAL_FAIL_CASES, ids=lambda case: case.id)
def p2_local_fail_case(request) -> P2LocalFailCase:
    """P2 3종 × 가드레일 문맥 = 4조합을 하나씩 흘려보낸다."""
    return request.param


@pytest.fixture()
def p2_local_fail_expected(aws_mode):
    """p2_local_fail_expected(runbook_id) → 이 환경에서 로컬 FAIL 이 기대값인가."""

    def _expected(runbook_id) -> bool:
        return expects_local_precheck_fail(runbook_id, aws_mode)

    return _expected

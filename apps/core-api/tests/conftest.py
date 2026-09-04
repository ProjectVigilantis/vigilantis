# ==============================================================================
# [파일 설명]
# 앱·라우터 테스트 픽스처. (Issue #68)
#
#   - import 경로: db/tests와 같은 방식(apps/core-api·packages 삽입, 저장소
#     루트 삽입 금지 — schemas identity 분열 방지).
#   - DB 비의존 테스트(client): 더미 DATABASE_URL로 앱만 구성한다. 엔진은
#     lazy라 접속이 일어나지 않는다 — /health·오류 봉투·로깅 검증용.
#   - PostgreSQL 통합(client_pg): db/tests/conftest.py와 같은 일회용 DB +
#     Alembic upgrade head 방식. DB 미기동 시 통합 테스트만 skip한다.
#     라우터에는 get_db 의존성 재정의로 테스트 세션을 주입한다. 테스트 종료 시
#     외부 트랜잭션을 rollback해 전부 되돌린다 — 세션이 create_savepoint 모드라
#     테스트 중의 commit·rollback이 외부 트랜잭션을 건드리지 않는다 (Issue #232).
#   - 앱 기동 시 실행 스캔 잡은 끈다(DISPATCH_ENABLED=false) — 테스트마다 스캔이
#     돌면 lru_cache된 세션 팩토리가 개발 DB로 굳은 채 그쪽을 스캔할 수 있다.
# ==============================================================================

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _host_db_port() -> str:
    """호스트에서 compose `db` 서비스에 붙을 포트.

    실제 환경변수 > 루트 `.env`의 `POSTGRES_PORT` > 5432 순으로 고른다. compose가
    호스트 포트를 `${POSTGRES_PORT:-5432}`로 열기 때문에, `.env`만 고친 팀원의 pytest가
    5432를 보고 **조용히 skip되고 초록불이 나는** 것을 막는다 — #92가 CI에서 막은
    사각지대의 로컬판이다. CI에는 `.env`가 없어 기본값 5432로 떨어진다. (Issue #111)

    `.env` 파싱은 pydantic-settings(`env_file=`)가 내부에서 쓰는 python-dotenv다.
    """
    from dotenv import dotenv_values

    return (
        os.getenv("POSTGRES_PORT")
        or dotenv_values(REPO_ROOT / ".env").get("POSTGRES_PORT")
        or "5432"
    )


# 접속 대상 — 전체 재지정은 TEST_DATABASE_ADMIN_URL 하나로 계속 가능하다
DB_HOSTPORT = f"localhost:{_host_db_port()}"
ADMIN_URL = os.getenv(
    "TEST_DATABASE_ADMIN_URL",
    f"postgresql+psycopg://vigilantis:vigilantis@{DB_HOSTPORT}/postgres",
)
# skip 메시지가 실제 접속 대상을 적게 한다(자격증명 제외) — 포트를 바꿔도 거짓말하지 않게
PG_TARGET = ADMIN_URL.rsplit("@", 1)[-1].split("/")[0]

# DB 비의존 테스트용 더미 — 엔진 생성만 되고 접속은 일어나지 않는다
os.environ.setdefault(
    "DATABASE_URL", f"postgresql+psycopg://vigilantis:vigilantis@{DB_HOSTPORT}/vigilantis"
)
# 테스트 앱 기동마다 스캔 잡이 도는 것을 막는다 (PR #236 리뷰)
os.environ.setdefault("DISPATCH_ENABLED", "false")
# 수집→판정 스캔 파이프라인도 테스트에서 끈다 — 앱 기동 시 실제 스캔이 돌지 않게 한다
os.environ.setdefault("SCAN_ENABLED", "false")

TEST_DB_NAME = f"vigilantis_test_{uuid.uuid4().hex[:8]}"
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB_NAME


def _postgres_available() -> bool:
    from sqlalchemy import create_engine, text

    try:
        engine = create_engine(ADMIN_URL, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


def _build_app():
    """환경변수 반영을 위해 설정 캐시를 비우고 앱을 새로 만든다."""
    from config import get_settings

    get_settings.cache_clear()
    from main import create_app

    return create_app()


@pytest.fixture()
def client():
    """DB 비의존 클라이언트 — DB에 접속하는 엔드포인트 검증에 쓰지 않는다."""
    from fastapi.testclient import TestClient

    with TestClient(_build_app()) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def pg_engine():
    """일회용 테스트 DB 생성 → Alembic head 적용 → 세션 종료 시 DB 삭제."""
    if not _postgres_available():
        pytest.skip(f"PostgreSQL({PG_TARGET}) 미기동 — 통합 테스트 skip")

    from sqlalchemy import create_engine, text

    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))

    os.environ["DATABASE_URL"] = TEST_URL
    from config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(CORE_API / "alembic.ini")), "head")

    engine = create_engine(TEST_URL)
    yield engine
    engine.dispose()

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def db(pg_engine):
    """외부 트랜잭션에 묶인 세션 — 테스트 종료 시 전부 rollback."""
    from sqlalchemy.orm import Session

    conn = pg_engine.connect()
    trans = conn.begin()
    # create_savepoint 명시 — 기본값(conditional_savepoint)은 세션 commit 이후의
    # rollback이 외부 트랜잭션까지 되돌려, 그 전에 commit된 시드가 통째로 사라진다.
    # 프로덕션 rollback 경로(dispatcher 예외 처리 등)를 지나는 테스트가 걸린다 (Issue #232)
    session = Session(
        bind=conn,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        session.close()
        if trans.is_active:
            trans.rollback()
        conn.close()


@pytest.fixture()
def client_pg(pg_engine, db):
    """통합 클라이언트 — 라우터의 get_db를 테스트 세션으로 재정의한다."""
    from fastapi.testclient import TestClient

    import db.session as db_session_module

    app = _build_app()

    def _override_get_db():
        yield db

    app.dependency_overrides[db_session_module.get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client


# ==============================================================================
# 실행 계열 시드 팩토리 (Issue #233)
#
#   파일마다 복사돼 있던 Incident·RunbookCandidate 시드 헬퍼를 여기로 모은다.
#   각 테스트가 요구하던 차이(summary_lines 유무 · status · 런북별 parameters)는
#   사본이 아니라 **인자**로 드러낸다 — 호출부만 보고 무엇이 다른지 알 수 있게.
#
#   두 팩토리가 생성 경로를 달리 쓴다. 대칭이 아닌 데는 이유가 있다:
#     - Incident  → ORM 직접. `incidents_repo.create_incident`는 값을 그대로
#       넘기기만 하는 래퍼라 거쳐서 얻는 것이 없고, `status`·`summary_lines`·
#       `created_at`을 받지 않아 기존 헬퍼 4벌 중 3벌을 덮지 못한다.
#     - Candidate → `RunbookCandidateData` 계약 경유. 이 계약이 `display_parameters`를
#       `parameters`에서 파생시키고, 손으로 박은 값이 파생과 다르면 거절한다
#       (`packages/schemas/candidates.py` `_enforce_contract`). 시드가 파생 규칙에서
#       벗어날 여지 자체를 없앤다 — 기존 헬퍼 하나가 그 값을 손으로 적고 있었다.
#
#   세션은 픽스처가 닫아 잡지 않고 **첫 인자로 받는다.** `db` 픽스처에 묶으면
#   독립 트랜잭션 경합을 검증하는 테스트(actions API의 동시 접수)가 자기 세션으로
#   시드할 수 없고, 쓰지도 않을 `db` 트랜잭션이 딸려 열린다.
#
#   범위는 Incident·Candidate 두 축이다. ActionExecution 시드는 이 카드에 넣지
#   않았다 — 호출부 계약이 다른 별도 축이라 같은 팩토리로 묶이지 않는다.
# ==============================================================================

SEED_SUBJECT_EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa"
SEED_SUMMARY_LINES = ("요약 1", "요약 2", "요약 3")
SEED_EVIDENCE_IDS = ("ev-1",)

# 이 디렉터리의 테스트가 기대는 런북 2종의 기본 parameters. 없는 런북을 부르면
# 조용히 {}로 넘어가지 않고 그 자리에서 실패한다 — 빈 파라미터는 계약 위반이 아니라
# "그 런북은 인자가 없다"는 뜻이라, 둘을 구분하지 못하면 시드가 틀린 채로 초록불이 난다.
#
# 정식 맵은 `tests/execution_harness.py`의 `CANDIDATE_PARAMS_BY_RUNBOOK`이고 본편 7종을
# 전부 덮는다(전량 커버는 `test_candidate_params_cover_every_ai_recommendable_runbook`이
# 지킨다). 여기서 import하지 못하는 것은 conftest가 저장소 루트를 sys.path에 넣지 않기
# 때문이다 — 넣으면 schemas identity가 갈린다(파일 상단 주석).
#
# **다만 이 사본은 조용히 낡지 않는다.** 값은 `RunbookCandidateData`가 typed 계약으로
# 검증하므로(#154), 파라미터 계약이 바뀌면 시드 시점에 이 팩토리가 곧바로 터진다.
_SEED_PARAMETERS: dict[str, dict] = {
    "RUNBOOK_NACL_ADD_DENY": {
        "rule_number": 100, "cidr_block": "203.0.113.5/32", "protocol": "-1",
    },
    "RUNBOOK_SG_DELETE_ISOLATED": {},
}


@pytest.fixture()
def seed_summary_lines() -> list[str]:
    """AI 요약 3줄. DB CheckConstraint `summary_lines_len`이 **0줄 또는 정확히 3줄**만
    허용하므로 길이가 계약이다 — 내용은 아무 값이나 되지만 개수는 그렇지 않다.
    conftest 상수는 테스트 모듈에서 import할 수 없어(importlib 모드) 픽스처로 낸다.
    """
    return list(SEED_SUMMARY_LINES)


@pytest.fixture()
def make_incident():
    """Incident 시드 — 위험 축의 모양을 `category`에서 파생시킨다.

    DB CheckConstraint `category_risk_shape`가 모양을 강제한다: SECOPS는
    `title`·`initial_risk_level` 필수 + 사유 코드 1개 이상, FINOPS는 위험 축 전부
    NULL + 사유 코드 빈 배열. 기존 헬퍼 4벌이 이 분기를 각자 손으로 적고 있었다.

    위험 축 인자(`title`·`initial_risk_level`·`response_mode`·
    `initial_risk_reason_codes`)는 keyword로 덮어쓸 수 있고, 그 이름 밖의 인자는
    오타를 조용히 삼키지 않도록 TypeError로 거절한다.
    """
    from db import models
    from schemas.api.incidents import (
        IncidentCategory,
        IncidentStatus,
        ResponseMode,
        RiskLevel,
    )

    def _make(
        session,
        *,
        category: "IncidentCategory" = IncidentCategory.SECOPS,
        subject_arn: str = SEED_SUBJECT_EC2,
        status: "IncidentStatus" = IncidentStatus.AWAITING_APPROVAL,
        summary_lines=(),
        created_at=None,
        updated_at=None,
        **risk,
    ):
        if category is IncidentCategory.SECOPS:
            shape = {
                "title": "SSH 브루트포스 탐지",
                "initial_risk_level": RiskLevel.MEDIUM,
                "response_mode": ResponseMode.AGENT_WAIT,
                "initial_risk_reason_codes": ["SSH_BRUTE_FORCE"],
            }
        else:
            shape = {
                "title": None,
                "initial_risk_level": None,
                "response_mode": None,
                "initial_risk_reason_codes": [],
            }

        unknown = sorted(set(risk) - set(shape))
        if unknown:
            raise TypeError(
                f"make_incident: 알 수 없는 인자 {unknown} — "
                f"위험 축 인자는 {sorted(shape)}뿐이다"
            )
        shape.update(risk)

        incident = models.Incident(
            subject_arn=subject_arn,
            category=category,
            status=status,
            summary_lines=list(summary_lines),
            **shape,
        )
        # created_at·updated_at은 서버 기본값이 있어 목록 정렬을 고정할 때만 넘긴다
        if created_at is not None:
            incident.created_at = created_at
        if updated_at is not None:
            incident.updated_at = updated_at
        session.add(incident)
        session.flush()
        return incident

    return _make


@pytest.fixture()
def make_candidate():
    """RunbookCandidate 시드 — `RunbookCandidateData` 계약을 거친다.

    `display_parameters`를 인자로 받지 않는 것이 이 팩토리의 요점이다. 계약이
    `parameters`에서 파생시키므로 시드가 파생 규칙과 어긋날 수 없다.

    `incident`는 ORM 객체와 `incident_id` 문자열을 모두 받는다 — 스캔 너머로
    식별자만 들고 다니는 dispatcher 계열 호출부가 있다.
    """
    from db.repositories import incidents as incidents_repo
    from schemas.candidates import CandidateStatus, RunbookCandidateData
    from schemas.runbooks import RunbookId

    def _make(
        session,
        incident,
        *,
        runbook_id: "RunbookId" = RunbookId.RUNBOOK_NACL_ADD_DENY,
        target_arn: str | None = None,
        parameters: dict | None = None,
        evidence_ids=SEED_EVIDENCE_IDS,
        status: "CandidateStatus" = CandidateStatus.EXECUTABLE,
    ):
        incident_id = getattr(incident, "incident_id", incident)
        if target_arn is None:
            target_arn = getattr(incident, "subject_arn", SEED_SUBJECT_EC2)
        if parameters is None:
            if runbook_id.value not in _SEED_PARAMETERS:
                raise KeyError(
                    f"{runbook_id.value}의 기본 parameters가 conftest에 없다 — "
                    "parameters=로 직접 넘기거나 _SEED_PARAMETERS에 추가할 것"
                )
            parameters = _SEED_PARAMETERS[runbook_id.value]

        return incidents_repo.add_candidate(
            session,
            RunbookCandidateData(
                candidate_id=str(uuid.uuid4()),
                incident_id=incident_id,
                runbook_id=runbook_id,
                target_arn=target_arn,
                parameters=dict(parameters),
                evidence_ids=list(evidence_ids),
                status=status,
            ),
        )

    return _make


@pytest.fixture()
def make_executable(make_incident, make_candidate):
    """접수 가능한 최소 상태 — Incident 1건 + EXECUTABLE 후보 1건."""

    def _make(session, *, incident=None, **candidate_kwargs):
        if incident is None:
            incident = make_incident(session)
        return incident, make_candidate(session, incident, **candidate_kwargs)

    return _make


@pytest.fixture()
def make_precontract_candidate():
    """typed 계약(#154) **이전에** 저장된 후보 행을 재현한다 — ORM 직접, 검증 우회.

    `make_candidate`는 `RunbookCandidateData`를 거치므로 계약을 어기는 행을 만들 수
    없다. 그게 정상이다. 다만 "계약 이전 행·마이그레이션 backfill을 접수 단계가
    거르는가"를 검증하려면 그런 행이 실제로 DB에 있어야 한다.

    **용도는 그 하나뿐이다.** 정상 후보에는 절대 쓰지 않는다 — 이름이 길고 낯선 것이
    의도다. 이걸로 시드한 테스트는 "계약을 통과하지 못하는 상태"를 검증하는 것이어야
    한다.
    """
    from db import models
    from schemas.candidates import CandidateStatus
    from schemas.runbooks import RunbookId

    def _make(
        session,
        incident,
        *,
        runbook_id: "RunbookId" = RunbookId.RUNBOOK_NACL_ADD_DENY,
        parameters: dict | None = None,
        status: "CandidateStatus" = CandidateStatus.EXECUTABLE,
    ):
        candidate = models.RunbookCandidate(
            incident_id=getattr(incident, "incident_id", incident),
            runbook_id=runbook_id,
            target_arn=getattr(incident, "subject_arn", SEED_SUBJECT_EC2),
            parameters={} if parameters is None else dict(parameters),
            evidence_ids=list(SEED_EVIDENCE_IDS),
            status=status,
        )
        session.add(candidate)
        session.flush()
        return candidate

    return _make

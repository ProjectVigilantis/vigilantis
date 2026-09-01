# ==============================================================================
# [파일 설명]
# 접수된 조치 실행을 AWS 실행으로 넘기고, 진행 중인 채로 남은 실행을 회수하는
# 모듈입니다. workflows.reserve_execution이 예약까지만 하므로, 예약과 실제 실행이
# 갈라지는 시점에 이 자리가 필요합니다. (Issue #232)
#
# 계층 경계 — 비종료 실행 회수 스캔은 이 모듈 하나가 소유합니다. 스캔이 둘이면
# 같은 실행 행을 두 주체가 만집니다. 개별 실행의 Status Check 확인과 자동 원복
# 판단은 services/aws/rollback.py 몫이라 여기서 다시 돌지 않습니다(executor.py
# [남은 작업] 2번의 "트리거 판단·감시는 rollback.py 담당"이 그것입니다).
# 실제 일은 아래로 내려보냅니다.
#   dispatcher → workflows.py              상태 전이·트랜잭션
#              → services/aws/executor.py  조치 실행
#              → services/aws/rollback.py  자동 원복 동작
# workflows.store_instance_spec_backup()과 services/aws/backup.py가 나눈 것과 같은
# 경계입니다 — AWS 호출은 services/aws/**, 커밋 순서는 workflows.py.
#
# 비종료 실행 1건이 가는 길은 **단계 기록의 유무**가 가릅니다. 백업이 모든 AWS
# 변경보다 먼저 커밋되고 단계는 호출 직전에 저장되므로, 단계가 없다는 것은 자산이
# 아직 만져지지 않았다는 뜻이라 실행으로 넘겨도 안전합니다(_RUNNERS). 단계가 남은
# 실행은 자산이 이미 바뀌었을 수 있어 재실행하지 않고 2/2 Status Check 판정으로
# 보냅니다(_JUDGES) — 기동 요청 접수는 성공의 경계가 아니고 그 판정이 SUCCESS와
# ROLLBACK_INITIATED를 가르기 때문입니다. (Issue #240)
#
# ROLLBACK_INITIATED로 확정된 실행은 스캔에 계속 걸리지만 여기서 다시 만지지
# 않습니다 — 자동 원복 실행을 낳는 것은 rollback.py·#241 몫입니다.
#
# [남은 작업]
# 1. RIGHTSIZING 외 9종 실행 — 실행 함수가 생기는 대로 _RUNNERS에, 종료 판정이
#    필요한 런북은 _JUDGES에 등록합니다(services/aws/executor.py [남은 작업] 1번).
# 2. ROLLBACK_INITIATED 원본의 자동 원복 발동 — trigger_source=AUTO_ON_FAILURE로
#    원복 실행을 만들고 원본을 ROLLED_BACK·ROLLBACK_FAILED로 확정합니다 (Issue #241).
#
# 기동 worker 개수는 미정입니다 — ADR-0005가 다중 worker·replica 실행 토폴로지를
# 별도 결정 대상으로 남겼고, 이 모듈은 worker 1개를 전제합니다. 선점(_claim)의
# 행 잠금은 실행 내부 commit(백업 확보 시점)에서 풀리므로, "runner 진입은 한
# 주체뿐"이라는 보장은 이 전제 + 스캔 비중첩(max_instances=1)에서 성립합니다.
# 다중 worker로 갈 때는 commit을 넘어 사는 선점(lease 컬럼 등)을 그 결정과 함께
# 도입해야 합니다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from schemas.api.actions import ExecutionStatus
from schemas.api.ws import WsEvent, WsEventType
from schemas.executions import ASSET_MAY_HAVE_CHANGED_EFFECTS
from schemas.runbooks import RunbookId

import workflows
from config import get_settings
from db import models
from db.repositories import executions as executions_repo
from db.session import get_session_factory
from realtime import execution_event, incident_event

logger = logging.getLogger("vigilantis.dispatcher")

JOB_ID = "execution_dispatch"

Publish = Callable[[WsEvent], None]

# 런북별 실행 진입점. 여기 없는 런북의 예약은 넘기지 않는다 — 실행 함수가 없다는
# 사실을 실패 확정으로 바꾸면, 미구현이 "조치가 실패했다"는 기록으로 둔갑한다.
_RUNNERS: dict[RunbookId, Callable[[Session, str], workflows.ExecutionRunOutcome]] = {
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: workflows.run_rightsizing_execution,
}

# 런북별 종료 판정 진입점 — AWS 변경이 이미 시작된 실행을 어느 종료 상태로 확정할지
# 정한다. _RUNNERS와 짝이며, 여기 없는 런북의 진행 중 실행은 판정 주체가 없다는
# 뜻이라 건드리지 않고 남긴다(미구현을 실패 확정으로 바꾸지 않는 것과 같은 이유).
_JUDGES: dict[RunbookId, Callable[[Session, str], workflows.BootJudgement]] = {
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: workflows.judge_rightsizing_boot,
}


def _changed_the_asset(outcome: workflows.ExecutionRunOutcome) -> bool:
    return any(
        step.effect in ASSET_MAY_HAVE_CHANGED_EFFECTS for step in outcome.steps
    )


@dataclass
class DispatchReport:
    """스캔 1회 요약 — 로그와 테스트가 읽는 값이다."""

    scanned: int = 0
    started: int = 0                # executor로 넘긴 실행
    judged: int = 0                 # 2/2 Status Check 판정을 수행한 실행
    closed: int = 0                 # 종료 상태로 확정한 실행
    awaiting_status_check: int = 0  # 요청은 접수됐고 다음 주기의 판정을 기다리는 실행
    rollback_initiated: int = 0     # 원복이 필요해 ROLLBACK_INITIATED로 남긴 실행
    skipped: int = 0                # 선점 실패·이미 확정된 실행
    unsupported: int = 0            # 실행 함수·판정 함수가 아직 없는 런북
    errored: int = 0


def _claim(db: Session, execution_id: str) -> Optional[models.ActionExecution]:
    """회수 대상 선점 — 행을 잠근 뒤 상태를 다시 확인한다.

    스캔이 목록을 읽은 시점과 여기 사이에 상태가 바뀌었을 수 있다. 다시 보지 않고
    넘기면 이미 끝난 실행을 한 번 더 돌려 **백업 없는 두 번째 AWS 변경**이 된다
    (run_rightsizing_execution이 상태만 보고 거절하는 것과 짝을 이루는 관문이다).

    IN_PROGRESS만 집는다. 비종료 상태에는 ROLLBACK_INITIATED도 있지만 그쪽은 이미
    판정이 끝나 자동 원복을 기다리는 실행이라 여기서 만지지 않는다 (Issue #241).
    """
    row = executions_repo.lock_execution(db, execution_id)
    if row is None or row.status is not ExecutionStatus.IN_PROGRESS:
        return None
    return row


def _failure_summary(outcome: workflows.ExecutionRunOutcome) -> str:
    """실패 사유 한 줄 — 분류 코드를 앞에 둬 로그·DB에서 사유별로 모인다."""
    code = outcome.reason_code.value if outcome.reason_code is not None else "UNKNOWN"
    detail = (outcome.error_summary or "").strip()
    if not detail:
        return code
    # 저장 컬럼 폭이 1024자다(db/models.py action_executions.error_summary)
    return f"{code}: {detail}"[:1024]


def _publish_closure(publish: Publish, closure: workflows.ExecutionClosure) -> None:
    """DB commit 이후에만 부른다 — 커밋 전에 보내면 받는 쪽이 아직 없는 상태를
    조회한다(realtime.py 규약).

    Incident 상태가 그대로여도 INCIDENT_UPDATED를 보낸다. 상세 응답에 실리는 자식
    실행 목록이 바뀌었으므로, 받는 쪽이 재조회해야 화면이 맞는다.
    """
    publish(
        execution_event(
            incident_id=closure.incident_id,
            execution_id=closure.execution_id,
            status=closure.execution_status,
            updated_at=closure.execution_updated_at,
        )
    )
    publish(
        incident_event(
            WsEventType.INCIDENT_UPDATED,
            incident_id=closure.incident_id,
            occurred_at=closure.incident_updated_at,
        )
    )


def _close_and_publish(
    db: Session,
    execution_id: str,
    publish: Optional[Publish],
    report: DispatchReport,
    *,
    next_status: ExecutionStatus,
    error_summary: Optional[str] = None,
) -> None:
    """확정 → 카운트 → 발행. 확정이 None이면 다른 주체가 먼저 옮긴 것이다."""
    closure = workflows.close_execution(
        db, execution_id, next_status=next_status, error_summary=error_summary
    )
    if closure is None:
        # 실행 도중 다른 주체가 먼저 확정했다 — 발행도 그쪽이 한다
        report.skipped += 1
        return
    if next_status is ExecutionStatus.ROLLBACK_INITIATED:
        report.rollback_initiated += 1
    else:
        report.closed += 1
    if publish is not None:
        _publish_closure(publish, closure)


def _judge_one(
    db: Session,
    claimed: models.ActionExecution,
    publish: Optional[Publish],
    report: DispatchReport,
) -> None:
    """AWS 변경이 시작된 실행 1건을 종료 판정으로 보낸다. (Issue #240)

    재실행하지 않는다 — 자산이 이미 만져졌을 수 있으므로 남은 질문은 "다시 돌릴까"가
    아니라 "이 조치가 성공으로 끝났는가"다. 판정 자체는 rollback.py가, 상태 확정은
    workflows.close_execution이 소유한다.
    """
    execution_id = claimed.execution_id
    judge = _JUDGES.get(claimed.runbook_id)
    if judge is None:
        # 판정 주체가 없는 런북이다. 실패로 확정하면 미구현이 "조치가 실패했다"로
        # 둔갑하므로 남긴다 — _RUNNERS 미등록을 다루는 것과 같은 규약이다
        logger.debug(
            "dispatch_judge_missing",
            extra={
                "execution_id": execution_id,
                "runbook_id": claimed.runbook_id.value,
            },
        )
        report.unsupported += 1
        db.commit()
        return

    report.judged += 1
    # 선점 잠금을 먼저 놓는다. 판정은 최대 STATUS_CHECK_WAIT_DELAY × MAX_ATTEMPTS만큼
    # AWS를 기다리는데, 그동안 행을 잠근 트랜잭션이 열려 있으면 커넥션과 잠금을 분
    # 단위로 붙잡는다. 놓아도 안전한 것은 확정이 close_execution 몫이고 그쪽이 다시
    # 잠근 뒤 상태를 재확인하기 때문이다 — 그 사이 누가 먼저 확정했으면 None이 온다.
    db.commit()
    try:
        judgement = judge(db, execution_id)
        _close_and_publish(
            db,
            execution_id,
            publish,
            report,
            next_status=judgement.next_status,
            error_summary=judgement.error_summary,
        )
    except Exception:  # noqa: BLE001 — 판정 1건의 오류가 스캔 전체를 멈추면 안 된다
        logger.exception("dispatch_judge_failed", extra={"execution_id": execution_id})
        db.rollback()
        report.errored += 1
        return
    logger.info(
        "dispatch_judged",
        extra={
            "execution_id": execution_id,
            "next_status": judgement.next_status.value,
            "verdict": judgement.verdict.value if judgement.verdict else None,
        },
    )


def _dispatch_one(
    db: Session,
    execution_id: str,
    publish: Optional[Publish],
    report: DispatchReport,
) -> None:
    claimed = _claim(db, execution_id)
    if claimed is None:
        report.skipped += 1
        # 선점 조회가 연 트랜잭션을 닫아 행 잠금을 놓는다. 쓴 것이 없으므로
        # commit이고, rollback을 쓰면 호출부가 같은 세션에 얹어 둔 작업까지 잃는다
        db.commit()
        return

    # 단계 기록이 1건이라도 있으면 자산이 이미 만져졌을 수 있다 — 재실행이 아니라
    # 종료 판정으로 간다. "단계 0건 = 자산 미변경"은 executor 계약에 의존한다:
    # AWS 호출 직전에 IN_PROGRESS 단계가 먼저 커밋된다(workflows._step_recorder).
    # 그 순서를 바꾸면 이 분기도 함께 무너진다.
    if executions_repo.list_steps(db, execution_id):
        _judge_one(db, claimed, publish, report)
        return

    runner = _RUNNERS.get(claimed.runbook_id)
    if runner is None:
        # 미지원 예약은 비종료로 남아 매 주기 다시 걸린다 — 주기 요약(unsupported
        # 카운터)이 신호를 이미 나르므로 행 단위 반복 로그는 debug로 낮춘다
        logger.debug(
            "dispatch_runner_missing",
            extra={
                "execution_id": execution_id,
                "runbook_id": claimed.runbook_id.value,
            },
        )
        report.unsupported += 1
        db.commit()
        return

    report.started += 1
    try:
        outcome = runner(db, execution_id)
        if outcome.succeeded:
            # 기동 요청 접수는 성공의 경계가 아니다 — 2/2 Status Check가 SUCCESS와
            # ROLLBACK_INITIATED를 가른다(services/aws/rollback.py). 여기서 SUCCESS를
            # 앞질러 쓰면 관제자 복구 경로가 판정 전에 열리고(EXECUTION_RECOVERABLE_
            # STATUSES), 뒤이은 자동 원복 개시가 종료 상태를 되살리는 전이가 된다.
            # 판정은 **다음 주기**가 한다 — 방금 기동을 요청한 인스턴스에 곧바로
            # 2/2를 물으면 부팅 시간만큼 이 스캔이 붙잡힌다(max_instances=1).
            report.awaiting_status_check += 1
            logger.info(
                "dispatch_awaiting_status_check", extra={"execution_id": execution_id}
            )
            db.commit()  # runner는 반환 전에 commit을 끝낸다 — 그 계약을 코드로 남긴다
            return
        if _changed_the_asset(outcome):
            # 자산이 바뀐 채 끝난 실행이다. FAILED로 확정하면 계약상 "변경 없이
            # 실패"가 되어(packages/schemas/executions.py 복구 가능 상태 주석)
            # 관제자 복구 목록이 닫히므로, 되돌릴 것이 남았다고 적는다. 2/2를
            # 물을 이유는 없다 — 조치가 제 갈 데까지 가지 못한 것이 이미 확정이다.
            logger.warning(
                "dispatch_rollback_initiated",
                extra={
                    "execution_id": execution_id,
                    "reason_code": outcome.reason_code.value,
                },
            )
            _close_and_publish(
                db,
                execution_id,
                publish,
                report,
                next_status=ExecutionStatus.ROLLBACK_INITIATED,
                error_summary=_failure_summary(outcome),
            )
            return
        _close_and_publish(
            db,
            execution_id,
            publish,
            report,
            next_status=ExecutionStatus.FAILED,
            error_summary=_failure_summary(outcome),
        )
    except Exception:  # noqa: BLE001 — 1건의 실행·확정 오류가 스캔 전체를 멈추면 안 된다.
        # 확정까지 같은 우산 아래 둔다 — 밖에 두면 깨진 행 하나가 던진 예외가
        # 남은 대상 전부를 건너뛰게 해, 매 주기 그 행 앞에서 멈추는 기아가 된다
        logger.exception("dispatch_run_failed", extra={"execution_id": execution_id})
        db.rollback()
        report.errored += 1
        return


def dispatch_pending(db: Session, publish: Optional[Publish] = None) -> DispatchReport:
    """비종료 실행 스캔 1회. **세션 수명은 호출부가 소유한다.**

    목록을 행이 아니라 식별자로만 받아 둔다. 처리 중에 커밋이 일어나므로 들고 있던
    행 상태는 곧 낡고, 그 값을 믿으면 선점 재확인이 무의미해진다.
    """
    report = DispatchReport()
    pending = [row.execution_id for row in executions_repo.list_non_terminal(db)]
    report.scanned = len(pending)
    for execution_id in pending:
        _dispatch_one(db, execution_id, publish, report)
    logger.info("dispatch_cycle_done", extra=vars(report))
    return report


def run_dispatch_cycle(
    session_factory: sessionmaker[Session], publish: Optional[Publish] = None
) -> DispatchReport:
    """주기 잡의 본체이자 수동 호출 진입점 — 스캔 1회에 세션 1개를 쓰고 닫는다."""
    db = session_factory()
    try:
        return dispatch_pending(db, publish)
    finally:
        db.close()


def start_dispatcher(publish: Optional[Publish] = None) -> Optional[AsyncIOScheduler]:
    """main의 lifespan에서 기동한다 — 스캔 잡 1개를 등록·기동해 반환한다.

    잡을 겹쳐 돌리지 않는다(max_instances=1). 스캔이 둘이면 같은 실행 행을 두
    주체가 만지고, 그것이 이 모듈이 스캔을 독점하는 이유다(파일 헤더).

    DISPATCH_ENABLED=false면 기동하지 않고 None을 돌려준다 — 테스트가 앱을 띄울
    때마다 스캔이 돌면 lru_cache된 세션 팩토리가 개발 DB로 굳은 채 그쪽을 스캔할
    수 있다(PR #236 리뷰).
    """
    settings = get_settings()
    if not settings.DISPATCH_ENABLED:
        logger.info("dispatcher disabled: DISPATCH_ENABLED=false")
        return None
    interval = settings.DISPATCH_INTERVAL_SECONDS
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: run_dispatch_cycle(get_session_factory(), publish),
        trigger=IntervalTrigger(seconds=interval),
        id=JOB_ID,
        name="접수된 조치 실행 디스패치·회수 스캔",
        max_instances=1,
        coalesce=True,  # 밀린 실행은 1회로 합친다
        replace_existing=True,
    )
    scheduler.start()
    logger.info("dispatcher started: job=%s interval=%ss", JOB_ID, interval)
    return scheduler

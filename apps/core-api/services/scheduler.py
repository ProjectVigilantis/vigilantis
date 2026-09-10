# ==============================================================================
# [파일 설명]  담당: 김세혁 / 김승철
# APScheduler 기반 주기 스캔 스케줄러입니다. (MVP에서 Step Functions/Fargate 대체)
# collector→rule_engine→FinOps Intake 파이프라인을 주기적으로 실행합니다.
#
# 구현: run_pipeline(수집→정형화→적재→판정→Incident 생성) 잡을 등록한다.
#   main.py lifespan 이 start_scheduler() 를 호출해 기동한다(SCAN_ENABLED=false 면 미기동).
#   실행 간격은 CollectorSettings.SCAN_INTERVAL_SECONDS(기본 300초, gt=0 검증).
# ==============================================================================

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from schemas.api.ws import WsEvent, WsEventType
from schemas.evidence import DetectionAssetSnapshot
from schemas.intake import INCIDENT_TRIGGERING_VERDICTS, FinOpsIncidentIntake
from schemas.rules import RuleEvaluationResult

from asset_mapping import to_asset_item
from config import get_collector_settings
from db.repositories import assets as assets_repo
from incident_intake import create_incident_from_intake
from realtime import incident_event

logger = logging.getLogger("vigilantis.scheduler")

JOB_ID = "finops_secops_scan"

# 파이프라인 잡 전용 PostgreSQL advisory lock 키(고정 64bit 정수).
# 다중 워커/레플리카에서 같은 tick 이 파이프라인을 겹쳐 돌리지 않게 한다 — 프로세스 내
# 겹침은 build_scheduler 의 max_instances=1 이, 프로세스 간 겹침은 이 락이 막는다. (#277)
# 값은 이 잡 전용으로 고정한 임의 상수다("vig_scan" ASCII). 현재 저장소에 다른 advisory
# lock 은 없다 — **새 advisory lock 을 추가하면 이 키와 겹치지 않는지 확인할 것**(#281 리뷰).
_ADVISORY_LOCK_KEY = 0x7669675F7363616E


def _build_finops_intakes(
    db: Session, evaluations: list[RuleEvaluationResult],
) -> list[FinOpsIncidentIntake]:
    # COST_CANDIDATE·UNUSED만 FINOPS로 올린다(schemas.intake 계약).
    # SKIP은 제외 판정, THREAT은 별도 위협 이벤트·위험 판정이 필요한 SECOPS다.
    selected = [e for e in evaluations if e.verdict in INCIDENT_TRIGGERING_VERDICTS]
    if not selected:
        return []

    assets = {asset.arn: asset for asset in assets_repo.list_assets(db)}
    relationships = defaultdict(list)
    for rel in assets_repo.list_all_relationships(db):
        relationships[rel.source_asset_id].append(rel)

    return [
        FinOpsIncidentIntake(
            rule_evaluation=evaluation,
            asset_snapshot=DetectionAssetSnapshot(
                # 판정의 ID를 복사하면 다른 회차의 자산을 정상처럼 포장한다.
                # 관측 행의 실제 ID를 넘겨 Intake의 회차 일치 검증을 통과시킨다.
                collection_run_id=assets[evaluation.asset_arn].last_collection_run_id,
                asset=to_asset_item(
                    assets[evaluation.asset_arn],
                    relationships[assets[evaluation.asset_arn].asset_id],
                    evaluation,
                ),
            ),
        )
        for evaluation in selected
    ]


def run_pipeline(publish: Callable[[WsEvent], None] | None = None) -> dict:
    """collector → rule_engine → FINOPS Intake 1회 실행.

    다중 워커/레플리카에서 같은 tick 이 파이프라인을 겹쳐 돌리지 않도록 PostgreSQL
    세션 레벨 advisory lock 을 전용 커넥션에 잡고 실행한다. 락을 못 잡으면(다른 프로세스가
    이미 실행 중) 이 tick 을 건너뛴다({"skipped": True}). 끝나면 락을 해제하고 커넥션을
    반납한다. 프로세스 내 겹침은 max_instances=1, 프로세스 간 겹침은 이 락이 막는다. (#277)
    """
    from sqlalchemy import text

    from db.session import get_engine, get_session_factory
    from services.collector import collect_and_store
    from services.rule_engine import run_rule_engine

    # AUTOCOMMIT — 세션 레벨 advisory lock 은 트랜잭션이 아니라 커넥션에 매이므로,
    # 장시간 열린 트랜잭션을 남기지 않고 커넥션이 살아있는 동안 락을 유지한다.
    lock_conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = False
    try:
        acquired = bool(
            lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            ).scalar()
        )
        if not acquired:
            logger.info("scan pipeline skipped: 다른 프로세스가 이미 실행 중(advisory lock 미획득)")
            return {"skipped": True}

        store = collect_and_store()  # 수집→정형화→assets/metric_summaries upsert
        session_factory = get_session_factory()
        db = session_factory()
        try:
            judged = run_rule_engine(db)  # RuleEvaluation 적재
            db.commit()
            # 판정은 독립 결과다. 뒤의 Intake 조립이 실패해도 판정 기록은 남긴다.
            intakes = _build_finops_intakes(db, judged["evaluations"])
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        incidents = {"created": 0, "existing": 0}
        for intake in intakes:
            # 판정 저장과 Incident 저장은 별도 트랜잭션이다. Intake의 commit이
            # 다른 자산의 작업을 확정하지 않도록 건별 세션을 만들고 닫는다.
            db = session_factory()
            try:
                outcome = create_incident_from_intake(db, intake)
            except Exception:
                db.rollback()
                # 저장 실패를 성공 요약으로 숨기지 않도록 건별 계속 처리 대신 tick을
                # 중단한다. 앞서 commit한 건은 유지하고 다음 스캔에서 멱등 재시도한다.
                raise
            finally:
                db.close()
            incidents["created" if outcome.created else "existing"] += 1
            if outcome.created and publish is not None:
                publish(incident_event(
                    WsEventType.INCIDENT_CREATED,
                    incident_id=outcome.incident_id,
                    occurred_at=outcome.occurred_at,
                ))

        summary = {"stored": store, "verdicts": judged["counts"], "incidents": incidents}
        logger.info("scan pipeline done: %s", summary)
        return summary
    finally:
        # 락을 잡은 tick 만 unlock 한다 — 미획득(skip) tick 에서 부르면 Postgres 가
        # "you don't own a lock" WARNING 을 서버 로그에 남긴다(다중 워커에서 매 tick 누적, #281 리뷰: 김세혁).
        if acquired:
            try:
                lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY}
                )
            except Exception:
                # close()는 커넥션을 풀에 반납할 뿐 백엔드 세션을 끝내지 않아 락이 남는다
                # (그러면 이 프로세스가 이후 모든 tick 을 조용히 skip). invalidate()로 실제
                # DBAPI 커넥션을 끊어 세션 레벨 락을 확실히 해제한다(#281 리뷰: 김세혁).
                logger.exception("advisory unlock 실패 — 커넥션 무효화로 백엔드 세션 종료해 락 해제")
                lock_conn.invalidate()
        lock_conn.close()



def _interval_seconds() -> int:
    """스캔 주기(초). 검증된 설정에서만 읽는다 — 생짜 os.getenv 는 0·음수·비정수를
    잡지 못해 잘못된 값이 잡 등록 시점까지 흘러갔다(#255)."""
    return get_collector_settings().SCAN_INTERVAL_SECONDS


def build_scheduler(publish: Callable[[WsEvent], None] | None = None) -> AsyncIOScheduler:
    """스케줄러를 구성하고 파이프라인 잡을 등록한다(기동은 하지 않음)."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_pipeline,
        kwargs={"publish": publish},
        trigger=IntervalTrigger(seconds=_interval_seconds()),
        id=JOB_ID,
        name="FinOps/SecOps 자산 스캔 파이프라인",
        max_instances=1,       # 이전 실행이 안 끝났으면 겹쳐 돌지 않음
        coalesce=True,         # 밀린 실행은 1회로 합침
        replace_existing=True,
    )
    return scheduler


def start_scheduler(publish: Callable[[WsEvent], None] | None = None) -> AsyncIOScheduler | None:
    """main의 lifespan에서 기동. 스케줄러를 구성·기동해 반환한다.

    SCAN_ENABLED=false 면 기동하지 않고 None 을 돌려준다 — 테스트가 앱을 띄울 때 실제
    수집·판정 스캔이 도는 것을 막는다(dispatcher.start_dispatcher 와 같은 결)."""
    if not get_collector_settings().SCAN_ENABLED:
        logger.info("scan scheduler disabled: SCAN_ENABLED=false")
        return None
    scheduler = build_scheduler(publish)
    scheduler.start()
    logger.info("scheduler started: job=%s interval=%ss", JOB_ID, _interval_seconds())
    return scheduler

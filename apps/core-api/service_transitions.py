# ==============================================================================
# [파일 설명]
# MVP 서비스 연결·전환 facade — 관리자 운영 CLI가 부르는 단일 진입점 run_service_command.
#
# 명령: schema · plan · status · prepare · verify · cutover · return · cleanup-plan · cleanup.
# 출력은 JSON으로 그대로 옮길 수 있는 dict이고 항상 "success"(bool)와 "command"를 갖는다.
# 예상한 거절·조회 실패는 success=False로 돌려주고, 예상하지 못한 예외는 그대로 올린다.
#
# 경계
#   - 트랜잭션은 여기서 소유한다. AWS·HTTP 호출 동안 DB 트랜잭션을 열어 두지 않는다(_close).
#   - 일지·관측은 #446 실행 단위다. 시도(transition)가 만든 서비스 자원 변경은 그 시도를 함께
#     가리키고, #446 인프라 정리는 시도 없이 남는다. 시도는 실행마다 한 줄로 잇고(대체는 명시),
#     현재 시도(leaf)는 아무도 대체하지 않은 시도로 유도한다. 앞선 계획은 고치지 않는다.
#   - AWS 변경마다 호출 전에 일지(PENDING)를 commit하고 결과로 닫는다. 불확실한 변경은 다시 보내지
#     않는다 — 의도한 효과가 현물에서 확인될 때까지 같은 실행의 변경 명령이 멈춘다. 효과가 안
#     보인다는 대조는 막음을 풀지 않는다(요청이 거절됐다는 증거가 아니다).
#   - 변경 명령은 공유 AWS 좌표(#446 실행 = ASG, 시도들의 운영 리스너)의 advisory lock을 명령 내내
#     쥔다. 이 도구 밖의 AWS 조작과는 원자적이지 않다 — 변경 전후 대조로 드러낼 뿐이다.
#   - 관문은 CLI 플래그가 아니라 저장된 사실과 새로 읽은 현물이다. 보안 보류(격리 SG·scale-in 보호·
#     DB 격리 실행)는 풀지 않는다 — 보고하고 막는다.
#   - #446 계획·상태는 다시 읽기만 한다(덮어쓰지 않는다). 격리 사실·scale-in 보호 결과는 읽기만 한다.
# ==============================================================================

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, get_args

from botocore.exceptions import BotoCoreError, ClientError
from db import models
from db.repositories import executions as executions_repo
from db.repositories import service_transitions as repo
from pydantic import BaseModel, ValidationError
from schemas.api.actions import ExecutionStatus
from schemas.arns import build_arn
from schemas.autoscaling import AutoscalingExecutionPlan
from schemas.backups import ScaleInProtectionResult
from schemas.executions import EXECUTION_NON_TERMINAL_STATUSES
from schemas.runbooks import RunbookId
from schemas.service_transition import (
    MUTATING_COMMANDS,
    PROBE_HEADER,
    RETAINED_BILLING_WARNING,
    SERVICE_PORT,
    CheckResult,
    CleanupOptions,
    CleanupPlanOptions,
    NoOptions,
    OriginalRoute,
    ProbeClient,
    RouteMember,
    RuntimeFacts,
    ServiceCommand,
    ServiceObservation,
    ServiceTransitionPlan,
    ServiceTransitionPlanOptions,
    compute_verdict,
    simultaneous_cost,
    transition_target_group_name,
)
from services.aws import service_transition as aws
from services.aws.errors import reason_code_for
from services.aws.executor import _summarize
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

COMMANDS: tuple[str, ...] = get_args(ServiceCommand)
_OPTIONS: dict[str, type[BaseModel]] = {
    "plan": ServiceTransitionPlanOptions,
    "cleanup-plan": CleanupPlanOptions,
    "cleanup": CleanupOptions,
}
_EVIDENCE_SCOPE = (
    "ALB 경로: 요청 전후 경로가 ASG 전용 TG로만 갔다는 스냅숏 + 실제 ALB 응답. 멤버: 사설 IP 직접 응답."
    " 둘 다 요청 단위 ALB 대상 귀속(접근 로그)이 아닙니다"
)
_NOT_ATOMIC = (
    "확인→변경 사이는 원자적이지 않습니다(AWS CAS 없음) — 변경 전후 대조로만 드러냅니다"
)
_SELECTED = object()


class _Refusal(Exception):
    def __init__(self, error: str, detail: Any, **fields: Any) -> None:
        super().__init__(error)
        self.error, self.detail, self.fields = error, detail, fields


def _now() -> datetime:
    return datetime.now(UTC)


def _result(command: str, success: bool, **fields: Any) -> dict:
    return {"success": success, "command": command, **fields}


def _close(db: Session) -> None:
    """읽기·쓰기 트랜잭션을 닫는다 — 다음 AWS·HTTP 호출 동안 DB 트랜잭션을 쥐지 않는다."""
    db.commit()


def _checks_view(checks) -> list[dict]:
    return [item.model_dump(mode="json") for item in checks]


# ------------------------------------------------------------------ 진입점
def run_service_command(
    db: Session | None,
    *,
    command: str,
    clients: Mapping[str, Any] | None,
    execution_id: str | None = None,
    transition_id: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict:
    """관리자 운영 CLI facade. schema는 DB·AWS 없이 답한다(db=None 허용)."""
    if command == "schema":
        return command_schema()
    if command not in COMMANDS:
        return _result(
            command,
            False,
            error="unknown_command",
            detail=f"명령은 {list(COMMANDS)}입니다",
        )
    try:
        parsed = _OPTIONS.get(command, NoOptions).model_validate(dict(options or {}))
    except ValidationError as exc:
        return _result(
            command,
            False,
            error="invalid_options",
            detail=[
                {"loc": ".".join(map(str, e["loc"])), "msg": e["msg"]}
                for e in exc.errors()
            ],
        )
    if db is None:
        return _result(
            command,
            False,
            error="db_required",
            detail="schema 밖의 명령은 DB 세션이 필요합니다",
        )
    try:
        c = aws.clients_from(clients or {})
    except KeyError as exc:
        return _result(
            command,
            False,
            error="missing_clients",
            detail=f"필수 클라이언트 없음: {exc.args[0]}",
        )
    try:
        return _HANDLERS[command](
            db,
            c,
            execution_id=_uuid(execution_id),
            transition_id=_uuid(transition_id),
            options=parsed,
        )
    except _Refusal as refusal:
        db.rollback()
        return _result(
            command, False, error=refusal.error, detail=refusal.detail, **refusal.fields
        )
    except (ClientError, BotoCoreError) as exc:
        db.rollback()
        return _result(
            command,
            False,
            error="aws_query_failed",
            reason_code=reason_code_for(exc).value,
            detail=f"조회 실패로 판정 불가(성공·부재 아님): {_summarize(exc)}",
        )


def _uuid(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise _Refusal("invalid_id", f"UUID 형식이 아닙니다: {value}") from None


# ------------------------------------------------------------------ 맥락
@dataclass(frozen=True)
class _Attempt:
    transition_id: str
    supersedes_transition_id: str | None
    plan: ServiceTransitionPlan
    created_at: str


@dataclass(frozen=True)
class _Context:
    """읽은 값만 든다 — ORM 객체를 들고 다니면 commit 뒤 지연 조회가 AWS 호출 사이에 트랜잭션을 연다."""

    execution_id: str
    execution_status: ExecutionStatus
    as_plan: AutoscalingExecutionPlan
    attempts: tuple[_Attempt, ...]  # root → leaf 순서
    leaf_id: str | None
    transition_id: str | None  # 선택한 시도(요청한 것, 없으면 leaf)
    plan: ServiceTransitionPlan | None

    @property
    def plans(self) -> list[ServiceTransitionPlan]:
        return [a.plan for a in self.attempts]

    def attempt_plan(self, transition_id: str | None) -> ServiceTransitionPlan | None:
        return next(
            (a.plan for a in self.attempts if a.transition_id == transition_id), None
        )

    def identities(self, extra_listeners: Iterable[str] = ()) -> list[str]:
        """변경 배제의 공유 좌표 — ASG(#446 실행)와 이 실행 시도들의 운영 리스너."""
        listeners = {a.plan.original_route.listener_arn for a in self.attempts} | set(
            extra_listeners
        )
        return [
            f"execution:{self.execution_id}",
            *(f"listener:{arn}" for arn in sorted(listeners)),
        ]

    def chain_view(self) -> list[dict]:
        return [
            {
                "transition_id": a.transition_id,
                "supersedes_transition_id": a.supersedes_transition_id,
                "created_at": a.created_at,
                "leaf": a.transition_id == self.leaf_id,
            }
            for a in self.attempts
        ]


def _autoscaling_context(
    db: Session, execution_id: str
) -> tuple[models.ActionExecution, AutoscalingExecutionPlan]:
    execution = executions_repo.get_execution(db, execution_id)
    if execution is None:
        raise _Refusal("execution_not_found", f"실행이 없습니다: {execution_id}")
    if execution.runbook_id is not RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING:
        raise _Refusal(
            "execution_not_autoscaling",
            f"EC2_ENABLE_AUTOSCALING 실행이 아닙니다: {execution.runbook_id.value}",
        )
    try:
        as_plan = AutoscalingExecutionPlan.model_validate(
            executions_repo.get_autoscaling_plan(db, execution_id)
        )
    except ValidationError:
        raise _Refusal(
            "autoscaling_plan_invalid", "저장된 #446 실행 계획을 읽을 수 없습니다"
        ) from None
    if (
        as_plan.execution_id != execution_id
        or as_plan.target_arn != execution.target_arn
    ):
        raise _Refusal(
            "autoscaling_plan_invalid",
            "저장된 #446 실행 계획이 실행 행과 맞지 않습니다",
        )
    return execution, as_plan


def _attempts(
    db: Session, execution_id: str
) -> tuple[tuple[_Attempt, ...], str | None]:
    """이 실행의 시도를 root부터 대체 순서로. leaf는 아무도 대체하지 않은 시도다."""
    rows = repo.list_transitions_for_execution(db, execution_id)
    by_predecessor = {row.supersedes_transition_id: row for row in rows}
    ordered, current = [], by_predecessor.get(None)
    while current is not None:
        ordered.append(current)
        current = by_predecessor.get(current.transition_id)
    attempts = tuple(
        _Attempt(
            row.transition_id,
            row.supersedes_transition_id,
            ServiceTransitionPlan.model_validate(row.plan),
            row.created_at.isoformat(),
        )
        for row in ordered
    )
    return attempts, repo.leaf_transition_id(rows)


def _load(
    db: Session,
    execution_id: str | None,
    transition_id: str | None,
    *,
    need_transition: bool = True,
    allow_superseded: bool = False,
) -> _Context:
    if transition_id is not None:
        row = repo.get_transition(db, transition_id)
        if row is None:
            raise _Refusal("transition_not_found", f"시도가 없습니다: {transition_id}")
        if execution_id is not None and row.execution_id != execution_id:
            raise _Refusal(
                "id_mismatch", "transition_id가 execution_id의 시도가 아닙니다"
            )
        execution_id = row.execution_id
    elif execution_id is None:
        raise _Refusal("id_required", "execution_id 또는 transition_id가 필요합니다")
    execution, as_plan = _autoscaling_context(db, execution_id)
    attempts, leaf = _attempts(db, execution_id)
    selected = transition_id or leaf
    if need_transition and selected is None:
        raise _Refusal(
            "transition_not_found",
            f"실행 {execution_id}의 서비스 시도가 없습니다 — plan을 먼저 실행합니다",
        )
    if selected is not None and selected != leaf and not allow_superseded:
        raise _Refusal(
            "superseded", f"대체된 시도입니다(읽기만 가능) — 현재 시도: {leaf}"
        )
    plan = next((a.plan for a in attempts if a.transition_id == selected), None)
    return _Context(
        execution_id, execution.status, as_plan, attempts, leaf, selected, plan
    )


def _scope(as_plan: AutoscalingExecutionPlan) -> tuple[str, str]:
    region, account = as_plan.target_arn.split(":")[3:5]
    return region, account


def _open(rows: list[models.ServiceTransitionOperation]) -> bool:
    """AWS를 만졌을 수 있고 정리(전환 TG 삭제)로 풀리지 않은 시도인가 — 다른 실행의 리스너 결속 관문."""
    touched = any(repo.is_effective(op) or repo.is_unresolved(op) for op in rows)
    released = any(
        op.aws_operation == aws._OP_DELETE_TARGET_GROUP and repo.is_effective(op)
        for op in rows
    )
    return touched and not released


def _listener_bound_elsewhere(
    db: Session, listener_arn: str, execution_id: str
) -> list[str]:
    others = [
        t
        for t in repo.transitions_on_listener(db, listener_arn)
        if t.execution_id != execution_id
    ]
    if not others:
        return []
    by_attempt: dict[str, list] = {}
    for op in repo.list_operations(db, execution_ids={t.execution_id for t in others}):
        if op.transition_id is not None:
            by_attempt.setdefault(op.transition_id, []).append(op)
    return sorted(
        t.transition_id for t in others if _open(by_attempt.get(t.transition_id, []))
    )


def _unresolved(
    db: Session, execution_id: str
) -> list[models.ServiceTransitionOperation]:
    return [
        op
        for op in repo.list_operations(db, execution_ids=[execution_id])
        if repo.is_unresolved(op)
    ]


def _chain_changed(
    db: Session, execution_id: str, attempts: Iterable[_Attempt]
) -> str | None:
    """잠금을 잡은 뒤 이 실행의 시도 목록을 다시 읽는다 — 읽어 둔 목록과 다르면 지금의 leaf(없으면 "")."""
    rows = repo.list_transitions_for_execution(db, execution_id)
    if sorted(row.transition_id for row in rows) == sorted(
        a.transition_id for a in attempts
    ):
        return None
    return repo.leaf_transition_id(rows) or ""


@contextmanager
def _mutation_gate(
    db: Session,
    ctx: _Context,
    *,
    listener_binding: bool,
    extra_listeners: Iterable[str] = (),
    aggregate: bool = False,
) -> Iterator[list[models.ServiceTransitionOperation]]:
    """공유 좌표 잠금 + 저장된 관문. 일지를 돌려준다.

    관문: 잠금 아래에서 다시 읽은 시도 목록이 _load 때와 같다(그 사이 대체 시도가 commit됐으면 낡은
    시도로 바꾸지 않는다 — 시도 명령은 superseded, 실행 단위 정리는 attempts_changed) · 이 실행에 결과를
    모르는 변경 없음 · 필요하면 리스너 결속.
    """
    with repo.mutation_claim(db, ctx.identities(extra_listeners)) as held:
        if not held:
            raise _Refusal(
                "mutation_in_progress",
                "같은 ASG·운영 리스너에 다른 변경 명령이 진행 중입니다",
            )
        current_leaf = _chain_changed(db, ctx.execution_id, ctx.attempts)
        if current_leaf is not None:
            if not aggregate and ctx.transition_id != current_leaf:
                raise _Refusal(
                    "superseded",
                    f"읽은 뒤 대체된 시도입니다(바꾸지 않았습니다) — 현재 시도: {current_leaf}",
                )
            raise _Refusal(
                "attempts_changed",
                "읽은 뒤 이 실행의 시도가 바뀌었습니다 — 다시 읽고 실행합니다",
            )
        ops = repo.list_operations(db, execution_ids=[ctx.execution_id])
        unresolved = [op for op in ops if repo.is_unresolved(op)]
        if unresolved:
            raise _Refusal(
                "unresolved_operation",
                "결과를 모르는 변경이 있어 다시 보내지 않습니다 — 의도한 효과가 status로 확인될 때까지 멈춥니다",
                operations=[_op_view(op) for op in unresolved],
            )
        if listener_binding and ctx.plan is not None:
            bound = _listener_bound_elsewhere(
                db, ctx.plan.original_route.listener_arn, ctx.execution_id
            )
            if bound:
                raise _Refusal(
                    "listener_bound",
                    f"같은 운영 리스너에 열린 다른 실행의 시도가 있습니다: {bound}",
                )
        yield ops


def _mutate(
    db: Session,
    ctx: _Context,
    command: str,
    mutation: aws.Mutation,
    *,
    transition_id: str | None,
) -> dict:
    """일지 PENDING commit → AWS 변경 1건 → 결과 commit."""
    op = repo.add_operation(
        db,
        execution_id=ctx.execution_id,
        transition_id=transition_id,
        command=command,
        aws_operation=mutation.aws_operation,
        resource_ref=mutation.resource_ref,
        request={
            "kind": mutation.kind,
            "expect_present": mutation.expect_present,
            **mutation.request,
        },
    )
    operation_id, sequence = op.operation_id, op.sequence
    db.commit()
    outcome = aws.perform(mutation)
    repo.finish_operation(
        db,
        operation_id,
        state=outcome.state,
        response=outcome.response,
        error_code=outcome.error_code,
        error_summary=outcome.error_summary,
    )
    db.commit()
    return {
        "sequence": sequence,
        "transition_id": transition_id,
        "aws_operation": mutation.aws_operation,
        "resource_ref": mutation.resource_ref,
        "state": outcome.state,
        "response": outcome.response,
        "error_code": outcome.error_code,
        "error_summary": outcome.error_summary,
    }


def _op_view(op: models.ServiceTransitionOperation) -> dict:
    return {
        "operation_id": op.operation_id,
        "sequence": op.sequence,
        "transition_id": op.transition_id,
        "command": op.command,
        "aws_operation": op.aws_operation,
        "resource_ref": op.resource_ref,
        "state": op.state,
        "reconciled_state": op.reconciled_state,
        "unresolved": repo.is_unresolved(op),
        "started_at": op.started_at.isoformat() if op.started_at else None,
        "finished_at": op.finished_at.isoformat() if op.finished_at else None,
        "error_code": op.error_code,
        "error_summary": op.error_summary,
    }


def _record(
    db: Session,
    ctx: _Context,
    kind: str,
    summary: str,
    *,
    checks=(),
    http=(),
    routes=(),
    facts=None,
    transition_id: Any = _SELECTED,
) -> ServiceObservation:
    observation = ServiceObservation(
        kind=kind,
        verdict=compute_verdict(tuple(checks), tuple(http)),
        observed_at=_now(),
        summary=summary,
        checks=tuple(checks),
        http=tuple(http),
        routes=tuple(routes),
        facts=facts or {},
    )
    repo.add_observation(
        db,
        execution_id=ctx.execution_id,
        transition_id=ctx.transition_id
        if transition_id is _SELECTED
        else transition_id,
        kind=kind,
        verdict=observation.verdict,
        observed_at=observation.observed_at,
        evidence=observation.model_dump(mode="json"),
    )
    db.commit()
    return observation


def _runtime(
    facts: RuntimeFacts,
    as_plan: AutoscalingExecutionPlan,
    source,
    asg_members: int | None,
) -> dict:
    return simultaneous_cost(
        facts,
        captured_instance_type=as_plan.source.instance_type,
        source=source,
        asg_members=asg_members,
        asg_max_size=as_plan.max_size,
    )


# ------------------------------------------------------------------ 격리 사실
def _protection_view(raw: Any) -> dict | None:
    if raw is None:
        return None
    try:
        return ScaleInProtectionResult.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        return {
            "invalid": True,
            "detail": "저장된 scale-in 보호 결과가 계약과 맞지 않습니다",
        }


def _isolation_view(facts: list[dict]) -> list[dict]:
    view = []
    for fact in facts:
        protection = _protection_view(fact["scale_in_protection_result"])
        unisolations = [
            {
                **child,
                "scale_in_protection_result": _protection_view(
                    child["scale_in_protection_result"]
                ),
            }
            for child in fact["unisolations"]
        ]
        residual = any(
            (p or {}).get("residual") is True or (p or {}).get("invalid")
            for p in [
                protection,
                *(u["scale_in_protection_result"] for u in unisolations),
            ]
        )
        view.append(
            {
                **fact,
                "scale_in_protection_result": protection,
                "unisolations": unisolations,
                "protection_residual": residual,
            }
        )
    return view


def _db_isolation(
    db: Session, *, region: str, account: str, instance_ids, execution_id: str
) -> tuple[dict[str, list[str]], list[dict]]:
    """(인스턴스별 열린 격리 실행 ID, 보기용 사실). DB를 읽기만 한다."""
    ids = sorted({i for i in instance_ids if i})
    facts = repo.isolation_facts(
        db,
        target_arns=[build_arn("instance", i, region, account) for i in ids],
        autoscaling_execution_id=execution_id,
    )
    active: dict[str, list[str]] = {}
    for fact in facts:
        instance_id = fact["target_arn"].rsplit("/", 1)[-1]
        if fact["active"] and instance_id in ids:
            active.setdefault(instance_id, []).append(fact["execution_id"])
    return active, _isolation_view(facts)


def _db_isolation_check(active: dict[str, list[str]], label: str) -> CheckResult:
    return aws.check(
        "db_isolation_clear",
        not active,
        f"DB의 열린 EC2_ISOLATE 실행({label}) {active or '없음'}",
    )


def _security_holds(
    db: Session, ctx: _Context, aws_holds: dict[str, list[str]], instance_ids
) -> dict[str, list[str]]:
    """AWS 보류(격리 SG·scale-in 보호·상태 불명) + DB의 열린 격리 실행. 호출부가 트랜잭션을 닫는다."""
    region, account = _scope(ctx.as_plan)
    active, _ = _db_isolation(
        db,
        region=region,
        account=account,
        instance_ids=instance_ids,
        execution_id=ctx.execution_id,
    )
    holds = {i: list(basis) for i, basis in aws_holds.items()}
    for instance_id, executions in active.items():
        holds.setdefault(instance_id, []).extend(
            f"DB 격리 실행 {e}" for e in executions
        )
    return holds


# ------------------------------------------------------------------ plan
def _plan(
    db, c, *, execution_id, transition_id, options: ServiceTransitionPlanOptions
) -> dict:
    if transition_id is not None or execution_id is None:
        raise _Refusal("id_required", "plan은 execution_id만 받습니다")
    execution, as_plan = _autoscaling_context(db, execution_id)
    if execution.status is not ExecutionStatus.SUCCESS:
        raise _Refusal(
            "execution_not_ready",
            f"#446 실행이 SUCCESS가 아닙니다: {execution.status.value}",
        )
    attempts, leaf = _attempts(db, execution_id)
    replaces = _uuid(options.replaces_transition_id)
    if leaf is None and replaces is not None:
        raise _Refusal("nothing_to_replace", "이 실행에는 대체할 시도가 없습니다")
    if leaf is not None and replaces != leaf:
        raise _Refusal(
            "replacement_required",
            f"앞선 시도가 있습니다 — replaces_transition_id로 현재 시도 {leaf}를 명시합니다",
        )
    bound = _listener_bound_elsewhere(db, options.listener_arn, execution_id)
    if bound:
        raise _Refusal(
            "listener_bound",
            f"같은 운영 리스너에 열린 다른 실행의 시도가 있습니다: {bound}",
        )
    previous = next((a.plan for a in attempts if a.transition_id == leaf), None)
    claim = _Context(
        execution_id, execution.status, as_plan, attempts, leaf, leaf, previous
    )
    with repo.mutation_claim(db, claim.identities([options.listener_arn])) as held:
        if not held:
            raise _Refusal(
                "mutation_in_progress",
                "같은 ASG·운영 리스너에 다른 변경 명령이 진행 중입니다",
            )
        current_leaf = _chain_changed(db, execution_id, attempts)
        if current_leaf is not None:
            raise _Refusal(
                "replacement_required",
                f"읽은 뒤 이 실행의 시도가 바뀌었습니다 — 현재 시도: {current_leaf or '없음'}",
            )
        unresolved = _unresolved(db, execution_id)
        if unresolved:
            raise _Refusal(
                "unresolved_operation",
                "결과를 모르는 변경이 있어 새 시도를 세우지 않습니다",
                operations=[_op_view(op) for op in unresolved],
            )
        _close(db)
        if previous is not None:
            footprint = aws.service_footprint(c, previous, as_plan)
            if footprint:
                raise _Refusal(
                    "predecessor_footprint_open",
                    "앞선 시도의 서비스 자원이 남아 있습니다 — return 뒤 cleanup(retain_execution_infrastructure)으로 걷습니다",
                    footprint=footprint,
                )
        inspection = aws.inspect_for_plan(c, options, as_plan)
        source = aws.source_instance(c, as_plan)
        targets = inspection.original_targets
        active, _ = _db_isolation(
            db,
            region=options.region,
            account=options.account_id,
            instance_ids=[
                as_plan.source.instance_id,
                *inspection.member_ids,
                *(t.instance_id for t in targets),
            ],
            execution_id=execution_id,
        )
        _close(db)
        fleet = {
            i: v
            for i, v in active.items()
            if i in {as_plan.source.instance_id, *inspection.member_ids}
        }
        checks = [*inspection.checks, _db_isolation_check(fleet, "원본·ASG 멤버")]
        runtime = _runtime(
            options.runtime_facts, as_plan, source, len(inspection.member_ids)
        )
        if not all(item.passed for item in checks):
            return _result(
                "plan",
                False,
                error="plan_rejected",
                detail="계획 전 확인 실패 — 저장하지 않았습니다",
                checks=_checks_view(checks),
                runtime=runtime,
            )

        new_id = str(uuid.uuid4())
        members = tuple(
            RouteMember(
                instance_id=t.instance_id,
                port=t.port or SERVICE_PORT,
                health_state=t.state,
                quarantined=bool(
                    inspection.original_quarantine.get(t.instance_id)
                    or active.get(t.instance_id)
                ),
                quarantine_basis=tuple(
                    inspection.original_quarantine.get(t.instance_id) or ()
                )
                + tuple(f"DB 격리 실행 {e}" for e in active.get(t.instance_id, ())),
                expected_body=options.original_target_expected_bodies[t.instance_id],
            )
            for t in targets
        )
        plan = ServiceTransitionPlan(
            transition_id=new_id,
            execution_id=execution_id,
            planned_at=_now(),
            account_id=options.account_id,
            region=options.region,
            vpc_id=options.vpc_id,
            service_unit="smoke-http.service",
            service_port=80,
            service_path="/",
            expected_status=200,
            clone_expected_body=options.clone_expected_body,
            load_balancer_arn=options.load_balancer_arn,
            original_route=OriginalRoute(
                listener_arn=options.listener_arn,
                listener_port=80,
                listener_protocol="HTTP",
                target_group_arn=inspection.original_target_group_arn,
                members=members,
                captured_at=_now(),
            ),
            validation_listener_port=options.validation_listener_port,
            target_group_name=transition_target_group_name(new_id),
            probe=ProbeClient(
                instance_id=options.probe_instance_id,
                security_group_id=options.probe_security_group_id,
                private_ip=inspection.probe_private_ip,
            ),
            runtime_facts=options.runtime_facts,
        )
        try:
            row = repo.create_transition(
                db,
                transition_id=new_id,
                execution_id=execution_id,
                supersedes_transition_id=leaf,
                plan=plan.model_dump(mode="json"),
            )
            created_at = row.created_at.isoformat()
        except IntegrityError:
            db.rollback()
            raise _Refusal(
                "attempt_conflict",
                "같은 실행에 다른 시도가 먼저 세워졌습니다 — status로 현재 시도를 봅니다",
            ) from None
        ctx = _Context(
            execution_id,
            execution.status,
            as_plan,
            attempts + (_Attempt(new_id, leaf, plan, created_at),),
            new_id,
            new_id,
            plan,
        )
        _record(
            db,
            ctx,
            "PLAN",
            "원래 경로 기본 동작 캡처·계획 전 확인 통과",
            checks=checks,
            facts={"runtime": runtime, "supersedes_transition_id": leaf},
        )
    return _result(
        "plan",
        True,
        transition_id=new_id,
        execution_id=execution_id,
        supersedes_transition_id=leaf,
        plan=plan.model_dump(mode="json"),
        checks=_checks_view(checks),
        runtime=runtime,
    )


# ------------------------------------------------------------------ status
def _status(db, c, *, execution_id, transition_id, options) -> dict:
    ctx = _load(
        db, execution_id, transition_id, need_transition=False, allow_superseded=True
    )
    plan, as_plan = ctx.plan, ctx.as_plan
    unresolved = [
        (op.operation_id, op.transition_id, op.resource_ref, dict(op.request))
        for op in _unresolved(db, ctx.execution_id)
    ]
    _close(db)
    region, account = _scope(as_plan)
    session = aws.session_checks(c, account_id=account, region=region)
    group = aws.managed_group(c, as_plan)
    source = aws.source_instance(c, as_plan)
    live: dict[str, Any] = {
        "session": _checks_view(session),
        "source_instance": source,
        "asg": {
            "name": as_plan.asg_name,
            "members": [
                {
                    k: m.get(k)
                    for k in (
                        "InstanceId",
                        "LifecycleState",
                        "HealthStatus",
                        "ProtectedFromScaleIn",
                    )
                }
                for m in group.members
            ],
            "problems": group.problems,
        },
    }
    original_targets = ()
    if plan is not None:
        live.update(aws.live_view(aws.observe_live(c, plan, as_plan), plan))
        original_targets = aws.original_route_targets(c, plan)
        live["original_route_targets"] = [
            t.model_dump(mode="json") for t in original_targets
        ]

    reconciliation: list[dict] = []
    if unresolved:
        with repo.mutation_claim(db, ctx.identities()) as held:
            if not held:
                reconciliation.append(
                    {"skipped": "다른 변경 명령이 진행 중이라 대조하지 않았습니다"}
                )
            elif _chain_changed(db, ctx.execution_id, ctx.attempts) is not None:
                _close(db)
                reconciliation.append(
                    {
                        "skipped": "읽은 뒤 이 실행의 시도가 바뀌어 대조하지 않았습니다 — status를 다시 봅니다"
                    }
                )
            else:
                _close(db)
                observed = [
                    (
                        op_id,
                        ref,
                        request,
                        aws.observe_resource(
                            c, ctx.attempt_plan(attempt), request["kind"], ref, request
                        ),
                    )
                    for op_id, attempt, ref, request in unresolved
                ]
                for op_id, ref, request, present in observed:
                    state = None
                    if present is not None:
                        state = (
                            "EFFECT_OBSERVED"
                            if present == request["expect_present"]
                            else "EFFECT_NOT_OBSERVED"
                        )
                        repo.reconcile_operation(db, op_id, reconciled_state=state)
                    reconciliation.append(
                        {
                            "operation_id": op_id,
                            "resource_ref": ref,
                            "reconciled_state": state,
                            "gate_released": state == "EFFECT_OBSERVED",
                        }
                    )
                db.commit()
                _record(
                    db,
                    ctx,
                    "RECONCILE",
                    "불확실 변경의 현물 대조(재전송 없음 · 효과가 보여야만 관문이 풀린다)",
                    checks=[
                        aws.check(
                            "effects_observed",
                            all(r["gate_released"] for r in reconciliation),
                            f"대조 {len(reconciliation)}건",
                        )
                    ],
                    facts={"reconciliation": reconciliation},
                    transition_id=None,
                )

    _, isolation = _db_isolation(
        db,
        region=region,
        account=account,
        instance_ids=[
            as_plan.source.instance_id,
            *group.member_ids,
            *(t.instance_id for t in original_targets),
        ],
        execution_id=ctx.execution_id,
    )
    journal = [
        _op_view(op)
        for op in repo.list_operations(db, execution_ids=[ctx.execution_id])
    ]
    history = [
        {
            "kind": row.kind,
            "verdict": row.verdict,
            "transition_id": row.transition_id,
            "observed_at": row.observed_at.isoformat(),
            "summary": (row.evidence or {}).get("summary"),
        }
        for row in repo.latest_observations(db, ctx.execution_id, limit=10)
    ]
    _close(db)
    return _result(
        "status",
        True,
        transition_id=ctx.transition_id,
        execution_id=ctx.execution_id,
        execution_status=ctx.execution_status.value,
        attempts=ctx.chain_view(),
        superseded=ctx.transition_id is not None and ctx.transition_id != ctx.leaf_id,
        intent=None
        if plan is None
        else {
            "note": "불변 계획 스냅숏 — 현재 상태가 아닙니다",
            "plan": plan.model_dump(mode="json"),
        },
        live=live,
        journal=journal,
        unresolved_operations=sum(1 for op in journal if op["unresolved"]),
        reconciliation=reconciliation,
        history={"note": "과거 관측 — 관측 시각의 사실입니다", "observations": history},
        isolation=isolation,
        runtime=_runtime(
            plan.runtime_facts if plan else RuntimeFacts(),
            as_plan,
            source,
            len(group.members),
        ),
    )


# ------------------------------------------------------------------ prepare
def _prepare(db, c, *, execution_id, transition_id, options) -> dict:
    ctx = _load(db, execution_id, transition_id)
    plan, as_plan = ctx.plan, ctx.as_plan
    with _mutation_gate(db, ctx, listener_binding=True):
        if ctx.execution_status is not ExecutionStatus.SUCCESS:
            raise _Refusal(
                "execution_not_ready",
                f"#446 실행이 SUCCESS가 아닙니다: {ctx.execution_status.value}",
            )
        _close(db)
        live = aws.observe_live(c, plan, as_plan)
        checks = aws.prepare_checks(c, plan, as_plan, live)
        source = aws.source_instance(c, as_plan)
        active, _ = _db_isolation(
            db,
            region=plan.region,
            account=plan.account_id,
            instance_ids=[as_plan.source.instance_id, *live.group.member_ids],
            execution_id=ctx.execution_id,
        )
        _close(db)
        checks.append(_db_isolation_check(active, "원본·ASG 멤버"))
        runtime = _runtime(plan.runtime_facts, as_plan, source, len(live.group.members))
        checks.append(
            aws.check(
                "runtime_cost_gate",
                not runtime["blockers"],
                "; ".join(runtime["blockers"]) or "운영자 상한 안",
            )
        )
        if not all(item.passed for item in checks):
            raise _Refusal(
                "prepare_rejected",
                "준비 전 확인 실패 — AWS를 바꾸지 않았습니다",
                checks=_checks_view(checks),
                runtime=runtime,
            )

        steps: list[dict] = []
        tg_arn = live.target_group_arn

        def step(mutation) -> dict:
            result = _mutate(
                db, ctx, "prepare", mutation, transition_id=ctx.transition_id
            )
            steps.append(result)
            return result

        if live.target_group is None:
            created = step(aws.create_target_group(c, plan, as_plan))
            tg_arn = (created["response"] or {}).get("target_group_arn")
            if created["state"] != "APPLIED" or not tg_arn:
                return _result(
                    "prepare",
                    False,
                    error="step_not_applied",
                    detail="TG 생성이 확정되지 않았습니다",
                    transition_id=ctx.transition_id,
                    steps=steps,
                )
        if (
            live.validation_listener is None
            and step(aws.create_validation_listener(c, plan, as_plan, tg_arn))["state"]
            != "APPLIED"
        ):
            return _result(
                "prepare",
                False,
                error="step_not_applied",
                detail="검증 리스너 생성이 확정되지 않았습니다",
                transition_id=ctx.transition_id,
                steps=steps,
            )
        if (
            tg_arn not in live.attached_target_groups
            and step(aws.attach_target_group(c, as_plan, tg_arn))["state"] != "APPLIED"
        ):
            return _result(
                "prepare",
                False,
                error="step_not_applied",
                detail="ASG에 TG 부착이 확정되지 않았습니다",
                transition_id=ctx.transition_id,
                steps=steps,
            )
    return _result(
        "prepare",
        True,
        transition_id=ctx.transition_id,
        steps=steps,
        checks=_checks_view(checks),
        runtime=runtime,
        target_group_arn=tg_arn,
        unverified="요청 접수까지입니다 — TG 헬스·HTTP 응답은 verify가 확인합니다. 운영 경로는 그대로입니다",
    )


# ------------------------------------------------------------------ verify
def _service_observation(
    db: Session, ctx: _Context, c: aws.Clients, label: str
) -> tuple[ServiceObservation, aws.ServiceInspection]:
    """새 경로의 서비스 증거를 새로 모아 VERIFY 관측으로 남긴다. 호출부는 DB 트랜잭션을 닫아 둔다."""
    inspection = aws.inspect_service(c, ctx.plan, ctx.as_plan)
    active, _ = _db_isolation(
        db,
        region=ctx.plan.region,
        account=ctx.plan.account_id,
        instance_ids=[ctx.as_plan.source.instance_id, *inspection.member_ids],
        execution_id=ctx.execution_id,
    )
    _close(db)
    checks = [*inspection.checks, _db_isolation_check(active, "원본·ASG 멤버")]
    observation = _record(
        db,
        ctx,
        "VERIFY",
        f"{label}: 새 경로 서비스 증거",
        checks=checks,
        http=inspection.http,
        routes=inspection.routes,
        facts={"evidence_scope": _EVIDENCE_SCOPE, "probe_header": PROBE_HEADER},
    )
    return observation, inspection


def _verify(db, c, *, execution_id, transition_id, options) -> dict:
    ctx = _load(db, execution_id, transition_id)
    _close(db)
    observation, _ = _service_observation(db, ctx, c, "verify")
    return _result(
        "verify",
        observation.verdict == "PASSED",
        transition_id=ctx.transition_id,
        observation=observation.model_dump(mode="json"),
    )


# ------------------------------------------------------------------ cutover
_PREPARE_OPERATIONS = (
    aws._OP_CREATE_TARGET_GROUP,
    aws._OP_CREATE_LISTENER,
    aws._OP_ATTACH_TARGET_GROUPS,
)


def _attempt_ops(ops, transition_id: str) -> list[models.ServiceTransitionOperation]:
    return [op for op in ops if op.transition_id == transition_id]


def _cutovers(
    ops, plan: ServiceTransitionPlan
) -> list[models.ServiceTransitionOperation]:
    return [
        op
        for op in _attempt_ops(ops, plan.transition_id)
        if op.aws_operation == aws._OP_MODIFY_LISTENER
        and repo.is_effective(op)
        and op.request.get("target_group_arn") != plan.original_route.target_group_arn
    ]


def _cutover(db, c, *, execution_id, transition_id, options) -> dict:
    ctx = _load(db, execution_id, transition_id)
    plan = ctx.plan
    with _mutation_gate(db, ctx, listener_binding=True) as ops:
        mine = _attempt_ops(ops, ctx.transition_id)
        missing = [
            name
            for name in _PREPARE_OPERATIONS
            if not any(
                op.aws_operation == name and repo.is_effective(op) for op in mine
            )
        ]
        if missing:
            raise _Refusal(
                "prepare_incomplete", f"일지에 확정되지 않은 준비 단계: {missing}"
            )
        if ctx.execution_status is not ExecutionStatus.SUCCESS:
            raise _Refusal(
                "execution_not_ready",
                f"#446 실행이 SUCCESS가 아닙니다: {ctx.execution_status.value}",
            )
        already_ours = bool(_cutovers(ops, plan))
        _close(db)
        observation, inspection = _service_observation(db, ctx, c, "cutover 직전")
        if observation.verdict != "PASSED":
            raise _Refusal(
                "fresh_verification_failed",
                "직전 검증이 통과하지 않아 운영 경로를 바꾸지 않았습니다",
                observation=observation.model_dump(mode="json"),
            )
        runtime = _runtime(
            plan.runtime_facts,
            ctx.as_plan,
            aws.source_instance(c, ctx.as_plan),
            len(inspection.member_ids),
        )
        if runtime["blockers"]:
            raise _Refusal("runtime_cost_gate", runtime["blockers"], runtime=runtime)
        tg_arn = inspection.live.target_group_arn
        original = plan.original_route.target_group_arn
        before = aws.route_snapshot(c.elbv2, plan.original_route.listener_arn)
        clean = before.unsupported is None and before.non_default_rules == 0
        if clean and before.target_group_arn == tg_arn and already_ours:
            _record(
                db,
                ctx,
                "CUTOVER",
                "이미 이 시도의 경로입니다(변경 없음)",
                checks=[
                    aws.check(
                        "route_is_our_cutover",
                        True,
                        "운영 리스너 → 전환 TG(일지의 cutover)",
                    )
                ],
                routes=[before],
            )
            return _result(
                "cutover",
                True,
                transition_id=ctx.transition_id,
                noop=True,
                route=before.model_dump(mode="json"),
            )
        if not (clean and before.target_group_arn == original):
            _record(
                db,
                ctx,
                "CUTOVER",
                "변경 전 경로 드리프트 — 바꾸지 않았습니다",
                checks=[
                    aws.check(
                        "route_before_original",
                        False,
                        f"현재 {before.target_group_arn} / 원래 {original}",
                    )
                ],
                routes=[before],
            )
            raise _Refusal(
                "route_drift",
                "운영 리스너가 계획의 원래 경로가 아닙니다",
                route=before.model_dump(mode="json"),
            )
        step = _mutate(
            db,
            ctx,
            "cutover",
            aws.set_production_route(
                c, plan, target_group_arn=tg_arn, previous_target_group_arn=original
            ),
            transition_id=ctx.transition_id,
        )
        after = aws.route_snapshot(c.elbv2, plan.original_route.listener_arn)
    checks = [
        aws.check("route_before_original", True, f"변경 전 {before.target_group_arn}"),
        aws.check(
            "change_accepted",
            step["state"] == "APPLIED",
            f"modify_listener {step['state']}",
        ),
        aws.check(
            "route_after_new",
            after.target_group_arn == tg_arn and after.non_default_rules == 0,
            f"변경 후 {after.target_group_arn}",
        ),
    ]
    recorded = _record(
        db,
        ctx,
        "CUTOVER",
        "운영 경로 전환",
        checks=checks,
        routes=[before, after],
        facts={
            "atomicity": _NOT_ATOMIC,
            "verification_observed_at": observation.observed_at.isoformat(),
            "runtime": runtime,
        },
    )
    return _result(
        "cutover",
        recorded.verdict == "PASSED",
        transition_id=ctx.transition_id,
        step=step,
        observation=recorded.model_dump(mode="json"),
        atomicity=_NOT_ATOMIC,
        runtime=runtime,
    )


# ------------------------------------------------------------------ return
def _return(db, c, *, execution_id, transition_id, options) -> dict:
    """원래 경로 복귀. 이미 원래 경로면 바꾸지 않되 같은 안전·실제 응답 확인을 거쳐야 성공이다.

    등록이 남은 원래 TG 대상은 상태(draining·unused 포함)와 관계없이 전부 격리 사실을 본다. 복귀 뒤 요청을
    받을 수 있는 대상(return_routing)은 전부 직접 응답으로 확인돼야 한다. 대상을 등록·해제·격리 해제하지 않는다.
    """
    ctx = _load(db, execution_id, transition_id)
    plan = ctx.plan
    original = plan.original_route.target_group_arn
    step = None
    with _mutation_gate(db, ctx, listener_binding=False) as ops:
        ours = _cutovers(ops, plan)
        _close(db)
        inspection = aws.inspect_return(c, plan)
        before = inspection.production
        if not all(item.passed for item in inspection.checks):
            recorded = _record(
                db,
                ctx,
                "RETURN",
                "복귀 전제(세션·탐침 출발지) 실패 — HTTP·변경 없음",
                checks=inspection.checks,
                routes=[before],
            )
            raise _Refusal(
                "return_prerequisite_failed",
                "세션·탐침 출발지 전제가 맞지 않아 복귀하지 않았습니다",
                observation=recorded.model_dump(mode="json"),
            )
        clean = before.unsupported is None and before.non_default_rules == 0
        already = clean and before.target_group_arn == original
        our_targets = {op.request.get("target_group_arn") for op in ours}
        if not already and not (clean and before.target_group_arn in our_targets):
            _record(
                db,
                ctx,
                "RETURN",
                "현재 경로가 이 시도의 cutover가 아닙니다 — 바꾸지 않았습니다",
                checks=[
                    aws.check(
                        "route_is_our_cutover", False, f"현재 {before.target_group_arn}"
                    )
                ],
                routes=[before],
            )
            raise _Refusal(
                "route_drift",
                "운영 리스너가 이 시도의 cutover 경로가 아닙니다",
                route=before.model_dump(mode="json"),
            )
        route_check = (
            aws.check(
                "route_already_original",
                True,
                f"이미 원래 경로 {original} — 바꾸지 않고 안전·응답만 확인합니다",
            )
            if already
            else aws.check(
                "route_is_our_cutover",
                True,
                f"현재 {before.target_group_arn}(일지의 cutover)",
            )
        )

        active, _ = _db_isolation(
            db,
            region=plan.region,
            account=plan.account_id,
            instance_ids=[t.instance_id for t in inspection.original_targets],
            execution_id=ctx.execution_id,
        )
        _close(db)
        expected = {m.instance_id: m.expected_body for m in plan.original_route.members}
        quarantined, unverified, not_routable, probes, verified = {}, {}, {}, [], []
        for target in inspection.original_targets:
            reasons = [
                *inspection.quarantine.get(target.instance_id, ()),
                *(f"DB 격리 실행 {e}" for e in active.get(target.instance_id, ())),
            ]
            if reasons:
                quarantined[target.instance_id] = reasons
                continue
            routing, basis = inspection.routing[(target.instance_id, target.port)]
            if routing == "not_routable":
                not_routable[target.instance_id] = basis
                continue
            if routing == "unknown":
                unverified[target.instance_id] = basis
                continue
            if target.instance_id not in expected:
                unverified[target.instance_id] = "계획에 기대 본문이 없는 대상"
                continue
            if not inspection.probe_access.get(target.instance_id):
                unverified[target.instance_id] = "탐침 SG 접근을 확인하지 못함"
                continue
            address = inspection.private_ips.get(target.instance_id)
            evidence = aws.http_probe(
                c.http,
                path_kind="member_direct",
                host=address,
                port=SERVICE_PORT,
                peers=(address,) if address else (),
                expected_bodies=(expected[target.instance_id],),
                instance_id=target.instance_id,
            )
            probes.append(evidence)
            if evidence.passed:
                verified.append(target.instance_id)
            else:
                unverified[target.instance_id] = "직접 응답이 기대 본문과 다름"
        pre_checks = [
            route_check,
            aws.check(
                "no_quarantined_registered",
                not quarantined,
                f"원래 TG에 등록된 보안 보류 대상 {quarantined or '없음'} — 해제·등록 해제하지 않습니다",
            ),
            aws.check(
                "routable_targets_verified",
                not unverified,
                f"확인하지 못한 라우팅 대상 {unverified or '없음'}",
            ),
            aws.check(
                "original_route_usable",
                bool(verified),
                f"직접 응답 확인 {verified or '없음'} · 지금 라우팅되지 않는 대상 {not_routable or '없음'}",
            ),
        ]
        if not all(item.passed for item in pre_checks):
            # 직접 응답은 판정 근거라 그대로 남기지만, 실패 확인이 있으면 관측 판정은 FAILED다
            recorded = _record(
                db,
                ctx,
                "RETURN",
                "원래 경로가 안전·사용 가능으로 확인되지 않아 바꾸지 않았습니다",
                checks=pre_checks,
                http=probes,
                routes=[before],
                facts={
                    "quarantined": quarantined,
                    "unverified": unverified,
                    "not_routable": not_routable,
                },
            )
            raise _Refusal(
                "original_route_unusable",
                "원래 경로의 라우팅 대상이 모두 확인되지 않았습니다",
                observation=recorded.model_dump(mode="json"),
            )
        if not already:
            step = _mutate(
                db,
                ctx,
                "return",
                aws.set_production_route(
                    c,
                    plan,
                    target_group_arn=original,
                    previous_target_group_arn=before.target_group_arn,
                ),
                transition_id=ctx.transition_id,
            )
        after = aws.route_snapshot(c.elbv2, plan.original_route.listener_arn)
        response = aws.http_probe(
            c.http,
            path_kind="alb_production_listener",
            host=inspection.dns_name,
            port=SERVICE_PORT,
            peers=aws.alb_private_ips(c.ec2, plan.load_balancer_arn),
            expected_bodies=tuple(sorted({expected[i] for i in verified})),
        )
    accepted = step is None or step["state"] == "APPLIED"
    # 경로 복귀(route_restored)와 실제 운영 ALB 응답(service_response_verified)은 따로 보고한다
    route_restored = accepted and after.target_group_arn == original
    checks = [*pre_checks]
    if step is not None:
        checks.append(
            aws.check("change_accepted", accepted, f"modify_listener {step['state']}")
        )
    checks.append(
        aws.check(
            "route_after_original",
            after.target_group_arn == original,
            f"지금 {after.target_group_arn}",
        )
    )
    summary = (
        "이미 원래 경로 — 바꾸지 않고 안전·실제 응답을 다시 확인"
        if already
        else "원래 경로 복귀(삭제·축소 없음)"
    )
    recorded = _record(
        db,
        ctx,
        "RETURN",
        summary,
        checks=checks,
        http=[*probes, response],
        routes=[before, after],
        facts={"not_routable": not_routable, "atomicity": _NOT_ATOMIC},
    )
    return _result(
        "return",
        recorded.verdict == "PASSED",
        transition_id=ctx.transition_id,
        noop=already,
        step=step,
        route_restored=route_restored,
        service_response_verified=response.passed,
        observation=recorded.model_dump(mode="json"),
        atomicity=_NOT_ATOMIC,
    )


# ------------------------------------------------------------------ cleanup
def _cleanup_context(db: Session, execution_id, transition_id) -> _Context:
    """정리는 실행 단위다 — 시도가 없어도(#446 부분 실패·고아) 되고, 대체된 시도 ID로 불러도 실행 전체를 본다."""
    ctx = _load(
        db, execution_id, transition_id, need_transition=False, allow_superseded=True
    )
    if ctx.execution_status in EXECUTION_NON_TERMINAL_STATUSES:
        raise _Refusal(
            "execution_running",
            f"#446 실행이 끝나지 않았습니다({ctx.execution_status.value}) — 정리하지 않습니다",
        )
    return ctx


def _recovery_refs(
    db: Session, target_group_arns: Iterable[str]
) -> dict[str, list[str]]:
    """TG → 그 TG를 불변 SG/TG 백업의 복원 대상으로 가리키는 열린 EC2_ISOLATE 실행. DB를 읽기만 한다.

    해제(ROLLED_BACK)됐거나 자산을 만지지 않고 실패한 격리는 세지 않는다(isolation_facts의 active).
    """
    arns = sorted(set(target_group_arns))
    if not arns:
        return {}
    refs: dict[str, list[str]] = {}
    for fact in repo.isolation_facts(
        db, target_arns=(), restore_target_group_arns=arns
    ):
        if fact["active"] and fact["restore_target_group_arn"] in arns:
            refs.setdefault(fact["restore_target_group_arn"], []).append(
                fact["execution_id"]
            )
    return refs


def _inventory_with_holds(
    db: Session, c: aws.Clients, ctx: _Context, retention
) -> tuple[aws.Inventory, dict]:
    inventory = aws.cleanup_inventory(
        c, ctx.as_plan, ctx.plans, retention_decision=retention
    )
    holds = _security_holds(
        db, ctx, inventory.security_holds, inventory.at_risk_instances
    )
    inventory.hold_for_recovery(_recovery_refs(db, inventory.target_groups))
    _close(db)
    db_only = sorted(set(holds) - set(inventory.security_holds))
    if db_only and inventory.destroys_instances:
        inventory.blockers.append(
            f"DB의 열린 격리 실행이 있는 인스턴스를 없애지 않습니다: {db_only}"
        )
    return inventory, holds


def _inventory_view(
    inventory: aws.Inventory, ctx: _Context, holds: dict, runtime: dict
) -> dict:
    return {
        "observed_at": inventory.observed_at.isoformat(),
        "attempts": ctx.chain_view(),
        "manifest": inventory.manifest.model_dump(mode="json"),
        "manifest_sha256": inventory.manifest.digest(),
        "resource_ids": list(inventory.manifest.resource_ids()),
        "cleanup_allowed": not inventory.blockers and bool(inventory.manifest.items),
        # 복원이 기대는 TG가 남아 있으면 완료가 아니다(보존 결정으로 남긴 것과 다르다)
        "complete": not inventory.manifest.items
        and not inventory.unknown
        and not inventory.recovery_holds,
        "blockers": inventory.blockers,
        "pending": [item.model_dump(mode="json") for item in inventory.manifest.items],
        "retained": inventory.retained,
        "unknown": inventory.unknown,
        "security_holds": holds,
        "security_hold_note": (
            "보안 보류 인스턴스는 보호 해제·격리 해제·등록 해제 없이 남깁니다 — 과금이 계속됩니다"
            if holds
            else None
        ),
        "recovery_holds": inventory.recovery_holds,
        "recovery_hold_note": (
            "열린 격리의 복원(UNISOLATE)이 쓰는 TG·부착은 지우거나 떼지 않습니다 — 격리가 풀린 뒤 다시 봅니다"
            if inventory.recovery_holds
            else None
        ),
        "billing_note": RETAINED_BILLING_WARNING if inventory.retained else None,
        "counts": inventory.counts,
        "runtime": runtime,
    }


def _cleanup_runtime(c: aws.Clients, ctx: _Context, inventory: aws.Inventory) -> dict:
    facts = ctx.plan.runtime_facts if ctx.plan is not None else RuntimeFacts()
    return _runtime(
        facts,
        ctx.as_plan,
        aws.source_instance(c, ctx.as_plan),
        inventory.counts.get("asg_members"),
    )


def _cleanup_plan(
    db, c, *, execution_id, transition_id, options: CleanupPlanOptions
) -> dict:
    ctx = _cleanup_context(db, execution_id, transition_id)
    unresolved = _unresolved(db, ctx.execution_id)
    _close(db)
    inventory, holds = _inventory_with_holds(db, c, ctx, options.retention_decision)
    if unresolved:
        inventory.blockers.append(
            f"결과를 모르는 변경 {len(unresolved)}건 — 의도한 효과가 status로 확인될 때까지 정리하지 않습니다"
        )
    view = _inventory_view(inventory, ctx, holds, _cleanup_runtime(c, ctx, inventory))
    _record(
        db,
        ctx,
        "CLEANUP",
        "정리 재고(읽기 전용)",
        checks=[
            aws.check(
                "cleanup_allowed",
                not inventory.blockers,
                "; ".join(inventory.blockers) or "차단 없음",
            )
        ],
        facts={
            "manifest_sha256": view["manifest_sha256"],
            "resource_ids": view["resource_ids"],
            "retention_decision": options.retention_decision,
        },
        transition_id=None,
    )
    return _result(
        "cleanup-plan",
        True,
        execution_id=ctx.execution_id,
        transition_id=ctx.leaf_id,
        **view,
    )


def _cleanup(db, c, *, execution_id, transition_id, options: CleanupOptions) -> dict:
    ctx = _cleanup_context(db, execution_id, transition_id)
    steps: list[dict] = []
    waiting = security_hold = recovery_hold = reference_hold = None
    with _mutation_gate(db, ctx, listener_binding=False, aggregate=True):
        _close(db)
        inventory, holds = _inventory_with_holds(db, c, ctx, options.retention_decision)
        if inventory.blockers:
            raise _Refusal(
                "cleanup_blocked",
                inventory.blockers,
                inventory=_inventory_view(
                    inventory, ctx, holds, _cleanup_runtime(c, ctx, inventory)
                ),
            )
        digest = inventory.manifest.digest()
        if options.manifest_sha256 != digest or sorted(options.resource_ids) != sorted(
            inventory.manifest.resource_ids()
        ):
            raise _Refusal(
                "manifest_mismatch",
                "승인한 매니페스트가 현재 재고와 다릅니다 — cleanup-plan을 다시 봅니다",
                current_manifest_sha256=digest,
                inventory=_inventory_view(
                    inventory, ctx, holds, _cleanup_runtime(c, ctx, inventory)
                ),
            )
        for item in inventory.manifest.items:
            reason = aws.cleanup_gate(c, item, ctx.as_plan)
            if reason is not None:
                waiting = {"item": item.model_dump(mode="json"), "reason": reason}
                break
            if item.action == "delete_auto_scaling_group":
                # 인스턴스를 없애는 단계 바로 앞에서 보안 보류를 다시 읽는다 — 계획 뒤의 격리·보호를 놓치지 않는다
                at_risk, aws_holds = aws.security_holds(c, ctx.as_plan)
                current = _security_holds(db, ctx, aws_holds, at_risk)
                _close(db)
                if current:
                    security_hold = {
                        "item": item.model_dump(mode="json"),
                        "holds": current,
                    }
                    break
            if item.kind in ("target_group_attachment", "target_group"):
                # TG를 떼거나 지우기 바로 앞에서 열린 격리의 복원 참조를 다시 읽는다
                target_group = item.resource_id.split("|", 1)[-1]
                refs = _recovery_refs(db, [target_group])
                _close(db)
                if refs:
                    recovery_hold = {
                        "item": item.model_dump(mode="json"),
                        "isolations": refs[target_group],
                    }
                    break
            shared_refs = aws.shared_resource_refs(c, item, ctx.as_plan)
            if shared_refs:
                reference_hold = {
                    "item": item.model_dump(mode="json"),
                    "references": shared_refs,
                }
                break
            mutation = aws.cleanup_mutation(c, item, ctx.as_plan)
            if mutation is None:
                steps.append(
                    {
                        "item": item.model_dump(mode="json"),
                        "state": "OBSERVED_TERMINATED",
                    }
                )
                continue
            step = _mutate(
                db, ctx, "cleanup", mutation, transition_id=item.transition_id
            )
            steps.append({"item": item.model_dump(mode="json"), **step})
            if step["state"] != "APPLIED":
                break
        # 완료는 삭제 응답이 아니라 새로 읽은 잔여·참조로 판단한다
        residual, residual_holds = _inventory_with_holds(
            db, c, ctx, options.retention_decision
        )
    failed = [s for s in steps if s.get("state") in ("NOT_APPLIED", "UNCERTAIN")]
    stopped = (
        security_hold is not None
        or recovery_hold is not None
        or reference_hold is not None
    )
    view = _inventory_view(
        residual, ctx, residual_holds, _cleanup_runtime(c, ctx, residual)
    )
    next_action = None
    if not view["complete"]:
        next_action = "남은 항목은 기다린 뒤 cleanup-plan으로 새 매니페스트를 승인해 다시 실행합니다"
    if reference_hold is not None:
        next_action = (
            "공유 참조로 정리를 중단했습니다. 운영자가 참조 관계를 확인·해결한 뒤 "
            "cleanup-plan으로 새 매니페스트를 승인해야 합니다"
        )
    _record(
        db,
        ctx,
        "CLEANUP",
        "정리 실행(기록은 지우지 않음)",
        checks=[
            aws.check(
                "cleanup_steps",
                not failed and not stopped,
                f"단계 {len(steps)}건 · 실패 {len(failed)}건 · 보안 보류 {bool(security_hold)}"
                f" · 복원 참조 보류 {bool(recovery_hold)} · 공유 참조 보류 {bool(reference_hold)}",
            )
        ],
        facts={
            "manifest_sha256": digest,
            "waiting": waiting,
            "security_hold": security_hold,
            "recovery_hold": recovery_hold,
            "reference_hold": reference_hold,
            "residual_manifest_sha256": view["manifest_sha256"],
        },
        transition_id=None,
    )
    return _result(
        "cleanup",
        not failed and not stopped,
        execution_id=ctx.execution_id,
        transition_id=ctx.leaf_id,
        steps=steps,
        waiting=waiting,
        security_hold=security_hold,
        recovery_hold=recovery_hold,
        reference_hold=reference_hold,
        complete=not failed and not stopped and view["complete"],
        residual=view,
        next=next_action,
    )


# ------------------------------------------------------------------ schema
_PLAN_EXAMPLE = {
    "account_id": "123456789012",
    "region": "ap-northeast-2",
    "vpc_id": "vpc-0123456789abcdef0",
    "load_balancer_arn": "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:loadbalancer/app/vigilantis-smoke-alb/0123456789abcdef",
    "listener_arn": "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:listener/app/vigilantis-smoke-alb/0123456789abcdef/fedcba9876543210",
    "validation_listener_port": 8080,
    "probe_instance_id": "i-0123456789abcdef0",
    "probe_security_group_id": "sg-0123456789abcdef0",
    "clone_expected_body": "vigilantis-smoke-idle-dev\n",
    "original_target_expected_bodies": {
        "i-0aaaaaaaaaaaaaaa1": "vigilantis-smoke-web-1\n",
        "i-0aaaaaaaaaaaaaaa2": "vigilantis-smoke-web-2\n",
    },
    "runtime_facts": {
        "captured_instance_type_hourly_usd": "<운영자 공급 단가>",
        "simultaneous_cost_cap_usd_per_hour": "<운영자 공급 상한>",
        "vcpu_quota": "<운영자 공급 vCPU 한도>",
        "rto_minutes": "<운영자 공급 RTO(분)>",
    },
    "replaces_transition_id": None,
}


def command_schema() -> dict:
    """옵션 계약과 예시 — DB·AWS 없이 답한다."""
    return _result(
        "schema",
        True,
        facade="service_transitions.run_service_command",
        clients={
            "required": list(aws.REQUIRED_CLIENT_KEYS),
            "optional": {
                "http": "HttpProbe(origin_instance_id·get) — 없으면 DirectHttpProbe"
            },
        },
        commands={
            name: {
                "mutating": name in MUTATING_COMMANDS,
                "ids": "execution_id"
                if name == "plan"
                else (
                    "없음" if name == "schema" else "execution_id 또는 transition_id"
                ),
                "options_schema": _OPTIONS.get(name, NoOptions).model_json_schema(),
            }
            for name in COMMANDS
        },
        examples={
            "plan": _PLAN_EXAMPLE,
            "plan_replacement": {
                "...": "plan과 같다",
                "replaces_transition_id": "<status의 현재 시도(leaf)>",
            },
            "cleanup-plan": {"retention_decision": "delete_execution_resources"},
            "cleanup": {
                "retention_decision": "delete_execution_resources",
                "manifest_sha256": "<cleanup-plan의 manifest_sha256>",
                "resource_ids": ["<cleanup-plan의 resource_ids 그대로>"],
            },
        },
        prerequisites=[
            "관리자 운영 프로필 전용 — 앱 IAM 정책에 이 변경 권한을 주지 않는다",
            "verify·cutover·return은 지정 탐침 인스턴스에서 실행한다(IMDSv2 instance-id가 계획의 탐침과 같아야 HTTP를 보낸다)",
            "ALB SG는 검증 포트를 탐침 SG에서만 받는다(CIDR·접두 목록·다른 SG 없음)",
            "ASG 멤버와 원래 경로 대상의 주 ENI SG는 80/tcp를 탐침 SG에서 받는다",
            "이 도구는 탐침 호스트·SG 규칙·DNS·TLS를 만들지 않는다",
            "prepare·cutover는 runtime_facts의 단가·동시 운용 compute 상한·vCPU 한도·RTO(운영자 공급값)와 원본 인스턴스 관측이 있어야 한다. 한도·RTO는 공급 여부만 확인하며 실제 AWS 한도나 복귀 시간 충족을 검증하지 않는다",
            "앞선 시도가 있으면 plan은 replaces_transition_id로 현재 시도를 명시하고, 앞선 시도의 서비스 자원이 없어야 한다",
            "cleanup은 #446 실행이 끝난 뒤 retention_decision과 cleanup-plan의 매니페스트(digest·resource_ids)를 그대로 요구한다",
            "보안 보류(격리 SG·scale-in 보호·DB 격리 실행) 인스턴스는 정리하지 않는다 — 풀어 주는 동작이 없다",
        ],
        evidence_scope=_EVIDENCE_SCOPE,
        atomicity=_NOT_ATOMIC,
    )


_HANDLERS = {
    "plan": _plan,
    "status": _status,
    "prepare": _prepare,
    "verify": _verify,
    "cutover": _cutover,
    "return": _return,
    "cleanup-plan": _cleanup_plan,
    "cleanup": _cleanup,
}

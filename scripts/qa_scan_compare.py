# ==============================================================================
# [파일 설명]  담당: 김승철 (QA & Scenario)
# 실 AWS 스모크의 수집·판정 대조(A3·A9) 조회를 한 번에 찍는 읽기 전용 스크립트.
# `docs/AWS_SMOKE_RESULT.md` §2 의 칸을 그 순서대로 출력한다 — 스모크 당일에 쿼리를
# 조립하지 않기 위한 것이다(조립하면 틀리고, 틀린 것을 결과로 남기면 되돌릴 수 없다).
#
# 실행 (repo 루트, DB 기동 후):
#   PowerShell: uv run python scripts/qa_scan_compare.py
#   bash      : uv run python scripts/qa_scan_compare.py
#   특정 회차만: ... scripts/qa_scan_compare.py --run <collection_run_id>
#
# **읽기만 한다.** INSERT·UPDATE·DELETE 를 하지 않으므로 실 AWS 를 향한 DB 에 그대로
# 돌려도 된다. 대신 어느 DB 를 읽었는지 머리말에 찍는다 — LocalStack 회차를 실 AWS
# 결과로 적는 것이 이 대조에서 가장 비싼 실수다. 회차의 `mode` 열이 그 구분의 원천이다.
#
# 리전은 좁히지 않는다(`latest_collection_run_per_region(db, None)`). 설정에 두 번째
# 리전이 남아 있으면 그 리전 회차가 매번 FAILED 로 마감될 수 있는데(ADR-0009 §6-3), 좁혀
# 보면 그 사실이 보이지 않는다 — 리전이 둘 이상 찍히는 것 자체가 관측 결과다. 출력은
# 원인을 단정하지 않고 리전별 상태만 적는다(`_region_notice`).
#
# 종료 코드는 **판정과 무관하게 0** 이다 — 이 스크립트는 판정하지 않고 적을 값을 보여 주며,
# 해소/미해소 판단은 사람이 §5 에 적는다. 단 인자 오류(2)·처리 안 된 예외(1)는 예외이고,
# DB 접속 실패는 화면에 사유를 찍고 0 으로 끝낸다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Mapping, Optional, Sequence

# Windows 기본 콘솔(cp949)은 이 파일의 한국어·em dash 를 못 낸다 — 팀 개발 환경이
# Windows 라 출력 스트림을 UTF-8 로 고정한다(scripts/inject_mock_threat.py 와 같은 이유).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # 파이프·리다이렉트 등 재설정 불가
        pass

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "apps" / "core-api", REPO_ROOT / "packages"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from config import get_aws_settings, get_settings  # noqa: E402
from db import models  # noqa: E402
from db.repositories.assets import (  # noqa: E402
    INVESTIGATE_KINDS,
    find_dangling_arns,
    latest_collection_run_per_region,
    summarize_dangling,
)
from schemas.api.assets import AssetType  # noqa: E402
from services.rule_engine import MIN_DATAPOINTS  # noqa: E402

KST = timezone(timedelta(hours=9))

# §2-2 는 7 유형을 모두 줄로 들고 있어야 한다 — 수집기가 못 본 유형은
# asset_inventory_counts 에 행을 남기지 않으므로(models.AssetInventoryCount), 유형 목록을
# 관측 결과에서 끌어오면 그 유형이 표에서 사라진다. "0 건" 과 "모름" 을 가르는 자리다.
_ALL_TYPES = tuple(AssetType)


def _session_factory(database_url: str) -> sessionmaker[Session]:
    """DSN 을 받아 세션 팩토리를 만든다. `db.session.get_session_factory()` 는 인자를 받지
    않고 `get_settings().DATABASE_URL` 하나만 보는데(엔진은 프로세스당 1개로 캐시), 이
    스크립트는 `.env` 의 컨테이너 DSN(`db:5432`)과 호스트에서 보이는 DSN
    (`localhost:${POSTGRES_PORT}`)을 모두 받아야 한다. 읽기 전용 조회 하나를 위해 운영
    경로의 엔진 캐시를 건드리지 않고 여기서 따로 만든다 — 옵션은 운영과 같게 둔다."""
    engine = create_engine(database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _kst(value: Optional[datetime]) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def _failures(error_summary: Optional[str]) -> tuple[dict[str, str], str]:
    """error_summary → (라벨→사유, 읽은 방식). `_failures_summary` 가 실은 compact JSON 을
    되돌린다. 과거 행이나 상한 초과 표식(`_truncated`)도 있을 수 있어 파싱 실패를 삼키지
    않고 원문을 그대로 돌려준다 — 사유 코드가 이 대조의 핵심 값이다."""
    if not error_summary:
        return {}, "빈 값"
    try:
        parsed = json.loads(error_summary)
    except (ValueError, TypeError):
        return {}, f"JSON 아님 — 원문 {error_summary!r}"
    if not isinstance(parsed, dict):
        return {}, f"dict 아님 — 원문 {error_summary!r}"
    if "_truncated" in parsed:
        return {}, f"상한 초과로 항목 생략 — {error_summary!r}"
    return {str(k): str(v) for k, v in parsed.items()}, "JSON"


def _region_notice(pairs: Sequence[tuple[str, str]]) -> list[str]:
    """리전이 둘 이상일 때의 주의 줄. `(리전, 회차 상태)` 쌍만 받는다 — 원인을 단정하지
    않고 **사실만** 찍기 위해서다. 스크립트는 리전이 둘인 이유를 모른다(리뷰 ③-2).
    ADR-0009 §6-3 은 "이럴 수 있다"는 참고로만 붙인다."""
    if len(pairs) <= 1:
        return []
    states = ", ".join(f"{region}={status}" for region, status in pairs)
    return [
        f"\n주의: 리전이 {len(pairs)}개 찍혔다 — {states}",
        "  참고: 설정에 남은 리전이 매 회차 FAILED 로 마감되는 경로가 있다(ADR-0009 §6-3).",
    ]


def _arn_account(arn: str) -> str:
    """ARN 의 계정 칸. 형식이 아니면 빈 문자열 — 계정 비교는 표시용이라 여기서 거절하지 않는다."""
    parts = arn.split(":")
    return parts[4] if len(parts) > 5 else ""


def _account_mark(account: str, run_accounts: Collection[str]) -> str:
    """조사 대상 한 줄에 붙일 계정 표시.

    **계정 칸을 읽지 못한 것을 "다른 계정"으로 적지 않는다.** ARN 형식이 아니거나 계정 칸이
    비면(서비스 전용 ARN 등) `_arn_account` 가 빈 문자열을 주는데, 그것을 그대로 비교하면
    회차 계정 집합에 없으니 타 계정으로 찍힌다 — 기록하는 사람이 "남의 것"으로 보고 빼
    버린다. 모르는 것은 모른다고 적고 §7 조사로 넘긴다.
    """
    if not account:
        return "  (계정 확인 불가)"
    return "" if account in run_accounts else "  (다른 계정)"


def _failure_lines(error_summary: Optional[str], mode: str) -> list[str]:
    """`collector_failures` 구역의 출력 줄. 순수 함수로 떼어 둔 이유는 **빈 값과 "읽지
    못했다"를 가르는 책임이 여기 있기** 때문이다. 둘을 섞으면 실패가 없었던 것처럼
    기록되고, 그 기록이 이월 4행 처분의 근거가 된다(PR #421 리뷰 ③-1).

    사유 코드 주석은 회차의 `mode` 로 가른다 — `InternalFailure` 는 실 AWS 에서도
    나오는 코드라, LocalStack 라이선스 설명을 실 AWS 회차에 붙이면 거짓이 된다.
    """
    failures, how = _failures(error_summary)
    if failures:
        lines = ["  collector_failures : 라벨 → 사유 코드"]
        for label, reason in sorted(failures.items()):
            note = ""
            if reason == "AccessDenied":
                note = "  <- 권한 누락. provision_smoke_aws.py policy 와 대조"
            elif reason == "InternalFailure":
                note = (
                    "  <- LocalStack 라이선스 밖"
                    if mode != "aws"
                    else "  <- AWS 측 내부 오류 — 재시도 후에도 같으면 §7에 기록"
                )
            lines.append(f"      {label:<24} {reason}{note}")
        return lines
    if how == "빈 값":
        return ["  collector_failures : (없음)"]
    # JSON 으로 읽혔는데 항목이 비는 경우({})와, 아예 읽지 못한 경우를 가른다.
    if how == "JSON":
        return ["  collector_failures : (없음 — error_summary 가 빈 묶음이다)"]
    return ["  collector_failures : 확인 불가 — 위 원문 참조"]


def _dangling_lines(
    findings: Sequence[Any], summary: Mapping[str, Any], run_accounts: Collection[str]
) -> list[str]:
    """§2-4 조인 무결성 구역의 출력 줄.

    **줄 조립을 호출부에서 떼어 둔 이유**는 `_account_mark` 가 실제로 쓰이는지까지
    테스트가 지키게 하려는 것이다(PR #428 리뷰 nit 2). 표시 함수만 테스트하면 호출부에서
    그 함수를 빼도 통과한다 — 리뷰어가 되돌려 보고 통과를 확인했다.

    조사 대상은 `INVESTIGATE_KINDS` 로 좁힌다. 위협 접수의 미등록 대상이나 가드레일 ③ 이
    거절한 후보처럼 **매달린 것이 정상인 자리**가 있어서, 전부 찍으면 정상 보존분이 조사
    대상으로 읽힌다(`repositories/assets.find_dangling_arns`).
    """
    lines = [
        f"  total {summary['total']} / investigate {summary['investigate']}",
        f"  종류별: {summary['by_kind'] or '(없음)'}",
    ]
    if not summary["investigate"]:
        lines.append(
            "  investigate 0 건 — DB 전체 기준으로 기준선(2026-09-14·09-16 실측 0건)과 같다."
        )
        return lines
    lines.append("  조사 대상:")
    for finding in findings:
        if finding.kind not in INVESTIGATE_KINDS:
            continue
        mark = _account_mark(_arn_account(finding.value), run_accounts)
        lines.append(
            f"      [{finding.kind}] {finding.value}"
            f"  자리={','.join(finding.sources)}{mark}"
        )
    return lines


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _report_run(db: Session, run: models.CollectionRun) -> None:
    _section(f"[회차] {run.region} · {run.collection_run_id}")
    print(f"  mode               : {run.mode}      <- 'aws' 가 아니면 실 AWS 회차가 아니다")
    print(f"  status             : {run.status.value}")
    print(f"  account_id         : {run.account_id}")
    print(f"  started / finished : {_kst(run.started_at)} / {_kst(run.finished_at)}")
    print(f"  lookback / period  : {run.lookback_days}일 / {run.period_seconds}초")

    _, how = _failures(run.error_summary)
    print(f"  error_summary      : {run.error_summary or '(빈 값)'}  [{how}]")
    for line in _failure_lines(run.error_summary, run.mode):
        print(line)

    # ----- §2-2 자산 유형별 수집 수
    counts = dict(
        db.execute(
            select(models.AssetInventoryCount.asset_type, models.AssetInventoryCount.count).where(
                models.AssetInventoryCount.collection_run_id == run.collection_run_id
            )
        ).all()
    )
    print()
    print("  §2-2 자산 유형별 수집 수")
    for asset_type in _ALL_TYPES:
        if asset_type in counts:
            print(f"      {asset_type.value:<20} {counts[asset_type]}")
        else:
            print(f"      {asset_type.value:<20} 미관측 — 조회가 실패해 이 회차가 못 본 유형(0건이 아니다)")

    # ----- §2-3 판정 분포
    rows = db.execute(
        select(
            models.RuleEvaluation.verdict,
            models.RuleEvaluation.skip_reason_code,
            models.Asset.asset_type,
            models.Asset.name,
            models.Asset.resource_id,
        )
        .join(models.Asset, models.Asset.asset_id == models.RuleEvaluation.asset_id)
        .where(models.RuleEvaluation.collection_run_id == run.collection_run_id)
    ).all()
    print()
    print(f"  §2-3 판정 분포 (판정 {len(rows)}건)")
    if not rows:
        # 원인을 단정하지 않는다 — 판정이 안 돈 회차뿐 아니라 FAILED 회차나 자산 0건
        # 회차도 판정 행이 없다. 스크립트는 그 원인을 확인하지 않는다(리뷰 ③-2).
        print(
            f"      (이 회차에 판정 행이 없다 — status={run.status.value},"
            f" 수집 자산 {sum(counts.values())}건)"
        )
    else:
        buckets: Counter[tuple[str, str]] = Counter()
        targets: dict[tuple[str, str], list[str]] = {}
        for verdict, skip_reason, asset_type, name, resource_id in rows:
            key = (verdict or "-", skip_reason or "-")
            buckets[key] += 1
            targets.setdefault(key, []).append(f"{name or resource_id}({asset_type.value})")
        for (verdict, skip_reason), count in sorted(buckets.items()):
            label = verdict if skip_reason == "-" else f"{verdict} / {skip_reason}"
            listed = ", ".join(sorted(targets[(verdict, skip_reason)])[:6])
            more = "" if len(targets[(verdict, skip_reason)]) <= 6 else " ..."
            print(f"      {label:<36} {count:>3}  {listed}{more}")

    # ----- 48 관측치 게이트 (A7·A9 가능 여부의 실측 근거)
    metrics = db.execute(
        select(
            models.Asset.name,
            models.Asset.resource_id,
            models.MetricSummary.cpu_datapoints,
            models.MetricSummary.cpu_avg,
            models.MetricSummary.cpu_max,
            models.MetricSummary.window_start,
            models.MetricSummary.window_end,
        )
        .join(models.Asset, models.Asset.asset_id == models.MetricSummary.asset_id)
        .where(models.MetricSummary.collection_run_id == run.collection_run_id)
        .order_by(models.MetricSummary.cpu_datapoints.desc())
    ).all()
    print()
    print(f"  CPU 관측치 — MIN_DATAPOINTS={MIN_DATAPOINTS} 게이트 (§1 · A7 · A9)")
    if not metrics:
        print("      (메트릭 요약 없음)")
    for name, resource_id, points, avg, cpu_max, win_start, win_end in metrics:
        gate = "충족" if points >= MIN_DATAPOINTS else f"부족 {MIN_DATAPOINTS - points}개"
        avg_text = "-" if avg is None else f"{avg:.2f}"
        max_text = "-" if cpu_max is None else f"{cpu_max:.2f}"
        print(
            f"      {(name or resource_id):<34} {points:>4}개  {gate:<10}"
            f" avg {avg_text:>7} max {max_text:>7}  창 {_kst(win_start)}~{_kst(win_end)}"
        )
    if metrics and all(p < MIN_DATAPOINTS for _, _, p, _, _, _, _ in metrics):
        print(
            f"      ↑ 전부 미달이다 — evaluate_ec2 는 관측치 검사가 prod 검사보다 앞이라"
            f"(rule_engine.py:64) A7 도 A9 도 아직 보이지 않는다."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="스모크 수집·판정 대조(A3·A9) — AWS_SMOKE_RESULT.md §2 의 칸을 찍는다(읽기 전용)"
    )
    parser.add_argument("--run", help="이 수집 회차만 본다(기본: 리전별 최신 회차)")
    parser.add_argument(
        "--database-url",
        help="DSN 직접 지정. 기본값은 .env 의 DATABASE_URL(컨테이너용 db:5432)이라,"
        " 호스트 셸에서 돌릴 때는 호스트에 열린 포트로 바꿔 넘긴다",
    )
    args = parser.parse_args()

    database_url = args.database_url or get_settings().DATABASE_URL
    target = database_url.rsplit("@", 1)[-1]
    # 이 셸의 AWS 설정은 **앱이 수집할 때 쓴 설정이 아니다.** AwsSettings 는 `.env` 를 읽지
    # 않고 실제 환경변수만 본다(ADR-0009 §6-3) — 호스트 셸에서 돌리면 앱 컨테이너의 설정과
    # 무관한 값이 찍힌다. 그래서 여기서는 참고로만 적고, 그 회차가 실제로 무엇을 향했는지는
    # 회차의 `mode`·`lookback`·`period` 로 판단한다(회차별 출력).
    aws = get_aws_settings()
    print("=" * 78)
    print("스모크 수집·판정 대조 (A3 · A9) — 읽기 전용")
    print(f"  조회 시각   : {_kst(datetime.now(timezone.utc))} KST")
    print(f"  DB          : {target}")
    print(f"  관측치 기준 : MIN_DATAPOINTS={MIN_DATAPOINTS} · 회차 설정값은 회차별로 출력")
    print(f"  (참고) 이 셸의 AWS 설정 — 리전 {', '.join(aws.regions_list())}"
          f" · 엔드포인트 {aws.endpoint_url() or '없음'}")
    print("          ↑ 앱의 .env 가 아니라 이 셸의 환경변수다. 회차가 무엇을 향했는지는 mode 로 본다.")
    print("=" * 78)

    try:
        session = _session_factory(database_url)()
        # create_engine 은 지연 연결이라 여기서 처음 실제로 붙는다 — 조회 도중이 아니라
        # 이 자리에서 실패하게 해, 못 붙은 이유를 트레이스백 대신 한 줄로 보여 준다.
        session.execute(text("SELECT 1"))
    except OperationalError as exc:
        print(f"\nDB 에 붙지 못했다: {target}")
        print(f"  {type(exc).__name__}: {str(exc).splitlines()[0]}")
        print("  호스트 셸에서 돌렸다면 DSN 의 db:5432 는 컨테이너 안에서만 풀린다 —")
        print("  --database-url 로 호스트에 열린 포트(.env 의 POSTGRES_PORT)를 넘길 것.")
        return 0

    with session as db:
        if args.run:
            run = db.get(models.CollectionRun, args.run)
            if run is None:
                print(f"\n그런 회차가 없다: {args.run}")
                return 0
            runs = [run]
        else:
            runs = latest_collection_run_per_region(db, None)

        if not runs:
            print("\n수집 회차가 없다 — 스캔을 먼저 돌릴 것.")
            return 0
        if not args.run:
            for line in _region_notice([(r.region, r.status.value) for r in runs]):
                print(line)
        for run in runs:
            _report_run(db, run)

        # ----- §2-4 조인 무결성
        # `find_dangling_arns` 에는 회차·계정 범위 인자가 없어 **DB 전체**를 본다. --run 으로
        # 회차를 좁혀도 이 구역은 좁혀지지 않는다 — 이전 회차나 골든 적재의 잔존 행이 함께
        # 찍히므로, 제목과 표기로 그 사실을 드러낸다. 그러지 않으면 남의 결과가 실 AWS 회차
        # 기록에 "조사 대상"으로 실린다(리뷰 ③-3, 리뷰어 로컬 재현).
        findings = find_dangling_arns(db)
        summary = summarize_dangling(findings)
        _section("[§2-4] 조인 무결성 — DB 전체 기준(선택 회차와 무관)")
        for line in _dangling_lines(findings, summary, {r.account_id for r in runs}):
            print(line)

    print()
    print("해소/미해소 판단은 이 출력이 하지 않는다 — AWS_SMOKE_RESULT.md §5 에 사람이 적는다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

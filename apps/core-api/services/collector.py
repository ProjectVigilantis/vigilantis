# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 자산/메트릭 수집기입니다. 단일 계정·1~2개 리전의 EC2/SG 인벤토리와 CloudWatch
# (CPU/Network) 지표를 수집·정형화합니다. (구 scan-worker 흡수)
#
# 구현 범위(현재): 수집 + packages/schemas.assets 로의 Pydantic 정형화까지.
#   - EC2/SG 인벤토리 수집 (describe_instances / describe_security_groups / ENI)
#   - CloudWatch CPU/Network 메트릭을 get_metric_data 로 배치 조회 후 ARN 키로 정형화
#   - 결과를 schemas.AssetInventory(Pydantic) 로 반환 → rule_engine 파이프라인 입력
#
# 아직 하지 않는 것(후속):
#   - DB 적재: 안성일의 db.models.Asset/SpecSnapshot 확정 후 연결 (TODO: persist)
#   - 판정: Idle/미사용/Skip 사유는 rule_engine 의 몫 (여기선 정형화까지만)
#
# 설정 주입: 아직 config.get_settings() 가 미구현이라 임시로 환경변수를 직접 읽는다.
#   config 가 준비되면 _runtime_config() 를 get_settings() 호출로 교체할 것. (TODO: config)
# ==============================================================================

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

from schemas.assets import (
    AssetInventory,
    Ec2Asset,
    MetricName,
    MetricSeries,
    MetricSummary,
    NaclAsset,
    OpenPort,
    SecurityGroupAsset,
)

# 스로틀링(RequestLimitExceeded) 대비. adaptive 모드가 자동으로 속도를 낮춘다.
_BOTO_CONFIG = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    user_agent_extra="vigilantis-collector/0.1",
)

_METRIC_NAMES = (MetricName.CPU_UTILIZATION, MetricName.NETWORK_IN, MetricName.NETWORK_OUT)
# get_metric_data 는 호출당 최대 500 쿼리를 받지만 응답 안정성을 위해 보수적으로 끊는다.
_QUERY_BATCH = 100


# ------------------------------------------------------------------ 설정(임시)
def _runtime_config() -> dict:
    """TODO(config): apps/core-api/config.py 의 get_settings() 확정 시 이 함수를 대체.
    LocalStack ↔ 실 AWS 전환은 AWS_ENDPOINT_URL 유무로 갈린다(있으면 LocalStack)."""
    regions = [r.strip() for r in os.getenv("AWS_REGIONS", os.getenv("AWS_REGION", "ap-northeast-2")).split(",") if r.strip()]
    endpoint = os.getenv("AWS_ENDPOINT_URL") or None
    # LocalStack 은 자격증명을 검사하지 않지만 boto3 는 키가 아예 없으면 예외를 낸다.
    if endpoint and not os.getenv("AWS_ACCESS_KEY_ID"):
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    return {
        "regions": regions,
        "endpoint_url": endpoint,
        "lookback_days": int(os.getenv("METRIC_LOOKBACK_DAYS", "14")),
        "period_seconds": int(os.getenv("METRIC_PERIOD_SECONDS", "3600")),
    }


def _client(service: str, region: str, endpoint: str | None):
    kwargs = {"region_name": region, "config": _BOTO_CONFIG}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client(service, **kwargs)


def _arn(resource_type: str, resource_id: str, region: str, account_id: str) -> str:
    """가드레일 3단계(ARN Match)가 이 문자열을 그대로 비교하므로 포맷을 반드시 고정한다.
    예) arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc123"""
    return f"arn:aws:ec2:{region}:{account_id}:{resource_type}/{resource_id}"


# ------------------------------------------------------------------ 정형화 헬퍼
def _name_tag(tags: list[dict]) -> str | None:
    return next((t["Value"] for t in tags or [] if t["Key"] == "Name"), None)


def _open_to_world(sg: dict) -> list[OpenPort]:
    """0.0.0.0/0 또는 ::/0 로 열린 인그레스만 추린다(22/tcp, 3389/tcp 위협 탐지용)."""
    ports: list[OpenPort] = []
    for perm in sg.get("IpPermissions", []):
        world_v4 = any(r.get("CidrIp") == "0.0.0.0/0" for r in perm.get("IpRanges", []))
        world_v6 = any(r.get("CidrIpv6") == "::/0" for r in perm.get("Ipv6Ranges", []))
        if not (world_v4 or world_v6):
            continue
        proto = perm.get("IpProtocol")
        ports.append(
            OpenPort(
                protocol="all" if proto == "-1" else proto,
                from_port=perm.get("FromPort"),
                to_port=perm.get("ToPort"),
                ipv6=world_v6,
            )
        )
    return ports


def _used_sg_ids(instances: list[dict], enis: list[dict]) -> set[str]:
    """ENI 에 붙은 SG 를 '사용 중'으로 본다. LocalStack 은 ENI 를 비워둘 때가 있어
    그 경우 인스턴스에 직접 붙은 SG 로 대체 집계한다(check_env 3번과 동일한 fallback)."""
    used = {g["GroupId"] for eni in enis for g in eni.get("Groups", [])}
    if not used and instances:
        used = {g["GroupId"] for i in instances for g in i.get("SecurityGroups", [])}
    return used


def _fetch_metrics(cw, instance_ids: list[str], start: datetime, end: datetime, period: int) -> dict[str, dict[MetricName, MetricSeries]]:
    """인스턴스별 CPU/Network 시계열을 get_metric_data 로 배치 조회.
    실 계정 비용 = 호출 수이므로 단건 반복 대신 배치 조회를 유지한다."""
    queries, ref = [], {}
    for idx, iid in enumerate(instance_ids):
        for m in _METRIC_NAMES:
            qid = f"q{idx}_{m.name.lower()}"
            ref[qid] = (iid, m)
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/EC2",
                            "MetricName": m.value,
                            "Dimensions": [{"Name": "InstanceId", "Value": iid}],
                        },
                        "Period": period,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                }
            )

    out: dict[str, dict[MetricName, MetricSeries]] = {iid: {} for iid in instance_ids}
    for i in range(0, len(queries), _QUERY_BATCH):
        batch = queries[i : i + _QUERY_BATCH]
        token = None
        while True:
            kwargs = dict(MetricDataQueries=batch, StartTime=start, EndTime=end, ScanBy="TimestampAscending")
            if token:
                kwargs["NextToken"] = token
            res = cw.get_metric_data(**kwargs)
            for r in res["MetricDataResults"]:
                iid, m = ref[r["Id"]]
                series = out[iid].get(m)
                if series is None:
                    series = MetricSeries(metric_name=m)
                    out[iid][m] = series
                series.timestamps.extend(r.get("Timestamps", []))
                series.values.extend(r.get("Values", []))
            token = res.get("NextToken")
            if not token:
                break
    return out


def _summarize(series_by_metric: dict[MetricName, MetricSeries]) -> MetricSummary:
    def avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 2) if vals else None

    cpu = series_by_metric.get(MetricName.CPU_UTILIZATION)
    net_in = series_by_metric.get(MetricName.NETWORK_IN)
    net_out = series_by_metric.get(MetricName.NETWORK_OUT)
    cpu_vals = cpu.values if cpu else []
    return MetricSummary(
        cpu_datapoints=len(cpu_vals),
        cpu_avg=avg(cpu_vals),
        cpu_max=round(max(cpu_vals), 2) if cpu_vals else None,
        net_in_avg=avg(net_in.values if net_in else []),
        net_out_avg=avg(net_out.values if net_out else []),
    )


# ------------------------------------------------------------------ 공개 API
def collect_region(region: str, cfg: dict | None = None) -> AssetInventory:
    """한 리전의 EC2/SG 인벤토리 + 메트릭을 수집해 AssetInventory 로 정형화한다."""
    cfg = cfg or _runtime_config()
    endpoint = cfg["endpoint_url"]

    ec2 = _client("ec2", region, endpoint)
    cw = _client("cloudwatch", region, endpoint)
    account_id = _client("sts", region, endpoint).get_caller_identity()["Account"]

    instances_raw = [
        i
        for page in ec2.get_paginator("describe_instances").paginate()
        for r in page["Reservations"]
        for i in r["Instances"]
    ]
    sgs_raw = ec2.describe_security_groups()["SecurityGroups"]
    enis_raw = ec2.describe_network_interfaces()["NetworkInterfaces"]
    nacls_raw = ec2.describe_network_acls()["NetworkAcls"]
    used = _used_sg_ids(instances_raw, enis_raw)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=cfg["lookback_days"])
    ids = [i["InstanceId"] for i in instances_raw]
    metrics = _fetch_metrics(cw, ids, start, end, cfg["period_seconds"]) if ids else {}

    ec2_assets: list[Ec2Asset] = []
    for i in instances_raw:
        iid = i["InstanceId"]
        series = metrics.get(iid, {})
        ec2_assets.append(
            Ec2Asset(
                arn=_arn("instance", iid, region, account_id),
                instance_id=iid,
                name=_name_tag(i.get("Tags", [])),
                instance_type=i.get("InstanceType"),
                state=i.get("State", {}).get("Name"),
                region=region,
                availability_zone=i.get("Placement", {}).get("AvailabilityZone"),
                vpc_id=i.get("VpcId"),
                subnet_id=i.get("SubnetId"),
                private_ip=i.get("PrivateIpAddress"),
                launch_time=i.get("LaunchTime"),
                security_group_ids=[g["GroupId"] for g in i.get("SecurityGroups", [])],
                tags={t["Key"]: t["Value"] for t in i.get("Tags", [])},
                metrics=series,
                metric_summary=_summarize(series),
            )
        )

    sg_assets = [
        SecurityGroupAsset(
            arn=_arn("security-group", sg["GroupId"], region, account_id),
            group_id=sg["GroupId"],
            name=sg.get("GroupName"),
            description=sg.get("Description"),
            region=region,
            vpc_id=sg.get("VpcId"),
            attached=sg["GroupId"] in used,
            open_to_world=_open_to_world(sg),
        )
        for sg in sgs_raw
    ]

    nacl_assets = [
        NaclAsset(
            arn=_arn("network-acl", n["NetworkAclId"], region, account_id),
            nacl_id=n["NetworkAclId"],
            region=region,
            vpc_id=n.get("VpcId"),
            is_default=bool(n.get("IsDefault", False)),
            associated_subnet_ids=[
                a["SubnetId"] for a in n.get("Associations", []) if a.get("SubnetId")
            ],
        )
        for n in nacls_raw
    ]

    return AssetInventory(
        account_id=account_id,
        region=region,
        mode="localstack" if endpoint else "aws",
        lookback_days=cfg["lookback_days"],
        period_seconds=cfg["period_seconds"],
        ec2_instances=ec2_assets,
        security_groups=sg_assets,
        nacls=nacl_assets,
    )


def collect() -> list[AssetInventory]:
    """설정된 모든 리전을 수집한다(정형화까지). DB 적재 없이 결과만 필요할 때 사용."""
    cfg = _runtime_config()
    return [collect_region(region, cfg) for region in cfg["regions"]]


# ------------------------------------------------------------------ DB 적재
def persist_inventory(inv: AssetInventory, db, collection_run_id: str | None = None) -> dict:
    """AssetInventory 를 DB(CollectionRun, Asset, MetricSummary, AssetRelationship)에 적재한다.
    Repository는 commit하지 않으므로 호출부에서 트랜잭션을 관리한다.
    """
    from datetime import timedelta

    from db.repositories import assets as assets_repo
    from schemas.api.assets import AssetType, RelationType
    from schemas.collections import CollectionRunStatus

    started_own_run = False
    if collection_run_id is None:
        run = assets_repo.start_collection_run(
            db,
            account_id=inv.account_id,
            region=inv.region,
            mode=inv.mode,
            lookback_days=inv.lookback_days,
            period_seconds=inv.period_seconds,
        )
        collection_run_id = run.collection_run_id
        started_own_run = True

    ec2_count = len(inv.ec2_instances)
    sg_count = len(inv.security_groups)
    nacl_count = len(inv.nacls)
    total = ec2_count + sg_count + nacl_count

    # subnet → NACL ARN (EC2→NACL PROTECTED_BY 파생용)
    subnet_to_nacl = {
        subnet_id: n.arn
        for n in inv.nacls
        for subnet_id in n.associated_subnet_ids
    }

    window_end = inv.collected_at
    window_start = window_end - timedelta(days=inv.lookback_days)

    # 1. EC2 적재
    for a in inv.ec2_instances:
        ec2_spec = {
            "instance_type": a.instance_type,
            "availability_zone": a.availability_zone,
            "vpc_id": a.vpc_id,
            "subnet_id": a.subnet_id,
            "private_ip": a.private_ip,
            "tags": a.tags or {},
        }
        asset = assets_repo.upsert_asset(
            db,
            arn=a.arn,
            asset_type=AssetType.EC2,
            resource_id=a.instance_id,
            account_id=inv.account_id,
            region=inv.region,
            spec=ec2_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=a.name,
            state=a.state,
        )
        assets_repo.add_metric_summary(
            db,
            asset_id=asset.asset_id,
            collection_run_id=collection_run_id,
            summary=a.metric_summary,
            window_start=window_start,
            window_end=window_end,
            collected_at=inv.collected_at,
        )
        # SG(SECURED_BY) + NACL(PROTECTED_BY) 를 한 번에 교체(replace 는 덮어쓰기)
        rel_items = [
            (RelationType.SECURED_BY, f"arn:aws:ec2:{inv.region}:{inv.account_id}:security-group/{sg_id}")
            for sg_id in a.security_group_ids
        ]
        nacl_arn = subnet_to_nacl.get(a.subnet_id)
        if nacl_arn:
            rel_items.append((RelationType.PROTECTED_BY, nacl_arn))
        if rel_items:
            assets_repo.replace_relationships(
                db,
                source_asset_id=asset.asset_id,
                items=rel_items,
                collection_run_id=collection_run_id,
            )

    # 2. SG 적재
    for g in inv.security_groups:
        sg_spec = {
            "description": g.description,
            "vpc_id": g.vpc_id,
            "attached": g.attached,
            "open_to_world": [p.model_dump(mode="json") for p in g.open_to_world],
        }
        assets_repo.upsert_asset(
            db,
            arn=g.arn,
            asset_type=AssetType.SG,
            resource_id=g.group_id,
            account_id=inv.account_id,
            region=inv.region,
            spec=sg_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=g.name,
            state=None,
        )

    # 3. NACL 적재 (판정 비대상 — role/status 파생은 조회단이 처리)
    for n in inv.nacls:
        nacl_spec = {
            "vpc_id": n.vpc_id,
            "is_default": n.is_default,
            "associated_subnet_ids": n.associated_subnet_ids,
        }
        assets_repo.upsert_asset(
            db,
            arn=n.arn,
            asset_type=AssetType.NACL,
            resource_id=n.nacl_id,
            account_id=inv.account_id,
            region=inv.region,
            spec=nacl_spec,
            collection_run_id=collection_run_id,
            collected_at=inv.collected_at,
            name=None,
            state=None,
        )

    if started_own_run:
        assets_repo.finish_collection_run(
            db,
            collection_run_id=collection_run_id,
            status=CollectionRunStatus.SUCCESS,
            finished_at=inv.collected_at,
        )

    return {
        "region": inv.region,
        "collection_run_id": collection_run_id,
        "ec2_count": ec2_count,
        "sg_count": sg_count,
        "nacl_count": nacl_count,
        "total": total,
    }


def collect_and_store() -> list[dict]:
    """수집 → 정형화 → DB 적재까지. scheduler 가 이 함수를 주기 호출한다.
    (판정은 이후 rule_engine 이 assets 테이블 및 metric_summaries 를 읽어 수행)"""
    from db.session import get_session_factory

    cfg = _runtime_config()
    summaries = []
    session_factory = get_session_factory()
    db = session_factory()
    try:
        for region in cfg["regions"]:
            inv = collect_region(region, cfg)
            summary = persist_inventory(inv, db)
            summaries.append(summary)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return summaries


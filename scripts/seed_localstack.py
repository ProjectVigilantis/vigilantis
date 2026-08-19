# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# LocalStack 시드 스크립트 — 팀 표준 개발 환경(ADR-0006 §2)의 더미 AWS 자산 주입.
# 수집(collector)·판정(rule_engine) 테스트가 전제하는 자산을 한 번에 만든다.
#
# 실행 (repo 루트, LocalStack 기동 후):
#   선행 1회(신규 클론): uv sync --all-packages   ← 루트 의존성만으로는 boto3가 없다
#   PowerShell: $env:AWS_ENDPOINT_URL='http://localhost:4566'; uv run python scripts/seed_localstack.py
#   bash      : AWS_ENDPOINT_URL=http://localhost:4566 uv run python scripts/seed_localstack.py
#   메트릭만 재주입: ... seed_localstack.py --metrics-only
#
# 안전 가드(ADR-0006 §2): 실 AWS에는 절대 실행되지 않는다 —
#   AWS_ENDPOINT_URL 미설정 시 즉시 종료 + LocalStack 헬스체크(/_localstack/health) 선행 확인.
#
# 멱등성: 태그(vigilantis:seed)·그룹명으로 식별해 이미 있으면 건너뛴다.
#   LocalStack은 비영속(재시작 시 초기화)이므로 compose 재기동 후 재실행할 것.
#   메트릭은 인스턴스 신규 생성 시 또는 메트릭 부재 시 주입한다 — 부분 실패 후에도
#   재실행만으로 자가 복구된다.
#
# 시드 데이터셋 (ADR-0006 §2 표 — rule_engine 임계값을 import해 결합을 코드로 강제):
#   idle EC2(t3.xlarge, CPU<IDLE_CPU_AVG)  → RIGHTSIZING 후보
#   normal EC2(t3.micro, CPU≥IDLE_CPU_AVG) → 오탐 방지(후보 미선정)
#   spike EC2(t3.small, 평균<IDLE, 최대≥SPIKE) → SKIP_LOW_UTIL 경로
#   OpenIP SG(0.0.0.0/0 22/tcp)            → 위협 탐지·토폴로지 붉은 노드
#   사용 중/미사용 SG                        → 미사용 SG 판별
#   미연결(available) EBS 볼륨               → EBS_DELETE_UNATTACHED (P1)
# ==============================================================================

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# import 경로: services(core-api) + schemas(packages) — 통합 테스트와 동일한 부트스트랩
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT / "apps" / "core-api"), str(_REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from schemas.assets import MetricName  # noqa: E402

# 리전·엔드포인트·자격증명 해석과 클라이언트 생성은 collector와 단일 원천(ADR-0006 §3).
# config.get_settings() 확정 시 collector와 함께 그쪽으로 이관한다.
from services.collector import _client, _runtime_config  # noqa: E402
from services.rule_engine import IDLE_CPU_AVG, MIN_DATAPOINTS, SPIKE_CPU_MAX  # noqa: E402

SEED_TAG_KEY = "vigilantis:seed"
SEED_TAG_VALUE = "true"

# CPU 프로필 — 임계값에서 파생시켜 rule_engine 상수 변경 시 함께 움직인다
IDLE_CPU = round(IDLE_CPU_AVG * 0.4, 1)      # 평균 < IDLE_CPU_AVG → RIGHTSIZING 후보
NORMAL_CPU = round(IDLE_CPU_AVG * 7.0, 1)    # 평균 ≥ IDLE_CPU_AVG → 후보 아님
SPIKE_BASE = round(IDLE_CPU_AVG * 0.2, 1)    # 평균은 낮게 유지
SPIKE_PEAK = round(SPIKE_CPU_MAX * 2.0, 1)   # 최대 ≥ SPIKE_CPU_MAX → SKIP_LOW_UTIL
METRIC_HOURS = 72                            # 최근 3일 × 1시간 해상도
SPIKE_PEAK_COUNT = 2

# 파생만으로는 보장되지 않는 불변식 2개 — 깨지면 시드가 조용히 무의미해지므로 즉시 종료.
# (assert 금지: python -O / PYTHONOPTIMIZE 에서 제거된다)
_spike_avg = (SPIKE_BASE * (METRIC_HOURS - SPIKE_PEAK_COUNT) + SPIKE_PEAK * SPIKE_PEAK_COUNT) / METRIC_HOURS
if not (_spike_avg < IDLE_CPU_AVG and METRIC_HOURS >= MIN_DATAPOINTS):
    sys.exit(
        f"[seed] 프로필 불변식 위반 — spike 평균({_spike_avg:.2f}) < IDLE_CPU_AVG({IDLE_CPU_AVG}) 이고 "
        f"METRIC_HOURS({METRIC_HOURS}) >= MIN_DATAPOINTS({MIN_DATAPOINTS}) 여야 한다. "
        "rule_engine 임계값 변경 시 시드 프로필도 함께 조정할 것."
    )

SG_OPEN = "vigilantis-seed-open-ssh"
SG_USED = "vigilantis-seed-used"
SG_UNUSED = "vigilantis-seed-unused"

# (Name 태그, 인스턴스 타입, SG 이름, CPU 프로필) — idle은 OpenIP SG를 달아 붉은 노드도 겸한다
INSTANCES = (
    ("vigilantis-seed-idle", "t3.xlarge", SG_OPEN, "idle"),
    ("vigilantis-seed-normal", "t3.micro", SG_USED, "normal"),
    ("vigilantis-seed-spike", "t3.small", SG_USED, "spike"),
)
VOLUME_NAME = "vigilantis-seed-unattached"
FALLBACK_AMI = "ami-00000000000000000"  # LocalStack 기본 AMI 조회 실패 시 폴백


# ------------------------------------------------------------------ 안전 가드
def _require_localstack() -> None:
    """실 AWS 실행 거부(ADR-0006 §2). 엔드포인트 필수 + LocalStack 헬스체크 확인."""
    endpoint = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint:
        sys.exit(
            "AWS_ENDPOINT_URL 미설정 — 이 스크립트는 LocalStack 전용이다.\n"
            "  예) AWS_ENDPOINT_URL=http://localhost:4566 (compose 내부에서는 http://localstack:4566)"
        )
    if "amazonaws.com" in endpoint:
        sys.exit(f"실 AWS 엔드포인트({endpoint})에는 시드를 실행하지 않는다.")
    try:
        with urllib.request.urlopen(f"{endpoint}/_localstack/health", timeout=3) as r:
            if r.status != 200:
                raise OSError(f"status {r.status}")
    except Exception as e:  # noqa: BLE001 — 원인 무관하게 같은 안내로 종료
        sys.exit(
            f"LocalStack 헬스체크 실패({endpoint}): {e}\n"
            "  docker compose up 으로 localstack 서비스가 기동됐는지 확인할 것."
        )


# ------------------------------------------------------------------ 리소스 ensure
def _seed_tags(name: str) -> list[dict]:
    return [{"Key": "Name", "Value": name}, {"Key": SEED_TAG_KEY, "Value": SEED_TAG_VALUE}]


def _ensure_sg(ec2, name: str, open_ssh: bool) -> tuple[str, bool]:
    found = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]}])["SecurityGroups"]
    if found:
        return found[0]["GroupId"], False
    sg_id = ec2.create_security_group(
        GroupName=name,
        Description=f"vigilantis seed: {name}",
        TagSpecifications=[{"ResourceType": "security-group", "Tags": _seed_tags(name)}],
    )["GroupId"]
    if open_ssh:  # OpenIP 위협 시나리오 — 22/tcp 전세계 개방
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "vigilantis seed: open to world"}],
            }],
        )
    return sg_id, True


def _default_ami(ec2) -> str:
    images = ec2.describe_images(Owners=["amazon"]).get("Images", [])
    return images[0]["ImageId"] if images else FALLBACK_AMI


def _find_instance(ec2, name: str) -> str | None:
    """살아있는(pending·running) 시드 인스턴스만 찾는다 — terminated 동명 인스턴스 오선택 방지."""
    found = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [name]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]},
    ])["Reservations"]
    return found[0]["Instances"][0]["InstanceId"] if found else None


def _ensure_instance(ec2, ami: str, name: str, itype: str, sg_id: str) -> tuple[str, bool]:
    iid = _find_instance(ec2, name)
    if iid:
        return iid, False
    iid = ec2.run_instances(
        ImageId=ami, InstanceType=itype, MinCount=1, MaxCount=1,
        SecurityGroupIds=[sg_id],
        TagSpecifications=[{"ResourceType": "instance", "Tags": _seed_tags(name)}],
    )["Instances"][0]["InstanceId"]
    return iid, True


def _ensure_volume(ec2, region: str) -> tuple[str, bool]:
    found = ec2.describe_volumes(Filters=[
        {"Name": "tag:Name", "Values": [VOLUME_NAME]},
        {"Name": "status", "Values": ["available"]},
    ])["Volumes"]
    if found:
        return found[0]["VolumeId"], False
    vol = ec2.create_volume(
        AvailabilityZone=f"{region}a", Size=8,
        TagSpecifications=[{"ResourceType": "volume", "Tags": _seed_tags(VOLUME_NAME)}],
    )
    return vol["VolumeId"], True


# ------------------------------------------------------------------ 메트릭 주입
def _cpu_series(profile: str) -> list[float]:
    if profile == "idle":
        return [IDLE_CPU] * METRIC_HOURS
    if profile == "normal":
        return [NORMAL_CPU] * METRIC_HOURS
    # spike: 평균은 낮게, 마지막 구간에 최대치 스파이크
    series = [SPIKE_BASE] * METRIC_HOURS
    for i in range(SPIKE_PEAK_COUNT):
        series[-(i * 24 + 1)] = SPIKE_PEAK
    return series


def _has_metrics(cw, instance_id: str) -> bool:
    return bool(
        cw.list_metrics(
            Namespace="AWS/EC2",
            MetricName=MetricName.CPU_UTILIZATION.value,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        )["Metrics"]
    )


def _put_metrics(cw, instance_id: str, profile: str) -> None:
    """CPU/Network 시계열을 AWS/EC2 네임스페이스에 직접 주입.
    실 AWS는 AWS/ 네임스페이스 커스텀 주입이 불가 — LocalStack 전용 경로다(ADR-0006 §2)."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    cpu = _cpu_series(profile)
    net = {"idle": 5_000.0, "spike": 50_000.0, "normal": 5_000_000.0}[profile]
    for metric, values in (
        (MetricName.CPU_UTILIZATION, cpu),
        (MetricName.NETWORK_IN, [net] * METRIC_HOURS),
        (MetricName.NETWORK_OUT, [net] * METRIC_HOURS),
    ):
        cw.put_metric_data(
            Namespace="AWS/EC2",
            MetricData=[{
                "MetricName": metric.value,
                "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                "Timestamp": now - timedelta(hours=METRIC_HOURS - i),
                "Value": v,
                "Unit": "Percent" if metric is MetricName.CPU_UTILIZATION else "Bytes",
            } for i, v in enumerate(values)],
        )


# ------------------------------------------------------------------ 실행 모드
def seed_all(ec2, cw, region: str) -> None:
    sg_ids: dict[str, str] = {}
    for sg_name, open_ssh in ((SG_OPEN, True), (SG_USED, False), (SG_UNUSED, False)):
        sg_id, created = _ensure_sg(ec2, sg_name, open_ssh)
        sg_ids[sg_name] = sg_id
        print(f"[seed] SG {sg_name}: {sg_id} ({'생성' if created else '존재 — skip'})")

    ami = _default_ami(ec2)
    for name, itype, sg_name, profile in INSTANCES:
        iid, created = _ensure_instance(ec2, ami, name, itype, sg_ids[sg_name])
        print(f"[seed] EC2 {name}({itype}): {iid} ({'생성' if created else '존재 — skip'})")
        # 신규 생성 또는 메트릭 부재 시 주입 — 이전 실행이 중간에 죽었어도 재실행으로 복구된다
        if created or not _has_metrics(cw, iid):
            _put_metrics(cw, iid, profile)
            print(f"[seed]   └ CloudWatch 메트릭 주입({METRIC_HOURS}h, {profile})")

    vol_id, created = _ensure_volume(ec2, region)
    print(f"[seed] EBS {VOLUME_NAME}: {vol_id} ({'생성' if created else '존재 — skip'})")


def reinject_metrics(ec2, cw) -> None:
    for name, _itype, _sg_name, profile in INSTANCES:
        iid = _find_instance(ec2, name)
        if not iid:
            sys.exit(f"[seed] {name} 없음(pending·running 기준) — 전체 시드를 먼저 실행할 것")
        _put_metrics(cw, iid, profile)
        print(f"[seed] EC2 {name}: {iid} — 메트릭 재주입({METRIC_HOURS}h, {profile})")


# ------------------------------------------------------------------ main
def main() -> None:
    parser = argparse.ArgumentParser(description="Vigilantis LocalStack 시드 (ADR-0006)")
    parser.add_argument("--metrics-only", action="store_true", help="자산 생성 없이 메트릭만 재주입")
    args = parser.parse_args()

    _require_localstack()
    cfg = _runtime_config()  # 리전·엔드포인트·자격증명 해석 = collector와 동일 규약
    if not cfg["regions"]:
        sys.exit("[seed] 리전 해석 실패 — AWS_REGION / AWS_REGIONS 값을 확인할 것")
    region, endpoint = cfg["regions"][0], cfg["endpoint_url"]

    ec2 = _client("ec2", region, endpoint)
    # cloudwatch 만 직접 생성: botocore가 10KB 초과 PutMetricData 본문을 gzip 압축하는데
    # LocalStack 구버전이 압축 본문을 못 읽는 사례가 있어("Missing Action") 방어적으로 끈다.
    # _client() 는 config 주입을 지원하지 않음 — 공용 헬퍼 이관(get_settings) 시 함께 정리.
    cw = boto3.client(
        "cloudwatch", region_name=region, endpoint_url=endpoint,
        config=Config(disable_request_compression=True),
    )
    print(f"[seed] endpoint={endpoint} region={region}")

    if args.metrics_only:
        reinject_metrics(ec2, cw)
    else:
        seed_all(ec2, cw, region)

    print("[seed] 완료 — 검증: uv run pytest apps/core-api/services/tests/test_collector_raw.py -q")


if __name__ == "__main__":
    main()

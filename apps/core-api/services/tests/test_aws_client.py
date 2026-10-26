"""Boto3 클라이언트 팩토리 단위 테스트 (Issue #128, ADR-0006 §3).

AWS/DB 불필요 — 클라이언트 생성 시점의 해석만 검증한다(호출은 하지 않는다).
설정은 .env 파일 영향을 받지 않도록 AwsSettings를 직접 구성해 주입한다.
"""

import os
import sys
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import AwsSettings  # noqa: E402
from services.aws import client as aws_client_module  # noqa: E402

LOCALSTACK = "http://localhost:4566"


@pytest.fixture
def settings(monkeypatch):
    """AwsSettings를 테스트가 지정한 값으로 갈아 끼운다."""

    def _apply(**values):
        # botocore도 AWS_ENDPOINT_URL을 자체적으로 읽는다 — 프로세스 환경을 비워
        # 팩토리가 넘긴 값만 반영되게 한다.
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        resolved = AwsSettings(_env_file=None, **values)
        monkeypatch.setattr(aws_client_module, "get_aws_settings", lambda: resolved)
        return resolved

    return _apply


# --- 설정 해석 -----------------------------------------------------------------


def test_regions_splits_comma_list_and_trims():
    resolved = AwsSettings(_env_file=None, AWS_REGIONS=" ap-northeast-2 , us-east-1 ")
    assert resolved.regions_list() == ["ap-northeast-2", "us-east-1"]


def test_regions_falls_back_to_single_region():
    resolved = AwsSettings(_env_file=None, AWS_REGION="eu-west-1", AWS_REGIONS="")
    assert resolved.regions_list() == ["eu-west-1"]


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_endpoint_means_real_aws(raw):
    assert AwsSettings(_env_file=None, AWS_ENDPOINT_URL=raw).endpoint_url() is None


def test_default_region_fails_loudly_when_unresolvable(settings):
    settings(AWS_REGION="", AWS_REGIONS="")
    with pytest.raises(RuntimeError, match="리전 해석 실패"):
        aws_client_module.default_region()


# --- 전환 스위치 (ADR-0006 §3) --------------------------------------------------


def test_endpoint_present_targets_localstack(settings):
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    assert aws_client_module.deployment_mode() == "localstack"
    client = aws_client_module.aws_client("ec2")
    assert client.meta.endpoint_url == LOCALSTACK


def test_endpoint_absent_targets_real_aws(settings):
    settings(AWS_ENDPOINT_URL="", AWS_REGION="ap-northeast-2")
    assert aws_client_module.deployment_mode() == "aws"
    client = aws_client_module.aws_client("ec2")
    assert client.meta.endpoint_url.endswith("amazonaws.com")


def test_real_aws_mode_ignores_service_specific_endpoint_env(settings, monkeypatch):
    """스위치를 껐으면 서비스별 엔드포인트 환경변수가 남아 있어도 실 AWS로 가야 한다.

    AwsSettings는 AWS_ENDPOINT_URL 하나만 읽지만 botocore는 AWS_ENDPOINT_URL_EC2 같은
    서비스별 변수도 읽는다. 막지 않으면 실 AWS 스모크가 조용히 LocalStack을 때린다.
    """
    monkeypatch.setenv("AWS_ENDPOINT_URL_EC2", LOCALSTACK)
    settings(AWS_ENDPOINT_URL="", AWS_REGION="ap-northeast-2")
    assert aws_client_module.aws_client("ec2").meta.endpoint_url.endswith("amazonaws.com")


def test_real_aws_mode_passes_ignore_configured_endpoints_to_boto3(settings, monkeypatch):
    """~/.aws/config의 endpoint_url·services 섹션도 서비스별 환경변수와 같은 경로다.

    설정 파일은 boto3 기본 세션에 캐시돼 테스트 중 주입이 불안정하므로, 두 경로를
    한꺼번에 차단하는 인자가 실 AWS 모드에서만 넘어가는지로 검증한다. botocore가
    클라이언트 생성 시 Config를 재구성하면서 이 플래그를 meta.config에 남기지 않아
    (엔드포인트 해석에만 쓰고 버린다) 넘기는 쪽에서 확인한다.
    """
    captured: dict = {}

    class CapturingSession:
        def client(self, service, **kwargs):
            captured.clear()
            captured.update(kwargs, service=service)
            return object()

    monkeypatch.setattr(aws_client_module, "_session", lambda: CapturingSession())

    settings(AWS_ENDPOINT_URL="", AWS_REGION="ap-northeast-2")
    aws_client_module.aws_client("ec2")
    assert captured["config"].ignore_configured_endpoint_urls is True
    # 차단 인자를 붙이면서 공통 재시도 설정이 떨어지지 않아야 한다
    assert captured["config"].retries["mode"] == "standard"
    assert "endpoint_url" not in captured

    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    aws_client_module.aws_client("ec2")
    assert not captured["config"].ignore_configured_endpoint_urls
    assert captured["endpoint_url"] == LOCALSTACK


def test_localstack_mode_is_unaffected_by_stray_endpoint_env(settings, monkeypatch):
    """스위치가 켜져 있으면 팩토리가 넘긴 엔드포인트가 이긴다 — 기존 동작 유지 확인."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_EC2", "http://localhost:9999")
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    assert aws_client_module.aws_client("ec2").meta.endpoint_url == LOCALSTACK


def test_no_module_reads_the_switch_from_the_environment():
    """전환 스위치는 AwsSettings가 읽고 팩토리가 해석한다 — 그 밖에서는 읽지 않는다.

    ADR-0006 §3(코드 분기 금지)은 조건이 한 곳에 있을 때만 지킬 수 있다. 소비자가
    환경변수를 각자 읽으면 붙는 대상과 검사 대상이 갈려도 알아챌 수 없다.
    """
    offenders = []
    for root in ("apps", "packages", "scripts"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            parts = path.parts
            if "tests" in parts or "__pycache__" in parts or ".venv" in parts:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "AWS_ENDPOINT_URL" not in line:
                    continue
                if "getenv" in line or "environ" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, f"전환 스위치를 직접 읽는 곳이 있습니다: {sorted(offenders)}"


# --- 클라이언트 구성 ------------------------------------------------------------


def test_common_retry_config_is_applied(settings):
    """standard 모드, 첫 요청 포함 5회 — adaptive가 아니다(파일 헤더 §재시도 정책)."""
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    config = aws_client_module.aws_client("ec2").meta.config
    assert config.retries["mode"] == "standard"
    assert config.retries["total_max_attempts"] == 5


def test_overrides_merge_without_dropping_retry_config(settings):
    """시드 스크립트의 cloudwatch 압축 해제처럼 한 축만 덧붙이는 경우."""
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    config = aws_client_module.aws_client(
        "cloudwatch", disable_request_compression=True
    ).meta.config
    assert config.disable_request_compression is True
    assert config.retries["mode"] == "standard"


# --- 세션 (Issue #434 2-1) -------------------------------------------------------


def test_clients_do_not_come_from_the_shared_default_session(settings, monkeypatch):
    """boto3 기본 세션(boto3.client)은 스레드 간 공유라 동시 생성이 안전하지 않다 — 쓰지 않는다."""
    settings(AWS_ENDPOINT_URL=LOCALSTACK)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("boto3.client(기본 세션)을 불렀습니다")

    monkeypatch.setattr(aws_client_module.boto3, "client", _forbidden)
    monkeypatch.setattr(aws_client_module.boto3, "DEFAULT_SESSION", None, raising=False)

    client = aws_client_module.aws_client("ec2")

    assert client.meta.endpoint_url == LOCALSTACK
    assert aws_client_module.boto3.DEFAULT_SESSION is None  # 기본 세션이 만들어지지 않았다


def test_each_thread_gets_its_own_session_and_reuses_it(settings):
    """세션은 스레드마다 하나이고, 같은 스레드의 두 번째 호출은 같은 세션을 쓴다."""
    import threading

    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    sessions: dict[str, list] = {"main": [], "worker": []}

    sessions["main"].append(aws_client_module._session())
    sessions["main"].append(aws_client_module._session())

    def worker():
        sessions["worker"].append(aws_client_module._session())
        sessions["worker"].append(aws_client_module._session())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert sessions["main"][0] is sessions["main"][1]
    assert sessions["worker"][0] is sessions["worker"][1]
    assert sessions["main"][0] is not sessions["worker"][0]


def test_concurrent_client_creation_across_threads(settings):
    """스레드 여럿이 동시에 클라이언트를 만들어도 각자 제 리전·엔드포인트를 받는다."""
    import threading

    settings(AWS_REGIONS="ap-northeast-2,us-east-1", AWS_ENDPOINT_URL=LOCALSTACK)
    regions = ["ap-northeast-2", "us-east-1"] * 8
    results: list[tuple[str, str]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(regions))

    def build(region: str):
        try:
            barrier.wait(timeout=10)
            client = aws_client_module.aws_client("ec2", region)
            results.append((region, client.meta.region_name))
        except BaseException as exc:  # noqa: BLE001 — 스레드 안의 실패를 본 스레드로 올린다
            errors.append(exc)

    threads = [threading.Thread(target=build, args=(r,)) for r in regions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert all(wanted == got for wanted, got in results) and len(results) == len(regions)


def test_region_argument_overrides_default(settings):
    settings(AWS_REGIONS="ap-northeast-2,us-east-1", AWS_ENDPOINT_URL=LOCALSTACK)
    assert aws_client_module.aws_client("ec2").meta.region_name == "ap-northeast-2"
    assert aws_client_module.aws_client("ec2", "us-east-1").meta.region_name == "us-east-1"


# --- 자격증명 -------------------------------------------------------------------


def test_dummy_credentials_only_for_localstack(settings, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    settings(AWS_ENDPOINT_URL="", AWS_REGION="ap-northeast-2")
    aws_client_module.aws_client("ec2")
    assert "AWS_ACCESS_KEY_ID" not in os.environ, "실 AWS에 더미 자격증명을 넣으면 안 됩니다"

    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    aws_client_module.aws_client("ec2")
    assert os.environ["AWS_ACCESS_KEY_ID"] == "test"


def test_existing_credentials_are_never_overwritten(settings, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAREAL")
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    aws_client_module.aws_client("ec2")
    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIAREAL"

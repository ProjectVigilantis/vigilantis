"""GuardDuty 켜기(services/guardduty.ensure_detector) — 상태를 드는 GuardDuty 대역 (#588 B).

대역은 리전마다 탐지기를 들고 있고 실제 API 처럼 「계정 · 리전마다 하나」를 지킨다(두 번째 CreateDetector 는
BadRequestException). 실 AWS 의 서비스 연결 역할 · 조직 관리 · SCP 는 학원 계정 실측(11/16 뒤)으로 본다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from services import guardduty  # noqa: E402
from services.guardduty import DETECTOR_DISABLED_REASON, ensure_detector  # noqa: E402

SESSION = object()
REGION = "ap-northeast-2"


def _error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


#: botocore DetectorFeature 전부. RUNTIME_MONITORING 만 기본으로 꺼져 있다(CreateDetector API 문서).
ALL_FEATURES = (*guardduty.DISABLED_FEATURES, "RUNTIME_MONITORING")
_OFF_BY_DEFAULT = {"RUNTIME_MONITORING"}


def created_features(params: dict) -> dict[str, str]:
    """CreateDetector 가 만든 탐지기의 부가 기능 상태 — AWS 기본값(생략하면 켜짐)을 따른다."""
    features = {name: "DISABLED" if name in _OFF_BY_DEFAULT else "ENABLED" for name in ALL_FEATURES}
    passed = {f["Name"]: f["Status"] for f in params.get("Features", ())}
    if {"EKS_RUNTIME_MONITORING", "RUNTIME_MONITORING"} <= set(passed):
        raise _error("BadRequestException", "CreateDetector")  # 둘을 함께 넘기면 거절(같은 문서)
    return {**features, **passed}


class FakeGuardDuty:
    """리전 하나의 GuardDuty. detectors: 탐지기 ID → 상태. features: 탐지기 ID → 부가 기능 → 상태."""

    def __init__(self, detectors: dict[str, str] | None = None) -> None:
        self.detectors = dict(detectors or {})
        self.features: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.deny: dict[str, str] = {}  # 작업 → 오류 코드
        self.race = False  # CreateDetector 직전에 다른 확인이 먼저 만든 것처럼

    def _call(self, name: str, **params):
        self.calls.append((name, params))
        if name in self.deny:
            raise _error(self.deny[name], name)

    def list_detectors(self, **params):
        self._call("ListDetectors", **params)
        return {"DetectorIds": sorted(self.detectors)}

    def create_detector(self, **params):
        if self.race:
            self.detectors["d-other"] = "ENABLED"
        self._call("CreateDetector", **params)
        if self.detectors:
            raise _error("BadRequestException", "CreateDetector")  # 계정 · 리전마다 하나
        self.features["d-new"] = created_features(params)
        self.detectors["d-new"] = "ENABLED" if params.get("Enable") else "DISABLED"
        return {"DetectorId": "d-new"}

    def get_detector(self, *, DetectorId):
        self._call("GetDetector", DetectorId=DetectorId)
        return {"Status": self.detectors[DetectorId]}

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def gd(monkeypatch):
    fake = FakeGuardDuty()
    seen = []

    def session_client(session, service, region):
        assert (session, service) == (SESSION, "guardduty")
        seen.append(region)
        return fake

    monkeypatch.setattr(guardduty, "session_client", session_client)
    fake.regions = seen
    return fake


def test_no_detector_is_created_enabled(gd):
    assert ensure_detector(SESSION, REGION) is None
    assert gd.detectors == {"d-new": "ENABLED"}  # 호출을 빼면 탐지기가 생기지 않는다
    assert gd.names() == ["ListDetectors", "CreateDetector"]
    assert gd.calls[1][1]["Enable"] is True
    assert gd.regions == [REGION]


def test_a_new_detector_has_every_optional_feature_off(gd):
    # Features 를 빼면 대역도 AWS 처럼 부가 기능을 켠다 — 그때 이 테스트가 깨진다(PR 리뷰 Features)
    ensure_detector(SESSION, REGION)
    assert gd.features["d-new"] == {name: "DISABLED" for name in ALL_FEATURES}


def test_the_two_runtime_features_are_never_passed_together():
    assert "RUNTIME_MONITORING" not in guardduty.DISABLED_FEATURES
    assert "EKS_RUNTIME_MONITORING" in guardduty.DISABLED_FEATURES  # 빼면 기본으로 켜진다


def test_the_feature_list_follows_botocore():
    # botocore 가 부가 기능을 더하면 그 기능은 기본으로 켜진다 — 목록이 따라오지 않으면 여기서 깨진다
    import botocore.session

    model = botocore.session.get_session().get_service_model("guardduty")
    assert set(model.shape_for("DetectorFeature").enum) == set(ALL_FEATURES)


def test_an_enabled_detector_is_left_alone(gd):
    gd.detectors = {"d-1": "ENABLED"}
    assert ensure_detector(SESSION, REGION) is None
    assert gd.names() == ["ListDetectors", "GetDetector"]


def test_a_suspended_detector_is_not_turned_back_on(gd):
    # 고객이 일부러 정지했을 수 있다(#588 Q1). 켜는 쓰기(UpdateDetector)는 부르지 않는다
    gd.detectors = {"d-1": "DISABLED"}
    assert ensure_detector(SESSION, REGION) == DETECTOR_DISABLED_REASON
    assert gd.detectors == {"d-1": "DISABLED"}
    assert gd.names() == ["ListDetectors", "GetDetector"]


def test_losing_the_create_race_reads_the_other_detector(gd):
    gd.race = True
    assert ensure_detector(SESSION, REGION) is None
    assert gd.detectors == {"d-other": "ENABLED"}
    assert gd.names() == ["ListDetectors", "CreateDetector", "ListDetectors", "GetDetector"]


def test_a_bad_request_that_is_not_the_race_is_raised(gd):
    # 조직 관리 계정이 막는 등 — 다시 봐도 탐지기가 없으면 거절 그대로 올린다
    gd.deny["CreateDetector"] = "BadRequestException"
    with pytest.raises(ClientError) as exc:
        ensure_detector(SESSION, REGION)
    assert exc.value.response["Error"]["Code"] == "BadRequestException"
    assert gd.detectors == {}


@pytest.mark.parametrize("operation", ["ListDetectors", "CreateDetector"])
def test_other_refusals_are_raised_for_the_judge(gd, operation):
    gd.deny[operation] = "AccessDeniedException"
    with pytest.raises(ClientError):
        ensure_detector(SESSION, REGION)
    assert gd.detectors == {}

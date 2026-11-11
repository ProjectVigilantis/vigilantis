"""GuardDuty 켜기(services/guardduty.ensure_detector · #588 B)와 탐지 결과 읽기(read_findings · #588 C) —
상태를 드는 GuardDuty 대역.

대역은 리전마다 탐지기를 들고 있고 실제 API 처럼 「계정 · 리전마다 하나」를 지킨다(두 번째 CreateDetector 는
BadRequestException). 실 AWS 의 서비스 연결 역할 · 조직 관리 · SCP 는 학원 계정 실측(11/16 뒤)으로 본다.
"""

import sys
from datetime import datetime, timedelta, timezone
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
        self.findings: list[dict] = []  # 그 리전에 쌓인 탐지 원문(#588 C)

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

    # --- 탐지 결과(#588 C). 조건 · 정렬 · 쪽 나눔을 API 처럼 해석한다
    def _matches(self, finding: dict, criterion: dict) -> bool:
        for field, condition in criterion.items():
            if field == "updatedAt":
                updated = int(_parse(finding["UpdatedAt"]).timestamp() * 1000)
                if updated < condition["GreaterThanOrEqual"]:
                    return False
            elif field == "accountId":
                if finding["AccountId"] not in condition["Equals"]:
                    return False
            elif field == "service.archived":
                archived = str(finding["Service"].get("Archived", False)).lower()
                if archived not in condition["Equals"]:
                    return False
            else:
                raise AssertionError(f"대역이 모르는 조건: {field}")
        return True

    def list_findings(self, *, DetectorId, FindingCriteria, SortCriteria, MaxResults, NextToken=None):
        self._call("ListFindings", DetectorId=DetectorId, FindingCriteria=FindingCriteria,
                   SortCriteria=SortCriteria, MaxResults=MaxResults, NextToken=NextToken)
        assert DetectorId in self.detectors and MaxResults <= 50
        assert SortCriteria == {"AttributeName": "updatedAt", "OrderBy": "ASC"}
        hits = sorted((f for f in self.findings if self._matches(f, FindingCriteria["Criterion"])),
                      key=lambda f: (_parse(f["UpdatedAt"]), f["Id"]))
        start = int(NextToken or 0)
        page = hits[start:start + MaxResults]
        more = start + MaxResults < len(hits)
        return {"FindingIds": [f["Id"] for f in page], **({"NextToken": str(start + MaxResults)} if more else {})}

    def get_findings(self, *, DetectorId, FindingIds):
        self._call("GetFindings", DetectorId=DetectorId, FindingIds=FindingIds)
        if len(FindingIds) > 50:
            raise _error("BadRequestException", "GetFindings")  # API 상한
        by_id = {f["Id"]: f for f in self.findings}
        return {"Findings": [by_id[i] for i in reversed(FindingIds)]}  # 순서를 지키지 않는다

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


ACCOUNT = "111122223333"
T0 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)


def finding(fid: str, *, updated: datetime, account: str = ACCOUNT, archived: bool = False,
            severity: float = 5.0) -> dict:
    """GetFindings 원문 모양의 탐지 1건(필요한 칸만)."""
    stamp = updated.isoformat().replace("+00:00", "Z")
    return {
        "Id": fid, "AccountId": account, "Region": REGION, "Severity": severity,
        "Type": "UnauthorizedAccess:EC2/SSHBruteForce", "UpdatedAt": stamp, "CreatedAt": stamp,
        "Arn": f"arn:aws:guardduty:{REGION}:{account}:detector/d-1/finding/{fid}",
        "Resource": {"ResourceType": "Instance", "InstanceDetails": {"InstanceId": "i-0abc"}},
        "Service": {"Archived": archived, "EventFirstSeen": stamp, "EventLastSeen": stamp},
    }


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


# --- 탐지 결과 읽기(#588 C) --------------------------------------------------------------------------------
@pytest.fixture
def reader(monkeypatch):
    """수집 주기는 tenant_scope 의 aws_client 로 읽는다 — 세션을 받지 않는다."""
    fake = FakeGuardDuty({"d-1": "ENABLED"})

    def aws_client(service, region):
        assert (service, region) == ("guardduty", REGION)
        return fake

    monkeypatch.setattr(guardduty, "aws_client", aws_client)
    return fake


def _read(since=T0):
    return guardduty.read_findings(REGION, account_id=ACCOUNT, updated_since=since)


def test_no_detector_means_nothing_to_read(reader):
    reader.detectors = {}
    assert _read() is None
    assert reader.names() == ["ListDetectors"]  # 켜기는 연결 확인의 몫이다 — 여기서 만들지 않는다


def test_reads_only_findings_updated_since_the_cursor(reader):
    reader.findings = [finding("old", updated=T0 - timedelta(seconds=1)), finding("at", updated=T0),
                       finding("new", updated=T0 + timedelta(hours=1))]
    # 커서와 같은 시각은 다시 읽는다(같은 밀리초의 탐지를 잃지 않게) — 중복은 받는 쪽이 접는다
    assert sorted(f["Id"] for f in _read()) == ["at", "new"]


def test_other_accounts_and_archived_findings_are_not_read(reader):
    # 위임 관리자 계정이면 멤버 계정 탐지도 돌아온다 — 조건이 계정으로 거른다
    reader.findings = [finding("mine", updated=T0), finding("member", updated=T0, account="999988887777"),
                       finding("archived", updated=T0, archived=True)]
    assert [f["Id"] for f in _read()] == ["mine"]


def test_pages_are_followed_and_details_fetched_fifty_at_a_time(reader):
    reader.findings = [finding(f"f{i:03}", updated=T0 + timedelta(seconds=i)) for i in range(120)]
    found = _read()
    assert sorted(f["Id"] for f in found) == [f"f{i:03}" for i in range(120)]
    batches = [len(params["FindingIds"]) for name, params in reader.calls if name == "GetFindings"]
    assert batches == [50, 50, 20]  # GetFindings 는 50건이 상한이다(대역도 넘기면 거절한다)


def test_one_round_reads_at_most_the_limit_oldest_first(reader, monkeypatch):
    monkeypatch.setattr(guardduty, "READ_LIMIT", 60)
    reader.findings = [finding(f"f{i:03}", updated=T0 + timedelta(seconds=i)) for i in range(120)]
    # 오름차순이라 남은 것은 다음 회차가 커서로 이어 읽는다 — 가장 늦은 것부터 읽으면 앞의 것을 영영 놓친다
    assert sorted(f["Id"] for f in _read()) == [f"f{i:03}" for i in range(60)]


def test_read_never_calls_anything_but_reads(reader):
    reader.findings = [finding("f", updated=T0)]
    _read()
    assert set(reader.names()) == {"ListDetectors", "ListFindings", "GetFindings"}


def test_refusal_is_raised_to_the_caller(reader):
    # 템플릿을 갱신하지 않은 고객 역할에는 ListFindings 가 없다 — 판정(FAILED)은 받는 쪽이 한다
    reader.findings = [finding("f", updated=T0)]
    reader.deny["ListFindings"] = "AccessDeniedException"
    with pytest.raises(ClientError):
        _read()

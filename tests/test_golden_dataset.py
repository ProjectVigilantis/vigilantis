# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# datasets/golden/ 의 Golden Dataset 회귀 테스트.
#
#   1) 입력 JSON 이 packages/schemas 의 Pydantic 모델로 검증되는가
#   2) 입력이 판정을 거쳤을 때 expected 와 일치하는가
#        FinOps 자산 → rule_engine (evaluate_ec2·evaluate_sg·evaluate_ebs)
#        SecOps 위협 → security/risk_evaluator (evaluate_threat, 2026-08-31 규칙 확정)
#   3) expected 에 기록된 임계값이 현재 판정기 상수와 같은가 (드리프트 감지)
#
# 3번이 ADR-0006 의 "임계값을 바꾸는 PR은 데이터 갱신을 포함해야 한다" 를
# 코드로 강제하는 장치다. JSON 은 상수를 import 할 수 없으므로 테스트가 대조한다.
# ==============================================================================

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import get_args

import pytest
from pydantic import TypeAdapter

# import 경로 추가 (apps/core-api/services/tests/test_rule_engine.py 와 동일 관례)
ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from schemas.api.assets import _RULE_TARGET_TYPES, AssetType  # noqa: E402
from schemas.assets import AssetInventory  # noqa: E402
from schemas.events import (  # noqa: E402
    MockThreatEventInput,
    NormalizedThreatEvent,
    OpenIpThreatPayload,
    SshBruteForceThreatPayload,
    ThreatEventType,
)
from security.risk_evaluator import (  # noqa: E402
    ALL_PORTS,
    ALL_PROTOCOL,
    SENSITIVE_PORTS,
    SSH_HIGH_ATTEMPT_MIN,
    SSH_HIGH_RATE_PER_MIN,
    SSH_SINGLE_ATTEMPT,
    WORLD_CIDRS,
    evaluate_threat,
)
from services.rule_engine import (  # noqa: E402
    IDLE_CPU_AVG,
    MIN_DATAPOINTS,
    SPIKE_CPU_MAX,
    evaluate_ebs,
    evaluate_ec2,
    evaluate_sg,
)

GOLDEN = ROOT / "datasets" / "golden"

FINOPS_INPUT = GOLDEN / "finops" / "input"
FINOPS_EXPECTED = GOLDEN / "finops" / "expected"
SECOPS_INPUT = GOLDEN / "secops" / "input"
SECOPS_EXPECTED = GOLDEN / "secops" / "expected"

_THREAT_ADAPTER = TypeAdapter(MockThreatEventInput)


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fp:
        data = json.load(fp)
    # 에디터 자동완성을 위한 $schema 는 계약 필드가 아니므로 제거한다.
    data.pop("$schema", None)
    return data


def _finops_pairs() -> list[tuple[Path, Path]]:
    pairs = []
    for src in sorted(FINOPS_INPUT.glob("*.json")):
        expected = FINOPS_EXPECTED / src.name
        assert expected.exists(), f"정답 파일 누락: {expected}"
        pairs.append((src, expected))
    return pairs


def _secops_pairs() -> list[tuple[Path, Path]]:
    """위협 입력 1건 = 정답 1건. 누락되면 여기서 즉시 걸린다."""
    pairs = []
    for src in sorted(SECOPS_INPUT.glob("*.json")):
        expected = SECOPS_EXPECTED / src.name
        assert expected.exists(), f"정답 파일 누락: {expected}"
        pairs.append((src, expected))
    return pairs


def _normalized(raw: dict) -> NormalizedThreatEvent:
    """Mock 위협 입력 → NormalizedThreatEvent.

    수집·정규화 단계가 아직 없어 테스트가 그 자리를 대신한다. 실제 정규화가 구현되면
    이 헬퍼를 그쪽으로 옮기고 여기서는 호출만 한다 —
    `apps/core-api/security/tests/test_risk_evaluator.py` 에도 같은 형태가 있다.
    """
    etype = ThreatEventType(raw["event_type"])
    if etype == ThreatEventType.OPEN_IP:
        payload = OpenIpThreatPayload(
            protocol=raw["protocol"],
            from_port=raw.get("from_port"),
            to_port=raw.get("to_port"),
            source_cidr=raw["source_cidr"],
        )
    else:
        payload = SshBruteForceThreatPayload(
            source_ip=raw["source_ip"],
            failed_attempt_count=raw["failed_attempt_count"],
            window_seconds=raw["window_seconds"],
        )
    return NormalizedThreatEvent(
        threat_event_id=f"te-{raw['event_id']}",
        source_event_id=raw["event_id"],
        event_type=etype,
        target_arn=raw["target_arn"],
        occurred_at=raw["occurred_at"],
        payload=payload,
        deduplication_key=raw["event_id"],
        collected_at=raw["occurred_at"],
    )


# ---------------------------------------------------------------- 입력 계약 검증


@pytest.mark.parametrize("path", sorted(SECOPS_INPUT.glob("*.json")), ids=lambda p: p.name)
def test_secops_input_validates(path: Path) -> None:
    """위협 입력이 MockThreatEventInput 으로 검증된다 (extra=forbid)."""
    _THREAT_ADAPTER.validate_python(_load(path))


@pytest.mark.parametrize("path", sorted(FINOPS_INPUT.glob("*.json")), ids=lambda p: p.name)
def test_finops_input_validates(path: Path) -> None:
    """자산 입력이 AssetInventory 로 검증된다."""
    AssetInventory.model_validate(_load(path))


@pytest.mark.parametrize("path", sorted(FINOPS_INPUT.glob("*.json")), ids=lambda p: p.name)
def test_finops_input_has_no_verdict_fields(path: Path) -> None:
    """자산 입력에 판정 결과가 섞여 있지 않다.

    AssetInventory 는 extra=forbid 가 아니라 모르는 필드를 '조용히 무시'하므로
    Pydantic 검증만으로는 잡히지 않는다. 원문 JSON 을 직접 본다.
    """
    forbidden = {
        "verdict", "skip_reason", "skip_reason_code", "health_score",
        "is_idle", "is_unused", "evaluation_status",
    }
    raw = _load(path)

    def walk(node, trail="") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in forbidden, (
                    f"{path.name}: 판정 결과 필드 '{key}' 가 입력에 들어 있습니다 "
                    f"(위치 {trail or 'root'}). 정답은 expected/ 에 둡니다."
                )
                walk(value, f"{trail}.{key}" if trail else key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{trail}[{i}]")

    walk(raw)


# ---------------------------------------------------------------- 임계값 드리프트


@pytest.mark.parametrize("_, expected_path", _finops_pairs(), ids=lambda p: getattr(p, "name", ""))
def test_thresholds_not_drifted(_: Path, expected_path: Path) -> None:
    """expected 에 기록된 임계값이 현재 rule_engine 상수와 같다.

    실패하면 임계값이 바뀐 것이다. Golden Dataset 의 경계값 케이스를 함께
    갱신해야 한다 (ADR-0006 임계값 결합 원칙).
    """
    recorded = _load(expected_path)["thresholds_at_authoring"]
    current = {
        "IDLE_CPU_AVG": IDLE_CPU_AVG,
        "SPIKE_CPU_MAX": SPIKE_CPU_MAX,
        "MIN_DATAPOINTS": MIN_DATAPOINTS,
    }
    for name, value in current.items():
        assert recorded[name] == value, (
            f"{name} 이 {recorded[name]} → {value} 로 변경됐습니다. "
            f"{expected_path.name} 의 경계값 케이스를 갱신하세요."
        )


# ---------------------------------------------------------------- 판정 대조


def _evaluate_inventory(inventory: AssetInventory) -> dict[str, tuple[str, str | None]]:
    """자산별 (verdict, skip_reason_code) 를 ARN 기준으로 반환."""
    results: dict[str, tuple[str, str | None]] = {}

    for ec2 in inventory.ec2_instances:
        summary = ec2.metric_summary
        verdict, skip, _health = evaluate_ec2(
            summary.cpu_avg, summary.cpu_max, summary.cpu_datapoints, ec2.tags
        )
        results[ec2.arn] = (verdict.value, skip.value if skip else None)

    for sg in inventory.security_groups:
        # collector.py 가 open_to_world(list) → bool 로 넘기는 규약과 동일하게 맞춘다.
        verdict, skip = evaluate_sg(sg.name, sg.attached, bool(sg.open_to_world))
        results[sg.arn] = (verdict.value, skip.value if skip else None)

    # EBS 도 판정 대상(_RULE_TARGET_TYPES). 미부착·available → UNUSED.
    for ebs in inventory.ebs_volumes:
        verdict, skip = evaluate_ebs(ebs.state, ebs.attached_instance_ids)
        results[ebs.arn] = (verdict.value, skip.value if skip else None)

    return results


@pytest.mark.parametrize("input_path, expected_path", _finops_pairs(), ids=lambda p: getattr(p, "name", ""))
def test_finops_verdicts_match_expected(input_path: Path, expected_path: Path) -> None:
    """rule_engine 판정 결과가 정답과 일치한다."""
    inventory = AssetInventory.model_validate(_load(input_path))
    actual = _evaluate_inventory(inventory)
    expected = _load(expected_path)

    covered = set()
    for case in expected["evaluations"]:
        arn = case["asset_arn"]
        assert arn in actual, f"{case['case_id']}: 입력에 없는 ARN {arn}"
        covered.add(arn)

        verdict, skip = actual[arn]
        assert verdict == case["verdict"], (
            f"{case['case_id']} verdict 불일치: 기대 {case['verdict']} / 실제 {verdict}\n"
            f"  목적: {case['purpose']}"
        )
        assert skip == case["skip_reason_code"], (
            f"{case['case_id']} skip_reason_code 불일치: "
            f"기대 {case['skip_reason_code']} / 실제 {skip}"
        )

    missing = set(actual) - covered
    assert not missing, f"정답이 없는 자산이 있습니다: {sorted(missing)}"


def test_finops_expected_has_no_runtime_fields() -> None:
    """정답에 런타임 값·미확정 값이 섞여 있지 않다."""
    excluded = {"collection_run_id", "evaluated_at", "health_score", "reason", "runbook_id"}
    for _, expected_path in _finops_pairs():
        for case in _load(expected_path)["evaluations"]:
            leaked = excluded & set(case)
            assert not leaked, f"{case['case_id']}: 제외 대상 필드가 있습니다 {sorted(leaked)}"


# ---------------------------------------------------------------- 자산 누락 감지


def _asset_list_fields() -> dict[str, AssetType]:
    """AssetInventory 의 '자산 리스트' 필드 → 그 리스트가 담는 자산 유형.

    필드 이름을 손으로 적지 않는다. 리스트 항목 모델의 asset_type 기본값에서 읽으므로
    (Ec2Asset.asset_type = AssetType.EC2, frozen), 자산 유형이 늘면 여기도 함께 는다.
    """
    fields: dict[str, AssetType] = {}
    for name, field in AssetInventory.model_fields.items():
        args = get_args(field.annotation)  # list[Ec2Asset] → (Ec2Asset,)
        if not args:
            continue
        declared = getattr(args[0], "model_fields", {}).get("asset_type")
        if declared is not None:
            fields[name] = declared.default
    return fields


# 판정이 붙지 않는 자산 리스트. 계약(_RULE_TARGET_TYPES)에서 **파생**시킨다 —
# 키 이름을 하드코딩하면 판정 대상이 늘어날 때 면제가 함께 좁아지지 않아, 가드가
# 조용히 헐거워진다. 지금은 NACL·Launch Template·ASG·ALB Target Group 4종이고,
# 어떤 유형이 _RULE_TARGET_TYPES 에 들어가는 순간 이 집합에서 자동으로 빠진다.
_JUDGEMENT_FREE_LIST_FIELDS = frozenset(
    name for name, asset_type in _asset_list_fields().items()
    if asset_type not in _RULE_TARGET_TYPES
)


def _count_asset_arns(raw: dict) -> int:
    """입력 JSON 원문에서 **판정 대상** 자산 ARN 개수를 센다(중첩 포함).

    판정 비대상 리스트는 세지 않는다. 그 자산들은 계약상 항상 NOT_APPLICABLE 이라
    (api/assets.py AssetItem._enforce_contract) 정답으로 적을 판정 자체가 없다.
    토폴로지가 그릴 노드를 골든에 넣으려면 이 면제가 필요하다.

    **모델이 모르는 키는 계속 센다.** 면제는 "판정 비대상임을 계약으로 증명한" 리스트에만
    준다 — 오타로 생긴 키나 모델보다 앞서 추가된 자산 리스트는 여기서 걸려야 한다.
    """
    count = 0
    counted = {key: value for key, value in raw.items() if key not in _JUDGEMENT_FREE_LIST_FIELDS}

    def walk(node) -> None:
        nonlocal count
        if isinstance(node, dict):
            if "arn" in node:
                count += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(counted)
    return count


def test_judgement_free_exemption_is_derived_from_the_contract() -> None:
    """면제 집합이 계약에서 파생됐는지 — 하드코딩으로 되돌아가면 여기서 걸린다.

    두 방향을 함께 본다. 면제된 것에 판정 대상이 섞이면 정답 누락을 못 잡고,
    판정 대상인데 리스트 필드가 없으면 그 유형은 골든에 담길 자리가 없다.
    """
    fields = _asset_list_fields()

    for name in _JUDGEMENT_FREE_LIST_FIELDS:
        assert fields[name] not in _RULE_TARGET_TYPES, (
            f"{name}({fields[name].value})은 판정 대상인데 면제됐다 — 정답 누락을 못 잡는다"
        )

    judged_fields = {t for n, t in fields.items() if n not in _JUDGEMENT_FREE_LIST_FIELDS}
    assert judged_fields == set(_RULE_TARGET_TYPES), (
        f"판정 대상 유형과 자산 리스트가 어긋난다: 계약 {sorted(t.value for t in _RULE_TARGET_TYPES)} / "
        f"리스트 {sorted(t.value for t in judged_fields)}"
    )


@pytest.mark.parametrize("input_path, expected_path", _finops_pairs(), ids=lambda p: getattr(p, "name", ""))
def test_finops_expected_covers_every_input_asset(input_path: Path, expected_path: Path) -> None:
    """입력의 모든 자산에 정답이 있다.

    _evaluate_inventory 는 ec2_instances·security_groups 만 순회한다. AssetInventory 에
    자산 리스트가 새로 생겼는데(예: ebs_volumes) 순회 추가를 빼먹으면, 그 자산은
    판정도 정답 대조도 없이 조용히 무시된다. AssetInventory 는 extra=forbid 가 아니라
    Pydantic 도 걸러주지 않으므로 원문 ARN 수를 직접 센다.

    test_finops_verdicts_match_expected 는 '정답에 있는데 입력에 없는' 방향만 잡는다.
    이 테스트가 반대 방향('입력에 있는데 정답에 없는')을 막는다.
    """
    asset_count = _count_asset_arns(_load(input_path))
    case_count = len(_load(expected_path)["evaluations"])
    assert asset_count == case_count, (
        f"{input_path.name}: 입력 자산 {asset_count}건 / 정답 {case_count}건.\n"
        f"  자산 유형이 추가됐다면 _evaluate_inventory 의 순회와 정답을 함께 갱신하세요."
    )

# ==============================================================================
# SecOps — 위험 판정 정답 대조 (판정 규칙 확정: 2026-08-31, PR #206 / 이슈 #210 J3)
# ==============================================================================
#
# FinOps 와 같은 3층 구조다.
#   1) 입력 계약 검증        — test_secops_input_validates (위 §입력 계약 검증)
#   2) 판정 대조             — test_secops_verdicts_match_expected
#   3) 임계값 드리프트 감지  — test_secops_thresholds_not_drifted
#
# 정답은 evaluate_threat 의 산출을 베낀 것이 아니라 **확정 규칙에서 도출**해 적었다.
# 베끼면 구현이 틀려도 정답지가 함께 틀려 회귀를 못 잡는다. 2)가 그 도출과 실제
# 산출을 대조하므로, 불일치는 "규칙 해석 오류" 아니면 "구현 버그" 둘 중 하나다.


@pytest.mark.parametrize("_, expected_path", _secops_pairs(), ids=lambda p: getattr(p, "name", ""))
def test_secops_thresholds_not_drifted(_: Path, expected_path: Path) -> None:
    """정답에 기록된 임계값이 현재 risk_evaluator 상수와 같다.

    실패하면 판정 임계가 바뀐 것이다. Golden 의 경계 케이스(S3·S4·S8·S9)가 어느
    분기에 떨어지는지가 함께 달라지므로 정답을 갱신해야 한다
    (ADR-0006 임계값 결합 원칙 — FinOps test_thresholds_not_drifted 와 같은 장치).
    """
    recorded = _load(expected_path)["thresholds_at_authoring"]
    current = {
        "WORLD_CIDRS": list(WORLD_CIDRS),
        "ALL_PROTOCOL": ALL_PROTOCOL,
        "ALL_PORTS": list(ALL_PORTS),
        "SENSITIVE_PORTS": list(SENSITIVE_PORTS),
        "SSH_SINGLE_ATTEMPT": SSH_SINGLE_ATTEMPT,
        "SSH_HIGH_ATTEMPT_MIN": SSH_HIGH_ATTEMPT_MIN,
        "SSH_HIGH_RATE_PER_MIN": SSH_HIGH_RATE_PER_MIN,
    }
    # 케이스가 실제로 의존하는 상수만 기록한다 — 기록된 것만 대조한다.
    checked = [name for name in current if name in recorded]
    assert checked, f"{expected_path.name}: thresholds_at_authoring 에 대조할 상수가 없습니다."
    for name in checked:
        assert recorded[name] == current[name], (
            f"{name} 이 {recorded[name]} → {current[name]} 로 변경됐습니다. "
            f"{expected_path.name} 의 정답을 갱신하세요."
        )


@pytest.mark.parametrize(
    "input_path, expected_path", _secops_pairs(), ids=lambda p: getattr(p, "name", "")
)
def test_secops_verdicts_match_expected(input_path: Path, expected_path: Path) -> None:
    """evaluate_threat 판정이 정답과 일치한다 (3값 전부)."""
    expected = _load(expected_path)
    result = evaluate_threat(_normalized(_load(input_path)))

    case = expected["case_id"]
    assert result.initial_risk_level.value == expected["initial_risk_level"], (
        f"{case} initial_risk_level 불일치: "
        f"기대 {expected['initial_risk_level']} / 실제 {result.initial_risk_level.value}\n"
        f"  목적: {expected['purpose']}\n"
        f"  도출: {expected['derivation']}"
    )
    assert result.response_mode.value == expected["response_mode"], (
        f"{case} response_mode 불일치: "
        f"기대 {expected['response_mode']} / 실제 {result.response_mode.value}"
    )
    assert sorted(c.value for c in result.reason_codes) == sorted(expected["reason_codes"]), (
        f"{case} reason_codes 불일치:\n"
        f"  기대 {sorted(expected['reason_codes'])}\n"
        f"  실제 {sorted(c.value for c in result.reason_codes)}\n"
        f"  도출: {expected['derivation']}"
    )


def test_secops_expected_source_points_at_its_input() -> None:
    """정답의 source 가 짝이 되는 입력을 가리킨다.

    파일명으로 짝을 짓기 때문에, source 를 잘못 적어도 대조는 통과한다. 사람이 정답을
    읽을 때 근거로 삼는 줄이라 여기서 못 박는다.
    """
    for input_path, expected_path in _secops_pairs():
        source = _load(expected_path)["source"]
        assert source == f"secops/input/{input_path.name}", (
            f"{expected_path.name}: source 가 {source} 로 적혀 있습니다."
        )


def test_secops_case_ids_are_unique() -> None:
    """case_id 가 10건 전부 다르다 — 복사로 만든 정답의 흔한 사고."""
    ids = [_load(p)["case_id"] for _, p in _secops_pairs()]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicated, f"중복된 case_id: {duplicated}"


def test_secops_expected_covers_every_risk_level() -> None:
    """정답 10건이 HIGH·MEDIUM·LOW 를 전부 담는다.

    Golden Dataset 은 '판정 분기 전량 커버'가 기준이다(FinOps 는 Verdict 4종·
    SkipReasonCode 5종 전량). 한 등급이 비면 그 경로는 회귀에서 빠진다.
    """
    levels = {_load(p)["initial_risk_level"] for _, p in _secops_pairs()}
    missing = {"HIGH", "MEDIUM", "LOW"} - levels
    assert not missing, (
        f"정답에 없는 위험도 등급: {sorted(missing)}. "
        f"해당 등급을 내는 입력 케이스를 secops/input 에 추가해야 합니다."
    )

"""C10 평가 전용 원천 파생: C01의 강한 SSH 관측·로그에서 출발지 주소만 루프백으로 바꾼다.

공유 corpus 생성기는 문서용 대역 출발지만 허용하므로 그 정책을 바꾸지 않고, 이 평가가 소유한
최소 변환을 쓴다. 횟수·창·시각·호스트 별칭·레코드 ID·메시지 틀은 그대로이고 바뀌는 값은
출발지 주소뿐이다. 변환한 원천은 `sources/C10/`에 동결하며 변환 결과의 지문을 새로 계산한다.
원본 지문을 변환본에 재사용하지 않는다. 모델·DB·AWS를 호출하지 않는다.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .dataset import EvalCase, digest, load_source

ORIGINAL_SOURCE_IP = "203.0.113.10"
LOOPBACK_SOURCE_IP = "127.0.0.1"
TRANSFORMATION = "evaluation_source_ip_substitution"
# 스키마(MockSshLogEvidence)가 C01~C07 별칭만 받으므로 로그의 사례·호스트 별칭은 C01을 유지한다.
SOURCE_DIR = "apps/core-api/ai/evaluation/secops/sources/C10"
FROZEN_FILES = {"threat": "threat.json", "manifest": "manifest.json",
                "expected": "expected.json", "records": "logs.jsonl"}


def _replace(value: str) -> str:
    return value.replace(ORIGINAL_SOURCE_IP, LOOPBACK_SOURCE_IP)


def derive_loopback_sources(original: dict[str, Any]) -> dict[str, Any]:
    """원본 C01 원천 넷(threat·manifest·expected·records)에서 C10 원천을 만든다."""
    threat = deepcopy(original["threat"])
    if threat["source_ip"] != ORIGINAL_SOURCE_IP:
        raise ValueError("C10 derivation expects the C01 documentation-range source")
    threat["source_ip"] = LOOPBACK_SOURCE_IP

    records = []
    for row in original["records"]:
        if ORIGINAL_SOURCE_IP not in row["message"]:
            raise ValueError(f"{row['record_id']}: record does not carry the original source")
        records.append({**row, "message": _replace(row["message"])})

    manifest = deepcopy(original["manifest"])
    if manifest["selection"]["source_ip"] != ORIGINAL_SOURCE_IP:
        raise ValueError("C10 derivation expects the C01 manifest source")
    manifest["selection"]["source_ip"] = LOOPBACK_SOURCE_IP
    manifest["golden_input"] = FROZEN_FILES["threat"]
    manifest["golden_fingerprint"] = digest(threat)
    manifest["evaluation_case_id"] = "C10"
    manifest["transformations"] = [*manifest["transformations"], TRANSFORMATION]

    leftovers = [row["record_id"] for row in records if ORIGINAL_SOURCE_IP in row["message"]]
    if leftovers:
        raise ValueError(f"original source remains in {leftovers[:3]}")
    return {"threat": threat, "manifest": manifest, "expected": deepcopy(original["expected"]),
            "records": records}


def check_derived_sources(cases: list[EvalCase]) -> None:
    """파생 사례의 동결 원천이 현재 원본에서 같은 변환으로 다시 나오는지 대조한다.

    계측 시점에만 호출한다. 원본 지문은 변환 전 값이므로 변환본 지문과 섞지 않는다.
    """
    for case in cases:
        derivation = case.provenance.get("derivation")
        if derivation is None:
            continue
        original = {name: load_source(name if name == "threat" else f"log_{name}",
                                      derivation["original"][f"{name}_source"])
                    for name in FROZEN_FILES}
        for name in FROZEN_FILES:
            if digest(original[name]) != derivation["original"][f"{name}_sha256"]:
                raise ValueError(f"{case.case_id}: original {name} source changed")
        derived = derive_loopback_sources(original)
        for name, expected in derived.items():
            key = "threat" if name == "threat" else f"log_{name}"
            frozen = load_source(key, case.provenance[f"{key}_source"])
            if frozen != expected or digest(frozen) != case.provenance[f"{key}_sha256"]:
                raise ValueError(f"{case.case_id}: frozen {name} differs from its derivation")

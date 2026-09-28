"""실험 실행기가 공유하는 입력 직렬화와 지문 계산."""

import hashlib
import json
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel


class GraphCase(Protocol):
    @property
    def graph_input(self) -> BaseModel: ...


def input_fingerprint(cases: Sequence[GraphCase]) -> str:
    """고정 입력 세트의 지문 — 그래프 입력(자산·판정·근거·capabilities) 전체의 sha256.

    스냅샷에 적어 두고 계측 도구가 대조한다 — CI가 아니다. 골든이 바뀌는 것은 골든 담당의
    일이라 막지 않고, 세트가 달라진 원자료로 이전 판과 짝 비교를 하려 할 때 도구가 알리고
    거절한다(ai/evaluation/summary/baseline.md §기준선).
    """
    digest = hashlib.sha256()
    for case in cases:
        digest.update(
            json.dumps(
                case.graph_input.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        )
    return digest.hexdigest()

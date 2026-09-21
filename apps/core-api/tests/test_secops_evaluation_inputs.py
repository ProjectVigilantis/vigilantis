"""고정 평가 입력을 자산 적재 → 모의 위협 접수 → PostgreSQL → Dispatcher와 대조한다.

생성된 UUID·접수/사본 보존 시각·관계 배열 순서는 정규화한다. 관측/자산 수집 시각,
관계 구성·로그·위험도·조치 메뉴는 일치해야 한다. 모델 요청의 바이트 단위 동일성,
모델 응답 품질이나 실제 AWS 수집을 입증하는 검증은 아니다.
"""

from contextlib import nullcontext
from uuid import UUID

import pytest
from agent_dispatcher import build_graph_input
from ai.evaluation.secops import dataset
from ai.evaluation.secops.dataset import load_cases, load_service_fixture
from mock_threat_source import MockThreatConsumer, prepare_observation
from services.collector import persist_inventory


def _normalize_generated_values(actual, expected):
    """키를 삭제하지 않고 정규화할 생성 필드를 명시적으로 열거한다."""
    UUID(actual["incident_id"])
    actual["incident_id"] = expected["incident_id"]
    assert len(actual["evidences"]) == 1
    evidence, frozen = actual["evidences"][0], expected["evidences"][0]
    UUID(evidence["evidence_id"])
    evidence["evidence_id"] = frozen["evidence_id"]
    event, frozen_event = evidence["content"]["event"], frozen["content"]["event"]
    UUID(event["threat_event_id"])
    assert event["threat_event_id"] == actual["initial_risk"]["threat_event_id"]
    event["threat_event_id"] = frozen_event["threat_event_id"]
    actual["initial_risk"]["threat_event_id"] = expected["initial_risk"]["threat_event_id"]
    event["collected_at"] = frozen_event["collected_at"]
    context, frozen_context = evidence["content"]["context"], frozen["content"]["context"]
    context["captured_at"] = frozen_context["captured_at"]
    collection_id = context["target"]["collection_run_id"]
    UUID(collection_id)
    context["target"]["collection_run_id"] = frozen_context["target"]["collection_run_id"]
    assert len(context["related_assets"]) == len(frozen_context["related_assets"])
    for related, frozen_related in zip(context["related_assets"], frozen_context["related_assets"], strict=True):
        assert related["collection_run_id"] == collection_id
        related["collection_run_id"] = frozen_related["collection_run_id"]


def _sort_relationship_collections(value):
    # PostgreSQL enum 순서는 수동 작성한 fixture와 다르다. 중복을 포함한 모든 행과
    # 값은 유지하고 배열 순서만 비교에서 제외한다.
    def sort_asset(asset):
        asset["relationships"].sort(key=lambda item: (item["relation_type"], item["target_arn"]))

    sort_asset(value["asset_context"])
    context = value["evidences"][0]["content"]["context"]
    sort_asset(context["target"]["asset"])
    context["related_assets"].sort(key=lambda item: item["asset"]["arn"])
    for related in context["related_assets"]:
        sort_asset(related["asset"])


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case.case_id)
def test_frozen_evaluation_input_matches_persisted_service_input(db, client_pg, tmp_path, monkeypatch, case):
    # 원본이 없어도 적재·접수·조회 경로를 검증한다. 기대 관계나 문맥을 DB에 직접 넣지 않는다.
    monkeypatch.setattr(dataset, "REPO_ROOT", tmp_path)
    fixture = load_service_fixture(case.case_id)
    persist_inventory(fixture.inventory, db)
    db.commit()
    prepare_observation(tmp_path, fixture.submission.observation, log_evidence=fixture.submission.log_evidence)
    published = []
    consumer = MockThreatConsumer(tmp_path, lambda: nullcontext(db), published.append, interval_seconds=1)
    assert consumer.consume_once() == {"created": 1, "existing": 0, "rejected": 0, "failed": 0}
    assert len(published) == 1
    incident_id = published[0].data.incident_id
    graph_input = build_graph_input(db, incident_id)
    evidence = graph_input.evidences[0].content
    assert evidence.context.captured_at >= evidence.event.collected_at
    actual = graph_input.model_dump(mode="json")
    expected = case.graph_input.model_dump(mode="json")
    _sort_relationship_collections(actual)
    _sort_relationship_collections(expected)
    _normalize_generated_values(actual, expected)
    assert actual == expected

    response = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["initial_risk_level"] == expected["initial_risk"]["initial_risk_level"]
    assert detail["response_mode"] == expected["initial_risk"]["response_mode"]
    assert detail["evidence_ids"] == [graph_input.evidences[0].evidence_id]

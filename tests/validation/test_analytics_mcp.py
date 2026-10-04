import json
from datetime import timedelta

import pytest
from mcp.client import Client
from rest_framework.test import APIClient

from analytics_platform.ingestion.credentials import create_credential
from analytics_platform.mcp_adapter.server import create_server
from scripts.mock_analytics_dataset import ANALYSIS_CUTOFF, BATCH_SIZE, DATASET_START, build_dataset

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def ingested_validation_dataset(project):
    _, secret = create_credential(project, name="generic-analytics-mcp-validation")
    producer = APIClient()
    producer.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
    dataset = build_dataset()
    for profile in dataset.profile_events:
        response = producer.post("/api/v1/capture/", profile, format="json")
        assert response.status_code == 201
    events = dataset.behavioral_events
    for index in range(0, len(events), BATCH_SIZE):
        batch = events[index : index + BATCH_SIZE]
        response = producer.post("/api/v1/bulk/", {"events": batch}, format="json")
        assert response.status_code == 200
        assert response.data["accepted"] == len(batch)
        assert response.data["rejected"] == 0
    return dataset


async def _call(client, name, arguments, project_id, outputs):
    result = await client.call_tool(name, arguments)
    assert result.is_error is False, (name, result.content)
    payload = result.structured_content
    assert payload["scope"] == {"project_id": str(project_id)}
    outputs[name + str(len(outputs))] = payload
    return payload


def _activity_rules_for_acme(dataset, start, end):
    choices = {}
    for event in dataset.behavioral_events:
        timestamp = event["timestamp"].replace("Z", "+00:00")
        if not start <= timestamp < end or event["groups"].get("account") != "acme":
            continue
        product = event["properties"].get("product")
        if product:
            choices.setdefault(product, event["event"])
    return [
        {"label": product, "event": event, "product": product}
        for product, event in sorted(choices.items())[:2]
    ]


@pytest.mark.anyio
async def test_mcp_exposes_generic_catalog_and_structured_query_tools(
    project, ingested_validation_dataset
):
    dataset = ingested_validation_dataset
    current_start = (ANALYSIS_CUTOFF - timedelta(days=30)).isoformat()
    previous_start = (ANALYSIS_CUTOFF - timedelta(days=60)).isoformat()
    history_start = DATASET_START.isoformat()
    end = ANALYSIS_CUTOFF.isoformat()
    rules = _activity_rules_for_acme(dataset, current_start, end)
    assert len(rules) == 2
    outputs = {}

    async with Client(create_server(project.pk)) as client:
        catalog = await _call(client, "discover_analytics_catalog", {}, project.pk, outputs)
        event_activity = await _call(
            client,
            "query_events",
            {
                "start": current_start,
                "end": end,
                "filters": [{"field": "product", "operator": "exists"}],
                "group_by": [{"kind": "product", "label": "product"}],
                "aggregations": [
                    {"kind": "event_count", "label": "events"},
                    {"kind": "distinct_id_count", "label": "exact_distinct_ids"},
                ],
                "comparison_start": previous_start,
                "comparison_end": current_start,
            },
            project.pk,
            outputs,
        )
        state = await _call(
            client,
            "analyze_group_state",
            {
                "group_type": "account",
                "state_basis": "period_end",
                "start": current_start,
                "end": end,
            },
            project.pk,
            outputs,
        )
        cross_product = await _call(
            client,
            "query_group_activity",
            {
                "group_type": "account",
                "start": current_start,
                "end": end,
                "state_basis": "current",
                "activity_rules": rules,
                "match": "all",
                "include_distinct_id_overlap": True,
            },
            project.pk,
            outputs,
        )
        adoption = await _call(
            client,
            "analyze_group_adoption",
            {
                "group_type": "account",
                "start": current_start,
                "end": end,
                "state_basis": "period_end",
                "eligibility_filters": [
                    {"property_name": "segment", "operator": "eq", "value": "enterprise"}
                ],
                "activity_rule": {
                    "label": "contact_center_activity",
                    "event": "call_initiated",
                    "product": "contact_center",
                },
            },
            project.pk,
            outputs,
        )
        funnel = await _call(
            client,
            "analyze_group_funnel",
            {
                "group_type": "account",
                "start": history_start,
                "end": end,
                "state_basis": "event_time",
                "steps": [
                    {"label": "initiated", "event": "call_initiated", "product": "contact_center"},
                    {"label": "connected", "event": "call_connected", "product": "contact_center"},
                ],
            },
            project.pk,
            outputs,
        )
        transitions = await _call(
            client,
            "analyze_group_transitions",
            {
                "group_type": "account",
                "property_name": "contact_center_plan",
                "start": history_start,
                "end": end,
                "state_basis": "event_time",
            },
            project.pk,
            outputs,
        )
        comparison = await _call(
            client,
            "compare_group_activity",
            {
                "group_type": "account",
                "start": current_start,
                "end": end,
                "comparison_start": previous_start,
                "comparison_end": current_start,
                "state_basis": "current",
                "activity_rules": rules,
                "match": "any",
            },
            project.pk,
            outputs,
        )

    assert catalog["counts"]["event_definitions"] == 24
    assert catalog["event_properties"]["returned_count"] > 0
    assert catalog["group_properties"]["returned_count"] > 0
    assert event_activity["rows"]["returned_count"] > 0
    assert event_activity["comparison"]["current"]["returned_count"] > 0
    assert state["state_basis"] == "period_end"
    assert state["groups"]["total_count"] == 10
    assert state["groups"]["returned_count"] == 10
    assert [row["group_key"] for row in cross_product["groups"]["items"]] == ["acme"]
    overlap = cross_product["distinct_id_overlap"]
    assert overlap["basis"] == "exact distinct_id string equality"
    assert overlap["identity_resolution_performed"] is False
    assert "does not establish" in overlap["zero_result_interpretation"]
    assert adoption["eligibility_filters"]
    assert adoption["denominator"] > 0
    assert funnel["steps"][0]["cohort_count"] >= funnel["steps"][1]["cohort_count"]
    assert transitions["state_basis"] == "event_time"
    assert comparison["match"] == "any"

    serialized = json.dumps(outputs, sort_keys=True)
    for event in dataset.behavioral_events:
        assert event["distinct_id"] not in serialized

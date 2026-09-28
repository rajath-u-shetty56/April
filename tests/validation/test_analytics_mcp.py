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
    _, secret = create_credential(project, name="analytics-mcp-validation")
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


@pytest.mark.anyio
async def test_mcp_answers_validation_questions_with_scoped_aggregate_evidence(
    project, ingested_validation_dataset
):
    dataset = ingested_validation_dataset
    current_start = (ANALYSIS_CUTOFF - timedelta(days=30)).isoformat()
    decline_start = (ANALYSIS_CUTOFF - timedelta(weeks=5)).isoformat()
    dataset_start = DATASET_START.isoformat()
    end = ANALYSIS_CUTOFF.isoformat()
    outputs = {}

    async with Client(create_server(project.pk)) as client:
        catalog = await _call(client, "describe_project", {}, project.pk, outputs)
        profile = await _call(
            client, "get_account_profile", {"account_key": "acme"}, project.pk, outputs
        )
        activity = await _call(
            client,
            "summarize_account_activity",
            {"account_key": "acme", "start": current_start, "end": end},
            project.pk,
            outputs,
        )
        cross_two = await _call(
            client,
            "find_cross_product_accounts",
            {
                "products": ["helpdesk", "contact_center"],
                "start": current_start,
                "end": end,
            },
            project.pk,
            outputs,
        )
        cross_three = await _call(
            client,
            "find_cross_product_accounts",
            {
                "products": ["helpdesk", "contact_center", "bi"],
                "start": current_start,
                "end": end,
            },
            project.pk,
            outputs,
        )
        shared_overlap = await _call(
            client,
            "find_cross_product_accounts",
            {
                "products": ["helpdesk", "contact_center", "rise"],
                "start": current_start,
                "end": end,
            },
            project.pk,
            outputs,
        )
        adoption = await _call(
            client,
            "analyze_product_adoption",
            {
                "product": "contact_center",
                "start": current_start,
                "end": end,
                "include_plan_breakdown": True,
            },
            project.pk,
            outputs,
        )
        users = await _call(
            client,
            "count_feature_users",
            {"product": "contact_center", "start": current_start, "end": end},
            project.pk,
            outputs,
        )
        funnels = {}
        for funnel in ("call_connection", "call_transfer", "callback"):
            funnels[funnel] = await _call(
                client,
                "analyze_funnel",
                {"funnel": funnel, "start": dataset_start, "end": end},
                project.pk,
                outputs,
            )
        decline = await _call(
            client,
            "analyze_account_change",
            {
                "kind": "usage_decline",
                "product": "contact_center",
                "start": decline_start,
                "end": end,
            },
            project.pk,
            outputs,
        )
        abandonment = await _call(
            client,
            "analyze_account_change",
            {
                "kind": "feature_abandonment",
                "product": "contact_center",
                "feature": "supervisor_whisper",
                "start": current_start,
                "end": end,
            },
            project.pk,
            outputs,
        )
        trials = await _call(
            client,
            "analyze_trial_outcomes",
            {"product": "contact_center", "start": dataset_start, "end": end},
            project.pk,
            outputs,
        )

    assert catalog["counts"] == {
        "events": 3524,
        "group_profiles": 10,
        "event_definitions": 24,
    }
    assert profile["found"] is True
    assert profile["state_basis"] == "current_profile"
    assert {"helpdesk", "contact_center", "bi"} <= set(activity["active_products"])
    assert [item["account_key"] for item in cross_two["accounts"]["items"]] == [
        "acme",
        "juliet",
    ]
    assert [item["account_key"] for item in cross_three["accounts"]["items"]] == ["acme"]
    overlap = shared_overlap["user_overlap"]
    assert overlap["users_by_product"]["rise"] > 0
    assert overlap["pairwise_overlap"]["contact_center|helpdesk"] > 0
    assert overlap["pairwise_overlap"]["helpdesk|rise"] > 0

    assert adoption["interpretation"]["entitlement_basis"] == "period_end"
    assert adoption["interpretation"]["plan_attribution_basis"] == "event_time"
    assert adoption["features"]["supervisor_listen"]["high_adoption_low_depth"] is True
    assert adoption["plans"]["pro"]["features"]["supervisor_listen"]["rate"] == 75.0
    acme_users = next(
        item for item in users["accounts"]["items"] if item["account_key"] == "acme"
    )
    assert acme_users["features"]["call_transfer"] == 4
    assert acme_users["features"]["supervisor_listen"] == 2

    assert all(result["started"] > result["completed"] for result in funnels.values())
    assert "golf" in [item["account_key"] for item in decline["accounts"]["items"]]
    assert [item["account_key"] for item in abandonment["accounts"]["items"]] == ["hotel"]
    assert [
        item["account_key"] for item in trials["used_before_conversion"]["items"]
    ] == ["echo"]
    assert [item["account_key"] for item in trials["used_then_expired"]["items"]] == [
        "foxtrot"
    ]

    for payload in outputs.values():
        if "period" in payload:
            assert payload["interpretation"]
    serialized = json.dumps(outputs, sort_keys=True)
    assert "distinct_id" not in serialized
    for event in dataset.behavioral_events:
        assert event["distinct_id"] not in serialized

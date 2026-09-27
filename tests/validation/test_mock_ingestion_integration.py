import pytest
from rest_framework.test import APIClient

from analytics_platform.ingestion.credentials import create_credential
from scripts.mock_analytics_analysis import analyze_project
from scripts.mock_analytics_dataset import ANALYSIS_CUTOFF, build_dataset

pytestmark = pytest.mark.django_db(transaction=True)


def test_representative_dataset_slice_flows_through_public_ingestion(project):
    _, secret = create_credential(project, name="synthetic-validation-test")
    producer = APIClient()
    producer.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
    dataset = build_dataset()
    profiles = {
        event["properties"]["$group_key"]: event
        for event in dataset.profile_events
        if event["properties"]["$group_key"] in {"acme", "delta"}
        and event["properties"]["$group_set"]["account_name"] in {"Acme", "Delta"}
    }
    for profile in profiles.values():
        response = producer.post("/api/v1/capture/", profile, format="json")
        assert response.status_code == 201

    latest_by_product = {}
    for event in dataset.behavioral_events:
        product = event["properties"]["product"]
        account = event["groups"]["account"]
        if (product in {"helpdesk", "contact_center", "bi"} and account == "acme") or (
            product == "rise" and account == "delta"
        ):
            latest_by_product[product] = event
    products = ("helpdesk", "contact_center", "bi", "rise")
    events = [latest_by_product[product] for product in products]

    response = producer.post("/api/v1/bulk/", {"events": events}, format="json")

    assert response.status_code == 200
    assert response.data["accepted"] == 4
    assert response.data["rejected"] == 0
    assert all(item["duplicate"] is False for item in response.data["results"])
    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)
    assert result["validation"] == {
        "event_count": 6,
        "behavioral_event_count": 4,
        "groupidentify_count": 2,
        "group_profile_count": 2,
        "event_definitions_by_product": {
            "bi": 1,
            "contact_center": 1,
            "helpdesk": 1,
            "rise": 1,
        },
    }
    assert set(project.group_profiles.values_list("group_key", flat=True)) == {"acme", "delta"}

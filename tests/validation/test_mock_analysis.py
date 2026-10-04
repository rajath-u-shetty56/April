import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from django.core.management import call_command

from analytics_platform.events.models import Event
from analytics_platform.group_analytics.models import GroupProfile
from scripts.mock_analytics_analysis import analyze_project, assert_expected_results
from scripts.mock_analytics_dataset import ANALYSIS_CUTOFF, build_dataset

pytestmark = pytest.mark.django_db


def test_analysis_script_can_be_invoked_directly():
    repository = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [sys.executable, "scripts/mock_analytics_analysis.py", "--help"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "generic structured analytics" in result.stdout


def test_analysis_script_bootstraps_repository_and_django_paths():
    repository = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [
            sys.executable,
            "scripts/mock_analytics_analysis.py",
            "--project-id",
            "00000000-0000-0000-0000-000000000000",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Project matching query does not exist" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def _store_dataset(project):
    dataset = build_dataset()
    current_profiles = {}
    for envelope in dataset.profile_events:
        properties = envelope["properties"]
        account = properties["$group_key"]
        current_profiles[account] = {
            **current_profiles.get(account, {}),
            **properties["$group_set"],
        }
    GroupProfile.objects.bulk_create(
        GroupProfile(
            project=project,
            group_type="account",
            group_key=account,
            properties=properties,
        )
        for account, properties in current_profiles.items()
    )
    envelopes = (*dataset.profile_events, *dataset.behavioral_events)
    Event.objects.bulk_create(
        Event(
            project=project,
            uuid=UUID(envelope["uuid"]),
            event=envelope["event"],
            distinct_id=envelope["distinct_id"],
            timestamp=datetime.fromisoformat(envelope["timestamp"].replace("Z", "+00:00")),
            groups=envelope["groups"],
            properties=envelope["properties"],
        )
        for envelope in envelopes
    )
    call_command(
        "backfill_property_catalog",
        project_id=str(project.pk),
        batch_size=150,
        verbosity=0,
    )
    return dataset


def test_analysis_runs_generic_structured_queries_on_backfilled_catalog(project):
    dataset = _store_dataset(project)

    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)

    assert result["validation"]["event_count"] == (
        len(dataset.profile_events) + len(dataset.behavioral_events)
    )
    assert result["validation"]["groupidentify_count"] == 13
    assert result["validation"]["group_profile_count"] == 10
    assert result["validation"]["event_definitions_by_product"] == {
        "bi": 4,
        "contact_center": 13,
        "helpdesk": 4,
        "rise": 3,
    }
    assert result["validation"]["event_property_definition_count"] > 0
    assert result["validation"]["group_property_definition_count"] > 0
    assert result["catalog"]["event_properties"]["returned_count"] > 0
    assert result["catalog"]["group_properties"]["returned_count"] > 0
    assert result["product_event_activity"]["rows"]["returned_count"] > 0
    assert result["product_activity_comparison"]["comparison"]["current"]["returned_count"] > 0
    assert result["group_state"]["state_basis"] == "period_end"
    assert result["group_state"]["groups"]["returned_count"] == 10

    activity = result["cross_product_activity"]
    assert activity["match"] == "all"
    assert len(activity["activity_rules"]) >= 2
    assert activity["distinct_id_overlap"]["basis"] == "exact distinct_id string equality"
    assert activity["distinct_id_overlap"]["identity_resolution_performed"] is False
    assert result["explicit_eligibility_adoption"]["eligibility_filters"]
    assert result["generic_funnel"]["state_basis"] == "event_time"
    assert result["group_property_transitions"]["state_basis"] == "event_time"
    assert assert_expected_results(result) == []


def test_expected_result_assertions_detect_count_and_catalog_drift(project):
    _store_dataset(project)
    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)
    result["validation"]["event_count"] -= 1
    result["validation"]["event_definitions_by_product"]["contact_center"] -= 1

    failures = assert_expected_results(result)

    assert any(failure.startswith("event count:") for failure in failures)
    assert any(failure.startswith("event definitions by product:") for failure in failures)


def test_exact_identifier_aggregates_never_return_identifier_values(project):
    dataset = _store_dataset(project)

    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)
    serialized = str(result)

    assert result["product_event_activity"]["identifier_semantics"] == (
        "exact_distinct_id_equality"
    )
    for event in dataset.behavioral_events:
        assert event["distinct_id"] not in serialized
    assert "distinct_id" in serialized
    assert "distinct_id_equality" in serialized

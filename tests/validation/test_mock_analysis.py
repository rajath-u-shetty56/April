import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from analytics_platform.event_catalog.models import EventDefinition
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
    assert "Analyze and assert" in result.stdout


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
    definitions = {
        (event["properties"]["product"], event["event"])
        for event in dataset.behavioral_events
    }
    EventDefinition.objects.bulk_create(
        EventDefinition(project=project, product_key=product, name=name)
        for product, name in definitions
    )
    return dataset


def test_analysis_answers_core_account_product_and_trial_questions(project):
    dataset = _store_dataset(project)

    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)

    assert result["validation"]["event_count"] == (
        len(dataset.profile_events) + len(dataset.behavioral_events)
    )
    assert result["validation"]["groupidentify_count"] == 13
    assert result["validation"]["group_profile_count"] == 10
    assert result["entitlement"]["current_entitled_accounts"] == [
        "acme",
        "bravo",
        "charlie",
        "echo",
        "golf",
        "hotel",
        "india",
        "juliet",
    ]
    assert result["entitlement"]["entitled_without_usage"] == ["charlie", "hotel"]
    assert result["entitlement"]["usage_percentage"] == 75.0
    assert result["entitlement"]["interpretation"]["entitlement_basis"] == "period_end"
    assert result["cross_product"]["helpdesk_and_contact_center"] == [
        "acme",
        "juliet",
    ]
    assert result["cross_product"]["helpdesk_contact_center_and_bi"] == ["acme"]
    assert result["trials"]["used_before_conversion"] == ["echo"]
    assert result["trials"]["used_then_expired"] == ["foxtrot"]
    assert "golf" in result["weekly_decline"]["declining_accounts"]
    assert result["abandonment"]["supervisor_whisper"]["stopped_accounts"] == ["hotel"]
    assert "call_initiated" not in result["feature_adoption"]
    assert "call_transfer" in result["feature_adoption"]
    assert "ai_summary" in result["feature_adoption"]
    assert "supervisor_listen" in result["high_adoption_low_depth"]
    pro_listen = result["adoption_by_plan_at_event_time"]["plans"]["pro"]["features"][
        "supervisor_listen"
    ]
    assert result["adoption_by_plan_at_event_time"]["plans"]["pro"][
        "eligible_account_count"
    ] == 4
    assert pro_listen == {
        "adopting_account_count": 3,
        "adopting_accounts": ["acme", "golf", "juliet"],
        "rate": 75.0,
    }
    assert result["adoption_by_plan_at_event_time"]["plans"]["enterprise"][
        "features"
    ]["supervisor_listen"]["rate"] == 50.0
    assert result["adoption_by_plan_at_event_time"]["interpretation"][
        "plan_attribution_basis"
    ] == "event_time"


def test_analysis_matches_expected_synthetic_outcomes(project):
    _store_dataset(project)
    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)

    assert assert_expected_results(result) == []


def test_expected_result_assertions_detect_count_and_catalog_drift(project):
    _store_dataset(project)
    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)
    result["validation"]["event_count"] -= 1
    result["validation"]["event_definitions_by_product"]["contact_center"] -= 1

    failures = assert_expected_results(result)

    assert any(failure.startswith("event count:") for failure in failures)
    assert any(failure.startswith("event definitions by product:") for failure in failures)


def test_funnels_match_events_by_call_id_not_only_aggregate_counts(project):
    _store_dataset(project)

    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)
    initiated = result["funnels"]["call_initiated_to_call_connected"]

    assert initiated["started"] > initiated["completed"]
    assert initiated["lost"] == initiated["started"] - initiated["completed"]
    assert initiated["completion_rate"] < 100
    assert result["funnels"][
        "call_transfer_initiated_to_call_transfer_completed"
    ]["lost"] > 0
    assert result["funnels"]["callback_requested_to_callback_fulfilled"]["lost"] > 0


def test_active_cross_product_usage_ignores_products_seen_only_before_period(project):
    _store_dataset(project)
    Event.objects.create(
        project=project,
        uuid=UUID("00000000-0000-0000-0000-000000000099"),
        event="dashboard_viewed",
        distinct_id="bi:juliet:user:1",
        timestamp=ANALYSIS_CUTOFF - timedelta(days=40),
        groups={"account": "juliet"},
        properties={"product": "bi", "dashboard_id": "old-dashboard"},
    )

    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)

    assert result["cross_product"]["period_days"] == 30
    assert result["cross_product"]["helpdesk_contact_center_and_bi"] == ["acme"]


def test_distinct_users_are_grouped_by_account_product_and_feature(project):
    _store_dataset(project)

    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)
    acme = result["distinct_users_by_account_product_feature"]["accounts"]["acme"]

    assert result["distinct_users_by_account_product_feature"]["period_days"] == 30
    assert acme["contact_center"]["call_transfer"] == 4
    assert acme["contact_center"]["supervisor_listen"] == 2
    assert acme["helpdesk"]["helpdesk_macro"] == 2

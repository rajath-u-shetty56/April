from datetime import UTC, datetime
from uuid import UUID

import pytest

from analytics_platform.analytics.contracts import TimeRange
from analytics_platform.analytics.profiles import load_profile_timelines
from analytics_platform.events.models import Event

pytestmark = pytest.mark.django_db


def _identify(project, *, event_id, timestamp, properties):
    return Event.objects.create(
        project=project,
        uuid=UUID(int=event_id),
        event="$groupidentify",
        distinct_id=f"system:{event_id}",
        timestamp=timestamp,
        groups={"account": "acme"},
        properties=properties,
    )


def test_profile_timeline_merges_changes_in_timestamp_and_uuid_order(project):
    first = datetime(2026, 9, 1, tzinfo=UTC)
    same_time = datetime(2026, 9, 10, tzinfo=UTC)
    _identify(
        project,
        event_id=1,
        timestamp=first,
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"contact_center_status": "trial", "region": "IN"},
        },
    )
    _identify(
        project,
        event_id=3,
        timestamp=same_time,
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"contact_center_plan": "pro"},
        },
    )
    _identify(
        project,
        event_id=2,
        timestamp=same_time,
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"contact_center_status": "active"},
        },
    )

    timeline = load_profile_timelines(project)["acme"]

    assert timeline.state_at(first) == {
        "contact_center_status": "trial",
        "region": "IN",
    }
    assert timeline.state_at(same_time) == {
        "contact_center_status": "active",
        "contact_center_plan": "pro",
        "region": "IN",
    }
    assert [transition.properties for transition in timeline.transitions] == [
        {"contact_center_status": "trial", "region": "IN"},
        {
            "contact_center_status": "active",
            "region": "IN",
        },
        {
            "contact_center_status": "active",
            "contact_center_plan": "pro",
            "region": "IN",
        },
    ]


def test_period_end_state_excludes_transition_at_exclusive_end(project):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 10, 1, tzinfo=UTC)
    _identify(
        project,
        event_id=1,
        timestamp=start,
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"contact_center_status": "trial"},
        },
    )
    _identify(
        project,
        event_id=2,
        timestamp=end,
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"contact_center_status": "active"},
        },
    )

    timeline = load_profile_timelines(project)["acme"]

    assert timeline.state_at(end, inclusive=False) == {"contact_center_status": "trial"}
    assert timeline.state_at(end) == {"contact_center_status": "active"}
    assert len(timeline.states_during(TimeRange.create(start, end))) == 1


def test_profile_loader_ignores_malformed_non_account_and_other_project(project, workspace):
    other = project.__class__.objects.create(workspace=workspace, key="other", name="Other")
    timestamp = datetime(2026, 9, 1, tzinfo=UTC)
    common = {"$group_key": "acme", "$group_set": {"status": "active"}}
    _identify(
        project,
        event_id=1,
        timestamp=timestamp,
        properties={**common, "$group_type": "company"},
    )
    _identify(
        project,
        event_id=2,
        timestamp=timestamp,
        properties={"$group_type": "account", "$group_key": "acme"},
    )
    _identify(
        other,
        event_id=3,
        timestamp=timestamp,
        properties={**common, "$group_type": "account"},
    )

    assert load_profile_timelines(project) == {}

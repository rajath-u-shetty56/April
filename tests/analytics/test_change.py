from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from analytics_platform.analytics.change import analyze_account_change
from analytics_platform.analytics.contracts import AnalyticsInputError, TimeRange
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event

pytestmark = pytest.mark.django_db


def _event(project, *, account, timestamp, name="call_connected"):
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event=name,
        distinct_id=f"private:{account}",
        timestamp=timestamp,
        groups={"account": account},
        properties={"product": "contact_center", "call_id": str(uuid4())},
    )


def test_usage_decline_uses_equal_preceding_period_and_full_population(project, workspace):
    other = Project.objects.create(workspace=workspace, key="other", name="Other")
    start = datetime(2026, 9, 11, tzinfo=UTC)
    end = start + timedelta(days=10)
    previous_start = start - timedelta(days=10)
    for account in ("alpha", "bravo", "charlie"):
        for offset in range(4):
            _event(project, account=account, timestamp=previous_start + timedelta(days=offset))
        _event(project, account=account, timestamp=start + timedelta(days=1))
    _event(project, account="new-user", timestamp=start + timedelta(days=1))
    for offset in range(4):
        _event(other, account="secret", timestamp=previous_start + timedelta(days=offset))

    payload = analyze_account_change(
        project,
        "usage_decline",
        "contact_center",
        TimeRange.create(start, end),
        account_limit=2,
    ).to_dict()

    assert payload["comparison_period"] == {
        "start": "2026-09-01T00:00:00Z",
        "end": "2026-09-11T00:00:00Z",
    }
    assert payload["threshold"] == 50.0
    assert payload["accounts"]["total_count"] == 3
    assert payload["accounts"]["returned_count"] == 2
    assert payload["accounts"]["truncated"] is True
    assert payload["matching_account_count"] == 3
    assert payload["accounts"]["items"][0] == {
        "account_key": "alpha",
        "previous_events": 4,
        "current_events": 1,
        "decline_percentage": 75.0,
    }
    assert "new-user" not in [item["account_key"] for item in payload["accounts"]["items"]]


def test_feature_abandonment_uses_qualifying_events(project):
    start = datetime(2026, 9, 11, tzinfo=UTC)
    end = start + timedelta(days=10)
    previous = start - timedelta(days=5)
    current = start + timedelta(days=1)
    for account in ("alpha", "bravo"):
        _event(project, account=account, timestamp=previous, name="listen_started")
    _event(project, account="bravo", timestamp=current, name="listen_started")
    _event(project, account="alpha", timestamp=current, name="call_connected")

    payload = analyze_account_change(
        project,
        "feature_abandonment",
        "contact_center",
        TimeRange.create(start, end),
        feature="supervisor_listen",
    ).to_dict()

    assert payload["matching_account_count"] == 1
    assert payload["accounts"]["items"] == [
        {
            "account_key": "alpha",
            "previous_feature_events": 1,
            "current_feature_events": 0,
        }
    ]


def test_change_analysis_validates_kind_threshold_and_feature(project):
    period = TimeRange.create(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
    )
    with pytest.raises(AnalyticsInputError, match="kind"):
        analyze_account_change(project, "unknown", "contact_center", period)
    with pytest.raises(AnalyticsInputError, match="threshold"):
        analyze_account_change(
            project, "usage_decline", "contact_center", period, decline_threshold=101
        )
    with pytest.raises(AnalyticsInputError, match="feature"):
        analyze_account_change(project, "feature_abandonment", "contact_center", period)

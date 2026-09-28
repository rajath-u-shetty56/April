from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from analytics_platform.analytics.contracts import TimeRange
from analytics_platform.analytics.trials import analyze_trial_outcomes
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event

pytestmark = pytest.mark.django_db


def _identify(project, *, account, timestamp, status):
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event="$groupidentify",
        distinct_id=f"system:{account}",
        timestamp=timestamp,
        groups={"account": account},
        properties={
            "$group_type": "account",
            "$group_key": account,
            "$group_set": {"contact_center_status": status},
        },
    )


def _usage(project, *, account, timestamp, product="contact_center"):
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event="call_connected" if product == "contact_center" else "ticket_created",
        distinct_id=f"private:{account}",
        timestamp=timestamp,
        groups={"account": account},
        properties={"product": product},
    )


def test_trial_outcomes_count_only_usage_before_first_terminal_transition(project, workspace):
    other = Project.objects.create(workspace=workspace, key="other", name="Other")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    for account, terminal in (("convert", "active"), ("expire", "expired")):
        trial_at = start - timedelta(days=1) if account == "convert" else start + timedelta(days=1)
        terminal_at = start + timedelta(days=10)
        _identify(project, account=account, timestamp=trial_at, status="trial")
        if account == "convert":
            _usage(project, account=account, timestamp=trial_at + timedelta(hours=1))
        _usage(project, account=account, timestamp=trial_at + timedelta(days=1))
        _usage(project, account=account, timestamp=trial_at + timedelta(days=2))
        _usage(
            project,
            account=account,
            timestamp=trial_at + timedelta(days=3),
            product="helpdesk",
        )
        _identify(project, account=account, timestamp=terminal_at, status=terminal)
        _usage(project, account=account, timestamp=terminal_at)
        _usage(project, account=account, timestamp=terminal_at + timedelta(days=1))
    _identify(project, account="no-use", timestamp=start + timedelta(days=1), status="trial")
    _identify(project, account="no-use", timestamp=start + timedelta(days=5), status="expired")
    _identify(other, account="secret", timestamp=start + timedelta(days=1), status="trial")
    _usage(other, account="secret", timestamp=start + timedelta(days=2))
    _identify(other, account="secret", timestamp=start + timedelta(days=3), status="active")

    payload = analyze_trial_outcomes(
        project, "contact_center", TimeRange.create(start, end)
    ).to_dict()

    assert payload["interpretation"]["historical_profile_reliability"] == (
        "conditional_on_complete_correctly_timestamped_groupidentify_history"
    )
    assert payload["semantics"] == {
        "trial_status": "trial",
        "conversion_status": "active",
        "expiry_status": "expired",
    }
    assert payload["used_before_conversion"]["total_count"] == 1
    assert payload["used_then_expired"]["total_count"] == 1
    conversion = payload["used_before_conversion"]["items"][0]
    expiry = payload["used_then_expired"]["items"][0]
    assert conversion["account_key"] == "convert"
    assert conversion["event_count"] == 2
    assert conversion["outcome_at"] == "2026-09-11T00:00:00Z"
    assert expiry["account_key"] == "expire"
    assert expiry["event_count"] == 2

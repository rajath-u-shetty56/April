from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from analytics_platform.analytics.contracts import TimeRange
from analytics_platform.analytics.funnels import analyze_funnel
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event

pytestmark = pytest.mark.django_db


def _event(project, *, account, name, call_id, timestamp):
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event=name,
        distinct_id=f"private:{account}",
        timestamp=timestamp,
        groups={"account": account},
        properties={"product": "contact_center", "call_id": call_id},
    )


def test_funnel_pairs_each_start_with_one_strictly_later_completion(project, workspace):
    other = Project.objects.create(workspace=workspace, key="other", name="Other")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    _event(
        project,
        account="acme",
        name="call_connected",
        call_id="call-1",
        timestamp=start + timedelta(minutes=1),
    )
    _event(
        project,
        account="acme",
        name="call_initiated",
        call_id="call-1",
        timestamp=start + timedelta(minutes=2),
    )
    _event(
        project,
        account="acme",
        name="call_initiated",
        call_id="call-1",
        timestamp=start + timedelta(minutes=3),
    )
    _event(
        project,
        account="acme",
        name="call_connected",
        call_id="call-1",
        timestamp=start + timedelta(minutes=4),
    )
    _event(
        project,
        account="bravo",
        name="call_initiated",
        call_id="call-2",
        timestamp=start + timedelta(minutes=2),
    )
    _event(
        project,
        account="bravo",
        name="call_connected",
        call_id="call-2",
        timestamp=start + timedelta(minutes=3),
    )
    _event(
        other,
        account="secret",
        name="call_initiated",
        call_id="call-3",
        timestamp=start,
    )

    payload = analyze_funnel(project, "call_connection", TimeRange.create(start, end)).to_dict()

    assert payload["configuration"] == {
        "start_event": "call_initiated",
        "completion_event": "call_connected",
        "correlation_property": "call_id",
    }
    assert payload["started"] == 3
    assert payload["completed"] == 2
    assert payload["lost"] == 1
    assert payload["completion_rate"] == 66.67
    assert payload["accounts_started"] == 2
    assert payload["accounts_completed"] == 2

    filtered = analyze_funnel(
        project,
        "call_connection",
        TimeRange.create(start, end),
        account_key="acme",
    ).to_dict()
    assert (filtered["started"], filtered["completed"], filtered["lost"]) == (2, 1, 1)

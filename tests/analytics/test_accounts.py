import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from analytics_platform.analytics.accounts import (
    find_cross_product_accounts,
    summarize_account_activity,
)
from analytics_platform.analytics.contracts import AnalyticsInputError, TimeRange
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event

pytestmark = pytest.mark.django_db


def _event(project, *, account, product, actor, name, timestamp):
    return Event.objects.create(
        project=project,
        uuid=uuid4(),
        event=name,
        distinct_id=actor,
        timestamp=timestamp,
        groups={"account": account},
        properties={"product": product},
    )


def test_summarize_account_activity_is_time_account_and_project_scoped(project, workspace):
    other = Project.objects.create(workspace=workspace, key="other", name="Other")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    _event(
        project,
        account="acme",
        product="helpdesk",
        actor="shared:one",
        name="ticket_created",
        timestamp=start,
    )
    _event(
        project,
        account="acme",
        product="helpdesk",
        actor="shared:two",
        name="ticket_created",
        timestamp=start + timedelta(hours=1),
    )
    _event(
        project,
        account="acme",
        product="contact_center",
        actor="shared:one",
        name="call_connected",
        timestamp=start + timedelta(hours=2),
    )
    _event(
        project,
        account="acme",
        product="rise",
        actor="excluded-at-end",
        name="course_completed",
        timestamp=end,
    )
    _event(
        project,
        account="other-account",
        product="bi",
        actor="excluded-account",
        name="report_viewed",
        timestamp=start,
    )
    _event(
        other,
        account="acme",
        product="bi",
        actor="excluded-project",
        name="report_viewed",
        timestamp=start,
    )

    payload = summarize_account_activity(project, "acme", TimeRange.create(start, end)).to_dict()

    assert payload["scope"] == {"project_id": str(project.pk)}
    assert payload["period"] == {
        "start": "2026-09-01T00:00:00Z",
        "end": "2026-09-02T00:00:00Z",
    }
    assert payload["active_products"] == ["contact_center", "helpdesk"]
    assert payload["event_counts"] == {
        "contact_center": {"call_connected": 1},
        "helpdesk": {"ticket_created": 2},
    }
    assert payload["distinct_users_by_product"] == {"contact_center": 1, "helpdesk": 2}
    serialized = json.dumps(payload)
    assert "shared:one" not in serialized
    assert "excluded" not in serialized


def test_cross_product_accounts_are_bounded_but_overlap_uses_full_population(project):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    for account in ("alpha", "bravo", "charlie"):
        for product, actor in (
            ("helpdesk", f"shared:{account}"),
            ("contact_center", f"shared:{account}"),
            ("rise", f"shared:{account}"),
        ):
            _event(
                project,
                account=account,
                product=product,
                actor=actor,
                name=f"{product}_used",
                timestamp=start + timedelta(days=1),
            )
    _event(
        project,
        account="delta",
        product="helpdesk",
        actor="shared:delta",
        name="ticket_created",
        timestamp=start + timedelta(days=1),
    )
    _event(
        project,
        account="old",
        product="contact_center",
        actor="shared:old",
        name="call_connected",
        timestamp=start - timedelta(days=1),
    )

    payload = find_cross_product_accounts(
        project,
        ["helpdesk", "contact_center", "rise"],
        TimeRange.create(start, end),
        limit=2,
    ).to_dict()

    assert payload["accounts"]["returned_count"] == 2
    assert payload["accounts"]["total_count"] == 3
    assert payload["accounts"]["truncated"] is True
    assert [item["account_key"] for item in payload["accounts"]["items"]] == [
        "alpha",
        "bravo",
    ]
    assert payload["aggregate_event_counts"] == {
        "contact_center": 3,
        "helpdesk": 3,
        "rise": 3,
    }
    assert payload["user_overlap"] == {
        "products": ["contact_center", "helpdesk", "rise"],
        "users_by_product": {"contact_center": 3, "helpdesk": 4, "rise": 3},
        "users_in_all_products": 3,
        "pairwise_overlap": {
            "contact_center|helpdesk": 3,
            "contact_center|rise": 3,
            "helpdesk|rise": 3,
        },
    }
    assert "shared:" not in json.dumps(payload)


def test_bi_identity_is_not_compared_with_helpdesk_namespace(project):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    for product in ("helpdesk", "bi"):
        _event(
            project,
            account="acme",
            product=product,
            actor="same-looking-id",
            name=f"{product}_used",
            timestamp=start,
        )

    payload = find_cross_product_accounts(
        project, ["helpdesk", "bi"], TimeRange.create(start, end)
    ).to_dict()

    assert payload["user_overlap"] is None


def test_shared_namespace_overlap_uses_all_product_users_not_only_all_product_accounts(
    project,
):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    for account, products in (
        ("support", ("helpdesk", "contact_center")),
        ("learning", ("helpdesk", "rise")),
    ):
        for product in products:
            _event(
                project,
                account=account,
                product=product,
                actor=f"shared:{account}",
                name=f"{product}_used",
                timestamp=start,
            )

    payload = find_cross_product_accounts(
        project,
        ["helpdesk", "contact_center", "rise"],
        TimeRange.create(start, end),
    ).to_dict()

    assert payload["accounts"]["total_count"] == 0
    assert payload["user_overlap"]["pairwise_overlap"] == {
        "contact_center|helpdesk": 1,
        "contact_center|rise": 0,
        "helpdesk|rise": 1,
    }


@pytest.mark.parametrize("products", [[], ["helpdesk"], ["helpdesk", "helpdesk"]])
def test_cross_product_query_requires_two_unique_products(project, products):
    period = TimeRange.create(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC))
    with pytest.raises(AnalyticsInputError, match="two unique"):
        find_cross_product_accounts(project, products, period)

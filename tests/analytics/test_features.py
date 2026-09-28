import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from analytics_platform.analytics.contracts import TimeRange
from analytics_platform.analytics.features import analyze_product_adoption, count_feature_users
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event

pytestmark = pytest.mark.django_db


def _event(
    project,
    *,
    account,
    actor,
    name,
    timestamp,
    product="contact_center",
    extra=None,
):
    return Event.objects.create(
        project=project,
        uuid=uuid4(),
        event=name,
        distinct_id=actor,
        timestamp=timestamp,
        groups={"account": account},
        properties={"product": product, **(extra or {})},
    )


def _identify(project, *, account, timestamp, status, plan=None):
    changes = {"contact_center_status": status}
    if plan is not None:
        changes["contact_center_plan"] = plan
    return Event.objects.create(
        project=project,
        uuid=uuid4(),
        event="$groupidentify",
        distinct_id=f"system:{account}",
        timestamp=timestamp,
        groups={"account": account},
        properties={
            "$group_type": "account",
            "$group_key": account,
            "$group_set": changes,
        },
    )


def test_feature_users_apply_predicates_preserve_dimensions_and_bound_evidence(project, workspace):
    other = Project.objects.create(workspace=workspace, key="other", name="Other")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    for account in ("alpha", "bravo", "charlie"):
        _event(
            project,
            account=account,
            actor=f"private:{account}",
            name="call_summary_generated",
            timestamp=start,
            extra={"success": True},
        )
    _event(
        project,
        account="alpha",
        actor="must-not-appear",
        name="call_summary_generated",
        timestamp=start,
        extra={"success": False},
    )
    _event(
        project,
        account="alpha",
        actor="lifecycle-user",
        name="call_connected",
        timestamp=start,
    )
    _event(
        other,
        account="alpha",
        actor="other-project",
        name="call_summary_generated",
        timestamp=start,
        extra={"success": True},
    )

    payload = count_feature_users(
        project,
        "contact_center",
        TimeRange.create(start, end),
        feature="ai_summary",
        account_limit=2,
    ).to_dict()

    assert payload["product"] == "contact_center"
    assert payload["features"] == ["ai_summary"]
    assert payload["accounts"]["returned_count"] == 2
    assert payload["accounts"]["total_count"] == 3
    assert payload["accounts"]["truncated"] is True
    assert payload["totals_by_feature"] == {"ai_summary": 3}
    assert payload["accounts"]["items"][0] == {
        "account_key": "alpha",
        "features": {"ai_summary": 1},
    }
    serialized = json.dumps(payload)
    assert "private:" not in serialized
    assert "must-not-appear" not in serialized

    filtered = count_feature_users(
        project,
        "contact_center",
        TimeRange.create(start, end),
        feature="ai_summary",
        account_key="charlie",
    ).to_dict()
    assert filtered["accounts"]["total_count"] == 1


def test_overall_adoption_uses_entitlement_immediately_before_period_end(project):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    for account, status in (
        ("active-user", "active"),
        ("trial-no-use", "trial"),
        ("expired-user", "active"),
    ):
        _identify(
            project,
            account=account,
            timestamp=start - timedelta(days=1),
            status=status,
            plan="pro",
        )
    _identify(
        project,
        account="expired-user",
        timestamp=end - timedelta(days=1),
        status="expired",
    )
    _identify(
        project,
        account="activated-at-end",
        timestamp=end,
        status="active",
        plan="pro",
    )
    for account in ("active-user", "expired-user", "activated-at-end"):
        _event(
            project,
            account=account,
            actor=f"private:{account}",
            name="listen_started",
            timestamp=end - timedelta(days=2),
        )

    payload = analyze_product_adoption(
        project,
        "contact_center",
        TimeRange.create(start, end),
        feature="supervisor_listen",
        account_limit=1,
    ).to_dict()
    listen = payload["features"]["supervisor_listen"]

    assert payload["interpretation"] == {
        "usage_window": "qualifying_events_in_period",
        "entitlement_basis": "period_end",
        "historical_profile_reliability": (
            "conditional_on_complete_correctly_timestamped_groupidentify_history"
        ),
    }
    assert listen["observed_adopting_account_count"] == 3
    assert listen["period_end_entitled_adoption"] == {
        "numerator": 1,
        "denominator": 2,
        "rate": 50.0,
    }
    assert listen["adopting_accounts"]["returned_count"] == 1
    assert listen["adopting_accounts"]["total_count"] == 3
    assert listen["adopting_accounts"]["truncated"] is True


def test_plan_adoption_uses_entitlement_at_event_time_and_historical_denominators(project):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    middle = start + timedelta(days=10)
    end = start + timedelta(days=30)
    _event(
        project,
        account="switcher",
        actor="private:switcher",
        name="call_transfer_completed",
        timestamp=start + timedelta(hours=1),
    )
    _identify(
        project,
        account="switcher",
        timestamp=start + timedelta(days=1),
        status="active",
        plan="basic",
    )
    _event(
        project,
        account="switcher",
        actor="private:switcher",
        name="call_transfer_completed",
        timestamp=start + timedelta(days=2),
    )
    _identify(
        project,
        account="switcher",
        timestamp=middle,
        status="active",
        plan="pro",
    )
    _event(
        project,
        account="switcher",
        actor="private:switcher",
        name="call_transfer_completed",
        timestamp=middle + timedelta(days=1),
    )
    _identify(
        project,
        account="basic-no-use",
        timestamp=start,
        status="trial",
        plan="basic",
    )

    payload = analyze_product_adoption(
        project,
        "contact_center",
        TimeRange.create(start, end),
        feature="call_transfer",
        include_plan_breakdown=True,
    ).to_dict()

    assert payload["interpretation"]["plan_attribution_basis"] == "event_time"
    assert payload["plans"] == {
        "basic": {
            "eligible_account_count": 2,
            "eligible_accounts": {
                "items": ["basic-no-use", "switcher"],
                "returned_count": 2,
                "total_count": 2,
                "truncated": False,
            },
            "features": {
                "call_transfer": {
                    "numerator": 1,
                    "denominator": 2,
                    "rate": 50.0,
                    "adopting_accounts": {
                        "items": ["switcher"],
                        "returned_count": 1,
                        "total_count": 1,
                        "truncated": False,
                    },
                }
            },
        },
        "pro": {
            "eligible_account_count": 1,
            "eligible_accounts": {
                "items": ["switcher"],
                "returned_count": 1,
                "total_count": 1,
                "truncated": False,
            },
            "features": {
                "call_transfer": {
                    "numerator": 1,
                    "denominator": 1,
                    "rate": 100.0,
                    "adopting_accounts": {
                        "items": ["switcher"],
                        "returned_count": 1,
                        "total_count": 1,
                        "truncated": False,
                    },
                }
            },
        },
    }

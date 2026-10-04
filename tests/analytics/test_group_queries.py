from datetime import UTC, datetime
from uuid import uuid4

import pytest

from analytics_platform.analytics.contracts import AnalyticsInputError, TimeRange
from analytics_platform.analytics.group_queries import (
    analyze_group_adoption,
    analyze_group_funnel,
    analyze_group_state,
    analyze_group_transitions,
    compare_group_activity,
    query_group_activity,
)
from analytics_platform.event_catalog.models import EventDefinition, EventPropertyDefinition
from analytics_platform.events.models import Event
from analytics_platform.group_analytics.models import GroupProfile, GroupPropertyDefinition

pytestmark = pytest.mark.django_db


def _period(start, end):
    return TimeRange.create(
        datetime.fromisoformat(start).replace(tzinfo=UTC),
        datetime.fromisoformat(end).replace(tzinfo=UTC),
    )


def _seed_group_data(project):
    for group_key, plan in (("acme", "pro"), ("bravo", "pro"), ("charlie", "trial")):
        GroupProfile.objects.create(
            project=project,
            group_type="account",
            group_key=group_key,
            properties={"plan": plan},
        )
    GroupPropertyDefinition.objects.create(
        project=project,
        group_type="account",
        property_name="plan",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 2, 28, tzinfo=UTC),
    )
    GroupPropertyDefinition.objects.create(
        project=project,
        group_type="account",
        property_name="secret_segment",
        status="hidden",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _identify(project, "acme", "2026-01-01T00:00:00+00:00", {"plan": "trial"})
    _identify(project, "bravo", "2026-01-02T00:00:00+00:00", {"plan": "pro"})
    _identify(project, "charlie", "2026-01-03T00:00:00+00:00", {"plan": "trial"})
    _identify(project, "acme", "2026-01-10T00:00:00+00:00", {"plan": "pro"})

    _activity(project, "acme", "ticket_created", "helpdesk", "2026-01-06T00:00:00+00:00", "h1")
    _activity(project, "acme", "report_opened", "bi", "2026-01-07T00:00:00+00:00", "b1")
    _activity(project, "bravo", "ticket_created", "helpdesk", "2026-01-06T00:00:00+00:00", "h2")
    _activity(project, "charlie", "report_opened", "bi", "2026-01-07T00:00:00+00:00", "b2")
    _activity(project, "acme", "ticket_created", "helpdesk", "2026-02-06T00:00:00+00:00", "h3")
    _activity(project, "acme", "ticket_created", "helpdesk", "2026-02-07T00:00:00+00:00", "h4")
    _activity(project, "acme", "report_opened", "bi", "2026-02-08T00:00:00+00:00", "b3")
    _activity(project, "acme", "ticket_resolved", "helpdesk", "2026-01-08T00:00:00+00:00", "h5")
    _activity(project, "bravo", "ticket_resolved", "helpdesk", "2026-01-05T00:00:00+00:00", "h6")


def _identify(project, group_key, timestamp, changes):
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event="$groupidentify",
        distinct_id="system:groups",
        timestamp=datetime.fromisoformat(timestamp),
        groups={},
        properties={
            "$group_type": "account",
            "$group_key": group_key,
            "$group_set": changes,
        },
    )


def _activity(project, group_key, event_name, product, timestamp, actor):
    EventDefinition.objects.get_or_create(
        project=project,
        product_key=product,
        name=event_name,
    )
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event=event_name,
        distinct_id=f"actor:{actor}",
        timestamp=datetime.fromisoformat(timestamp),
        groups={"account": group_key},
        properties={"product": product},
    )


def _rules():
    return [
        {"label": "helpdesk", "event": "ticket_created", "product": "helpdesk"},
        {"label": "bi", "event": "report_opened", "product": "bi"},
    ]


def _group_filter(plan):
    return [{"property_name": "plan", "operator": "eq", "value": plan}]


def test_group_state_requires_basis_and_bounds_all_matching_groups(project):
    _seed_group_data(project)
    period = _period("2026-01-05T00:00:00", "2026-01-12T00:00:00")

    start_state = analyze_group_state(
        project,
        group_type="account",
        group_key="acme",
        period=period,
        state_basis="period_start",
    )
    assert start_state["groups"]["items"] == [
        {"group_key": "acme", "properties": {"plan": "trial"}}
    ]

    event_state = analyze_group_state(
        project,
        group_type="account",
        group_key="acme",
        event_time=datetime(2026, 1, 7, tzinfo=UTC),
        state_basis="event_time",
    )
    assert event_state["groups"]["items"][0]["properties"] == {"plan": "trial"}

    all_states = analyze_group_state(
        project,
        group_type="account",
        period=period,
        state_basis="period_start",
        group_filters=_group_filter("trial"),
        limit=1,
    )["groups"]
    assert all_states == {
        "items": [{"group_key": "acme", "properties": {"plan": "trial"}}],
        "returned_count": 1,
        "total_count": 2,
        "truncated": True,
    }


def test_group_activity_uses_multiple_labeled_rules_and_explicit_match_mode(project):
    _seed_group_data(project)
    period = _period("2026-01-05T00:00:00", "2026-01-12T00:00:00")

    all_match = query_group_activity(
        project,
        group_type="account",
        period=period,
        state_basis="period_end",
        activity_rules=_rules(),
        match="all",
        include_distinct_id_overlap=True,
    ).to_dict()
    any_match = query_group_activity(
        project,
        group_type="account",
        period=period,
        state_basis="period_end",
        activity_rules=_rules(),
        match="any",
    ).to_dict()

    assert [row["group_key"] for row in all_match["groups"]["items"]] == ["acme"]
    assert all_match["groups"]["items"][0]["activity"]["helpdesk"]["events"] == 1
    assert [row["group_key"] for row in any_match["groups"]["items"]] == [
        "acme",
        "bravo",
        "charlie",
    ]
    assert all_match["distinct_id_overlap"]["count"] == 0
    assert all_match["distinct_id_overlap"]["basis"] == "exact distinct_id string equality"
    assert all_match["distinct_id_overlap"]["identity_resolution_performed"] is False
    assert (
        "does not establish that the underlying people are different"
        in all_match["distinct_id_overlap"]["zero_result_interpretation"]
    )

    event_time = query_group_activity(
        project,
        group_type="account",
        period=_period("2026-02-01T00:00:00", "2026-03-01T00:00:00"),
        state_basis="event_time",
        activity_rules=_rules(),
        match="all",
        group_filters=_group_filter("pro"),
    ).to_dict()
    assert [row["group_key"] for row in event_time["groups"]["items"]] == ["acme"]


def test_group_adoption_requires_an_explicit_eligibility_denominator(project):
    _seed_group_data(project)
    period = _period("2026-01-05T00:00:00", "2026-01-12T00:00:00")
    rule = {"label": "ticket_created", "event": "ticket_created", "product": "helpdesk"}

    result = analyze_group_adoption(
        project,
        group_type="account",
        period=period,
        state_basis="period_end",
        eligibility_filters=_group_filter("pro"),
        activity_rule=rule,
    )
    assert result["denominator"] == 2
    assert result["numerator"] == 2
    assert result["adoption_rate"] == 100.0
    assert result["state_basis"] == "period_end"

    with pytest.raises(AnalyticsInputError, match="eligibility filter"):
        analyze_group_adoption(
            project,
            group_type="account",
            period=period,
            state_basis="period_end",
            eligibility_filters=[],
            activity_rule=rule,
        )


def test_group_adoption_event_time_eligibility_and_depth(project):
    _seed_group_data(project)
    result = analyze_group_adoption(
        project,
        group_type="account",
        period=_period("2026-01-05T00:00:00", "2026-01-12T00:00:00"),
        state_basis="event_time",
        eligibility_filters=_group_filter("pro"),
        activity_rule={
            "label": "tickets",
            "event": "ticket_created",
            "product": "helpdesk",
        },
    )

    assert result["eligibility_basis"] == "eligible_at_any_point_during_period"
    assert result["numerator"] == 1
    assert result["denominator"] == 2
    assert result["event_depth"] == {
        "total_matching_events": 1,
        "median_events_per_adopting_group": 1,
        "groups": {
            "items": [{"group_key": "bravo", "event_count": 1}],
            "returned_count": 1,
            "total_count": 1,
            "truncated": False,
        },
    }


def test_group_activity_comparison_reuses_shared_comparison_shape(project):
    _seed_group_data(project)

    result = compare_group_activity(
        project,
        group_type="account",
        period=_period("2026-02-01T00:00:00", "2026-03-01T00:00:00"),
        comparison_period=_period("2026-01-04T00:00:00", "2026-02-01T00:00:00"),
        state_basis="current",
        activity_rules=_rules(),
        match="all",
    )

    assert result["comparison"]["changes"]["items"] == [
        {
            "group_key": "acme",
            "current": {
                "helpdesk.events": 2,
                "helpdesk.distinct_ids": 2,
                "bi.events": 1,
                "bi.distinct_ids": 1,
            },
            "baseline": {
                "helpdesk.events": 1,
                "helpdesk.distinct_ids": 1,
                "bi.events": 1,
                "bi.distinct_ids": 1,
            },
            "delta": {
                "helpdesk.events": 1,
                "helpdesk.distinct_ids": 1,
                "bi.events": 0,
                "bi.distinct_ids": 0,
            },
            "percent_change": {
                "helpdesk.events": 100.0,
                "helpdesk.distinct_ids": 100.0,
                "bi.events": 0.0,
                "bi.distinct_ids": 0.0,
            },
        }
    ]


def test_generic_funnel_and_group_property_transitions(project):
    _seed_group_data(project)
    period = _period("2026-01-05T00:00:00", "2026-01-12T00:00:00")

    funnel = analyze_group_funnel(
        project,
        group_type="account",
        period=period,
        state_basis="event_time",
        steps=[
            {"label": "started", "event": "ticket_created", "product": "helpdesk"},
            {"label": "resolved", "event": "ticket_resolved", "product": "helpdesk"},
        ],
    )
    assert funnel["steps"][0]["cohort_count"] == 2
    assert funnel["steps"][1]["cohort_count"] == 1
    assert funnel["correlation"] == {"kind": "group_key", "group_type": "account"}

    transitions = analyze_group_transitions(
        project,
        group_type="account",
        property_name="plan",
        period=period,
        state_basis="event_time",
    )
    assert transitions["state_basis"] == "event_time"
    assert transitions["transitions"]["items"] == [
        {
            "group_key": "acme",
            "timestamp": "2026-01-10T00:00:00Z",
            "previous_value": "trial",
            "new_value": "pro",
        }
    ]


def test_generic_funnel_correlates_overlapping_calls_by_event_property(project):
    period = _period("2026-01-01T00:00:00", "2026-01-02T00:00:00")
    definitions = {}
    for event_name in ("call_initiated", "call_connected"):
        definition = EventDefinition.objects.create(
            project=project,
            product_key="contact_center",
            name=event_name,
        )
        EventPropertyDefinition.objects.create(
            event_definition=definition,
            property_name="call_id",
            observed_non_null_types=["string"],
            first_seen_at=period.start,
            last_seen_at=period.start,
        )
        definitions[event_name] = definition

    for event_name, call_id, minute in (
        ("call_initiated", "call-a", 0),
        ("call_initiated", "call-b", 1),
        ("call_connected", "call-b", 2),
        ("call_connected", "call-a", 3),
    ):
        Event.objects.create(
            project=project,
            uuid=uuid4(),
            event=event_name,
            distinct_id="actor:dispatcher",
            timestamp=datetime(2026, 1, 1, 12, minute, tzinfo=UTC),
            groups={"account": "acme"},
            properties={"product": "contact_center", "call_id": call_id},
        )

    funnel = analyze_group_funnel(
        project,
        group_type="account",
        period=period,
        state_basis="event_time",
        correlation={"kind": "property", "property_name": "call_id"},
        steps=[
            {"label": "initiated", "event": "call_initiated", "product": "contact_center"},
            {"label": "connected", "event": "call_connected", "product": "contact_center"},
        ],
    )

    assert funnel["started_cohort_count"] == 2
    assert funnel["completed_cohort_count"] == 2
    assert funnel["steps"][1]["cohort_count"] == 2
    assert set(funnel["steps"][0]) == {
        "label",
        "event",
        "product",
        "cohort_count",
        "conversion_rate_from_start",
    }


def test_generic_funnel_correlates_numeric_property_values_without_state_basis(project):
    period = _period("2026-01-01T00:00:00", "2026-01-02T00:00:00")
    for event_name in ("call_initiated", "call_connected"):
        definition = EventDefinition.objects.create(
            project=project,
            product_key="contact_center",
            name=event_name,
        )
        EventPropertyDefinition.objects.create(
            event_definition=definition,
            property_name="call_id",
            observed_non_null_types=["number"],
            first_seen_at=period.start,
            last_seen_at=period.start,
        )

    for event_name, call_id, minute in (
        ("call_initiated", 101, 0),
        ("call_initiated", 102, 1),
        ("call_connected", 102, 2),
    ):
        Event.objects.create(
            project=project,
            uuid=uuid4(),
            event=event_name,
            distinct_id="actor:dispatcher",
            timestamp=datetime(2026, 1, 1, 12, minute, tzinfo=UTC),
            groups={"account": "acme"},
            properties={"product": "contact_center", "call_id": call_id},
        )

    funnel = analyze_group_funnel(
        project,
        group_type=None,
        period=period,
        correlation={"kind": "property", "property_name": "call_id"},
        steps=[
            {"label": "initiated", "event": "call_initiated", "product": "contact_center"},
            {"label": "connected", "event": "call_connected", "product": "contact_center"},
        ],
    )

    assert funnel["state_basis"] is None
    assert funnel["started_cohort_count"] == 2
    assert funnel["completed_cohort_count"] == 1
    assert set(funnel["steps"][0]) == {
        "label",
        "event",
        "product",
        "cohort_count",
        "conversion_rate_from_start",
    }


def test_generic_funnel_correlates_by_distinct_id_without_a_group_type(project):
    period = _period("2026-01-01T00:00:00", "2026-01-02T00:00:00")
    for event_name in ("started", "completed"):
        EventDefinition.objects.create(project=project, product_key="app", name=event_name)
    for event_name, actor, minute in (
        ("started", "actor:1", 0),
        ("started", "actor:2", 1),
        ("completed", "actor:2", 2),
    ):
        Event.objects.create(
            project=project,
            uuid=uuid4(),
            event=event_name,
            distinct_id=actor,
            timestamp=datetime(2026, 1, 1, 12, minute, tzinfo=UTC),
            groups={},
            properties={"product": "app"},
        )

    funnel = analyze_group_funnel(
        project,
        group_type=None,
        period=period,
        state_basis="current",
        correlation={"kind": "distinct_id"},
        steps=[
            {"label": "started", "event": "started", "product": "app"},
            {"label": "completed", "event": "completed", "product": "app"},
        ],
    )

    assert funnel["started_cohort_count"] == 2
    assert funnel["completed_cohort_count"] == 1


def test_generic_funnel_requires_a_distinct_occurrence_for_each_step(project):
    period = _period("2026-01-01T00:00:00", "2026-01-02T00:00:00")
    EventDefinition.objects.create(project=project, product_key="app", name="button_clicked")
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event="button_clicked",
        distinct_id="actor:1",
        timestamp=datetime(2026, 1, 1, 12, tzinfo=UTC),
        groups={},
        properties={"product": "app"},
    )

    funnel = analyze_group_funnel(
        project,
        group_type=None,
        period=period,
        correlation={"kind": "distinct_id"},
        steps=[
            {"label": "first_click", "event": "button_clicked", "product": "app"},
            {"label": "second_click", "event": "button_clicked", "product": "app"},
        ],
    )

    assert funnel["started_cohort_count"] == 1
    assert funnel["steps"][1]["cohort_count"] == 0
    assert funnel["completed_cohort_count"] == 0


def test_group_state_rejects_explicitly_hidden_properties(project):
    _seed_group_data(project)

    with pytest.raises(AnalyticsInputError, match="hidden"):
        analyze_group_state(
            project,
            group_type="account",
            period=_period("2026-01-05T00:00:00", "2026-01-12T00:00:00"),
            state_basis="period_end",
            group_filters=[{"property_name": "secret_segment", "operator": "eq", "value": "vip"}],
        )


def test_group_state_limits_in_filter_values(project):
    _seed_group_data(project)
    with pytest.raises(AnalyticsInputError, match="limited to 100 values"):
        analyze_group_state(
            project,
            group_type="account",
            state_basis="current",
            group_filters=[
                {
                    "property_name": "plan",
                    "operator": "in",
                    "value": [f"plan-{index}" for index in range(101)],
                }
            ],
        )


def test_group_property_equality_rejects_an_unobserved_json_type(project):
    _seed_group_data(project)

    with pytest.raises(AnalyticsInputError, match="incompatible with observed property types"):
        analyze_group_state(
            project,
            group_type="account",
            state_basis="current",
            group_filters=[{"property_name": "plan", "operator": "eq", "value": 1}],
        )


def test_historical_group_state_rejects_unbounded_profile_history(project, monkeypatch):
    from analytics_platform.analytics import group_queries

    _identify(project, "acme", "2026-01-01T00:00:00+00:00", {"plan": "trial"})
    _identify(project, "acme", "2026-01-02T00:00:00+00:00", {"plan": "pro"})
    monkeypatch.setattr(group_queries, "MAX_GROUP_HISTORY_ROWS", 1)

    with pytest.raises(AnalyticsInputError, match="limited to 1 profile events"):
        analyze_group_state(
            project,
            group_type="account",
            period=_period("2026-01-03T00:00:00", "2026-01-04T00:00:00"),
            state_basis="period_start",
        )


def test_period_start_state_includes_profile_changes_at_the_boundary(project):
    _identify(project, "acme", "2026-01-03T00:00:00+00:00", {"plan": "pro"})

    result = analyze_group_state(
        project,
        group_type="account",
        group_key="acme",
        period=_period("2026-01-03T00:00:00", "2026-01-04T00:00:00"),
        state_basis="period_start",
    )

    assert result["found"] is True
    assert result["groups"]["items"] == [{"group_key": "acme", "properties": {}}]


def test_transitions_include_a_change_exactly_at_the_period_start(project):
    GroupPropertyDefinition.objects.create(
        project=project,
        group_type="account",
        property_name="plan",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    _identify(project, "acme", "2026-01-01T00:00:00+00:00", {"plan": "trial"})
    _identify(project, "acme", "2026-01-03T00:00:00+00:00", {"plan": "pro"})

    result = analyze_group_transitions(
        project,
        group_type="account",
        property_name="plan",
        period=_period("2026-01-03T00:00:00", "2026-01-04T00:00:00"),
        state_basis="event_time",
    )

    assert result["transitions"]["items"] == [
        {
            "group_key": "acme",
            "timestamp": "2026-01-03T00:00:00Z",
            "previous_value": "trial",
            "new_value": "pro",
        }
    ]


def test_requested_current_group_does_not_materialize_every_profile(project, monkeypatch):
    from analytics_platform.analytics import group_queries

    for group_key in ("acme", "bravo"):
        GroupProfile.objects.create(
            project=project,
            group_type="account",
            group_key=group_key,
            properties={},
        )
    monkeypatch.setattr(group_queries, "MAX_GROUP_STATE_GROUPS", 1)

    result = analyze_group_state(
        project,
        group_type="account",
        group_key="acme",
        state_basis="current",
    )

    assert result["found"] is True
    assert result["groups"]["items"] == [{"group_key": "acme", "properties": {}}]


def test_requested_group_transitions_ignore_unrelated_history_for_the_cap(project, monkeypatch):
    from analytics_platform.analytics import group_queries

    GroupPropertyDefinition.objects.create(
        project=project,
        group_type="account",
        property_name="plan",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    _identify(project, "acme", "2026-01-01T00:00:00+00:00", {"plan": "trial"})
    _identify(project, "acme", "2026-01-03T00:00:00+00:00", {"plan": "pro"})
    _identify(project, "bravo", "2026-01-03T01:00:00+00:00", {"plan": "pro"})
    _identify(project, "charlie", "2026-01-03T02:00:00+00:00", {"plan": "trial"})
    monkeypatch.setattr(group_queries, "MAX_GROUP_HISTORY_ROWS", 1)

    result = analyze_group_transitions(
        project,
        group_type="account",
        group_key="acme",
        property_name="plan",
        period=_period("2026-01-02T00:00:00", "2026-01-04T00:00:00"),
        state_basis="event_time",
    )

    assert result["transitions"]["total_count"] == 1

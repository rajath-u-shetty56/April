from datetime import UTC, datetime
from uuid import uuid4

import pytest

from analytics_platform.analytics.contracts import AnalyticsInputError, TimeRange
from analytics_platform.analytics.query import query_events
from analytics_platform.event_catalog.models import (
    EventDefinition,
    EventDefinitionStatus,
    EventPropertyDefinition,
)
from analytics_platform.events.models import Event

pytestmark = pytest.mark.django_db


def _period(start, end):
    return TimeRange.create(
        datetime.fromisoformat(start).replace(tzinfo=UTC),
        datetime.fromisoformat(end).replace(tzinfo=UTC),
    )


def _seed(project):
    definition = EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="ticket_created",
    )
    EventPropertyDefinition.objects.create(
        event_definition=definition,
        property_name="priority",
        observed_non_null_types=["number"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 2, 3, tzinfo=UTC),
    )
    EventPropertyDefinition.objects.create(
        event_definition=definition,
        property_name="legacy",
        status="hidden",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    data = [
        ("2026-01-02T00:00:00+00:00", "actor:1", 1, {"account": "acme"}),
        ("2026-01-03T00:00:00+00:00", "actor:2", 2, {"account": "acme"}),
        ("2026-02-03T00:00:00+00:00", "actor:1", 4, {"account": "bravo"}),
    ]
    Event.objects.bulk_create(
        Event(
            project=project,
            uuid=uuid4(),
            event="ticket_created",
            distinct_id=distinct_id,
            timestamp=datetime.fromisoformat(timestamp),
            groups=groups,
            properties={"product": "helpdesk", "priority": priority},
        )
        for timestamp, distinct_id, priority, groups in data
    )
    return definition


def _seed_priority_coverage(project):
    definitions = []
    for product in ("helpdesk", "bi"):
        definitions.append(
            EventDefinition.objects.create(
                project=project,
                product_key=product,
                name="ticket_created",
            )
        )
    for definition in definitions:
        EventPropertyDefinition.objects.create(
            event_definition=definition,
            property_name="priority",
            observed_non_null_types=["string"],
            nullable=True,
            first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    rows = []
    for index in range(253):
        properties = {"product": "helpdesk"}
        if index == 0:
            properties["priority"] = "high"
        elif index == 1:
            properties["priority"] = None
        elif index == 2:
            properties["priority"] = "low"
        rows.append(
            Event(
                project=project,
                uuid=uuid4(),
                event="ticket_created",
                distinct_id=f"actor:helpdesk:{index}",
                timestamp=datetime(2026, 1, 15, tzinfo=UTC),
                groups={},
                properties=properties,
            )
        )
    rows.extend(
        [
            Event(
                project=project,
                uuid=uuid4(),
                event="ticket_created",
                distinct_id="actor:bi:1",
                timestamp=datetime(2026, 1, 15, tzinfo=UTC),
                groups={},
                properties={"product": "bi", "priority": "low"},
            ),
            Event(
                project=project,
                uuid=uuid4(),
                event="ticket_created",
                distinct_id="actor:bi:2",
                timestamp=datetime(2026, 1, 15, tzinfo=UTC),
                groups={},
                properties={"product": "bi"},
            ),
        ]
    )
    Event.objects.bulk_create(rows)


def test_query_events_filters_and_aggregates_using_catalog_scoped_properties(project):
    _seed(project)

    result = query_events(
        project,
        period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
        filters=[
            {"field": "event", "operator": "eq", "value": "ticket_created"},
            {
                "field": "property",
                "event": "ticket_created",
                "product": "helpdesk",
                "property_name": "priority",
                "operator": "gte",
                "value": 1,
            },
        ],
        group_by=[{"kind": "group_key", "label": "account", "group_type": "account"}],
        aggregations=[
            {"kind": "event_count", "label": "events"},
            {"kind": "distinct_id_count", "label": "exact_distinct_ids"},
            {
                "kind": "sum_property",
                "label": "priority_sum",
                "event": "ticket_created",
                "product": "helpdesk",
                "property_name": "priority",
            },
        ],
        limit=10,
    )

    assert result.to_dict()["rows"] == {
        "items": [{"account": "acme", "events": 2, "exact_distinct_ids": 2, "priority_sum": 3.0}],
        "returned_count": 1,
        "total_count": 1,
        "truncated": False,
    }
    assert result.to_dict()["identifier_semantics"] == "exact_distinct_id_equality"


def test_query_events_rejects_hidden_or_undefined_explicit_property(project):
    _seed(project)
    common = {
        "period": _period("2026-01-01T00:00:00", "2026-03-01T00:00:00"),
        "aggregations": [{"kind": "event_count", "label": "events"}],
    }

    with pytest.raises(AnalyticsInputError, match="hidden"):
        query_events(
            project,
            **common,
            filters=[],
            group_by=[
                {
                    "kind": "property",
                    "label": "legacy",
                    "event": "ticket_created",
                    "product": "helpdesk",
                    "property_name": "legacy",
                }
            ],
        )

    with pytest.raises(AnalyticsInputError, match="not discovered"):
        query_events(
            project,
            **common,
            group_by=[],
            filters=[
                {
                    "field": "property",
                    "event": "ticket_created",
                    "product": "helpdesk",
                    "property_name": "unknown",
                    "operator": "eq",
                    "value": "x",
                }
            ],
        )


def test_query_events_rejects_equality_value_with_an_unobserved_json_type(project):
    _seed(project)

    with pytest.raises(AnalyticsInputError, match="incompatible with observed property types"):
        query_events(
            project,
            period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
            filters=[
                {
                    "field": "property",
                    "event": "ticket_created",
                    "product": "helpdesk",
                    "property_name": "priority",
                    "operator": "eq",
                    "value": "1",
                }
            ],
            group_by=[],
            aggregations=[{"kind": "event_count", "label": "events"}],
        )


def test_query_events_comparison_reuses_the_same_aggregation_spec(project):
    _seed(project)

    result = query_events(
        project,
        period=_period("2026-02-01T00:00:00", "2026-03-04T00:00:00"),
        filters=[{"field": "event", "operator": "eq", "value": "ticket_created"}],
        group_by=[],
        aggregations=[
            {"kind": "event_count", "label": "events"},
            {"kind": "distinct_id_count", "label": "actors"},
        ],
        comparison_period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
    )

    comparison = result.to_dict()["comparison"]
    assert comparison["current"]["items"][0] == {"events": 1, "actors": 1}
    assert comparison["baseline"]["items"][0] == {"events": 2, "actors": 2}
    assert comparison["changes"]["items"] == [
        {
            "current": {"events": 1, "actors": 1},
            "baseline": {"events": 2, "actors": 2},
            "delta": {"events": -1, "actors": -1},
            "percent_change": {"events": -50.0, "actors": -50.0},
        }
    ]
    assert comparison["changes"]["returned_count"] == 1
    assert comparison["changes"]["total_count"] == 1
    assert comparison["changes"]["truncated"] is False


def test_query_events_reports_property_coverage_for_both_comparison_periods(project):
    _seed(project)

    result = query_events(
        project,
        period=_period("2026-02-01T00:00:00", "2026-03-04T00:00:00"),
        filters=[
            {
                "field": "property",
                "event": "ticket_created",
                "product": "helpdesk",
                "property_name": "priority",
                "operator": "gte",
                "value": 1,
            }
        ],
        group_by=[],
        aggregations=[{"kind": "event_count", "label": "events"}],
        comparison_period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
    ).to_dict()

    assert result["property_coverage"]["current"]["items"][0]["base_event_count"] == 1
    assert result["property_coverage"]["baseline"]["items"][0]["base_event_count"] == 2


def test_query_events_rejects_unknown_filter_fields_instead_of_treating_them_as_sql(project):
    _seed(project)

    with pytest.raises(AnalyticsInputError, match="unsupported filter field"):
        query_events(
            project,
            period=_period("2026-01-01T00:00:00", "2026-03-01T00:00:00"),
            filters=[{"field": "sql", "operator": "eq", "value": "DROP TABLE events"}],
            group_by=[],
            aggregations=[{"kind": "event_count", "label": "events"}],
        )


def test_property_filter_reports_population_coverage_across_matching_definitions(project):
    _seed_priority_coverage(project)

    result = query_events(
        project,
        period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
        filters=[
            {"field": "event", "operator": "eq", "value": "ticket_created"},
            {
                "field": "property",
                "event": "ticket_created",
                "property_name": "priority",
                "operator": "eq",
                "value": "high",
            },
        ],
        group_by=[],
        aggregations=[{"kind": "event_count", "label": "matched_events"}],
    ).to_dict()

    assert result["rows"]["items"] == [{"matched_events": 1}]
    assert result["property_coverage"]["current"]["items"] == [
        {
            "event": "ticket_created",
            "product": "bi",
            "property_name": "priority",
            "base_event_count": 2,
            "property_present_count": 1,
            "explicit_null_count": 0,
            "missing_property_count": 1,
            "matched_count": 0,
        },
        {
            "event": "ticket_created",
            "product": "helpdesk",
            "property_name": "priority",
            "base_event_count": 253,
            "property_present_count": 3,
            "explicit_null_count": 1,
            "missing_property_count": 250,
            "matched_count": 1,
        },
    ]
    assert result["property_coverage"]["baseline"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("event", "ticket_cretaed"), ("product", "helpdesk_typo")],
)
def test_query_events_rejects_unknown_event_and_product_selectors(project, field, value):
    _seed(project)

    with pytest.raises(AnalyticsInputError, match="visible or verified event catalog"):
        query_events(
            project,
            period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
            filters=[{"field": field, "operator": "eq", "value": value}],
            group_by=[],
            aggregations=[{"kind": "event_count", "label": "events"}],
        )


@pytest.mark.parametrize(
    ("field", "value", "event", "product"),
    [
        ("event", "hidden_event", "hidden_event", "helpdesk"),
        ("product", "hidden_product", "hidden_event", "hidden_product"),
    ],
)
def test_query_events_cannot_select_hidden_event_definitions(project, field, value, event, product):
    _seed(project)
    EventDefinition.objects.create(
        project=project,
        product_key=product,
        name=event,
        status=EventDefinitionStatus.HIDDEN,
    )

    with pytest.raises(AnalyticsInputError, match="visible or verified event catalog"):
        query_events(
            project,
            period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
            filters=[{"field": field, "operator": "eq", "value": value}],
            group_by=[],
            aggregations=[{"kind": "event_count", "label": "events"}],
        )


def test_property_dimension_distinguishes_json_null_from_missing(project):
    _seed_priority_coverage(project)
    result = query_events(
        project,
        period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
        filters=[
            {"field": "product", "operator": "eq", "value": "helpdesk"},
            {"field": "event", "operator": "eq", "value": "ticket_created"},
        ],
        group_by=[
            {
                "kind": "property",
                "label": "priority_state",
                "event": "ticket_created",
                "product": "helpdesk",
                "property_name": "priority",
            }
        ],
        aggregations=[{"kind": "event_count", "label": "events"}],
    ).to_dict()

    assert result["rows"]["items"] == [
        {"priority_state": {"kind": "missing"}, "events": 250},
        {"priority_state": {"kind": "null"}, "events": 1},
        {"priority_state": {"kind": "value", "value": "high"}, "events": 1},
        {"priority_state": {"kind": "value", "value": "low"}, "events": 1},
    ]


def test_query_events_rejects_in_filter_over_100_values(project):
    _seed(project)
    with pytest.raises(AnalyticsInputError, match="limited to 100 values"):
        query_events(
            project,
            period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
            filters=[
                {"field": "event", "operator": "in", "value": [f"event-{n}" for n in range(101)]}
            ],
            group_by=[],
            aggregations=[{"kind": "event_count", "label": "events"}],
        )


def test_query_events_rejects_comparison_period_with_different_duration(project):
    _seed(project)
    with pytest.raises(AnalyticsInputError, match="equal duration"):
        query_events(
            project,
            period=_period("2026-02-01T00:00:00", "2026-03-01T00:00:00"),
            comparison_period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
            filters=[],
            group_by=[],
            aggregations=[{"kind": "event_count", "label": "events"}],
        )


def test_property_aggregations_are_scoped_to_their_event_definitions(project):
    for event_name, product, amount in (
        ("ticket_created", "helpdesk", 10),
        ("report_created", "bi", 100),
    ):
        definition = EventDefinition.objects.create(
            project=project,
            product_key=product,
            name=event_name,
        )
        EventPropertyDefinition.objects.create(
            event_definition=definition,
            property_name="amount",
            observed_non_null_types=["number"],
            first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        Event.objects.create(
            project=project,
            uuid=uuid4(),
            event=event_name,
            distinct_id=f"actor:{product}",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            groups={},
            properties={"product": product, "amount": amount},
        )

    result = query_events(
        project,
        period=_period("2026-01-01T00:00:00", "2026-01-02T00:00:00"),
        aggregations=[
            {
                "kind": "sum_property",
                "label": "helpdesk_amount",
                "event": "ticket_created",
                "product": "helpdesk",
                "property_name": "amount",
            },
            {
                "kind": "sum_property",
                "label": "bi_amount",
                "event": "report_created",
                "product": "bi",
                "property_name": "amount",
            },
        ],
    ).to_dict()

    assert result["rows"]["items"] == [{"helpdesk_amount": 10.0, "bi_amount": 100.0}]


def test_comparison_keeps_missing_non_additive_aggregations_null(project):
    definition = EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="ticket_created",
    )
    EventPropertyDefinition.objects.create(
        event_definition=definition,
        property_name="resolution_minutes",
        observed_non_null_types=["number"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    for timestamp, account, value in (
        (datetime(2026, 1, 2, tzinfo=UTC), "acme", 20),
        (datetime(2026, 2, 2, tzinfo=UTC), "bravo", 10),
    ):
        Event.objects.create(
            project=project,
            uuid=uuid4(),
            event="ticket_created",
            distinct_id=f"actor:{account}",
            timestamp=timestamp,
            groups={"account": account},
            properties={"product": "helpdesk", "resolution_minutes": value},
        )

    result = query_events(
        project,
        period=_period("2026-02-01T00:00:00", "2026-03-04T00:00:00"),
        comparison_period=_period("2026-01-01T00:00:00", "2026-02-01T00:00:00"),
        group_by=[{"kind": "group_key", "label": "account", "group_type": "account"}],
        aggregations=[
            {"kind": "event_count", "label": "events"},
            {
                "kind": "avg_property",
                "label": "average_resolution",
                "event": "ticket_created",
                "product": "helpdesk",
                "property_name": "resolution_minutes",
            },
        ],
    ).to_dict()

    changes = {row["account"]: row for row in result["comparison"]["changes"]["items"]}
    assert changes["acme"]["current"] == {"events": 0, "average_resolution": None}
    assert changes["acme"]["delta"] == {"events": -1, "average_resolution": None}
    assert changes["bravo"]["baseline"] == {"events": 0, "average_resolution": None}


def test_property_dimensions_reject_nested_json_values(project):
    definition = EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="ticket_created",
    )
    EventPropertyDefinition.objects.create(
        event_definition=definition,
        property_name="metadata",
        observed_non_null_types=["object"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(AnalyticsInputError, match="scalar"):
        query_events(
            project,
            period=_period("2026-01-01T00:00:00", "2026-01-02T00:00:00"),
            group_by=[
                {
                    "kind": "property",
                    "label": "metadata",
                    "event": "ticket_created",
                    "product": "helpdesk",
                    "property_name": "metadata",
                }
            ],
            aggregations=[{"kind": "event_count", "label": "events"}],
        )


def test_unfiltered_event_queries_exclude_reserved_profile_events(project):
    EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="ticket_created",
    )
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event="ticket_created",
        distinct_id="actor:1",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        groups={"account": "acme"},
        properties={"product": "helpdesk"},
    )
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event="$groupidentify",
        distinct_id="system:groups",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        groups={},
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"plan": "pro"},
        },
    )

    result = query_events(
        project,
        period=_period("2026-01-01T00:00:00", "2026-01-02T00:00:00"),
        aggregations=[{"kind": "event_count", "label": "events"}],
    ).to_dict()

    assert result["rows"]["items"] == [{"events": 1}]

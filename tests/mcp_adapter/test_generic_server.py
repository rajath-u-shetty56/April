from datetime import UTC, datetime
from uuid import uuid4

import pytest
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection, connections, transaction
from mcp.client import Client
from pydantic import ValidationError

from analytics_platform.event_catalog.models import EventDefinition, EventPropertyDefinition
from analytics_platform.events.models import Event
from analytics_platform.group_analytics.models import GroupProfile, GroupPropertyDefinition
from analytics_platform.mcp_adapter.schemas import (
    ActivityPropertyFilterInput,
    GroupFunnelOutput,
)
from analytics_platform.mcp_adapter.server import create_server

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed(project):
    definition = EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="ticket_created",
    )
    EventPropertyDefinition.objects.create(
        event_definition=definition,
        property_name="priority",
        status="verified",
        observed_non_null_types=["number"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 2, 10, tzinfo=UTC),
    )
    EventPropertyDefinition.objects.create(
        event_definition=definition,
        property_name="secret_token",
        status="hidden",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    GroupPropertyDefinition.objects.create(
        project=project,
        group_type="account",
        property_name="plan",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 2, 10, tzinfo=UTC),
    )
    GroupPropertyDefinition.objects.create(
        project=project,
        group_type="account",
        property_name="internal_note",
        status="hidden",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    GroupProfile.objects.create(
        project=project,
        group_type="account",
        group_key="acme",
        properties={"plan": "pro", "internal_note": "hidden"},
    )
    _event(
        project,
        "$groupidentify",
        "2026-01-02T00:00:00+00:00",
        {},
        {
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"plan": "trial"},
        },
    )
    _event(
        project,
        "$groupidentify",
        "2026-01-06T00:00:00+00:00",
        {},
        {
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"plan": "pro"},
        },
    )
    _event(
        project,
        "ticket_created",
        "2026-01-03T00:00:00+00:00",
        {"account": "acme"},
        {
            "product": "helpdesk",
            "priority": 1,
        },
        distinct_id="actor:jan1",
    )
    _event(
        project,
        "report_opened",
        "2026-01-04T00:00:00+00:00",
        {"account": "acme"},
        {
            "product": "bi",
        },
        distinct_id="actor:jan2",
    )
    _event(
        project,
        "ticket_resolved",
        "2026-01-05T00:00:00+00:00",
        {"account": "acme"},
        {
            "product": "helpdesk",
        },
        distinct_id="actor:jan3",
    )
    _event(
        project,
        "ticket_created",
        "2026-02-03T00:00:00+00:00",
        {"account": "acme"},
        {
            "product": "helpdesk",
            "priority": 3,
        },
        distinct_id="actor:feb1",
    )
    _event(
        project,
        "ticket_created",
        "2026-02-04T00:00:00+00:00",
        {"account": "acme"},
        {
            "product": "helpdesk",
            "priority": 4,
        },
        distinct_id="actor:feb2",
    )
    _event(
        project,
        "report_opened",
        "2026-02-05T00:00:00+00:00",
        {"account": "acme"},
        {
            "product": "bi",
        },
        distinct_id="actor:feb3",
    )


def _event(project, event, timestamp, groups, properties, *, distinct_id="system:groups"):
    if event != "$groupidentify":
        EventDefinition.objects.get_or_create(
            project=project,
            product_key=properties.get("product", ""),
            name=event,
        )
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event=event,
        distinct_id=distinct_id,
        timestamp=datetime.fromisoformat(timestamp),
        groups=groups,
        properties=properties,
    )


@pytest.mark.anyio
async def test_mcp_exposes_only_generic_structured_read_only_tools():
    async with Client(create_server(uuid4())) as client:
        result = await client.list_tools()

    expected = {
        "discover_analytics_catalog",
        "query_events",
        "analyze_group_state",
        "query_group_activity",
        "analyze_group_adoption",
        "analyze_group_funnel",
        "analyze_group_transitions",
        "compare_group_activity",
    }
    assert {tool.name for tool in result.tools} == expected
    tools = {tool.name: tool for tool in result.tools}
    for tool in result.tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
        assert tool.output_schema is not None
        schema_text = str(tool.input_schema).lower()
        assert "project_id" not in schema_text
        assert "sql" not in schema_text
        assert "happyfox" not in tool.description.lower()
    group_activity_schema = str(tools["query_group_activity"].input_schema)
    assert "activity_rules" in group_activity_schema
    assert "all" in group_activity_schema and "any" in group_activity_schema
    assert "state_basis" in group_activity_schema
    state_schema = tools["analyze_group_state"].input_schema
    assert "group_key" not in state_schema.get("required", [])
    adoption_schema = tools["analyze_group_adoption"].input_schema
    assert "eligibility_filters" in adoption_schema.get("required", [])
    funnel_schema = tools["analyze_group_funnel"].input_schema
    assert "state_basis" not in funnel_schema.get("required", [])
    assert "limit" not in funnel_schema.get("properties", {})


def test_activity_property_filter_inherits_event_and_product_from_its_rule():
    value = ActivityPropertyFilterInput(
        property_name="priority",
        operator="eq",
        value="high",
    )
    assert value.model_dump(exclude_none=True) == {
        "property_name": "priority",
        "operator": "eq",
        "value": "high",
    }

    with pytest.raises(ValidationError):
        ActivityPropertyFilterInput(
            property_name="priority",
            operator="eq",
            value="high",
            event="ticket_created",
        )


def test_funnel_output_allows_no_state_basis_when_profiles_are_not_read():
    result = GroupFunnelOutput.model_validate(
        {
            "scope": {"project_id": str(uuid4())},
            "period": {
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-02T00:00:00Z",
            },
            "group_type": None,
            "correlation": {"kind": "distinct_id"},
            "state_basis": None,
            "historical_profile_reliability": None,
            "started_cohort_count": 1,
            "completed_cohort_count": 1,
            "completion_rate": 100.0,
            "steps": [],
        }
    )

    assert result.state_basis is None


@pytest.mark.anyio
async def test_generic_mcp_tools_return_discovery_queries_state_and_comparison(project):
    await sync_to_async(_seed)(project)
    start = "2026-01-01T00:00:00Z"
    mid = "2026-02-01T00:00:00Z"
    end = "2026-03-04T00:00:00Z"
    rules = [
        {"label": "helpdesk", "event": "ticket_created", "product": "helpdesk"},
        {"label": "bi", "event": "report_opened", "product": "bi"},
    ]

    async with Client(create_server(project.pk)) as client:
        catalog = await client.call_tool("discover_analytics_catalog", {})
        event_result = await client.call_tool(
            "query_events",
            {
                "start": start,
                "end": mid,
                "filters": [
                    {
                        "field": "property",
                        "event": "ticket_created",
                        "product": "helpdesk",
                        "property_name": "priority",
                        "operator": "gte",
                        "value": 1,
                    }
                ],
                "group_by": [{"kind": "product", "label": "product"}],
                "aggregations": [
                    {"kind": "event_count", "label": "events"},
                    {"kind": "distinct_id_count", "label": "actors"},
                ],
                "comparison_start": mid,
                "comparison_end": end,
            },
        )
        state = await client.call_tool(
            "analyze_group_state",
            {
                "group_type": "account",
                "state_basis": "current",
                "limit": 10,
            },
        )
        activity = await client.call_tool(
            "query_group_activity",
            {
                "group_type": "account",
                "start": mid,
                "end": end,
                "state_basis": "current",
                "activity_rules": rules,
                "match": "all",
                "include_distinct_id_overlap": True,
            },
        )
        adoption = await client.call_tool(
            "analyze_group_adoption",
            {
                "group_type": "account",
                "start": mid,
                "end": end,
                "state_basis": "current",
                "eligibility_filters": [
                    {"property_name": "plan", "operator": "eq", "value": "pro"}
                ],
                "activity_rule": rules[0],
            },
        )
        funnel = await client.call_tool(
            "analyze_group_funnel",
            {
                "group_type": "account",
                "start": start,
                "end": mid,
                "state_basis": "event_time",
                "steps": [
                    {"label": "created", "event": "ticket_created", "product": "helpdesk"},
                    {"label": "resolved", "event": "ticket_resolved", "product": "helpdesk"},
                ],
            },
        )
        transitions = await client.call_tool(
            "analyze_group_transitions",
            {
                "group_type": "account",
                "property_name": "plan",
                "start": "2026-01-03T00:00:00Z",
                "end": mid,
                "state_basis": "event_time",
            },
        )
        comparison = await client.call_tool(
            "compare_group_activity",
            {
                "group_type": "account",
                "start": mid,
                "end": end,
                "comparison_start": start,
                "comparison_end": mid,
                "state_basis": "current",
                "activity_rules": rules,
                "match": "all",
            },
        )

    results = {
        "catalog": catalog,
        "event_result": event_result,
        "state": state,
        "activity": activity,
        "adoption": adoption,
        "funnel": funnel,
        "transitions": transitions,
        "comparison": comparison,
    }
    for name, result in results.items():
        assert result.is_error is False, (name, result.content)
        assert result.structured_content["scope"] == {"project_id": str(project.pk)}
    catalog_data = catalog.structured_content
    assert "secret_token" not in str(catalog_data)
    assert "internal_note" not in str(catalog_data)
    assert state.structured_content["groups"]["items"] == [
        {"group_key": "acme", "properties": {"plan": "pro"}}
    ]
    assert [row["group_key"] for row in activity.structured_content["groups"]["items"]] == ["acme"]
    assert adoption.structured_content["denominator"] == 1
    assert funnel.structured_content["completed_cohort_count"] == 1
    assert transitions.structured_content["transitions"]["returned_count"] == 1
    assert comparison.structured_content["comparison"]["changes"]["items"][0]["group_key"] == "acme"
    await sync_to_async(connections.close_all)()


@pytest.mark.anyio
async def test_database_timeout_is_applied_only_by_mcp_analytics_execution(project, monkeypatch):
    from analytics_platform.mcp_adapter import server as server_module

    calls = []
    original = server_module._set_analytics_statement_timeout

    def record_timeout():
        calls.append(True)
        original()

    monkeypatch.setattr(server_module, "_set_analytics_statement_timeout", record_timeout)
    async with Client(create_server(project.pk)) as client:
        result = await client.call_tool("discover_analytics_catalog", {})

    assert result.is_error is False
    assert calls
    assert "statement_timeout" not in str(settings.DATABASES["default"].get("OPTIONS", {}))


@pytest.mark.anyio
async def test_mcp_rejects_oversized_structured_results(project, monkeypatch):
    await sync_to_async(GroupPropertyDefinition.objects.create)(
        project=project,
        group_type="account",
        property_name="description",
        observed_non_null_types=["string"],
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await sync_to_async(GroupProfile.objects.create)(
        project=project,
        group_type="account",
        group_key="acme",
        properties={"description": "sensitive-value-" * 100},
    )
    monkeypatch.setattr(settings, "ANALYTICS_MAX_RESPONSE_BYTES", 300)

    async with Client(create_server(project.pk)) as client:
        result = await client.call_tool(
            "analyze_group_state",
            {"group_type": "account", "state_basis": "current", "group_key": "acme"},
        )
    await sync_to_async(connections.close_all)()

    assert result.is_error is True
    assert "result exceeds" in str(result.content).lower()
    assert "sensitive-value" not in str(result.content)


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL local timeout behavior")
def test_analytics_statement_timeout_resets_at_transaction_boundary():
    from analytics_platform.mcp_adapter.server import _set_analytics_statement_timeout

    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        before = cursor.fetchone()[0]
    with transaction.atomic():
        _set_analytics_statement_timeout()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('statement_timeout')::interval = "
                "%s * interval '1 millisecond'",
                [settings.ANALYTICS_QUERY_TIMEOUT_MS],
            )
            inside_matches_configured_value = cursor.fetchone()[0]
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        after = cursor.fetchone()[0]

    assert inside_matches_configured_value is True
    assert after == before

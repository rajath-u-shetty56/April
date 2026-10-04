from uuid import uuid4

import pytest
from asgiref.sync import sync_to_async
from django.db import connections
from django.utils import timezone
from mcp.client import Client

from analytics_platform.catalog.models import Project
from analytics_platform.event_catalog.models import EventDefinition, EventPropertyDefinition
from analytics_platform.mcp_adapter.server import create_server

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def inactive_project(workspace):
    return Project.objects.create(
        workspace=workspace, key="inactive", name="Inactive", is_active=False
    )


def _error_text(result) -> str:
    return " ".join(block.text for block in result.content if hasattr(block, "text"))


def _seed_hidden_property(project):
    definition = EventDefinition.objects.create(
        project=project,
        product_key="example",
        name="record_created",
    )
    EventPropertyDefinition.objects.create(
        event_definition=definition,
        property_name="secret",
        status="hidden",
        observed_non_null_types=["string"],
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
    )


@pytest.mark.anyio
async def test_tool_discovery_exposes_only_generic_read_only_tools():
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
        assert tool.description and len(tool.description.strip()) >= 40
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
        assert tool.output_schema is not None
        schema_text = str(tool.input_schema).lower()
        assert "project_id" not in schema_text
        assert "sql" not in schema_text
        assert "happyfox" not in tool.description.lower()

    activity_schema = str(tools["query_group_activity"].input_schema)
    assert "activity_rules" in activity_schema
    assert "all" in activity_schema and "any" in activity_schema
    assert "state_basis" in activity_schema
    state_schema = tools["analyze_group_state"].input_schema
    assert "group_key" not in state_schema.get("required", [])
    assert "state_basis" in state_schema.get("required", [])
    adoption_schema = tools["analyze_group_adoption"].input_schema
    assert "eligibility_filters" in adoption_schema.get("required", [])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("start", "end", "expected_message"),
    [
        ("not-a-timestamp", "2026-10-01T00:00:00Z", "ISO 8601 timestamps"),
        ("2026-09-01T00:00:00", "2026-10-01T00:00:00Z", "timezone-aware"),
        ("2026-10-01T00:00:00Z", "2026-09-01T00:00:00Z", "start must be before end"),
    ],
)
async def test_query_events_period_errors_are_specific_and_safe(
    project, start, end, expected_message
):
    async with Client(create_server(project.pk)) as client:
        result = await client.call_tool(
            "query_events",
            {
                "start": start,
                "end": end,
                "aggregations": [{"kind": "event_count", "label": "events"}],
            },
        )

    assert result.is_error is True
    assert expected_message in _error_text(result)
    assert "Traceback" not in _error_text(result)
    assert "DATABASE_URL" not in _error_text(result)


@pytest.mark.anyio
async def test_tool_arguments_cannot_override_project_scope_or_query_hidden_properties(
    project, inactive_project
):
    await sync_to_async(_seed_hidden_property)(project)
    async with Client(create_server(project.pk)) as client:
        override = await client.call_tool(
            "discover_analytics_catalog", {"project_id": str(inactive_project.pk)}
        )
        hidden = await client.call_tool(
            "query_events",
            {
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-10-01T00:00:00Z",
                "filters": [
                    {
                        "field": "property",
                        "event": "record_created",
                        "product": "example",
                        "property_name": "secret",
                        "operator": "eq",
                        "value": "x",
                    }
                ],
                "aggregations": [{"kind": "event_count", "label": "events"}],
            },
        )

    async with Client(create_server(inactive_project.pk)) as client:
        unavailable_scope = await client.call_tool("discover_analytics_catalog", {})
    await sync_to_async(connections.close_all)()

    assert override.is_error is False
    assert override.structured_content["scope"] == {"project_id": str(project.pk)}
    assert hidden.is_error is True
    assert "hidden" in _error_text(hidden)
    assert unavailable_scope.is_error is True
    assert "Traceback" not in _error_text(unavailable_scope)
    assert "DATABASE_URL" not in _error_text(unavailable_scope)


@pytest.mark.anyio
async def test_unexpected_service_failure_is_generic_and_safely_logged(
    project, monkeypatch, caplog
):
    from analytics_platform.mcp_adapter import server as server_module

    def fail(*args, **kwargs):
        raise RuntimeError("database password=fake-secret")

    monkeypatch.setattr(server_module, "discover_analytics_catalog_service", fail)
    caplog.set_level("ERROR", logger="analytics.mcp")

    async with Client(create_server(project.pk)) as client:
        result = await client.call_tool("discover_analytics_catalog", {})

    assert result.is_error is True
    assert "Analytics query failed" in _error_text(result)
    assert "fake-secret" not in _error_text(result)
    assert "fake-secret" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert str(project.pk) in caplog.text
    assert "Traceback" not in caplog.text

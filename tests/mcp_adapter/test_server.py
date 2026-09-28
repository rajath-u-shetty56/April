from uuid import uuid4

import pytest
from mcp.client import Client

from analytics_platform.catalog.models import Project
from analytics_platform.mcp_adapter.server import create_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def inactive_project(workspace):
    return Project.objects.create(
        workspace=workspace, key="inactive", name="Inactive", is_active=False
    )


@pytest.mark.anyio
async def test_tool_discovery_exposes_only_reviewed_read_only_tools():
    server = create_server(uuid4())

    async with Client(server) as client:
        result = await client.list_tools()

    expected = {
        "describe_project",
        "get_account_profile",
        "summarize_account_activity",
        "find_cross_product_accounts",
        "analyze_product_adoption",
        "count_feature_users",
        "analyze_funnel",
        "analyze_account_change",
        "analyze_trial_outcomes",
    }
    assert {tool.name for tool in result.tools} == expected
    for tool in result.tools:
        assert tool.description
        assert len(tool.description.strip()) >= 40
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
        assert tool.output_schema is not None
        schema_text = str(tool.input_schema).lower()
        for forbidden in ("project_id", "workspace", "sql", "predicate"):
            assert forbidden not in schema_text
    tools_by_name = {tool.name: tool for tool in result.tools}
    assert "population_basis" in str(
        tools_by_name["find_cross_product_accounts"].output_schema
    )
    adoption_schema = str(tools_by_name["analyze_product_adoption"].output_schema)
    assert "any_observed_product_event_in_period" in adoption_schema
    assert "qualifying_feature_events_in_period" in adoption_schema
    assert "adoption_rate_threshold" in adoption_schema
    assert "actual_median_depth" in adoption_schema
    adoption_description = tools_by_name["analyze_product_adoption"].description
    assert "any observed product event" in adoption_description
    assert "qualifying feature events" in adoption_description


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_every_tool_returns_structured_scope_and_evidence(project):
    server = create_server(project.pk)
    start = "2026-09-01T00:00:00Z"
    end = "2026-10-01T00:00:00Z"
    calls = {
        "describe_project": {},
        "get_account_profile": {"account_key": "missing"},
        "summarize_account_activity": {
            "account_key": "missing",
            "start": start,
            "end": end,
        },
        "find_cross_product_accounts": {
            "products": ["helpdesk", "contact_center", "rise"],
            "start": start,
            "end": end,
        },
        "analyze_product_adoption": {
            "product": "contact_center",
            "feature": "ai_summary",
            "start": start,
            "end": end,
            "include_plan_breakdown": True,
        },
        "count_feature_users": {
            "product": "contact_center",
            "feature": "ai_summary",
            "start": start,
            "end": end,
        },
        "analyze_funnel": {
            "funnel": "call_connection",
            "start": start,
            "end": end,
        },
        "analyze_account_change": {
            "kind": "usage_decline",
            "product": "contact_center",
            "start": start,
            "end": end,
        },
        "analyze_trial_outcomes": {
            "product": "contact_center",
            "start": start,
            "end": end,
        },
    }

    async with Client(server) as client:
        results = {
            name: await client.call_tool(name, arguments) for name, arguments in calls.items()
        }

    for name, result in results.items():
        assert result.is_error is False, name
        assert result.structured_content["scope"] == {"project_id": str(project.pk)}
    assert results["get_account_profile"].structured_content["found"] is False
    assert results["describe_project"].structured_content["event_definitions"]["truncated"] is False
    for name in set(results) - {"describe_project", "get_account_profile"}:
        payload = results[name].structured_content
        assert payload["period"] == {"start": start, "end": end}
        assert payload["interpretation"]


def _error_text(result) -> str:
    return " ".join(block.text for block in result.content if hasattr(block, "text"))


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("start", "end", "expected_message"),
    [
        (
            "not-a-timestamp",
            "2026-10-01T00:00:00Z",
            "start and end must be ISO 8601 timestamps",
        ),
        (
            "2026-09-01T00:00:00",
            "2026-10-01T00:00:00Z",
            "start must be timezone-aware",
        ),
        (
            "2026-10-01T00:00:00Z",
            "2026-09-01T00:00:00Z",
            "start must be before end",
        ),
    ],
)
async def test_period_errors_preserve_specific_safe_validation_messages(
    project, start, end, expected_message
):
    async with Client(create_server(project.pk)) as client:
        result = await client.call_tool(
            "summarize_account_activity",
            {"account_key": "acme", "start": start, "end": end},
        )

    assert result.is_error is True
    text = _error_text(result)
    assert expected_message in text
    assert "Traceback" not in text
    assert "ValueError" not in text
    assert "DATABASE_URL" not in text


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_tool_errors_are_sanitized_for_invalid_inputs_and_unavailable_scope(
    project, inactive_project
):
    start = "2026-09-01T00:00:00Z"
    end = "2026-10-01T00:00:00Z"
    invalid_calls = [
        (
            create_server(project.pk),
            "analyze_product_adoption",
            {"product": "unknown", "start": start, "end": end},
        ),
        (
            create_server(project.pk),
            "count_feature_users",
            {
                "product": "contact_center",
                "start": "2026-09-01T00:00:00",
                "end": end,
            },
        ),
        (
            create_server(project.pk),
            "summarize_account_activity",
            {"account_key": "acme", "start": end, "end": start},
        ),
        (
            create_server(project.pk),
            "describe_project",
            {"event_definition_limit": 201},
        ),
        (create_server(inactive_project.pk), "describe_project", {}),
        (create_server(uuid4()), "describe_project", {}),
    ]

    for server, name, arguments in invalid_calls:
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
        assert result.is_error is True
        text = _error_text(result)
        assert "Traceback" not in text
        assert "DATABASE_URL" not in text


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_unexpected_service_failure_is_generic_and_safely_logged(
    project, monkeypatch, caplog
):
    from analytics_platform.mcp_adapter import server as server_module

    def fail(*args, **kwargs):
        raise RuntimeError("database password=fake-secret")

    monkeypatch.setattr(server_module, "describe_project_service", fail)
    caplog.set_level("ERROR", logger="analytics.mcp")

    async with Client(create_server(project.pk)) as client:
        result = await client.call_tool("describe_project", {})

    assert result.is_error is True
    assert "Analytics query failed" in _error_text(result)
    assert "fake-secret" not in _error_text(result)
    assert "fake-secret" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert str(project.pk) in caplog.text
    assert "Traceback" not in caplog.text

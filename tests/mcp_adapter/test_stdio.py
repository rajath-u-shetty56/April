import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from django.db import connection
from mcp import StdioServerParameters
from mcp.client import Client

from analytics_platform.events.models import Event


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def malformed_event(project):
    leaked_secret = "database-password=fake-secret"
    Event.objects.create(
        project=project,
        uuid=uuid4(),
        event="malformed_test_event",
        distinct_id="test-user",
        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        groups={"account": "malformed"},
        properties=leaked_secret,
    )
    return leaked_secret


def _active_test_database_url() -> str:
    settings = connection.settings_dict
    user = quote(str(settings.get("USER") or ""), safe="")
    password = quote(str(settings.get("PASSWORD") or ""), safe="")
    credentials = user
    if password:
        credentials = f"{user}:{password}"
    if credentials:
        credentials += "@"
    host = settings.get("HOST") or "localhost"
    port = f":{settings['PORT']}" if settings.get("PORT") else ""
    name = quote(str(settings["NAME"]), safe="")
    return f"postgresql://{credentials}{host}{port}/{name}"


def _error_text(result) -> str:
    return " ".join(block.text for block in result.content if hasattr(block, "text"))


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_real_stdio_server_uses_only_environment_project_scope(
    project, malformed_event, capfd
):
    repository = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "DATABASE_URL": _active_test_database_url(),
        "ANALYTICS_PROJECT_ID": str(project.pk),
    }
    parameters = StdioServerParameters(
        command="uv",
        args=["run", "python", "scripts/run_analytics_mcp.py"],
        env=environment,
        cwd=repository,
    )

    async with Client(parameters) as client:
        tools = await client.list_tools()
        catalog = await client.call_tool("discover_analytics_catalog", {})
        attempted_override = await client.call_tool(
            "discover_analytics_catalog",
            {"project_id": "00000000-0000-0000-0000-000000000000"},
        )
        query = await client.call_tool(
            "query_events",
            {
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-10-01T00:00:00Z",
                "group_by": [{"kind": "event", "label": "event"}],
                "aggregations": [{"kind": "event_count", "label": "events"}],
            },
        )
        hidden_or_unknown_property = await client.call_tool(
            "query_events",
            {
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-10-01T00:00:00Z",
                "filters": [
                    {
                        "field": "property",
                        "event": "malformed_test_event",
                        "product": "",
                        "property_name": "credential",
                        "operator": "eq",
                        "value": "x",
                    }
                ],
                "aggregations": [{"kind": "event_count", "label": "events"}],
            },
        )
        state = await client.call_tool(
            "analyze_group_state",
            {
                "group_type": "account",
                "state_basis": "current",
            },
        )
        period_errors = []
        for start, end in (
            ("not-a-timestamp", "2026-10-01T00:00:00Z"),
            ("2026-09-01T00:00:00", "2026-10-01T00:00:00Z"),
            ("2026-10-01T00:00:00Z", "2026-09-01T00:00:00Z"),
        ):
            period_errors.append(
                await client.call_tool(
                    "query_events",
                    {
                        "start": start,
                        "end": end,
                        "aggregations": [{"kind": "event_count", "label": "events"}],
                    },
                )
            )

    assert len(tools.tools) == 8
    assert all(tool.description and len(tool.description.strip()) >= 40 for tool in tools.tools)
    assert catalog.is_error is False
    assert catalog.structured_content["scope"] == {"project_id": str(project.pk)}
    assert attempted_override.is_error is False
    assert attempted_override.structured_content["scope"] == {"project_id": str(project.pk)}
    assert query.is_error is False
    assert query.structured_content["scope"] == {"project_id": str(project.pk)}
    assert {row["event"] for row in query.structured_content["rows"]["items"]} == {
        "malformed_test_event"
    }
    assert state.is_error is False
    assert "groups" in state.structured_content
    assert hidden_or_unknown_property.is_error is True
    assert "not discovered" in _error_text(hidden_or_unknown_property)
    assert [
        "start and end must be ISO 8601 timestamps" in _error_text(period_errors[0]),
        "start must be timezone-aware" in _error_text(period_errors[1]),
        "start must be before end" in _error_text(period_errors[2]),
    ] == [True, True, True]
    assert all(result.is_error for result in period_errors)
    assert all("Traceback" not in _error_text(result) for result in period_errors)
    captured = capfd.readouterr()
    assert malformed_event not in captured.err
    assert "Traceback" not in captured.err
    assert "DATABASE_URL" not in captured.err
    assert environment.get("DJANGO_SECRET_KEY", "not-present") not in captured.err

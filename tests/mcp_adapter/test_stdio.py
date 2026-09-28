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
        description = await client.call_tool("describe_project", {})
        attempted_override = await client.call_tool(
            "describe_project", {"project_id": "00000000-0000-0000-0000-000000000000"}
        )
        overlap = await client.call_tool(
            "find_cross_product_accounts",
            {
                "products": ["helpdesk", "contact_center", "rise"],
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-10-01T00:00:00Z",
            },
        )
        adoption = await client.call_tool(
            "analyze_product_adoption",
            {
                "product": "contact_center",
                "feature": "supervisor_listen",
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-10-01T00:00:00Z",
            },
        )
        unexpected_error = await client.call_tool(
            "summarize_account_activity",
            {
                "account_key": "malformed",
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-10-01T00:00:00Z",
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
                    "summarize_account_activity",
                    {"account_key": "acme", "start": start, "end": end},
                )
            )

    assert len(tools.tools) == 9
    assert all(tool.description and len(tool.description.strip()) >= 40 for tool in tools.tools)
    assert description.is_error is False
    assert description.structured_content["scope"] == {"project_id": str(project.pk)}
    assert attempted_override.structured_content["scope"] == {"project_id": str(project.pk)}
    assert overlap.structured_content["user_overlap"]["population_basis"] == (
        "all_accounts_with_activity_in_any_requested_product"
    )
    classification = adoption.structured_content["features"]["supervisor_listen"][
        "high_adoption_low_depth"
    ]
    assert classification["adoption_numerator"] == 0
    assert classification["adoption_denominator"] == 0
    assert classification["adoption_rate_threshold"] == 50.0
    assert classification["minimum_account_threshold"] == 2
    assert classification["median_depth_threshold"] == 2.0
    assert classification["actual_median_depth"] == 0.0
    assert classification["classified"] is False
    assert unexpected_error.is_error is True
    assert malformed_event not in _error_text(unexpected_error)
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

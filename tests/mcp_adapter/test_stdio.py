import os
from pathlib import Path
from urllib.parse import quote

import pytest
from django.db import connection
from mcp import StdioServerParameters
from mcp.client import Client


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


@pytest.mark.anyio
@pytest.mark.django_db(transaction=True)
async def test_real_stdio_server_uses_only_environment_project_scope(project, capfd):
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

    assert len(tools.tools) == 9
    assert description.is_error is False
    assert description.structured_content["scope"] == {"project_id": str(project.pk)}
    assert attempted_override.structured_content["scope"] == {"project_id": str(project.pk)}
    captured = capfd.readouterr()
    assert "DATABASE_URL" not in captured.err
    assert environment.get("DJANGO_SECRET_KEY", "not-present") not in captured.err

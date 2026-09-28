import importlib
from uuid import uuid4

import pytest

from analytics_platform.catalog.models import Project, Workspace
from scripts import run_analytics_mcp

pytestmark = pytest.mark.django_db


def test_resolve_project_id_accepts_only_active_configured_scope(project):
    assert (
        run_analytics_mcp.resolve_project_id({"ANALYTICS_PROJECT_ID": str(project.pk)})
        == project.pk
    )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "ANALYTICS_PROJECT_ID is required"),
        ({"ANALYTICS_PROJECT_ID": "not-a-uuid"}, "must be a valid UUID"),
        ({"ANALYTICS_PROJECT_ID": str(uuid4())}, "is unavailable"),
    ],
)
def test_resolve_project_id_rejects_missing_malformed_or_unknown_scope(environment, message):
    with pytest.raises(run_analytics_mcp.StartupConfigurationError, match=message):
        run_analytics_mcp.resolve_project_id(environment)


def test_resolve_project_id_rejects_inactive_project_and_workspace(workspace):
    inactive_project = Project.objects.create(
        workspace=workspace, key="inactive", name="Inactive", is_active=False
    )
    inactive_workspace = Workspace.objects.create(
        key="inactive-workspace", name="Inactive Workspace", is_active=False
    )
    project_in_inactive_workspace = Project.objects.create(
        workspace=inactive_workspace, key="main", name="Main"
    )

    for project in (inactive_project, project_in_inactive_workspace):
        with pytest.raises(run_analytics_mcp.StartupConfigurationError, match="unavailable"):
            run_analytics_mcp.resolve_project_id({"ANALYTICS_PROJECT_ID": str(project.pk)})


def test_import_and_successful_resolution_write_nothing(project, capsys):
    importlib.reload(run_analytics_mcp)
    run_analytics_mcp.resolve_project_id({"ANALYTICS_PROJECT_ID": str(project.pk)})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_reports_sanitized_startup_failure_to_stderr(capsys):
    result = run_analytics_mcp.main({}, run_protocol=False)

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == "Analytics MCP startup failed: ANALYTICS_PROJECT_ID is required\n"

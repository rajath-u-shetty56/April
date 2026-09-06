import pytest

from analytics_platform.catalog.models import Project, Workspace


@pytest.fixture
def workspace(db):
    return Workspace.objects.create(key="acme", name="Acme")


@pytest.fixture
def project(workspace):
    return Project.objects.create(workspace=workspace, key="main", name="Main")

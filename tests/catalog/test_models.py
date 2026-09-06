import pytest
from django.db import IntegrityError, transaction

from analytics_platform.catalog.models import Project, Workspace


@pytest.mark.django_db
def test_project_keys_are_scoped_to_workspace():
    workspace = Workspace.objects.create(key="acme", name="Acme")
    Project.objects.create(workspace=workspace, key="main", name="Main")

    with pytest.raises(IntegrityError), transaction.atomic():
        Project.objects.create(workspace=workspace, key="main", name="Duplicate")

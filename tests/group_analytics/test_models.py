import pytest
from django.db import IntegrityError

from analytics_platform.group_analytics.models import GroupProfile

pytestmark = pytest.mark.django_db


def test_group_identity_is_unique_inside_project(project):
    GroupProfile.objects.create(project=project, group_type="account", group_key="acme")

    with pytest.raises(IntegrityError):
        GroupProfile.objects.create(project=project, group_type="account", group_key="acme")


def test_same_group_key_can_exist_under_another_type(project):
    account = GroupProfile.objects.create(
        project=project,
        group_type="account",
        group_key="acme",
    )
    team = GroupProfile.objects.create(project=project, group_type="team", group_key="acme")

    assert account.pk != team.pk

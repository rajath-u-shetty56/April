from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, close_old_connections, connection, connections

from analytics_platform.group_analytics import services
from analytics_platform.group_analytics.models import GroupProfile
from analytics_platform.group_analytics.services import identify_group, touch_event_groups

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


def test_touch_creates_profile_and_never_moves_last_seen_backwards(project):
    newer = datetime(2026, 9, 25, tzinfo=UTC)
    older = datetime(2026, 9, 20, tzinfo=UTC)

    touch_event_groups(project=project, groups={"account": "acme"}, occurred_at=newer)
    touch_event_groups(project=project, groups={"account": "acme"}, occurred_at=older)

    assert project.group_profiles.get().last_seen_at == newer


def test_identify_merges_current_properties(project):
    identify_group(
        project=project,
        group_type="account",
        group_key="acme",
        properties={"name": "Acme", "plan": "trial"},
    )
    identify_group(
        project=project,
        group_type="account",
        group_key="acme",
        properties={"plan": "enterprise"},
    )

    assert project.group_profiles.get().properties == {
        "name": "Acme",
        "plan": "enterprise",
    }


def test_touch_acquires_multiple_group_locks_in_stable_order(project, monkeypatch):
    occurred_at = datetime(2026, 9, 25, tzinfo=UTC)
    calls = []

    class UnchangedProfile:
        last_seen_at = occurred_at

    def record_lock(*, project, group_type, group_key):
        calls.append((group_type, group_key))
        return UnchangedProfile()

    monkeypatch.setattr(services, "_locked_profile", record_lock)

    touch_event_groups(
        project=project,
        groups={"team": "support", "account": "acme"},
        occurred_at=occurred_at,
    )

    assert calls == [("account", "acme"), ("team", "support")]


@pytest.mark.django_db(transaction=True)
def test_concurrent_identification_keeps_both_property_updates(project):
    assert connection.vendor == "postgresql", "Concurrency tests require PostgreSQL"

    def identify(properties):
        close_old_connections()
        try:
            identify_group(
                project=project,
                group_type="account",
                group_key="acme",
                properties=properties,
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(identify, {"plan": "enterprise"})
        second = pool.submit(identify, {"region": "india"})
        first.result(timeout=15)
        second.result(timeout=15)

    profile = project.group_profiles.get(group_type="account", group_key="acme")
    assert profile.properties == {"plan": "enterprise", "region": "india"}

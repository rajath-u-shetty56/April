from django.db import transaction

from analytics_platform.common.property_observations import observe_properties
from analytics_platform.group_analytics.models import GroupProfile, GroupPropertyDefinition


def _locked_profile(*, project, group_type, group_key):
    profile, _ = GroupProfile.objects.get_or_create(
        project=project,
        group_type=group_type,
        group_key=group_key,
    )
    return GroupProfile.objects.select_for_update().get(pk=profile.pk)


@transaction.atomic
def touch_event_groups(*, project, groups, occurred_at):
    # A deterministic order prevents two multi-group events from taking the
    # same profile row locks in opposite orders.
    for group_type, group_key in sorted(groups.items()):
        profile = _locked_profile(
            project=project,
            group_type=group_type,
            group_key=group_key,
        )
        if profile.last_seen_at is None or occurred_at > profile.last_seen_at:
            profile.last_seen_at = occurred_at
            profile.save(update_fields=("last_seen_at", "updated_at"))


@transaction.atomic
def identify_group(*, project, group_type, group_key, properties):
    profile = _locked_profile(
        project=project,
        group_type=group_type,
        group_key=group_key,
    )
    profile.properties = {**profile.properties, **properties}
    profile.save(update_fields=("properties", "updated_at"))
    return profile


@transaction.atomic
def observe_group_properties(*, project, group_type, properties, occurred_at):
    observe_properties(
        GroupPropertyDefinition,
        {"project": project, "group_type": group_type},
        properties,
        occurred_at,
    )

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from analytics_platform.analytics.contracts import TimeRange
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event


@dataclass(frozen=True)
class ProfileTransition:
    timestamp: datetime
    event_uuid: UUID
    properties: Mapping[str, object]


@dataclass(frozen=True)
class ProfileTimeline:
    account_key: str
    transitions: tuple[ProfileTransition, ...]

    def state_at(self, timestamp: datetime, *, inclusive: bool = True) -> Mapping[str, object]:
        state: Mapping[str, object] = MappingProxyType({})
        for transition in self.transitions:
            if transition.timestamp > timestamp or (
                not inclusive and transition.timestamp == timestamp
            ):
                break
            state = transition.properties
        return state

    def states_during(self, period: TimeRange) -> tuple[ProfileTransition, ...]:
        return tuple(
            transition
            for transition in self.transitions
            if period.start <= transition.timestamp < period.end
        )


def load_profile_timelines(project: Project) -> dict[str, ProfileTimeline]:
    rows = Event.objects.filter(project=project, event="$groupidentify").order_by(
        "timestamp", "uuid"
    )
    states: dict[str, dict[str, object]] = defaultdict(dict)
    transitions: dict[str, list[ProfileTransition]] = defaultdict(list)
    for event in rows:
        properties = event.properties
        account_key = properties.get("$group_key")
        changes = properties.get("$group_set")
        if (
            properties.get("$group_type") != "account"
            or not isinstance(account_key, str)
            or not isinstance(changes, dict)
        ):
            continue
        states[account_key] = {**states[account_key], **changes}
        transitions[account_key].append(
            ProfileTransition(
                timestamp=event.timestamp,
                event_uuid=event.uuid,
                properties=MappingProxyType(dict(states[account_key])),
            )
        )
    return {
        account_key: ProfileTimeline(account_key, tuple(account_transitions))
        for account_key, account_transitions in transitions.items()
    }

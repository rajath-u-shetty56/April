from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from analytics_platform.analytics.catalog import _validate_account_key
from analytics_platform.analytics.contracts import Interpretation, Scope, TimeRange
from analytics_platform.analytics.semantics import SEMANTICS
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event


@dataclass(frozen=True)
class FunnelResult:
    scope: Scope
    period: TimeRange
    funnel: str
    configuration: dict[str, str]
    started: int
    completed: int
    accounts_started: int
    accounts_completed: int

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "interpretation": Interpretation(
                usage_window="ordered_correlated_events_in_period"
            ).to_dict(),
            "funnel": self.funnel,
            "configuration": self.configuration,
            "started": self.started,
            "completed": self.completed,
            "lost": self.started - self.completed,
            "completion_rate": (
                round(self.completed * 100 / self.started, 2) if self.started else 0.0
            ),
            "accounts_started": self.accounts_started,
            "accounts_completed": self.accounts_completed,
        }


def analyze_funnel(
    project: Project,
    funnel: str,
    period: TimeRange,
    *,
    account_key: str | None = None,
) -> FunnelResult:
    definition = SEMANTICS.get_funnel(funnel)
    query = Event.objects.filter(
        project=project,
        timestamp__gte=period.start,
        timestamp__lt=period.end,
        event__in=(definition.start_event, definition.completion_event),
        properties__product=definition.product,
    ).order_by("timestamp", "uuid")
    if account_key is not None:
        query = query.filter(groups__account=_validate_account_key(account_key))

    starts: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    completions: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for row in query.values("event", "timestamp", "groups", "properties"):
        account = row["groups"].get("account")
        correlation = row["properties"].get(definition.correlation_property)
        if not isinstance(account, str) or not isinstance(correlation, str) or not correlation:
            continue
        target = starts if row["event"] == definition.start_event else completions
        target[(account, correlation)].append(row["timestamp"])

    completed = 0
    completed_accounts = set()
    for key, start_times in starts.items():
        completion_times = completions.get(key, [])
        completion_index = 0
        for started_at in start_times:
            while (
                completion_index < len(completion_times)
                and completion_times[completion_index] <= started_at
            ):
                completion_index += 1
            if completion_index < len(completion_times):
                completed += 1
                completed_accounts.add(key[0])
                completion_index += 1
    started = sum(len(values) for values in starts.values())
    return FunnelResult(
        scope=Scope(project.pk),
        period=period,
        funnel=funnel,
        configuration={
            "start_event": definition.start_event,
            "completion_event": definition.completion_event,
            "correlation_property": definition.correlation_property,
        },
        started=started,
        completed=completed,
        accounts_started=len({account for account, _ in starts}),
        accounts_completed=len(completed_accounts),
    )

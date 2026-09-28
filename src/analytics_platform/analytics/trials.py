from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from analytics_platform.analytics.contracts import (
    HISTORICAL_PROFILE_RELIABILITY,
    BoundedList,
    Interpretation,
    Scope,
    TimeRange,
    validate_limit,
)
from analytics_platform.analytics.profiles import ProfileTransition, load_profile_timelines
from analytics_platform.analytics.semantics import SEMANTICS
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TrialOutcomeResult:
    scope: Scope
    period: TimeRange
    product: str
    semantics: dict[str, str]
    used_before_conversion: BoundedList[dict[str, object]]
    used_then_expired: BoundedList[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "interpretation": Interpretation(
                usage_window="observed_product_events_during_trial",
                plan_attribution_basis="historical_transition_time",
                historical_profile_reliability=HISTORICAL_PROFILE_RELIABILITY,
            ).to_dict(),
            "product": self.product,
            "semantics": self.semantics,
            "used_before_conversion": self.used_before_conversion.to_dict(),
            "used_then_expired": self.used_then_expired.to_dict(),
        }


def _first_transition(
    transitions: tuple[ProfileTransition, ...],
    *,
    after: datetime | None,
    before: datetime,
    status_property: str,
    statuses: set[str],
) -> ProfileTransition | None:
    return next(
        (
            transition
            for transition in transitions
            if (after is None or transition.timestamp > after)
            and transition.timestamp < before
            and transition.properties.get(status_property) in statuses
        ),
        None,
    )


def analyze_trial_outcomes(
    project: Project,
    product: str,
    period: TimeRange,
    *,
    account_limit: int = 50,
) -> TrialOutcomeResult:
    limit = validate_limit(account_limit)
    product_definition = SEMANTICS.get_product(product)
    trial = product_definition.trial
    conversions: list[dict[str, object]] = []
    expiries: list[dict[str, object]] = []

    for account, timeline in sorted(load_profile_timelines(project).items()):
        trial_transition = _first_transition(
            timeline.transitions,
            after=None,
            before=period.end,
            status_property=product_definition.status_property,
            statuses={trial.status},
        )
        if trial_transition is None or trial_transition.timestamp < period.start:
            continue
        outcome = _first_transition(
            timeline.transitions,
            after=trial_transition.timestamp,
            before=period.end,
            status_property=product_definition.status_property,
            statuses={trial.conversion_status, trial.expiry_status},
        )
        if outcome is None:
            continue
        timestamps = list(
            Event.objects.filter(
                project=project,
                timestamp__gte=trial_transition.timestamp,
                timestamp__lt=outcome.timestamp,
                groups__account=account,
                properties__product=product,
            )
            .order_by("timestamp", "uuid")
            .values_list("timestamp", flat=True)
        )
        if not timestamps:
            continue
        evidence = {
            "account_key": account,
            "trial_started_at": _iso(trial_transition.timestamp),
            "outcome_at": _iso(outcome.timestamp),
            "first_usage_at": _iso(timestamps[0]),
            "last_usage_at": _iso(timestamps[-1]),
            "event_count": len(timestamps),
        }
        outcome_status = outcome.properties.get(product_definition.status_property)
        if outcome_status == trial.conversion_status:
            conversions.append(evidence)
        elif outcome_status == trial.expiry_status:
            expiries.append(evidence)

    return TrialOutcomeResult(
        scope=Scope(project.pk),
        period=period,
        product=product,
        semantics={
            "trial_status": trial.status,
            "conversion_status": trial.conversion_status,
            "expiry_status": trial.expiry_status,
        },
        used_before_conversion=BoundedList.from_items(conversions, limit=limit),
        used_then_expired=BoundedList.from_items(expiries, limit=limit),
    )

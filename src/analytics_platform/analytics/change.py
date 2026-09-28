from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from analytics_platform.analytics.contracts import (
    AnalyticsInputError,
    BoundedList,
    Interpretation,
    Scope,
    TimeRange,
    validate_limit,
)
from analytics_platform.analytics.features import qualifying_events
from analytics_platform.analytics.semantics import SEMANTICS
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event


@dataclass(frozen=True)
class AccountChangeResult:
    scope: Scope
    period: TimeRange
    comparison_period: TimeRange
    kind: str
    product: str
    feature: str | None
    threshold: float | None
    accounts: BoundedList[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "comparison_period": self.comparison_period.as_dict(),
            "interpretation": Interpretation(
                usage_window="requested_period_vs_immediately_preceding_equal_period"
            ).to_dict(),
            "kind": self.kind,
            "product": self.product,
            "feature": self.feature,
            "threshold": self.threshold,
            "matching_account_count": self.accounts.total_count,
            "accounts": self.accounts.to_dict(),
        }


def _event_counts(project: Project, product: str, period: TimeRange) -> Counter[str]:
    counts = Counter[str]()
    rows = Event.objects.filter(
        project=project,
        timestamp__gte=period.start,
        timestamp__lt=period.end,
        properties__product=product,
    ).values_list("groups", flat=True)
    for groups in rows:
        account = groups.get("account")
        if isinstance(account, str):
            counts[account] += 1
    return counts


def analyze_account_change(
    project: Project,
    kind: Literal["usage_decline", "feature_abandonment"],
    product: str,
    period: TimeRange,
    *,
    feature: str | None = None,
    decline_threshold: float = 50.0,
    account_limit: int = 50,
) -> AccountChangeResult:
    if kind not in {"usage_decline", "feature_abandonment"}:
        raise AnalyticsInputError("kind must be usage_decline or feature_abandonment")
    SEMANTICS.get_product(product)
    limit = validate_limit(account_limit)
    if isinstance(decline_threshold, bool) or not 0 <= decline_threshold <= 100:
        raise AnalyticsInputError("decline_threshold must be between 0 and 100")
    if kind == "feature_abandonment" and feature is None:
        raise AnalyticsInputError("feature is required for feature_abandonment")
    duration = period.end - period.start
    comparison = TimeRange.create(period.start - duration, period.start)

    evidence: list[dict[str, object]]
    threshold: float | None
    if kind == "usage_decline":
        previous = _event_counts(project, product, comparison)
        current = _event_counts(project, product, period)
        evidence = []
        for account in sorted(set(previous) | set(current)):
            previous_count = previous[account]
            current_count = current[account]
            decline = (
                round((previous_count - current_count) * 100 / previous_count, 2)
                if previous_count
                else 0.0
            )
            if decline >= decline_threshold:
                evidence.append(
                    {
                        "account_key": account,
                        "previous_events": previous_count,
                        "current_events": current_count,
                        "decline_percentage": decline,
                    }
                )
        threshold = float(decline_threshold)
    else:
        previous_events = qualifying_events(
            project, product, comparison, feature=feature
        )
        current_events = qualifying_events(project, product, period, feature=feature)
        previous = Counter(event.account_key for event in previous_events)
        current = Counter(event.account_key for event in current_events)
        evidence = [
            {
                "account_key": account,
                "previous_feature_events": previous[account],
                "current_feature_events": current[account],
            }
            for account in sorted(set(previous) - set(current))
        ]
        threshold = None

    return AccountChangeResult(
        scope=Scope(project.pk),
        period=period,
        comparison_period=comparison,
        kind=kind,
        product=product,
        feature=feature,
        threshold=threshold,
        accounts=BoundedList.from_items(evidence, limit=limit),
    )

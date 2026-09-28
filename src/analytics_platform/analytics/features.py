from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from analytics_platform.analytics.catalog import _validate_account_key
from analytics_platform.analytics.contracts import (
    HISTORICAL_PROFILE_RELIABILITY,
    BoundedList,
    Interpretation,
    Scope,
    TimeRange,
    validate_limit,
)
from analytics_platform.analytics.profiles import load_profile_timelines
from analytics_platform.analytics.semantics import SEMANTICS, FeatureDefinition
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event


@dataclass(frozen=True)
class QualifyingEvent:
    account_key: str
    feature: str
    timestamp: datetime
    distinct_id: str


@dataclass(frozen=True)
class FeatureUsersResult:
    scope: Scope
    period: TimeRange
    product: str
    features: tuple[str, ...]
    accounts: BoundedList[dict[str, object]]
    totals_by_feature: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "interpretation": Interpretation(usage_window="qualifying_events_in_period").to_dict(),
            "product": self.product,
            "features": list(self.features),
            "accounts": self.accounts.to_dict(),
            "totals_by_feature": self.totals_by_feature,
        }


@dataclass(frozen=True)
class AdoptionResult:
    scope: Scope
    period: TimeRange
    product: str
    interpretation: Interpretation
    features: dict[str, dict[str, object]]
    plans: dict[str, dict[str, object]] | None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "interpretation": self.interpretation.to_dict(),
            "product": self.product,
            "features": self.features,
        }
        if self.plans is not None:
            result["plans"] = self.plans
        return result


def _selected_features(product: str, feature: str | None) -> tuple[FeatureDefinition, ...]:
    definition = SEMANTICS.get_product(product)
    if feature is not None:
        return (SEMANTICS.get_feature(product, feature),)
    return definition.features


def qualifying_events(
    project: Project,
    product: str,
    period: TimeRange,
    *,
    feature: str | None = None,
    account_key: str | None = None,
) -> list[QualifyingEvent]:
    definitions = _selected_features(product, feature)
    by_event = {definition.predicate.event: definition for definition in definitions}
    query = Event.objects.filter(
        project=project,
        timestamp__gte=period.start,
        timestamp__lt=period.end,
        event__in=tuple(by_event),
        properties__product=product,
    )
    if account_key is not None:
        query = query.filter(groups__account=_validate_account_key(account_key))
    result = []
    for row in query.values("event", "timestamp", "distinct_id", "groups", "properties"):
        account = row["groups"].get("account")
        definition = by_event[row["event"]]
        if not isinstance(account, str) or not definition.predicate.matches(
            row["event"], row["properties"]
        ):
            continue
        result.append(
            QualifyingEvent(
                account_key=account,
                feature=definition.key,
                timestamp=row["timestamp"],
                distinct_id=row["distinct_id"],
            )
        )
    return result


def count_feature_users(
    project: Project,
    product: str,
    period: TimeRange,
    *,
    feature: str | None = None,
    account_key: str | None = None,
    account_limit: int = 50,
) -> FeatureUsersResult:
    limit = validate_limit(account_limit)
    definitions = _selected_features(product, feature)
    events = qualifying_events(project, product, period, feature=feature, account_key=account_key)
    users: dict[tuple[str, str], set[str]] = defaultdict(set)
    for event in events:
        users[(event.account_key, event.feature)].add(event.distinct_id)
    accounts = sorted({account for account, _ in users})
    evidence = [
        {
            "account_key": account,
            "features": {
                definition.key: len(users[(account, definition.key)])
                for definition in definitions
                if users[(account, definition.key)]
            },
        }
        for account in accounts
    ]
    return FeatureUsersResult(
        scope=Scope(project.pk),
        period=period,
        product=product,
        features=tuple(definition.key for definition in definitions),
        accounts=BoundedList.from_items(evidence, limit=limit),
        totals_by_feature={
            definition.key: sum(
                len(feature_users)
                for (candidate_account, candidate_feature), feature_users in users.items()
                if candidate_feature == definition.key
            )
            for definition in definitions
        },
    )


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


def analyze_product_adoption(
    project: Project,
    product: str,
    period: TimeRange,
    *,
    feature: str | None = None,
    include_plan_breakdown: bool = False,
    account_limit: int = 50,
) -> AdoptionResult:
    limit = validate_limit(account_limit)
    product_definition = SEMANTICS.get_product(product)
    definitions = _selected_features(product, feature)
    events = qualifying_events(project, product, period, feature=feature)
    timelines = load_profile_timelines(project)

    entitled_at_end = {
        account
        for account, timeline in timelines.items()
        if timeline.state_at(period.end, inclusive=False).get(product_definition.status_property)
        in product_definition.entitled_statuses
    }
    accounts_by_feature: dict[str, set[str]] = defaultdict(set)
    event_counts = Counter[str]()
    account_counts = Counter[tuple[str, str]]()
    for event in events:
        accounts_by_feature[event.feature].add(event.account_key)
        event_counts[event.feature] += 1
        account_counts[(event.account_key, event.feature)] += 1

    feature_results: dict[str, dict[str, object]] = {}
    for definition in definitions:
        adopting = accounts_by_feature[definition.key]
        depths = [account_counts[(account, definition.key)] for account in adopting]
        median_depth = float(statistics.median(depths)) if depths else 0.0
        entitled_adopters = adopting & entitled_at_end
        feature_results[definition.key] = {
            "observed_adopting_account_count": len(adopting),
            "observed_event_count": event_counts[definition.key],
            "median_events_per_adopting_account": median_depth,
            "high_adoption_low_depth": (
                len(adopting) >= max(2, len(entitled_at_end) / 2) and median_depth <= 2
            ),
            "period_end_entitled_adoption": {
                "numerator": len(entitled_adopters),
                "denominator": len(entitled_at_end),
                "rate": _rate(len(entitled_adopters), len(entitled_at_end)),
            },
            "adopting_accounts": BoundedList.from_items(sorted(adopting), limit=limit).to_dict(),
        }

    plan_results = None
    if include_plan_breakdown:
        eligible_by_plan: dict[str, set[str]] = defaultdict(set)
        for account, timeline in timelines.items():
            states = [timeline.state_at(period.start)]
            states.extend(transition.properties for transition in timeline.states_during(period))
            for state in states:
                plan = state.get(product_definition.plan_property or "")
                status = state.get(product_definition.status_property)
                if status in product_definition.entitled_statuses and isinstance(plan, str):
                    eligible_by_plan[plan].add(account)

        adopters_by_plan_feature: dict[tuple[str, str], set[str]] = defaultdict(set)
        for event in events:
            timeline = timelines.get(event.account_key)
            state = timeline.state_at(event.timestamp) if timeline else {}
            plan = state.get(product_definition.plan_property or "")
            status = state.get(product_definition.status_property)
            if status in product_definition.entitled_statuses and isinstance(plan, str):
                adopters_by_plan_feature[(plan, event.feature)].add(event.account_key)
        plan_results = {
            plan: {
                "eligible_account_count": len(eligible_accounts),
                "eligible_accounts": BoundedList.from_items(
                    sorted(eligible_accounts), limit=limit
                ).to_dict(),
                "features": {
                    definition.key: {
                        "numerator": len(adopters_by_plan_feature[(plan, definition.key)]),
                        "denominator": len(eligible_accounts),
                        "rate": _rate(
                            len(adopters_by_plan_feature[(plan, definition.key)]),
                            len(eligible_accounts),
                        ),
                        "adopting_accounts": BoundedList.from_items(
                            sorted(adopters_by_plan_feature[(plan, definition.key)]),
                            limit=limit,
                        ).to_dict(),
                    }
                    for definition in definitions
                },
            }
            for plan, eligible_accounts in sorted(eligible_by_plan.items())
        }

    return AdoptionResult(
        scope=Scope(project.pk),
        period=period,
        product=product,
        interpretation=Interpretation(
            usage_window="qualifying_events_in_period",
            entitlement_basis="period_end",
            plan_attribution_basis="event_time" if include_plan_breakdown else None,
            historical_profile_reliability=HISTORICAL_PROFILE_RELIABILITY,
        ),
        features=feature_results,
        plans=plan_results,
    )

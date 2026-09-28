from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from analytics_platform.analytics.catalog import _validate_account_key
from analytics_platform.analytics.contracts import (
    AnalyticsInputError,
    BoundedList,
    Interpretation,
    Scope,
    TimeRange,
    validate_limit,
)
from analytics_platform.analytics.semantics import SEMANTICS
from analytics_platform.catalog.models import Project
from analytics_platform.events.models import Event


@dataclass(frozen=True)
class AccountActivityResult:
    scope: Scope
    period: TimeRange
    account_key: str
    event_counts: dict[str, dict[str, int]]
    distinct_users_by_product: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "interpretation": Interpretation(usage_window="observed_events").to_dict(),
            "account_key": self.account_key,
            "active_products": sorted(self.event_counts),
            "event_counts": self.event_counts,
            "distinct_users_by_product": self.distinct_users_by_product,
        }


@dataclass(frozen=True)
class CrossProductResult:
    scope: Scope
    period: TimeRange
    products: tuple[str, ...]
    accounts: BoundedList[dict[str, object]]
    aggregate_event_counts: dict[str, int]
    user_overlap: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "interpretation": Interpretation(usage_window="observed_events").to_dict(),
            "products": list(self.products),
            "accounts": self.accounts.to_dict(),
            "aggregate_event_counts": self.aggregate_event_counts,
            "user_overlap": self.user_overlap,
        }


def _behavior_rows(project: Project, period: TimeRange):
    return Event.objects.filter(
        project=project,
        timestamp__gte=period.start,
        timestamp__lt=period.end,
    ).values("event", "distinct_id", "groups", "properties")


def summarize_account_activity(
    project: Project, account_key: str, period: TimeRange
) -> AccountActivityResult:
    account_key = _validate_account_key(account_key)
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    users: dict[str, set[str]] = defaultdict(set)
    rows = _behavior_rows(project, period).filter(groups__account=account_key)
    for row in rows:
        product = row["properties"].get("product")
        if not isinstance(product, str):
            continue
        counts[product][row["event"]] += 1
        users[product].add(row["distinct_id"])
    return AccountActivityResult(
        scope=Scope(project.pk),
        period=period,
        account_key=account_key,
        event_counts={
            product: dict(sorted(product_counts.items()))
            for product, product_counts in sorted(counts.items())
        },
        distinct_users_by_product={
            product: len(product_users) for product, product_users in sorted(users.items())
        },
    )


def find_cross_product_accounts(
    project: Project,
    products: Sequence[str],
    period: TimeRange,
    *,
    limit: int = 50,
) -> CrossProductResult:
    limit = validate_limit(limit)
    unique_products = tuple(sorted(set(products)))
    if len(unique_products) < 2:
        raise AnalyticsInputError("products must contain at least two unique product keys")
    definitions = [SEMANTICS.get_product(product) for product in unique_products]
    requested = set(unique_products)
    events_by_account: dict[str, Counter[str]] = defaultdict(Counter)
    users_by_account_product: dict[tuple[str, str], set[str]] = defaultdict(set)
    rows = _behavior_rows(project, period).filter(properties__product__in=unique_products)
    for row in rows:
        account = row["groups"].get("account")
        product = row["properties"].get("product")
        if not isinstance(account, str) or product not in requested:
            continue
        events_by_account[account][product] += 1
        users_by_account_product[(account, product)].add(row["distinct_id"])

    matching = sorted(
        account
        for account, product_counts in events_by_account.items()
        if requested <= set(product_counts)
    )
    evidence = [
        {
            "account_key": account,
            "event_counts": {
                product: events_by_account[account][product] for product in unique_products
            },
        }
        for account in matching
    ]
    aggregate_event_counts = {
        product: sum(events_by_account[account][product] for account in matching)
        for product in unique_products
    }

    namespaces = {definition.identity_namespace for definition in definitions}
    user_overlap = None
    if len(namespaces) == 1:
        aggregate_users = {
            product: set().union(
                *(users_by_account_product[(account, product)] for account in events_by_account)
            )
            for product in unique_products
        }
        user_overlap = {
            "population_basis": "all_accounts_with_activity_in_any_requested_product",
            "products": list(unique_products),
            "users_by_product": {
                product: len(aggregate_users[product]) for product in unique_products
            },
            "users_in_all_products": len(
                set.intersection(*(aggregate_users[product] for product in unique_products))
            ),
            "pairwise_overlap": {
                f"{left}|{right}": len(aggregate_users[left] & aggregate_users[right])
                for left, right in combinations(unique_products, 2)
            },
        }

    return CrossProductResult(
        scope=Scope(project.pk),
        period=period,
        products=unique_products,
        accounts=BoundedList.from_items(evidence, limit=limit),
        aggregate_event_counts=aggregate_event_counts,
        user_overlap=user_overlap,
    )

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from django.db.models import Max, Min

from analytics_platform.analytics.contracts import (
    AnalyticsInputError,
    BoundedList,
    Scope,
    validate_limit,
)
from analytics_platform.catalog.models import Project
from analytics_platform.event_catalog.models import EventDefinition
from analytics_platform.events.models import Event
from analytics_platform.group_analytics.models import GROUP_KEY_MAX_LENGTH, GroupProfile


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProjectDescription:
    scope: Scope
    workspace_key: str
    project_key: str
    project_name: str
    counts: dict[str, int]
    products: tuple[str, ...]
    earliest_event: datetime | None
    latest_event: datetime | None
    event_definitions: BoundedList[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "workspace_key": self.workspace_key,
            "project_key": self.project_key,
            "project_name": self.project_name,
            "counts": self.counts,
            "products": list(self.products),
            "event_range": {
                "earliest": _iso(self.earliest_event),
                "latest": _iso(self.latest_event),
            },
            "event_definitions": self.event_definitions.to_dict(),
        }


@dataclass(frozen=True)
class AccountProfileResult:
    scope: Scope
    account_key: str
    found: bool
    properties: dict[str, object]
    last_seen_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_dict(),
            "account_key": self.account_key,
            "found": self.found,
            "state_basis": "current_profile",
            "properties": self.properties,
            "last_seen_at": _iso(self.last_seen_at),
        }


def describe_project(project: Project, *, event_definition_limit: int = 50) -> ProjectDescription:
    limit = validate_limit(event_definition_limit)
    event_query = Event.objects.filter(project=project)
    definition_query = EventDefinition.objects.filter(project=project).order_by(
        "product_key", "name"
    )
    definition_count = definition_query.count()
    definitions = [
        {
            "product": row["product_key"],
            "name": row["name"],
            "description": row["description"],
            "owner": row["owner"],
            "status": row["status"],
            "last_seen_at": _iso(row["last_seen_at"]),
        }
        for row in definition_query.values(
            "product_key", "name", "description", "owner", "status", "last_seen_at"
        )[:limit]
    ]
    bounds = event_query.aggregate(earliest=Min("timestamp"), latest=Max("timestamp"))
    products = tuple(
        definition_query.exclude(product_key="")
        .values_list("product_key", flat=True)
        .distinct()
        .order_by("product_key")
    )
    return ProjectDescription(
        scope=Scope(project.pk),
        workspace_key=project.workspace.key,
        project_key=project.key,
        project_name=project.name,
        counts={
            "events": event_query.count(),
            "group_profiles": GroupProfile.objects.filter(project=project).count(),
            "event_definitions": definition_count,
        },
        products=products,
        earliest_event=bounds["earliest"],
        latest_event=bounds["latest"],
        event_definitions=BoundedList.from_items(
            definitions, limit=limit, total_count=definition_count
        ),
    )


def _validate_account_key(account_key: str) -> str:
    if (
        not isinstance(account_key, str)
        or not account_key.strip()
        or len(account_key) > GROUP_KEY_MAX_LENGTH
    ):
        raise AnalyticsInputError(
            f"account_key must be a nonblank string up to {GROUP_KEY_MAX_LENGTH} characters"
        )
    return account_key


def get_account_profile(project: Project, account_key: str) -> AccountProfileResult:
    account_key = _validate_account_key(account_key)
    row = (
        GroupProfile.objects.filter(
            project=project,
            group_type="account",
            group_key=account_key,
        )
        .values("properties", "last_seen_at")
        .first()
    )
    return AccountProfileResult(
        scope=Scope(project.pk),
        account_key=account_key,
        found=row is not None,
        properties=dict(row["properties"]) if row else {},
        last_seen_at=row["last_seen_at"] if row else None,
    )

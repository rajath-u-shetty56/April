from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

MAX_QUERY_WINDOW = timedelta(days=366)


class AnalyticsInputError(ValueError):
    """A sanitized error caused by an analytics query argument."""


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime

    @classmethod
    def create(cls, start: datetime, end: datetime) -> TimeRange:
        if start.tzinfo is None or start.utcoffset() is None:
            raise AnalyticsInputError("start must be timezone-aware")
        if end.tzinfo is None or end.utcoffset() is None:
            raise AnalyticsInputError("end must be timezone-aware")
        normalized_start = start.astimezone(UTC)
        normalized_end = end.astimezone(UTC)
        if normalized_start >= normalized_end:
            raise AnalyticsInputError("start must be before end")
        if normalized_end - normalized_start > MAX_QUERY_WINDOW:
            raise AnalyticsInputError("query periods are limited to 366 days")
        return cls(start=normalized_start, end=normalized_end)

    def as_dict(self) -> dict[str, str]:
        return {"start": _iso_utc(self.start), "end": _iso_utc(self.end)}


@dataclass(frozen=True)
class Scope:
    project_id: UUID

    def to_dict(self) -> dict[str, str]:
        return {"project_id": str(self.project_id)}


def validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise AnalyticsInputError("limit must be between 1 and 200")
    return limit


@dataclass(frozen=True)
class BoundedList[T]:
    items: tuple[T, ...]
    returned_count: int
    total_count: int
    truncated: bool

    @classmethod
    def from_items(
        cls,
        items: Sequence[T],
        *,
        limit: int,
        total_count: int | None = None,
    ) -> BoundedList[T]:
        validate_limit(limit)
        available = len(items)
        full_count = available if total_count is None else total_count
        if full_count < available:
            raise AnalyticsInputError("total_count cannot be smaller than the supplied items")
        returned = tuple(items[:limit])
        return cls(
            items=returned,
            returned_count=len(returned),
            total_count=full_count,
            truncated=full_count > len(returned),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "items": list(self.items),
            "returned_count": self.returned_count,
            "total_count": self.total_count,
            "truncated": self.truncated,
        }

from datetime import UTC, datetime, timedelta, timezone

import pytest

from analytics_platform.analytics.contracts import (
    AnalyticsInputError,
    BoundedList,
    TimeRange,
    validate_limit,
)


def test_time_range_normalizes_aware_datetimes_to_utc():
    offset = timezone(timedelta(hours=5, minutes=30))

    period = TimeRange.create(
        datetime(2026, 9, 1, 5, 30, tzinfo=offset),
        datetime(2026, 9, 2, 5, 30, tzinfo=offset),
    )

    assert period.as_dict() == {
        "start": "2026-09-01T00:00:00Z",
        "end": "2026-09-02T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 9, 1), datetime(2026, 9, 2, tzinfo=UTC)),
        (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2)),
        (
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 2, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
        ),
    ],
)
def test_time_range_rejects_naive_or_non_increasing_bounds(start, end):
    with pytest.raises(AnalyticsInputError):
        TimeRange.create(start, end)


def test_time_range_rejects_windows_longer_than_366_days():
    with pytest.raises(AnalyticsInputError, match="limited to 366 days"):
        TimeRange.create(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 3, tzinfo=UTC),
        )


def test_bounded_list_reports_full_population():
    result = BoundedList.from_items(["alpha", "bravo", "charlie"], limit=2)

    assert result.items == ("alpha", "bravo")
    assert result.returned_count == 2
    assert result.total_count == 3
    assert result.truncated is True


@pytest.mark.parametrize("limit", [1, 200])
def test_validate_limit_accepts_boundaries(limit):
    assert validate_limit(limit) == limit


@pytest.mark.parametrize("limit", [0, 201])
def test_validate_limit_rejects_values_outside_boundaries(limit):
    with pytest.raises(AnalyticsInputError, match="between 1 and 200"):
        validate_limit(limit)

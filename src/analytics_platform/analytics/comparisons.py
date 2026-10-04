from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from analytics_platform.analytics.contracts import BoundedList, TimeRange


@dataclass(frozen=True)
class QuerySnapshot:
    rows: BoundedList[dict[str, object]]
    all_rows: tuple[dict[str, object], ...]


def compare_snapshots(
    current: QuerySnapshot,
    baseline: QuerySnapshot,
    *,
    current_period: TimeRange,
    baseline_period: TimeRange,
    dimension_labels: tuple[str, ...],
    aggregation_labels: tuple[str, ...],
    aggregation_empty_values: dict[str, object],
    limit: int,
) -> dict[str, object]:
    current_map = {_row_key(row, dimension_labels): row for row in current.all_rows}
    baseline_map = {_row_key(row, dimension_labels): row for row in baseline.all_rows}
    if not dimension_labels:
        current_map = {(): current.all_rows[0]}
        baseline_map = {(): baseline.all_rows[0]}
        keys = [()]
        total_count = 1
    else:
        keys = sorted(
            set(current_map) | set(baseline_map),
            key=lambda key: tuple((value[0] == "null", value[1]) for value in key),
        )
        total_count = len(keys)

    changes = []
    for key in keys[:limit]:
        current_row = current_map.get(key)
        baseline_row = baseline_map.get(key)
        current_values = {
            label: (
                current_row.get(label)
                if current_row is not None
                else aggregation_empty_values[label]
            )
            for label in aggregation_labels
        }
        baseline_values = {
            label: (
                baseline_row.get(label)
                if baseline_row is not None
                else aggregation_empty_values[label]
            )
            for label in aggregation_labels
        }
        delta_values = {}
        percent_values = {}
        for label in aggregation_labels:
            current_value = current_values[label]
            baseline_value = baseline_values[label]
            if _is_numeric(current_value) and _is_numeric(baseline_value):
                delta = current_value - baseline_value
                delta_values[label] = delta
                percent_values[label] = (
                    round(delta * 100 / abs(baseline_value), 2) if baseline_value else None
                )
            else:
                delta_values[label] = None
                percent_values[label] = None
        dimension_row = current_row or baseline_row or {}
        item = {label: dimension_row.get(label) for label in dimension_labels}
        item.update(
            current=current_values,
            baseline=baseline_values,
            delta=delta_values,
            percent_change=percent_values,
        )
        changes.append(item)
    return {
        "current_period": current_period.as_dict(),
        "baseline_period": baseline_period.as_dict(),
        "current": current.rows.to_dict(),
        "baseline": baseline.rows.to_dict(),
        "changes": BoundedList.from_items(changes, limit=limit, total_count=total_count).to_dict(),
    }


def _row_key(row, labels):
    return tuple(_dimension_key(row.get(label)) for label in labels)


def _dimension_key(value):
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("boolean", str(value))
    if isinstance(value, (int, float)):
        return ("number", str(value))
    if isinstance(value, (datetime, date)):
        return ("date", value.isoformat())
    if isinstance(value, (dict, list)):
        return ("json", json.dumps(value, sort_keys=True, separators=(",", ":")))
    return ("string", str(value))


def _is_numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)

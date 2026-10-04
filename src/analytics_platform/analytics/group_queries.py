from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from uuid import UUID

from django.db.models.fields.json import KeyTransform

from analytics_platform.analytics.comparisons import QuerySnapshot, compare_snapshots
from analytics_platform.analytics.contracts import (
    AnalyticsInputError,
    BoundedList,
    Scope,
    TimeRange,
    validate_limit,
)
from analytics_platform.analytics.query import (
    MAX_COMPARISON_GROUPS,
    MAX_FILTER_VALUES,
    query_group_buckets,
    query_matching_event_rows,
)
from analytics_platform.catalog.models import Project
from analytics_platform.common.property_observations import observed_non_null_type
from analytics_platform.events.models import Event
from analytics_platform.group_analytics.models import (
    GROUP_KEY_MAX_LENGTH,
    GROUP_TYPE_MAX_LENGTH,
    GroupProfile,
    GroupPropertyDefinition,
)

GROUP_STATE_BASES = frozenset({"current", "period_start", "period_end", "event_time"})
HISTORICAL_PROFILE_RELIABILITY = (
    "conditional_on_complete_correctly_timestamped_groupidentify_history"
)
MAX_GROUP_ACTIVITY_RULES = 8
MAX_FUNNEL_STEPS = 10
MAX_GROUP_HISTORY_ROWS = 100_000
MAX_GROUP_STATE_GROUPS = 20_000


@dataclass(frozen=True)
class ActivityRule:
    label: str
    event: str
    product: str
    filters: tuple[dict[str, object], ...]

    def to_dict(self):
        return {"label": self.label, "event": self.event, "product": self.product}


@dataclass(frozen=True)
class GroupTimeline:
    keys: tuple[tuple[datetime, UUID], ...]
    states: tuple[dict[str, object], ...]

    def state_at(self, timestamp: datetime, event_uuid: UUID) -> dict[str, object]:
        index = bisect_right(self.keys, (timestamp, event_uuid)) - 1
        return self.states[index] if index >= 0 else {}


@dataclass(frozen=True)
class GroupActivityResult:
    scope: Scope
    period: TimeRange
    group_type: str
    state_basis: str
    match: str
    activity_rules: tuple[ActivityRule, ...]
    groups: BoundedList[dict[str, object]]
    all_groups: tuple[dict[str, object], ...]
    distinct_id_overlap: dict[str, object] | None

    def to_dict(self):
        result = {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "group_type": self.group_type,
            "state_basis": self.state_basis,
            "match": self.match,
            "activity_rules": [rule.to_dict() for rule in self.activity_rules],
            "groups": self.groups.to_dict(),
            "historical_profile_reliability": (
                HISTORICAL_PROFILE_RELIABILITY if self.state_basis != "current" else None
            ),
        }
        if self.distinct_id_overlap is not None:
            result["distinct_id_overlap"] = self.distinct_id_overlap
        return result


def analyze_group_state(
    project: Project,
    *,
    group_type: str,
    state_basis: str,
    group_key: str | None = None,
    period: TimeRange | None = None,
    event_time: datetime | None = None,
    group_filters: Sequence[Mapping[str, object]] = (),
    limit: int = 50,
):
    group_type = _group_type(group_type)
    state_basis = _state_basis(state_basis)
    limit = validate_limit(limit)
    group_filters = _compile_group_filters(project, group_type, group_filters)
    requested_key = _group_key(group_key) if group_key is not None else None
    state_map, as_of = _group_state_map(
        project,
        group_type,
        state_basis=state_basis,
        period=period,
        event_time=event_time,
        group_key=requested_key,
    )
    visible_names = set(
        GroupPropertyDefinition.objects.filter(
            project=project,
            group_type=group_type,
            status__in=("visible", "verified"),
        ).values_list("property_name", flat=True)
    )
    if requested_key is not None:
        state_map = {requested_key: state_map[requested_key]} if requested_key in state_map else {}

    matched = [
        {"group_key": key, "properties": _visible_state(properties, visible_names)}
        for key, properties in sorted(state_map.items())
        if _matches_group_filters(properties, group_filters)
    ]
    group_limit = 1 if requested_key is not None else limit
    result = {
        "scope": Scope(project.pk).to_dict(),
        "group_type": group_type,
        "state_basis": state_basis,
        "state_as_of": as_of,
        "historical_profile_reliability": (
            HISTORICAL_PROFILE_RELIABILITY if state_basis != "current" else None
        ),
        "groups": BoundedList.from_items(
            matched,
            limit=group_limit,
            total_count=len(matched),
        ).to_dict(),
    }
    if period is not None:
        result["period"] = period.as_dict()
    if requested_key is not None:
        result["requested_group_key"] = requested_key
        result["found"] = bool(matched)
    return result


def query_group_activity(
    project: Project,
    *,
    group_type: str,
    period: TimeRange,
    state_basis: str,
    activity_rules: Sequence[Mapping[str, object]],
    match: str,
    group_filters: Sequence[Mapping[str, object]] = (),
    include_distinct_id_overlap: bool = False,
    limit: int = 50,
) -> GroupActivityResult:
    return _compute_group_activity(
        project,
        group_type=group_type,
        period=period,
        state_basis=state_basis,
        activity_rules=activity_rules,
        match=match,
        group_filters=group_filters,
        include_distinct_id_overlap=include_distinct_id_overlap,
        limit=limit,
    )


def compare_group_activity(
    project: Project,
    *,
    group_type: str,
    period: TimeRange,
    comparison_period: TimeRange,
    state_basis: str,
    activity_rules: Sequence[Mapping[str, object]],
    match: str,
    group_filters: Sequence[Mapping[str, object]] = (),
    limit: int = 50,
):
    if period.end - period.start != comparison_period.end - comparison_period.start:
        raise AnalyticsInputError("current and comparison periods must have equal duration")
    current = _compute_group_activity(
        project,
        group_type=group_type,
        period=period,
        state_basis=state_basis,
        activity_rules=activity_rules,
        match=match,
        group_filters=group_filters,
        include_distinct_id_overlap=False,
        limit=limit,
    )
    baseline = _compute_group_activity(
        project,
        group_type=group_type,
        period=comparison_period,
        state_basis=state_basis,
        activity_rules=activity_rules,
        match=match,
        group_filters=group_filters,
        include_distinct_id_overlap=False,
        limit=limit,
    )
    metric_labels = tuple(
        f"{rule.label}.{metric}"
        for rule in current.activity_rules
        for metric in ("events", "distinct_ids")
    )
    current_snapshot = _activity_snapshot(current, limit)
    baseline_snapshot = _activity_snapshot(baseline, limit)
    comparison = compare_snapshots(
        current_snapshot,
        baseline_snapshot,
        current_period=period,
        baseline_period=comparison_period,
        dimension_labels=("group_key",),
        aggregation_labels=metric_labels,
        aggregation_empty_values={label: 0 for label in metric_labels},
        limit=validate_limit(limit),
    )
    return {
        "scope": Scope(project.pk).to_dict(),
        "group_type": current.group_type,
        "state_basis": current.state_basis,
        "match": current.match,
        "activity_rules": [rule.to_dict() for rule in current.activity_rules],
        "comparison": comparison,
    }


def analyze_group_adoption(
    project: Project,
    *,
    group_type: str,
    period: TimeRange,
    state_basis: str,
    eligibility_filters: Sequence[Mapping[str, object]],
    activity_rule: Mapping[str, object],
    limit: int = 50,
):
    group_type = _group_type(group_type)
    state_basis = _state_basis(state_basis)
    compiled_eligibility = _compile_group_filters(project, group_type, eligibility_filters)
    if not compiled_eligibility:
        raise AnalyticsInputError("group adoption requires an explicit eligibility filter")
    rules = _compile_activity_rules(project, [activity_rule], group_type, minimum=1)
    rule = rules[0]
    event_depths: dict[str, int] = defaultdict(int)
    if state_basis == "event_time":
        initial_state, _ = _group_state_map(
            project,
            group_type,
            state_basis="period_start",
            period=period,
        )
        eligible = {
            key
            for key, properties in initial_state.items()
            if _matches_group_filters(properties, compiled_eligibility)
        }
        state = {key: dict(properties) for key, properties in initial_state.items()}
        for row in _groupidentify_rows(project, group_type, start=period.start, end=period.end):
            properties = row["properties"]
            key = properties.get("$group_key")
            changes = properties.get("$group_set")
            if not isinstance(key, str) or not isinstance(changes, dict):
                continue
            state.setdefault(key, {}).update(changes)
            if _matches_group_filters(state[key], compiled_eligibility):
                eligible.add(key)
        timelines = _load_group_timelines(project, group_type, before=period.end)
        eligible_keys = sorted(eligible)
        adopting = set()
        event_rows = query_matching_event_rows(
            project,
            period=period,
            filters=_event_filters(rule),
            group_type=group_type,
        )
        for row in event_rows:
            key = row["groups"].get(group_type)
            if not isinstance(key, str) or key not in eligible:
                continue
            timeline = timelines.get(key)
            properties = timeline.state_at(row["timestamp"], row["uuid"]) if timeline else {}
            if _matches_group_filters(properties, compiled_eligibility):
                adopting.add(key)
                event_depths[key] += 1
        adopted_keys = sorted(adopting)
    else:
        state_map, _ = _group_state_map(
            project,
            group_type,
            state_basis=state_basis,
            period=period,
        )
        eligible_keys = sorted(
            key
            for key, properties in state_map.items()
            if _matches_group_filters(properties, compiled_eligibility)
        )
        activity_rows = query_group_buckets(
            project,
            period=period,
            group_type=group_type,
            filters=_event_filters(rule),
        )
        adopted_keys = sorted(
            key for key in eligible_keys if _find_group_row(activity_rows, key) is not None
        )
        event_depths = {
            str(row["group_key"]): int(row["events"])
            for row in activity_rows
            if row["group_key"] in set(eligible_keys)
        }
    denominator = len(eligible_keys)
    numerator = len(adopted_keys)
    limit = validate_limit(limit)
    return {
        "scope": Scope(project.pk).to_dict(),
        "period": period.as_dict(),
        "group_type": group_type,
        "state_basis": state_basis,
        "eligibility_basis": (
            "eligible_at_any_point_during_period"
            if state_basis == "event_time"
            else "groups_matching_explicit_group_property_filters_at_snapshot"
        ),
        "historical_profile_reliability": (
            HISTORICAL_PROFILE_RELIABILITY if state_basis != "current" else None
        ),
        "eligibility_filters": [dict(item) for item in eligibility_filters],
        "activity_rule": rule.to_dict(),
        "numerator": numerator,
        "denominator": denominator,
        "adoption_rate": round(numerator * 100 / denominator, 2) if denominator else 0.0,
        "event_depth": {
            "total_matching_events": sum(event_depths.values()),
            "median_events_per_adopting_group": (
                median(event_depths.values()) if event_depths else 0
            ),
            "groups": BoundedList.from_items(
                [{"group_key": key, "event_count": event_depths[key]} for key in adopted_keys],
                limit=limit,
                total_count=len(adopted_keys),
            ).to_dict(),
        },
        "adopting_group_keys": BoundedList.from_items(
            adopted_keys,
            limit=limit,
            total_count=numerator,
        ).to_dict(),
        "eligible_group_keys": BoundedList.from_items(
            eligible_keys,
            limit=limit,
            total_count=denominator,
        ).to_dict(),
    }


def analyze_group_funnel(
    project: Project,
    *,
    group_type: str | None,
    period: TimeRange,
    state_basis: str | None = None,
    steps: Sequence[Mapping[str, object]],
    correlation: Mapping[str, object] | None = None,
    group_filters: Sequence[Mapping[str, object]] = (),
):
    requested_group_type = _group_type(group_type) if group_type is not None else None
    if state_basis is not None:
        state_basis = _state_basis(state_basis)
    if correlation is None:
        if requested_group_type is None:
            raise AnalyticsInputError("group_type is required when funnel correlation is omitted")
        correlation_spec = {"kind": "group_key", "group_type": requested_group_type}
    else:
        correlation_spec = _mapping(correlation, "correlation")
    correlation_kind = _choice(
        correlation_spec.get("kind"),
        {"group_key", "distinct_id", "property"},
        "funnel correlation kind",
    )
    if correlation_kind == "group_key":
        _reject_extra_keys(correlation_spec, {"kind", "group_type"}, "correlation")
        correlation_group_type = _group_type(
            correlation_spec.get("group_type", requested_group_type)
        )
        requested_group_type = correlation_group_type
        correlation_property_name = None
    elif correlation_kind == "distinct_id":
        _reject_extra_keys(correlation_spec, {"kind"}, "correlation")
        correlation_group_type = requested_group_type
        correlation_property_name = None
    else:
        _reject_extra_keys(correlation_spec, {"kind", "property_name"}, "correlation")
        correlation_property_name = _nonblank(
            correlation_spec.get("property_name"), "correlation.property_name"
        )
        correlation_group_type = requested_group_type
    if group_filters and correlation_group_type is None:
        raise AnalyticsInputError("group_type is required to apply funnel group filters")
    if group_filters and state_basis is None:
        raise AnalyticsInputError("state_basis is required when funnel group filters are supplied")
    rules = _compile_activity_rules(
        project,
        steps,
        correlation_group_type,
        minimum=2,
        maximum=MAX_FUNNEL_STEPS,
    )
    filters = (
        _compile_group_filters(project, correlation_group_type, group_filters)
        if group_filters
        else ()
    )
    state_map = None
    if filters and state_basis != "event_time":
        state_map, _ = _group_state_map(
            project,
            correlation_group_type,
            state_basis=state_basis,
            period=period,
        )
    timelines = (
        _load_group_timelines(project, correlation_group_type, before=period.end)
        if filters and state_basis == "event_time"
        else None
    )

    cohort_events: dict[tuple[str, object], list[tuple[int, datetime, UUID]]] = defaultdict(list)
    for step_index, rule in enumerate(rules):
        for row in query_matching_event_rows(
            project,
            period=period,
            filters=_event_filters(rule),
            group_type=(
                correlation_group_type if correlation_kind == "group_key" or filters else None
            ),
            correlation_property_name=correlation_property_name,
        ):
            correlation_value = (
                row["groups"].get(correlation_group_type)
                if correlation_kind == "group_key"
                else row["distinct_id"]
                if correlation_kind == "distinct_id"
                else row["properties"].get(correlation_property_name)
            )
            cohort_key = _funnel_correlation_key(
                correlation_value,
                allow_number=correlation_kind == "property",
            )
            if cohort_key is None:
                continue
            if filters:
                state_group_key = row["groups"].get(correlation_group_type)
                if not isinstance(state_group_key, str):
                    continue
                if state_basis == "event_time":
                    timeline = (timelines or {}).get(state_group_key)
                    properties = (
                        timeline.state_at(row["timestamp"], row["uuid"]) if timeline else {}
                    )
                else:
                    properties = (state_map or {}).get(state_group_key, {})
                    if state_group_key not in (state_map or {}):
                        continue
                if not _matches_group_filters(properties, filters):
                    continue
            cohort_events[cohort_key].append((step_index, row["timestamp"], row["uuid"]))

    reached_by_step: list[list[tuple[str, object]]] = [[] for _ in rules]
    for cohort_key, events in cohort_events.items():
        next_step = 0
        previous_occurrence = None
        for step_index, _timestamp, _event_uuid in sorted(events, key=lambda row: (row[1], row[2])):
            occurrence = (_timestamp, _event_uuid)
            if step_index == next_step and (
                previous_occurrence is None or occurrence > previous_occurrence
            ):
                next_step += 1
                previous_occurrence = occurrence
                if next_step == len(rules):
                    break
        for step_index in range(next_step):
            reached_by_step[step_index].append(cohort_key)
    reached_by_step = [sorted(cohorts) for cohorts in reached_by_step]
    started = len(reached_by_step[0])
    step_results = []
    for rule, cohorts in zip(rules, reached_by_step, strict=True):
        step_results.append(
            {
                **rule.to_dict(),
                "cohort_count": len(cohorts),
                "conversion_rate_from_start": (
                    round(len(cohorts) * 100 / started, 2) if started else 0.0
                ),
            }
        )
    completed = len(reached_by_step[-1])
    correlation_metadata = {"kind": correlation_kind}
    if correlation_kind == "group_key":
        correlation_metadata["group_type"] = correlation_group_type
    elif correlation_property_name is not None:
        correlation_metadata["property_name"] = correlation_property_name
    return {
        "scope": Scope(project.pk).to_dict(),
        "period": period.as_dict(),
        "group_type": correlation_group_type,
        "correlation": correlation_metadata,
        "state_basis": state_basis,
        "historical_profile_reliability": (
            HISTORICAL_PROFILE_RELIABILITY if filters and state_basis != "current" else None
        ),
        "started_cohort_count": started,
        "completed_cohort_count": completed,
        "completion_rate": round(completed * 100 / started, 2) if started else 0.0,
        "steps": step_results,
    }


def analyze_group_transitions(
    project: Project,
    *,
    group_type: str,
    property_name: str,
    period: TimeRange,
    state_basis: str,
    group_key: str | None = None,
    limit: int = 50,
):
    group_type = _group_type(group_type)
    state_basis = _choice(state_basis, {"event_time"}, "group transition state basis")
    prop = _resolve_group_property(project, group_type, property_name)
    requested_key = _group_key(group_key) if group_key is not None else None
    starting_timelines = _load_group_timelines(
        project,
        group_type,
        before=period.start,
        inclusive=False,
        group_key=requested_key,
    )
    state_map = {
        key: dict(timeline.states[-1])
        for key, timeline in starting_timelines.items()
        if timeline.states
    }
    rows = _groupidentify_rows(
        project,
        group_type,
        start=period.start,
        end=period.end,
        group_key=requested_key,
    )
    transitions = []
    for row in rows:
        properties = row["properties"]
        key = properties.get("$group_key")
        changes = properties.get("$group_set")
        if (
            not isinstance(key, str)
            or (requested_key is not None and key != requested_key)
            or not isinstance(changes, dict)
            or prop.property_name not in changes
        ):
            continue
        prior_state = state_map.setdefault(key, {})
        old_value = prior_state.get(prop.property_name)
        new_value = changes[prop.property_name]
        if not _strict_equal(old_value, new_value):
            transitions.append(
                {
                    "group_key": key,
                    "timestamp": _iso(row["timestamp"]),
                    "previous_value": old_value,
                    "new_value": new_value,
                }
            )
        prior_state.update(changes)
    total_count = len(transitions)
    return {
        "scope": Scope(project.pk).to_dict(),
        "period": period.as_dict(),
        "group_type": group_type,
        "property_name": prop.property_name,
        "state_basis": state_basis,
        "historical_profile_reliability": HISTORICAL_PROFILE_RELIABILITY,
        "transitions": BoundedList.from_items(
            transitions,
            limit=validate_limit(limit),
            total_count=total_count,
        ).to_dict(),
    }


def _compute_group_activity(
    project,
    *,
    group_type,
    period,
    state_basis,
    activity_rules,
    match,
    group_filters,
    include_distinct_id_overlap,
    limit,
):
    group_type = _group_type(group_type)
    state_basis = _state_basis(state_basis)
    match = _choice(match, {"all", "any"}, "match mode")
    rules = _compile_activity_rules(project, activity_rules, group_type, minimum=2)
    limit = validate_limit(limit)
    filters = _compile_group_filters(project, group_type, group_filters)
    state_map = None
    allowed_group_keys = None
    if filters and state_basis != "event_time":
        state_map, _ = _group_state_map(
            project,
            group_type,
            state_basis=state_basis,
            period=period,
        )
        allowed_group_keys = {
            key
            for key, properties in state_map.items()
            if _matches_group_filters(properties, filters)
        }
    timelines = (
        _load_group_timelines(project, group_type, before=period.end)
        if filters and state_basis == "event_time"
        else None
    )
    raw_needed = include_distinct_id_overlap or bool(filters and state_basis == "event_time")
    rule_groups: dict[str, dict[str, dict[str, int]]] = {}
    id_sets: dict[str, set[str]] = {}

    for rule in rules:
        if filters and state_basis == "event_time":
            group_counts: dict[str, dict[str, object]] = defaultdict(
                lambda: {"events": 0, "ids": set()}
            )
        else:
            group_counts = {
                str(row["group_key"]): {
                    "events": int(row["events"]),
                    "ids": int(row["distinct_ids"]),
                }
                for row in query_group_buckets(
                    project,
                    period=period,
                    group_type=group_type,
                    filters=_event_filters(rule),
                )
            }
        actors = set()
        if raw_needed:
            event_rows = query_matching_event_rows(
                project,
                period=period,
                filters=_event_filters(rule),
                group_type=group_type,
            )
            for row in event_rows:
                key = row["groups"].get(group_type)
                if not isinstance(key, str):
                    continue
                if filters:
                    if state_basis == "event_time":
                        timeline = (timelines or {}).get(key)
                        state = timeline.state_at(row["timestamp"], row["uuid"]) if timeline else {}
                        if not _matches_group_filters(state, filters):
                            continue
                    elif key not in (allowed_group_keys or set()):
                        continue
                actors.add(row["distinct_id"])
                if filters and state_basis == "event_time":
                    group_counts[key]["events"] += 1
                    group_counts[key]["ids"].add(row["distinct_id"])
            id_sets[rule.label] = actors
        if filters and state_basis == "event_time":
            rule_groups[rule.label] = {
                key: {
                    "events": int(counts["events"]),
                    "distinct_ids": len(counts["ids"]),
                }
                for key, counts in group_counts.items()
            }
        else:
            rule_groups[rule.label] = {
                key: {
                    "events": int(counts["events"]),
                    "distinct_ids": int(counts["ids"]),
                }
                for key, counts in group_counts.items()
                if allowed_group_keys is None or key in allowed_group_keys
            }

    candidate_keys = set().union(*(set(rows) for rows in rule_groups.values()))
    if match == "all":
        candidate_keys = {
            key for key in candidate_keys if all(key in rule_groups[rule.label] for rule in rules)
        }
    groups = []
    for key in sorted(candidate_keys):
        activity = {
            rule.label: rule_groups[rule.label][key]
            for rule in rules
            if key in rule_groups[rule.label]
        }
        groups.append(
            {
                "group_key": key,
                "matched_labels": [rule.label for rule in rules if rule.label in activity],
                "activity": activity,
            }
        )
    if len(groups) > MAX_COMPARISON_GROUPS:
        raise AnalyticsInputError(
            f"group activity is limited to {MAX_COMPARISON_GROUPS} matching groups; add filters"
        )
    overlap = None
    if include_distinct_id_overlap:
        intersection = set.intersection(*(id_sets[rule.label] for rule in rules))
        population = set.union(*(id_sets[rule.label] for rule in rules))
        overlap = {
            "count": len(intersection),
            "users_by_rule": {rule.label: len(id_sets[rule.label]) for rule in rules},
            "population_count": len(population),
            "basis": "exact distinct_id string equality",
            "identity_resolution_performed": False,
            "zero_result_interpretation": (
                "A zero means no exact identifier equality was observed; it does not establish "
                "that the underlying people are different."
            ),
        }
    bounded = BoundedList.from_items(groups, limit=limit, total_count=len(groups))
    return GroupActivityResult(
        scope=Scope(project.pk),
        period=period,
        group_type=group_type,
        state_basis=state_basis,
        match=match,
        activity_rules=rules,
        groups=bounded,
        all_groups=tuple(groups),
        distinct_id_overlap=overlap,
    )


def _activity_snapshot(result: GroupActivityResult, limit):
    rows = []
    for item in result.all_groups:
        row = {"group_key": item["group_key"]}
        for rule in result.activity_rules:
            counts = item["activity"].get(rule.label, {})
            row[f"{rule.label}.events"] = counts.get("events", 0)
            row[f"{rule.label}.distinct_ids"] = counts.get("distinct_ids", 0)
        rows.append(row)
    return QuerySnapshot(
        rows=BoundedList.from_items(rows, limit=limit, total_count=len(rows)),
        all_rows=tuple(rows),
    )


def _compile_activity_rules(
    project, values, group_type, *, minimum, maximum=MAX_GROUP_ACTIVITY_RULES
):
    if not isinstance(values, (list, tuple)) or not minimum <= len(values) <= maximum:
        raise AnalyticsInputError(
            f"activity rules must contain between {minimum} and {maximum} items"
        )
    rules = []
    labels = set()
    for index, raw in enumerate(values):
        item = _mapping(raw, f"activity_rules[{index}]")
        _reject_extra_keys(
            item, {"label", "event", "product", "filters"}, f"activity_rules[{index}]"
        )
        label = _label(item.get("label"), f"activity_rules[{index}].label")
        if label in labels:
            raise AnalyticsInputError("activity rule labels must be unique")
        labels.add(label)
        event = _nonblank(item.get("event"), f"activity_rules[{index}].event")
        product = item.get("product")
        if not isinstance(product, str):
            raise AnalyticsInputError(f"activity_rules[{index}].product must be a string")
        raw_filters = item.get("filters", ())
        if not isinstance(raw_filters, (list, tuple)):
            raise AnalyticsInputError(f"activity_rules[{index}].filters must be a list")
        property_filters = []
        for filter_index, raw_filter in enumerate(raw_filters):
            predicate = _mapping(raw_filter, f"activity_rules[{index}].filters[{filter_index}]")
            _reject_extra_keys(
                predicate,
                {"property_name", "operator", "value"},
                f"activity_rules[{index}].filters[{filter_index}]",
            )
            property_filters.append(
                {
                    "field": "property",
                    **predicate,
                    "event": event,
                    "product": product,
                }
            )
        rules.append(
            ActivityRule(
                label=label,
                event=event,
                product=product,
                filters=tuple(property_filters),
            )
        )
    return tuple(rules)


def _funnel_correlation_key(value, *, allow_number):
    if isinstance(value, str):
        return ("string", value) if value else None
    if allow_number and not isinstance(value, bool) and isinstance(value, (int, float)):
        return ("number", value)
    if value is None:
        return None
    expected = "strings or numbers" if allow_number else "non-empty strings"
    raise AnalyticsInputError(f"funnel correlation values must be {expected}")


def _event_filters(rule):
    return (
        {"field": "event", "operator": "eq", "value": rule.event},
        {"field": "product", "operator": "eq", "value": rule.product},
        *rule.filters,
    )


def _compile_group_filters(project, group_type, values):
    if not isinstance(values, (list, tuple)):
        raise AnalyticsInputError("group_filters must be a list")
    if len(values) > 20:
        raise AnalyticsInputError("group_filters is limited to 20 items")
    compiled = []
    for index, raw in enumerate(values):
        item = _mapping(raw, f"group_filters[{index}]")
        _reject_extra_keys(item, {"property_name", "operator", "value"}, f"group_filters[{index}]")
        property_name = _nonblank(item.get("property_name"), "group property name")
        prop = _resolve_group_property(project, group_type, property_name)
        operator = _choice(
            item.get("operator"),
            {"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "exists"},
            "group property operator",
        )
        if operator == "exists":
            if "value" in item:
                raise AnalyticsInputError("exists group filters must omit value")
        elif "value" not in item:
            raise AnalyticsInputError("group property filter value is required")
        value = item.get("value")
        if operator in {"in", "not_in"} and (not isinstance(value, (list, tuple)) or not value):
            raise AnalyticsInputError(f"{operator} group filter value must be a non-empty list")
        if operator in {"in", "not_in"} and len(value) > MAX_FILTER_VALUES:
            raise AnalyticsInputError(
                f"{operator} group filters are limited to {MAX_FILTER_VALUES} values"
            )
        if operator in {"eq", "ne", "in", "not_in"}:
            candidates = value if operator in {"in", "not_in"} else (value,)
            observed_types = set(prop.observed_non_null_types)
            for candidate in candidates:
                candidate_type = observed_non_null_type(candidate)
                if (candidate_type is None and not prop.nullable) or (
                    candidate_type is not None and candidate_type not in observed_types
                ):
                    raise AnalyticsInputError(
                        "group property filter value type is incompatible with observed "
                        "property types"
                    )
        if operator in {"gt", "gte", "lt", "lte"}:
            types = set(prop.observed_non_null_types)
            if prop.has_type_conflict or types not in ({"number"}, {"string"}):
                raise AnalyticsInputError(
                    "range operators require one discovered non-null string or number type"
                )
            if types == {"number"} and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise AnalyticsInputError("numeric group property filters require a number value")
            if types == {"string"} and not isinstance(value, str):
                raise AnalyticsInputError("string group property filters require a string value")
        compiled.append({"property_name": prop.property_name, "operator": operator, "value": value})
    return tuple(compiled)


def _matches_group_filters(properties, filters):
    for item in filters:
        name = item["property_name"]
        operator = item["operator"]
        present = name in properties
        actual = properties.get(name)
        expected = item.get("value")
        if operator == "exists":
            matched = present
        elif operator == "eq":
            matched = present and _strict_equal(actual, expected)
        elif operator == "ne":
            matched = present and not _strict_equal(actual, expected)
        elif operator == "in":
            matched = present and any(_strict_equal(actual, candidate) for candidate in expected)
        elif operator == "not_in":
            matched = present and not any(
                _strict_equal(actual, candidate) for candidate in expected
            )
        elif operator == "gt":
            matched = present and actual > expected
        elif operator == "gte":
            matched = present and actual >= expected
        elif operator == "lt":
            matched = present and actual < expected
        else:
            matched = present and actual <= expected
        if not matched:
            return False
    return True


def _group_state_map(
    project,
    group_type,
    *,
    state_basis,
    period=None,
    event_time=None,
    group_key=None,
):
    if state_basis == "current":
        profiles = GroupProfile.objects.filter(project=project, group_type=group_type)
        if group_key is not None:
            profiles = profiles.filter(group_key=group_key)
        rows = profiles.values("group_key", "properties")
        _require_bounded_group_count(rows.count())
        return {row["group_key"]: dict(row["properties"]) for row in rows}, "current"
    if state_basis in {"period_start", "period_end"}:
        if period is None:
            raise AnalyticsInputError(f"{state_basis} state_basis requires a period")
        cutoff = period.start if state_basis == "period_start" else period.end
    else:
        if event_time is None or event_time.tzinfo is None or event_time.utcoffset() is None:
            raise AnalyticsInputError("event_time state_basis requires a timezone-aware event_time")
        cutoff = event_time
    timelines = _load_group_timelines(
        project,
        group_type,
        before=cutoff,
        inclusive=state_basis in {"event_time", "period_start"},
        group_key=group_key,
    )
    state_map = {key: timeline.states[-1] for key, timeline in timelines.items() if timeline.states}
    return state_map, _iso(cutoff)


def _load_group_timelines(project, group_type, *, before, inclusive=False, group_key=None):
    query = Event.objects.filter(project=project, event="$groupidentify")
    query = query.filter(timestamp__lte=before) if inclusive else query.filter(timestamp__lt=before)
    query = query.alias(
        _aq_group_type=KeyTransform("$group_type", "properties"),
        _aq_group_key=KeyTransform("$group_key", "properties"),
    ).filter(_aq_group_type=group_type)
    if group_key is not None:
        query = query.filter(_aq_group_key=group_key)
    _require_bounded_group_history(query.count())
    rows = query.order_by("timestamp", "uuid").values("uuid", "timestamp", "properties")
    state = defaultdict(dict)
    keys = defaultdict(list)
    snapshots = defaultdict(list)
    for row in rows:
        properties = row["properties"]
        group_key = properties.get("$group_key")
        changes = properties.get("$group_set")
        if not isinstance(group_key, str) or not isinstance(changes, dict):
            continue
        state[group_key].update(changes)
        keys[group_key].append((row["timestamp"], row["uuid"]))
        snapshots[group_key].append(dict(state[group_key]))
    return {key: GroupTimeline(tuple(keys[key]), tuple(snapshots[key])) for key in keys}


def _groupidentify_rows(project, group_type, *, start, end, group_key=None):
    query = (
        Event.objects.filter(
            project=project,
            event="$groupidentify",
            timestamp__gte=start,
            timestamp__lt=end,
        )
        .alias(
            _aq_group_type=KeyTransform("$group_type", "properties"),
            _aq_group_key=KeyTransform("$group_key", "properties"),
        )
        .filter(_aq_group_type=group_type)
    )
    if group_key is not None:
        query = query.filter(_aq_group_key=group_key)
    _require_bounded_group_history(query.count())
    return tuple(query.order_by("timestamp", "uuid").values("uuid", "timestamp", "properties"))


def _require_bounded_group_history(row_count):
    if row_count > MAX_GROUP_HISTORY_ROWS:
        raise AnalyticsInputError(
            "historical group analysis is limited to "
            f"{MAX_GROUP_HISTORY_ROWS} profile events; narrow the period"
        )


def _require_bounded_group_count(group_count):
    if group_count > MAX_GROUP_STATE_GROUPS:
        raise AnalyticsInputError(
            f"group state analysis is limited to {MAX_GROUP_STATE_GROUPS} groups; query one group"
        )


def _resolve_group_property(project, group_type, property_name):
    property_name = _nonblank(property_name, "group property name")
    prop = GroupPropertyDefinition.objects.filter(
        project=project,
        group_type=group_type,
        property_name_hash=GroupPropertyDefinition.hash_property_name(property_name),
    ).first()
    if prop is None or prop.property_name != property_name:
        raise AnalyticsInputError("group property was not discovered for that group type")
    if prop.status == "hidden":
        raise AnalyticsInputError(f"group property {property_name!r} is hidden")
    return prop


def _find_group_row(rows, group_key):
    return next((row for row in rows if row["group_key"] == group_key), None)


def _visible_state(properties, visible_names):
    return {key: value for key, value in properties.items() if key in visible_names}


def _strict_equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _group_type(value):
    value = _nonblank(value, "group_type")
    if len(value) > GROUP_TYPE_MAX_LENGTH:
        raise AnalyticsInputError(f"group_type must be at most {GROUP_TYPE_MAX_LENGTH} characters")
    return value


def _group_key(value):
    value = _nonblank(value, "group_key")
    if len(value) > GROUP_KEY_MAX_LENGTH:
        raise AnalyticsInputError(f"group_key must be at most {GROUP_KEY_MAX_LENGTH} characters")
    return value


def _state_basis(value):
    return _choice(value, GROUP_STATE_BASES, "group state basis")


def _choice(value, allowed, name):
    if not isinstance(value, str) or value not in allowed:
        raise AnalyticsInputError(f"unsupported {name}")
    return value


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise AnalyticsInputError(f"{name} must be an object")
    return dict(value)


def _reject_extra_keys(value, allowed, name):
    if set(value) - allowed:
        raise AnalyticsInputError(f"{name} has unsupported fields")


def _nonblank(value, name):
    if not isinstance(value, str) or not value.strip():
        raise AnalyticsInputError(f"{name} must be a nonblank string")
    return value


def _label(value, name):
    value = _nonblank(value, name)
    if len(value) > 64:
        raise AnalyticsInputError(f"{name} must be at most 64 characters")
    return value


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

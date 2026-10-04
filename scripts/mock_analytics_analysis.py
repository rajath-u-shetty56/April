#!/usr/bin/env python3
"""Run generic structured analytics queries against the synthetic validation dataset."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import django

if __package__:
    from scripts.mock_analytics_dataset import ANALYSIS_CUTOFF
else:
    from mock_analytics_dataset import ANALYSIS_CUTOFF


def _items(result: dict) -> list:
    return result["items"]


def _query_periods(cutoff):
    current = cutoff - timedelta(days=30)
    previous = current - timedelta(days=30)
    history = cutoff - timedelta(weeks=10)
    return current, previous, history


def analyze_project(project, *, cutoff=ANALYSIS_CUTOFF) -> dict:
    """Build a reusable set of structured event and group analytics results."""
    from analytics_platform.analytics.contracts import TimeRange
    from analytics_platform.analytics.group_queries import (
        analyze_group_adoption,
        analyze_group_funnel,
        analyze_group_state,
        analyze_group_transitions,
        compare_group_activity,
        query_group_activity,
    )
    from analytics_platform.analytics.query import query_events
    from analytics_platform.analytics.query_catalog import discover_analytics_catalog
    from analytics_platform.events.models import Event
    from analytics_platform.group_analytics.models import GroupProfile

    current_start, previous_start, history_start = _query_periods(cutoff)
    current_period = TimeRange.create(current_start, cutoff)
    previous_period = TimeRange.create(previous_start, current_start)
    history_period = TimeRange.create(history_start, cutoff)
    catalog = discover_analytics_catalog(
        project,
        event_definition_limit=200,
        event_property_limit=200,
        group_property_limit=200,
    )

    product_event_activity = query_events(
        project,
        period=current_period,
        filters=[{"field": "product", "operator": "exists"}],
        group_by=[
            {"kind": "product", "label": "product"},
            {"kind": "event", "label": "event"},
        ],
        aggregations=[
            {"kind": "event_count", "label": "events"},
            {"kind": "distinct_id_count", "label": "exact_distinct_ids"},
            {
                "kind": "distinct_group_count",
                "label": "groups",
                "group_type": "account",
            },
        ],
        limit=200,
    ).to_dict()
    product_activity_comparison = query_events(
        project,
        period=current_period,
        filters=[{"field": "product", "operator": "exists"}],
        group_by=[{"kind": "product", "label": "product"}],
        aggregations=[
            {"kind": "event_count", "label": "events"},
            {"kind": "distinct_id_count", "label": "exact_distinct_ids"},
        ],
        comparison_period=previous_period,
        limit=100,
    ).to_dict()

    chosen_events = {}
    for row in _items(product_event_activity["rows"]):
        product = row.get("product")
        if isinstance(product, str) and product:
            chosen_events.setdefault(product, row["event"])
    activity_rules = [
        {"label": product, "event": event, "product": product}
        for product, event in sorted(chosen_events.items())[:3]
    ]

    group_type = (
        GroupProfile.objects.filter(project=project)
        .order_by("group_type")
        .values_list("group_type", flat=True)
        .first()
    )
    group_state = None
    group_activity = None
    group_activity_comparison = None
    group_adoption = None
    group_funnel = None
    group_transitions = None
    if group_type:
        group_state = analyze_group_state(
            project,
            group_type=group_type,
            state_basis="period_end",
            period=current_period,
            limit=200,
        )
        if len(activity_rules) >= 2:
            group_activity = query_group_activity(
                project,
                group_type=group_type,
                period=current_period,
                state_basis="current",
                activity_rules=activity_rules,
                match="all",
                include_distinct_id_overlap=True,
                limit=200,
            ).to_dict()
            group_activity_comparison = compare_group_activity(
                project,
                group_type=group_type,
                period=current_period,
                comparison_period=previous_period,
                state_basis="current",
                activity_rules=activity_rules,
                match="any",
                limit=200,
            )

        visible_group_properties = catalog["group_properties"]["items"]
        if visible_group_properties and activity_rules:
            group_adoption = analyze_group_adoption(
                project,
                group_type=group_type,
                period=current_period,
                state_basis="period_end",
                eligibility_filters=[
                    {
                        "property_name": visible_group_properties[0]["property_name"],
                        "operator": "exists",
                    }
                ],
                activity_rule=activity_rules[0],
                limit=200,
            )

        product_events = {}
        for row in _items(product_event_activity["rows"]):
            if row.get("product"):
                product_events.setdefault(row["product"], []).append(row["event"])
        funnel_product = next(
            (product for product, events in sorted(product_events.items()) if len(events) >= 2),
            None,
        )
        if funnel_product:
            steps = product_events[funnel_product][:2]
            group_funnel = analyze_group_funnel(
                project,
                group_type=group_type,
                period=history_period,
                state_basis="event_time",
                steps=[
                    {"label": f"step_{index + 1}", "event": event, "product": funnel_product}
                    for index, event in enumerate(steps)
                ],
            )

        if visible_group_properties:
            group_transitions = analyze_group_transitions(
                project,
                group_type=group_type,
                property_name=visible_group_properties[0]["property_name"],
                period=history_period,
                state_basis="event_time",
                limit=200,
            )

    event_definitions = catalog["event_definitions"]["items"]
    definitions_by_product = dict(
        sorted(Counter(item["product"] for item in event_definitions).items())
    )
    groupidentify_count = Event.objects.filter(project=project, event="$groupidentify").count()
    event_count = Event.objects.filter(project=project).count()
    return {
        "scope": {
            "project_id": str(project.pk),
            "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        },
        "validation": {
            "event_count": event_count,
            "behavioral_event_count": event_count - groupidentify_count,
            "groupidentify_count": groupidentify_count,
            "group_profile_count": GroupProfile.objects.filter(project=project).count(),
            "event_definitions_by_product": definitions_by_product,
            "event_property_definition_count": catalog["counts"]["event_property_definitions"],
            "group_property_definition_count": catalog["counts"]["group_property_definitions"],
        },
        "catalog": catalog,
        "product_event_activity": product_event_activity,
        "product_activity_comparison": product_activity_comparison,
        "group_state": group_state,
        "cross_product_activity": group_activity,
        "cross_product_activity_comparison": group_activity_comparison,
        "explicit_eligibility_adoption": group_adoption,
        "generic_funnel": group_funnel,
        "group_property_transitions": group_transitions,
    }


def assert_expected_results(result: dict) -> list[str]:
    """Check the fixed synthetic dataset using generic query result contracts."""
    failures = []

    def expect(label, actual, expected):
        if actual != expected:
            failures.append(f"{label}: expected {expected!r}, got {actual!r}")

    expect("event count", result["validation"]["event_count"], 3527)
    expect("behavioral event count", result["validation"]["behavioral_event_count"], 3514)
    expect("groupidentify count", result["validation"]["groupidentify_count"], 13)
    expect("group profile count", result["validation"]["group_profile_count"], 10)
    expect(
        "event definitions by product",
        result["validation"]["event_definitions_by_product"],
        {"bi": 4, "contact_center": 13, "helpdesk": 4, "rise": 3},
    )

    activity = result["product_event_activity"]
    if not activity["rows"]["items"]:
        failures.append("product event activity: expected observed event groups")
    comparison = result["product_activity_comparison"]["comparison"]
    if "current" not in comparison or "baseline" not in comparison:
        failures.append("product activity comparison: current and baseline windows are required")
    state = result["group_state"]
    if state is None or state["state_basis"] != "period_end":
        failures.append("group state: expected an explicit period_end basis")
    cross_product = result["cross_product_activity"]
    if cross_product is None or cross_product["match"] != "all":
        failures.append("cross-product activity: expected multiple labeled all-match rules")
    elif not cross_product["activity_rules"]:
        failures.append("cross-product activity: expected labeled event rules")
    adoption = result["explicit_eligibility_adoption"]
    if adoption is not None and not adoption["eligibility_filters"]:
        failures.append("adoption: expected an explicit eligibility denominator")
    funnel = result["generic_funnel"]
    if funnel is not None and funnel["state_basis"] != "event_time":
        failures.append("funnel: expected an explicit event_time basis")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default=os.environ.get("ANALYTICS_PROJECT_ID"))
    parser.add_argument(
        "--workspace-key", default=os.environ.get("ANALYTICS_WORKSPACE_KEY", "happyfox")
    )
    parser.add_argument("--project-key", default=os.environ.get("ANALYTICS_PROJECT_KEY", "main"))
    parser.add_argument("--no-assert", action="store_true")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(repository), str(repository / "src")]
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from analytics_platform.catalog.models import Project

    if args.project_id:
        project = Project.objects.get(pk=args.project_id)
    else:
        project = Project.objects.get(workspace__key=args.workspace_key, key=args.project_key)
    result = analyze_project(project, cutoff=ANALYSIS_CUTOFF)
    failures = [] if args.no_assert else assert_expected_results(result)
    payload = {"analysis": result, "assertion_failures": failures}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

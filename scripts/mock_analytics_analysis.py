#!/usr/bin/env python3
"""Analyze and assert April's stored synthetic analytics validation dataset."""

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


def analyze_project(project, *, cutoff=ANALYSIS_CUTOFF) -> dict:
    """Adapt reusable analytics service results to the legacy validation payload."""
    from analytics_platform.analytics.accounts import find_cross_product_accounts
    from analytics_platform.analytics.catalog import describe_project
    from analytics_platform.analytics.change import analyze_account_change
    from analytics_platform.analytics.contracts import TimeRange
    from analytics_platform.analytics.features import (
        analyze_product_adoption,
        count_feature_users,
    )
    from analytics_platform.analytics.funnels import analyze_funnel
    from analytics_platform.analytics.profiles import load_profile_timelines
    from analytics_platform.analytics.semantics import SEMANTICS
    from analytics_platform.analytics.trials import analyze_trial_outcomes

    current_period = TimeRange.create(cutoff - timedelta(days=30), cutoff)
    dataset_period = TimeRange.create(cutoff - timedelta(weeks=10), cutoff)
    decline_period = TimeRange.create(cutoff - timedelta(weeks=5), cutoff)

    catalog = describe_project(project, event_definition_limit=200).to_dict()
    timelines = load_profile_timelines(project)
    groupidentify_count = sum(len(timeline.transitions) for timeline in timelines.values())
    definition_counts = dict(
        sorted(Counter(item["product"] for item in _items(catalog["event_definitions"])).items())
    )

    adoption_result = analyze_product_adoption(
        project,
        "contact_center",
        current_period,
        include_plan_breakdown=True,
        account_limit=200,
    ).to_dict()
    feature_adoption = {}
    for feature, values in adoption_result["features"].items():
        entitled = values["period_end_entitled_adoption"]
        feature_adoption[feature] = {
            "accounts": _items(values["adopting_accounts"]),
            "account_count": values["observed_adopting_account_count"],
            "current_entitled_rate": entitled["rate"],
            "event_count": values["observed_event_count"],
            "median_events_per_adopting_account": values["median_events_per_adopting_account"],
        }

    plan_adoption = {}
    for plan, values in adoption_result.get("plans", {}).items():
        plan_adoption[plan] = {
            "eligible_account_count": values["eligible_account_count"],
            "eligible_accounts": _items(values["eligible_accounts"]),
            "features": {
                feature: {
                    "adopting_account_count": feature_values["numerator"],
                    "adopting_accounts": _items(feature_values["adopting_accounts"]),
                    "rate": feature_values["rate"],
                }
                for feature, feature_values in values["features"].items()
            },
        }

    product = SEMANTICS.get_product("contact_center")
    overall_adoption = adoption_result["overall"]
    entitled_accounts = _items(overall_adoption["period_end_entitled_accounts"])
    observed_accounts = _items(overall_adoption["observed_accounts"])

    cross_two = find_cross_product_accounts(
        project, ["helpdesk", "contact_center"], current_period, limit=200
    ).to_dict()
    cross_three = find_cross_product_accounts(
        project, ["helpdesk", "contact_center", "bi"], current_period, limit=200
    ).to_dict()
    cross_shared = find_cross_product_accounts(
        project, ["helpdesk", "contact_center", "rise"], current_period, limit=200
    ).to_dict()

    funnels = {
        "call_initiated_to_call_connected": analyze_funnel(
            project, "call_connection", dataset_period
        ).to_dict(),
        "call_transfer_initiated_to_call_transfer_completed": analyze_funnel(
            project, "call_transfer", dataset_period
        ).to_dict(),
        "callback_requested_to_callback_fulfilled": analyze_funnel(
            project, "callback", dataset_period
        ).to_dict(),
    }
    trials = analyze_trial_outcomes(
        project, "contact_center", dataset_period, account_limit=200
    ).to_dict()
    decline = analyze_account_change(
        project,
        "usage_decline",
        "contact_center",
        decline_period,
        account_limit=200,
    ).to_dict()

    abandonment = {}
    for feature in product.features:
        result = analyze_account_change(
            project,
            "feature_abandonment",
            "contact_center",
            current_period,
            feature=feature.key,
            account_limit=200,
        ).to_dict()
        stopped = [item["account_key"] for item in _items(result["accounts"])]
        abandonment[feature.key] = {"stopped_accounts": stopped}

    distinct_users: dict[str, dict[str, dict[str, int]]] = {}
    for product_key in SEMANTICS.products:
        usage = count_feature_users(
            project, product_key, current_period, account_limit=200
        ).to_dict()
        for account in _items(usage["accounts"]):
            distinct_users.setdefault(account["account_key"], {})[product_key] = account["features"]

    return {
        "scope": {
            "project_id": str(project.pk),
            "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
            "historical_entitlement_reliability": (
                "conditional_on_complete_correctly_timestamped_groupidentify_history"
            ),
        },
        "validation": {
            "event_count": catalog["counts"]["events"],
            "behavioral_event_count": catalog["counts"]["events"] - groupidentify_count,
            "groupidentify_count": groupidentify_count,
            "group_profile_count": catalog["counts"]["group_profiles"],
            "event_definitions_by_product": definition_counts,
        },
        "feature_adoption": feature_adoption,
        "adoption_by_plan_at_event_time": {
            "period_start": current_period.as_dict()["start"],
            "period_end": current_period.as_dict()["end"],
            "interpretation": adoption_result["interpretation"],
            "plans": plan_adoption,
        },
        "high_adoption_low_depth": sorted(
            feature
            for feature, values in adoption_result["features"].items()
            if values["high_adoption_low_depth"]["classified"]
        ),
        "abandonment": abandonment,
        "entitlement": {
            "interpretation": adoption_result["interpretation"],
            "current_entitled_accounts": entitled_accounts,
            "observed_usage_accounts": observed_accounts,
            "entitled_without_usage": _items(overall_adoption["entitled_without_usage"]),
            "usage_percentage": overall_adoption["period_end_entitled_adoption"]["rate"],
        },
        "funnels": funnels,
        "cross_product": {
            "period_days": 30,
            "period_start": current_period.as_dict()["start"],
            "period_end": current_period.as_dict()["end"],
            "helpdesk_and_contact_center": [
                item["account_key"] for item in _items(cross_two["accounts"])
            ],
            "helpdesk_contact_center_and_bi": [
                item["account_key"] for item in _items(cross_three["accounts"])
            ],
            "shared_user_overlap": cross_shared["user_overlap"],
        },
        "trials": {
            "used_before_conversion": [
                item["account_key"] for item in _items(trials["used_before_conversion"])
            ],
            "used_then_expired": [
                item["account_key"] for item in _items(trials["used_then_expired"])
            ],
        },
        "weekly_decline": {
            "declining_accounts": [item["account_key"] for item in _items(decline["accounts"])]
        },
        "distinct_users_by_account_product_feature": {
            "period_days": 30,
            "period_start": current_period.as_dict()["start"],
            "period_end": current_period.as_dict()["end"],
            "accounts": dict(sorted(distinct_users.items())),
        },
    }


def assert_expected_results(result: dict) -> list[str]:
    failures = []

    def expect(label, actual, expected):
        if actual != expected:
            failures.append(f"{label}: expected {expected!r}, got {actual!r}")

    expect("event count", result["validation"]["event_count"], 3524)
    expect("behavioral event count", result["validation"]["behavioral_event_count"], 3511)
    expect("groupidentify count", result["validation"]["groupidentify_count"], 13)
    expect("group profile count", result["validation"]["group_profile_count"], 10)
    expect(
        "event definitions by product",
        result["validation"]["event_definitions_by_product"],
        {"bi": 4, "contact_center": 13, "helpdesk": 4, "rise": 3},
    )
    expect(
        "entitled without usage",
        result["entitlement"]["entitled_without_usage"],
        ["charlie", "hotel"],
    )
    expect("entitled usage percentage", result["entitlement"]["usage_percentage"], 75.0)
    expect(
        "helpdesk and contact center",
        result["cross_product"]["helpdesk_and_contact_center"],
        ["acme", "juliet"],
    )
    expect(
        "helpdesk, contact center, and BI",
        result["cross_product"]["helpdesk_contact_center_and_bi"],
        ["acme"],
    )
    expect("trial conversion usage", result["trials"]["used_before_conversion"], ["echo"])
    expect("trial expiry usage", result["trials"]["used_then_expired"], ["foxtrot"])
    expect(
        "high adoption and low depth",
        result["high_adoption_low_depth"],
        ["supervisor_listen"],
    )
    if "golf" not in result["weekly_decline"]["declining_accounts"]:
        failures.append("weekly decline: golf was not classified as declining")
    initiated = result["funnels"]["call_initiated_to_call_connected"]
    if not initiated["started"] > initiated["completed"]:
        failures.append("call connection funnel: expected at least one lost call")
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

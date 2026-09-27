#!/usr/bin/env python3
"""Analyze and assert April's stored synthetic analytics validation dataset."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import django

if __package__:
    from scripts.mock_analytics_dataset import ANALYSIS_CUTOFF
else:
    from mock_analytics_dataset import ANALYSIS_CUTOFF

QUALIFYING_FEATURE_EVENTS = {
    "call_transfer_completed": "call_transfer",
    "callback_fulfilled": "callback",
    "call_hold_ended": "call_hold",
    "listen_started": "supervisor_listen",
    "whisper_started": "supervisor_whisper",
    "barge_started": "supervisor_barge",
    "macro_applied": "helpdesk_macro",
    "sla_policy_created": "sla_policy",
    "course_completed": "course_completion",
    "report_created": "report_creation",
    "report_viewed": "report_viewing",
    "dashboard_created": "dashboard_creation",
    "dashboard_viewed": "dashboard_viewing",
}


def _feature_name(event) -> str | None:
    if event.event == "call_summary_generated":
        return "ai_summary" if event.properties.get("success") is True else None
    return QUALIFYING_FEATURE_EVENTS.get(event.event)


def _round_rate(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


def _account(event) -> str | None:
    value = event.groups.get("account")
    return value if isinstance(value, str) else None


def _product(event) -> str | None:
    value = event.properties.get("product")
    return value if isinstance(value, str) else None


def _feature_usage(events, start: datetime, end: datetime):
    feature_accounts = defaultdict(set)
    feature_counts = Counter()
    account_feature_counts = Counter()
    for event in events:
        if not (start <= event.timestamp < end):
            continue
        feature = _feature_name(event)
        if feature is None:
            continue
        account = _account(event)
        if account is None:
            continue
        feature_accounts[feature].add(account)
        feature_counts[feature] += 1
        account_feature_counts[(account, feature)] += 1
    return feature_accounts, feature_counts, account_feature_counts


def _funnel(events, first: str, second: str) -> dict:
    starts = defaultdict(list)
    completions = defaultdict(list)
    for event in events:
        call_id = event.properties.get("call_id")
        if not call_id:
            continue
        key = (_account(event), call_id)
        if event.event == first:
            starts[key].append(event.timestamp)
        elif event.event == second:
            completions[key].append(event.timestamp)

    started_count = sum(len(timestamps) for timestamps in starts.values())
    completed_count = 0
    completed_accounts = set()
    for key, start_times in starts.items():
        completion_times = sorted(completions.get(key, ()))
        completion_index = 0
        for started_at in sorted(start_times):
            while (
                completion_index < len(completion_times)
                and completion_times[completion_index] <= started_at
            ):
                completion_index += 1
            if completion_index < len(completion_times):
                completed_count += 1
                completed_accounts.add(key[0])
                completion_index += 1
    return {
        "started": started_count,
        "completed": completed_count,
        "lost": started_count - completed_count,
        "completion_rate": _round_rate(completed_count, started_count),
        "accounts_started": len({account for account, _ in starts}),
        "accounts_completed": len(completed_accounts),
    }


def _profile_timelines(group_events) -> dict[str, list[tuple[datetime, dict]]]:
    timelines = defaultdict(list)
    state = defaultdict(dict)
    for event in sorted(group_events, key=lambda item: (item.timestamp, str(item.uuid))):
        properties = event.properties
        if properties.get("$group_type") != "account":
            continue
        account = properties.get("$group_key")
        changes = properties.get("$group_set")
        if not isinstance(account, str) or not isinstance(changes, dict):
            continue
        state[account] = {**state[account], **changes}
        timelines[account].append((event.timestamp, dict(state[account])))
    return timelines


def _state_at(timeline: list[tuple[datetime, dict]], timestamp: datetime) -> dict:
    state = {}
    for changed_at, snapshot in timeline:
        if changed_at > timestamp:
            break
        state = snapshot
    return state


def analyze_project(project, *, cutoff: datetime = ANALYSIS_CUTOFF) -> dict:
    from analytics_platform.event_catalog.models import EventDefinition
    from analytics_platform.events.models import Event
    from analytics_platform.group_analytics.models import GroupProfile

    stored_events = list(Event.objects.filter(project=project).order_by("timestamp", "uuid"))
    group_events = [event for event in stored_events if event.event == "$groupidentify"]
    behavior = [event for event in stored_events if event.event != "$groupidentify"]
    contact_center = [event for event in behavior if _product(event) == "contact_center"]
    profiles = list(
        GroupProfile.objects.filter(project=project, group_type="account").order_by("group_key")
    )
    profile_properties = {profile.group_key: profile.properties for profile in profiles}
    timelines = _profile_timelines(group_events)

    entitled = {
        account
        for account, properties in profile_properties.items()
        if properties.get("contact_center_status") in {"active", "trial"}
    }
    cc_accounts = {_account(event) for event in contact_center if _account(event) is not None}
    current_start = cutoff - timedelta(days=30)
    previous_start = cutoff - timedelta(days=60)
    current_accounts, current_counts, current_account_counts = _feature_usage(
        contact_center, current_start, cutoff
    )
    previous_accounts, _, _ = _feature_usage(contact_center, previous_start, current_start)

    adoption = {}
    for feature, accounts in sorted(current_accounts.items()):
        depths = [current_account_counts[(account, feature)] for account in accounts]
        adoption[feature] = {
            "accounts": sorted(accounts),
            "account_count": len(accounts),
            "current_entitled_rate": _round_rate(len(accounts & entitled), len(entitled)),
            "event_count": current_counts[feature],
            "median_events_per_adopting_account": float(statistics.median(depths)),
        }

    eligible_plan_accounts = defaultdict(set)
    for account, timeline in timelines.items():
        period_states = [_state_at(timeline, current_start)]
        period_states.extend(
            state for changed_at, state in timeline if current_start <= changed_at < cutoff
        )
        for state in period_states:
            plan = state.get("contact_center_plan")
            if state.get("contact_center_status") in {"active", "trial"} and plan:
                eligible_plan_accounts[str(plan)].add(account)

    plan_feature_accounts = defaultdict(lambda: defaultdict(set))
    for event in contact_center:
        if not (current_start <= event.timestamp < cutoff):
            continue
        feature = _feature_name(event)
        if feature is None:
            continue
        account = _account(event)
        state = _state_at(timelines.get(account, []), event.timestamp)
        plan = state.get("contact_center_plan")
        if state.get("contact_center_status") in {"active", "trial"} and plan:
            plan_feature_accounts[str(plan)][feature].add(account)

    plan_adoption = {}
    for plan, eligible_accounts in sorted(eligible_plan_accounts.items()):
        plan_adoption[plan] = {
            "eligible_account_count": len(eligible_accounts),
            "eligible_accounts": sorted(eligible_accounts),
            "features": {
                feature: {
                    "adopting_account_count": len(
                        plan_feature_accounts[plan].get(feature, set())
                    ),
                    "adopting_accounts": sorted(
                        plan_feature_accounts[plan].get(feature, set())
                    ),
                    "rate": _round_rate(
                        len(plan_feature_accounts[plan].get(feature, set())),
                        len(eligible_accounts),
                    ),
                }
                for feature in sorted(current_accounts)
            },
        }

    previous_four_start = cutoff - timedelta(days=35)
    weekly_feature_accounts = defaultdict(lambda: defaultdict(set))
    for event in contact_center:
        if not (previous_four_start <= event.timestamp < cutoff):
            continue
        feature = _feature_name(event)
        if feature is None:
            continue
        week_index = int((event.timestamp - previous_four_start).days // 7)
        weekly_feature_accounts[feature][week_index].add(_account(event))
    stickiness = {}
    for feature, weeks in sorted(weekly_feature_accounts.items()):
        current = weeks[4]
        sticky = {
            account
            for account in current
            if sum(account in weeks[index] for index in range(4)) >= 3
        }
        stickiness[feature] = {
            "current_week_accounts": len(current),
            "sticky_accounts": len(sticky),
            "rate": _round_rate(len(sticky), len(current)),
        }

    abandonment = {}
    for feature in sorted(set(previous_accounts) | set(current_accounts)):
        previous = previous_accounts.get(feature, set())
        current = current_accounts.get(feature, set())
        stopped = previous - current
        abandonment[feature] = {
            "previous_accounts": sorted(previous),
            "stopped_accounts": sorted(stopped),
            "rate": _round_rate(len(stopped), len(previous)),
        }

    products_by_account = defaultdict(set)
    distinct_users = defaultdict(set)
    for event in behavior:
        if not (current_start <= event.timestamp < cutoff):
            continue
        account = _account(event)
        product = _product(event)
        if account and product:
            products_by_account[account].add(product)
            feature = _feature_name(event)
            if feature is not None:
                distinct_users[(account, product, feature)].add(event.distinct_id)

    historical_plan_usage = defaultdict(Counter)
    for event in contact_center:
        feature = _feature_name(event)
        if feature is None:
            continue
        account = _account(event)
        state = _state_at(timelines.get(account, []), event.timestamp)
        historical_plan_usage[str(state.get("contact_center_plan"))][feature] += 1

    used_before_conversion = []
    used_then_expired = []
    for account, timeline in timelines.items():
        trial_at = next(
            (
                changed_at
                for changed_at, state in timeline
                if state.get("contact_center_status") == "trial"
            ),
            None,
        )
        if trial_at is None:
            continue
        conversion_at = next(
            (
                changed_at
                for changed_at, state in timeline
                if changed_at > trial_at and state.get("contact_center_status") == "active"
            ),
            None,
        )
        expiry_at = next(
            (
                changed_at
                for changed_at, state in timeline
                if changed_at > trial_at and state.get("contact_center_status") == "expired"
            ),
            None,
        )
        end = conversion_at or expiry_at or cutoff
        used_during_trial = any(
            _account(event) == account and trial_at <= event.timestamp < end
            for event in contact_center
        )
        if conversion_at and used_during_trial:
            used_before_conversion.append(account)
        if expiry_at and used_during_trial:
            used_then_expired.append(account)

    weekly_counts = defaultdict(lambda: [0] * 10)
    dataset_start = cutoff - timedelta(weeks=10)
    for event in contact_center:
        if dataset_start <= event.timestamp < cutoff:
            index = int((event.timestamp - dataset_start).days // 7)
            weekly_counts[_account(event)][index] += 1
    weekly_decline = {}
    for account, counts in sorted(weekly_counts.items()):
        previous_average = sum(counts[:5]) / 5
        current_average = sum(counts[5:]) / 5
        decline = (
            round((previous_average - current_average) * 100 / previous_average, 2)
            if previous_average
            else 0.0
        )
        weekly_decline[account] = {
            "weekly_counts": counts,
            "previous_average": previous_average,
            "current_average": current_average,
            "decline_percentage": decline,
        }

    definition_counts = dict(
        Counter(
            EventDefinition.objects.filter(project=project).values_list("product_key", flat=True)
        )
    )
    high_adoption_low_depth = [
        feature
        for feature, values in adoption.items()
        if values["account_count"] >= max(2, len(entitled) / 2)
        and values["median_events_per_adopting_account"] <= 2
    ]
    return {
        "scope": {
            "project_id": str(project.pk),
            "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
            "historical_entitlement_reliability": (
                "conditional_on_complete_correctly_timestamped_groupidentify_history"
            ),
        },
        "validation": {
            "event_count": len(stored_events),
            "behavioral_event_count": len(behavior),
            "groupidentify_count": len(group_events),
            "group_profile_count": len(profiles),
            "event_definitions_by_product": dict(sorted(definition_counts.items())),
        },
        "feature_adoption": adoption,
        "adoption_by_plan_at_event_time": {
            "period_start": current_start.isoformat().replace("+00:00", "Z"),
            "period_end": cutoff.isoformat().replace("+00:00", "Z"),
            "plans": plan_adoption,
        },
        "historical_plan_event_counts": {
            plan: dict(sorted(counts.items()))
            for plan, counts in sorted(historical_plan_usage.items())
        },
        "high_adoption_low_depth": sorted(high_adoption_low_depth),
        "stickiness": stickiness,
        "abandonment": abandonment,
        "entitlement": {
            "current_entitled_accounts": sorted(entitled),
            "observed_usage_accounts": sorted(cc_accounts),
            "entitled_without_usage": sorted(entitled - cc_accounts),
            "usage_percentage": _round_rate(len(entitled & cc_accounts), len(entitled)),
        },
        "funnels": {
            "call_initiated_to_call_connected": _funnel(
                contact_center, "call_initiated", "call_connected"
            ),
            "call_transfer_initiated_to_call_transfer_completed": _funnel(
                contact_center, "call_transfer_initiated", "call_transfer_completed"
            ),
            "callback_requested_to_callback_fulfilled": _funnel(
                contact_center, "callback_requested", "callback_fulfilled"
            ),
        },
        "cross_product": {
            "period_days": 30,
            "period_start": current_start.isoformat().replace("+00:00", "Z"),
            "period_end": cutoff.isoformat().replace("+00:00", "Z"),
            "helpdesk_and_contact_center": sorted(
                account
                for account, products in products_by_account.items()
                if {"helpdesk", "contact_center"} <= products
            ),
            "helpdesk_contact_center_and_bi": sorted(
                account
                for account, products in products_by_account.items()
                if {"helpdesk", "contact_center", "bi"} <= products
            ),
        },
        "trials": {
            "used_before_conversion": sorted(used_before_conversion),
            "used_then_expired": sorted(used_then_expired),
        },
        "weekly_decline": {
            "accounts": weekly_decline,
            "declining_accounts": sorted(
                account
                for account, values in weekly_decline.items()
                if values["decline_percentage"] >= 50
            ),
        },
        "distinct_users_by_account_product_feature": {
            "period_days": 30,
            "period_start": current_start.isoformat().replace("+00:00", "Z"),
            "period_end": cutoff.isoformat().replace("+00:00", "Z"),
            "accounts": {
                account: {
                    product: {
                        feature: len(users)
                        for (candidate_account, candidate_product, feature), users in sorted(
                            distinct_users.items()
                        )
                        if candidate_account == account and candidate_product == product
                    }
                    for product in sorted(products_by_account[account])
                    if any(
                        candidate_account == account and candidate_product == product
                        for candidate_account, candidate_product, _ in distinct_users
                    )
                }
                for account in sorted(products_by_account)
                if any(candidate_account == account for candidate_account, _, _ in distinct_users)
            },
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
    expect("entitled without usage", result["entitlement"]["entitled_without_usage"], ["charlie"])
    expect("entitled usage percentage", result["entitlement"]["usage_percentage"], 87.5)
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

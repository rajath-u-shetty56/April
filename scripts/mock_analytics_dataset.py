#!/usr/bin/env python3
"""Build and ingest April's deterministic synthetic analytics validation dataset."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DATASET_NAMESPACE = uuid.UUID("f95bbc7d-50a8-49a8-9fc1-f63e97889919")
DATASET_START = datetime(2026, 7, 6, tzinfo=UTC)
ANALYSIS_CUTOFF = datetime(2026, 9, 14, tzinfo=UTC)
BATCH_SIZE = 250
USER_STORE_BY_PRODUCT = {
    "helpdesk": "helpdesk",
    "contact_center": "helpdesk",
    "rise": "helpdesk",
    "bi": "bi",
}


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Do not forward an ingestion credential to a redirected destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class SyntheticDataset:
    profile_events: tuple[dict, ...]
    behavioral_events: tuple[dict, ...]


def _event_uuid(*parts: object) -> str:
    return str(uuid.uuid5(DATASET_NAMESPACE, "|".join(str(part) for part in parts)))


def _timestamp(week: int, sequence: int) -> str:
    value = DATASET_START + timedelta(weeks=week, hours=9, minutes=sequence)
    return value.isoformat().replace("+00:00", "Z")


def _profile(account: str, timestamp: datetime, properties: dict, revision: str) -> dict:
    return {
        "uuid": _event_uuid("profile", account, revision),
        "event": "$groupidentify",
        "distinct_id": "system:billing",
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "groups": {},
        "properties": {
            "$group_type": "account",
            "$group_key": account,
            "$group_set": properties,
        },
    }


def _behavior(
    account: str,
    product: str,
    name: str,
    week: int,
    sequence: int,
    actor: int,
    identity: str,
    **properties,
) -> dict:
    user_store = USER_STORE_BY_PRODUCT[product]
    return {
        "uuid": _event_uuid("behavior", account, product, name, week, identity),
        "event": name,
        "distinct_id": f"{user_store}:{account}:user:{actor}",
        "timestamp": _timestamp(week, sequence),
        "groups": {"account": account},
        "properties": {
            "product": product,
            "version": 1,
            "synthetic_scenario": account,
            **properties,
        },
    }


def _append_call(
    events: list[dict], account: str, week: int, call_number: int, *, connected: bool = True
) -> None:
    call_id = f"{account}-w{week + 1}-c{call_number + 1}"
    base = call_number * 20
    actor = call_number % 7 + 1
    events.append(
        _behavior(
            account,
            "contact_center",
            "call_initiated",
            week,
            base,
            actor,
            call_id,
            call_id=call_id,
            direction="inbound" if call_number % 2 == 0 else "outbound",
        )
    )
    if connected:
        events.append(
            _behavior(
                account,
                "contact_center",
                "call_connected",
                week,
                base + 1,
                actor,
                call_id,
                call_id=call_id,
            )
        )
        events.append(
            _behavior(
                account,
                "contact_center",
                "call_ended",
                week,
                base + 9,
                actor,
                call_id,
                call_id=call_id,
                duration_seconds=480,
            )
        )


def _append_pair(
    events: list[dict],
    account: str,
    week: int,
    number: int,
    first: str,
    second: str,
    *,
    completed: bool = True,
) -> None:
    call_id = f"{account}-w{week + 1}-c{number + 1}"
    actor = number % 7 + 1
    base = number * 20 + 3
    names = (first, second) if completed else (first,)
    for offset, name in enumerate(names):
        events.append(
            _behavior(
                account,
                "contact_center",
                name,
                week,
                base + offset,
                actor,
                f"{call_id}-{first}",
                call_id=call_id,
            )
        )


def _append_contact_center_week(
    events: list[dict],
    account: str,
    week: int,
    calls: int,
    *,
    rich: bool = False,
) -> None:
    for number in range(calls):
        _append_call(events, account, week, number, connected=number % 8 != 7)
    for number in range(calls // 2):
        _append_pair(
            events,
            account,
            week,
            number,
            "call_transfer_initiated",
            "call_transfer_completed",
            completed=number % 5 != 0,
        )
    for number in range(calls // 4):
        _append_pair(
            events,
            account,
            week,
            number,
            "callback_requested",
            "callback_fulfilled",
            completed=number % 4 != 0,
        )
    for number in range(calls // 3):
        _append_pair(events, account, week, number, "call_hold_started", "call_hold_ended")
    for number in range(max(1, calls // 2)):
        call_id = f"{account}-w{week + 1}-c{number + 1}"
        events.append(
            _behavior(
                account,
                "contact_center",
                "call_summary_generated",
                week,
                number * 20 + 12,
                number % 7 + 1,
                f"{call_id}-summary",
                call_id=call_id,
                success=True,
            )
        )
    if rich:
        for number, name in enumerate(("listen_started", "whisper_started", "barge_started")):
            for occurrence in range(max(1, calls // 5)):
                call_id = f"{account}-w{week + 1}-c{occurrence + 1}"
                events.append(
                    _behavior(
                        account,
                        "contact_center",
                        name,
                        week,
                        occurrence * 20 + 13 + number,
                        20 + occurrence,
                        f"{call_id}-{name}",
                        call_id=call_id,
                    )
                )


def _append_helpdesk(events: list[dict], account: str, week: int, tickets: int) -> None:
    for number in range(tickets):
        ticket_id = f"{account}-w{week + 1}-t{number + 1}"
        actor = number % 6 + 1
        for offset, name in enumerate(("ticket_created", "ticket_resolved")):
            events.append(
                _behavior(
                    account,
                    "helpdesk",
                    name,
                    week,
                    300 + number * 3 + offset,
                    actor,
                    f"{ticket_id}-{name}",
                    ticket_id=ticket_id,
                )
            )
        if number % 3 == 0:
            events.append(
                _behavior(
                    account,
                    "helpdesk",
                    "macro_applied",
                    week,
                    300 + number * 3 + 2,
                    actor,
                    f"{ticket_id}-macro",
                    ticket_id=ticket_id,
                    macro_id="standard-response",
                )
            )
    events.append(
        _behavior(
            account,
            "helpdesk",
            "sla_policy_created",
            week,
            390,
            1,
            f"{account}-w{week + 1}-sla",
            policy_id=f"{account}-sla-{week + 1}",
        )
    )


def _append_bi(events: list[dict], account: str, week: int, views: int) -> None:
    names = ("report_created", "report_viewed", "dashboard_created", "dashboard_viewed")
    for number in range(views):
        name = names[number % len(names)]
        events.append(
            _behavior(
                account,
                "bi",
                name,
                week,
                420 + number,
                number % 4 + 1,
                f"{account}-w{week + 1}-bi{number + 1}-{name}",
                report_id=f"{account}-report-{number % 3 + 1}",
            )
        )


def _append_rise(events: list[dict], account: str, week: int, courses: int) -> None:
    for number in range(courses):
        course_id = f"{account}-course-{number % 3 + 1}"
        for offset, name in enumerate(
            ("course_assigned", "course_started", "course_completed")
        ):
            events.append(
                _behavior(
                    account,
                    "rise",
                    name,
                    week,
                    450 + number * 3 + offset,
                    number % 5 + 1,
                    f"{account}-w{week + 1}-assignment{number + 1}-{course_id}-{name}",
                    course_id=course_id,
                )
            )


def build_dataset() -> SyntheticDataset:
    initial = DATASET_START - timedelta(days=1)
    account_properties = {
        "acme": ("enterprise", "active", "pro", 50),
        "bravo": ("pro", "active", "basic", 20),
        "charlie": ("basic", "active", "basic", 10),
        "delta": ("pro", "inactive", None, 15),
        "echo": ("pro", "trial", "trial", 12),
        "foxtrot": ("basic", "trial", "trial", 8),
        "golf": ("enterprise", "active", "enterprise", 40),
        "hotel": ("basic", "active", "basic", 5),
        "india": ("enterprise", "active", "enterprise", 75),
        "juliet": ("pro", "active", "pro", 25),
    }
    profiles = []
    for account, (helpdesk_plan, cc_status, cc_plan, seats) in account_properties.items():
        profiles.append(
            _profile(
                account,
                initial,
                {
                    "account_name": account.title(),
                    "segment": "enterprise" if seats >= 40 else "mid_market",
                    "helpdesk_status": "active",
                    "helpdesk_plan": helpdesk_plan,
                    "helpdesk_licensed_agents": seats,
                    "contact_center_status": cc_status,
                    "contact_center_plan": cc_plan,
                    "rise_status": "active" if account == "delta" else "inactive",
                    "bi_status": "active" if account == "acme" else "inactive",
                },
                "initial",
            )
        )
    profiles.extend(
        (
            _profile(
                "echo",
                datetime(2026, 8, 17, tzinfo=UTC),
                {"contact_center_status": "active", "contact_center_plan": "pro"},
                "converted",
            ),
            _profile(
                "foxtrot",
                datetime(2026, 8, 10, tzinfo=UTC),
                {"contact_center_status": "expired", "contact_center_plan": None},
                "expired",
            ),
            _profile(
                "golf",
                datetime(2026, 8, 31, tzinfo=UTC),
                {"contact_center_plan": "pro"},
                "downgraded",
            ),
        )
    )

    events: list[dict] = []
    declining_calls = (18, 17, 16, 15, 12, 8, 5, 3, 2, 1)
    for week in range(10):
        _append_contact_center_week(events, "acme", week, 12, rich=True)
        _append_helpdesk(events, "acme", week, 12)
        _append_bi(events, "acme", week, 10)

        _append_contact_center_week(events, "bravo", week, 7)
        _append_helpdesk(events, "charlie", week, 3)
        _append_helpdesk(events, "delta", week, 6)
        _append_rise(events, "delta", week, 4)
        _append_contact_center_week(events, "golf", week, declining_calls[week])
        _append_contact_center_week(events, "india", week, 10, rich=True)
        _append_contact_center_week(events, "juliet", week, 6)
        _append_helpdesk(events, "juliet", week, 4)

        if week < 7:
            _append_contact_center_week(events, "echo", week, 5)
        if week < 4:
            _append_contact_center_week(events, "foxtrot", week, 5)

    for account in ("bravo", "golf", "juliet"):
        events.append(
            _behavior(
                account,
                "contact_center",
                "listen_started",
                8,
                18,
                30,
                f"{account}-w9-one-time-listen",
                call_id=f"{account}-w9-c1",
            )
        )

    _append_call(events, "hotel", 2, 0)
    events.append(
        _behavior(
            "hotel",
            "contact_center",
            "whisper_started",
            2,
            15,
            2,
            "hotel-once-whisper",
            call_id="hotel-w3-c1",
        )
    )
    events.sort(key=lambda event: (event["timestamp"], event["uuid"]))
    return SyntheticDataset(tuple(profiles), tuple(events))


def _chunks(values: tuple[dict, ...], size: int) -> Iterable[tuple[dict, ...]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _request(base_url: str, key: str, path: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            body = error.read().decode(errors="replace")
        raise RuntimeError(f"ingestion returned HTTP {error.code}: {body[:500]}") from None


def ingest_dataset(dataset: SyntheticDataset, base_url: str, key: str, timeout: float) -> dict:
    created = duplicates = rejected = 0
    for profile in dataset.profile_events:
        result = _request(base_url, key, "/api/v1/capture/", profile, timeout)
        if result.get("status") != "accepted":
            raise RuntimeError(f"profile event rejected: {result.get('code', 'unknown')}")
        duplicates += bool(result.get("duplicate"))
        created += not result.get("duplicate", False)
    for batch in _chunks(dataset.behavioral_events, BATCH_SIZE):
        result = _request(base_url, key, "/api/v1/bulk/", {"events": batch}, timeout)
        results = result.get("results", ())
        if not isinstance(results, list) or len(results) != len(batch):
            received = len(results) if isinstance(results, list) else 0
            raise RuntimeError(
                f"expected {len(batch)} bulk results, received {received}"
            )
        for item in results:
            if item.get("status") != "accepted":
                rejected += 1
                continue
            duplicates += bool(item.get("duplicate"))
            created += not item.get("duplicate", False)
    if rejected:
        raise RuntimeError(f"{rejected} valid synthetic events were rejected")
    return {"created": created, "duplicates": duplicates, "rejected": rejected}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--describe", action="store_true", help="print counts without ingesting")
    args = parser.parse_args()
    dataset = build_dataset()
    if args.describe:
        print(
            json.dumps(
                {
                    "profile_events": len(dataset.profile_events),
                    "behavioral_events": len(dataset.behavioral_events),
                    "first_timestamp": dataset.behavioral_events[0]["timestamp"],
                    "last_timestamp": dataset.behavioral_events[-1]["timestamp"],
                },
                indent=2,
            )
        )
        return 0
    key = os.environ.get("ANALYTICS_INGESTION_KEY", "")
    if not key or any(character in key for character in "\r\n"):
        print("Set ANALYTICS_INGESTION_KEY to a valid ingestion credential.", file=sys.stderr)
        return 2
    base_url = os.environ.get("ANALYTICS_BASE_URL", "http://localhost:8000").rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        print("ANALYTICS_BASE_URL must be an HTTP(S) URL without credentials.", file=sys.stderr)
        return 2
    timeout = float(os.environ.get("ANALYTICS_TIMEOUT_SECONDS", "30"))
    result = ingest_dataset(dataset, base_url, key, timeout)
    total = len(dataset.profile_events) + len(dataset.behavioral_events)
    print(json.dumps({**result, "total": total}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

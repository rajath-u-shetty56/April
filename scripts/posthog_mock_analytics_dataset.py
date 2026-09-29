#!/usr/bin/env python3
"""Validate or send April's deterministic dataset to a dedicated PostHog project."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Protocol

if __package__:
    from scripts.mock_analytics_dataset import SyntheticDataset, build_dataset
else:
    from mock_analytics_dataset import SyntheticDataset, build_dataset

DEFAULT_HOST = "https://us.i.posthog.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
BATCH_SIZE = 100
MAX_RETRIES = 3
RETRIABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class _Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float): ...


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Do not forward the project token to a redirected destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def build_manifest(dataset: SyntheticDataset) -> dict:
    """Return a deterministic, credential-independent description of the dataset."""
    events = (*dataset.profile_events, *dataset.behavioral_events)
    product_counts = Counter(event["properties"]["product"] for event in dataset.behavioral_events)
    event_counts = Counter(event["event"] for event in events)
    accounts = {event["groups"]["account"] for event in dataset.behavioral_events}
    timestamps = [event["timestamp"] for event in events]
    return {
        "profile_event_count": len(dataset.profile_events),
        "behavioral_event_count": len(dataset.behavioral_events),
        "total_event_count": len(events),
        "first_timestamp": min(timestamps),
        "last_timestamp": max(timestamps),
        "event_counts_by_product": dict(sorted(product_counts.items())),
        "event_counts_by_name": dict(sorted(event_counts.items())),
        "account_count": len(accounts),
        "dataset_sha256": hashlib.sha256(_canonical_json(events)).hexdigest(),
    }


def translate_event(event: dict) -> dict:
    """Translate one April event into PostHog's public capture representation."""
    properties = dict(event["properties"])
    if event["event"] != "$groupidentify":
        properties["$groups"] = dict(event["groups"])
    return {
        "uuid": event["uuid"],
        "event": event["event"],
        "distinct_id": event["distinct_id"],
        "timestamp": event["timestamp"],
        "properties": properties,
    }


def _timezone_aware(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_translated_dataset(profiles: Sequence[dict], behaviors: Sequence[dict]) -> None:
    """Validate the translated dataset before either a dry run or send."""
    for index, event in enumerate((*profiles, *behaviors)):
        prefix = f"event {index}"
        try:
            uuid.UUID(event.get("uuid", ""))
        except (ValueError, TypeError, AttributeError):
            raise ValueError(f"{prefix} must have a valid UUID") from None
        if not _timezone_aware(event.get("timestamp")):
            raise ValueError(f"{prefix} must have a timezone-aware timestamp")
        if not isinstance(event.get("event"), str) or not event["event"].strip():
            raise ValueError(f"{prefix} must have a non-empty event name")
        if not isinstance(event.get("distinct_id"), str) or not event["distinct_id"].strip():
            raise ValueError(f"{prefix} must have a non-empty distinct ID")

    for index, event in enumerate(profiles):
        properties = event.get("properties", {})
        if event["event"] != "$groupidentify":
            raise ValueError(f"profile event {index} must be $groupidentify")
        if properties.get("$group_type") != "account":
            raise ValueError(f"profile event {index} must use account group type")
        if not properties.get("$group_key"):
            raise ValueError(f"profile event {index} must have a group key")
        if not isinstance(properties.get("$group_set"), dict) or not properties["$group_set"]:
            raise ValueError(f"profile event {index} must have group properties")
        if "$groups" in properties:
            raise ValueError(f"profile event {index} must not add $groups")

    for index, event in enumerate(behaviors):
        properties = event.get("properties", {})
        if not properties.get("product"):
            raise ValueError(f"behavioral event {index} must preserve product")
        groups = properties.get("$groups")
        if not isinstance(groups, dict) or not groups.get("account"):
            raise ValueError(f"behavioral event {index} must map account to $groups")
        if "groups" in properties:
            raise ValueError(f"behavioral event {index} must not retain plain groups")


def validate_host(host: str) -> str:
    """Accept an HTTP(S) origin only; credentials and URL suffixes are unsafe here."""
    normalized = host.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("POSTHOG_HOST must be an HTTP(S) origin with a valid port") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("POSTHOG_HOST must be an HTTP(S) origin without credentials or a path")
    return normalized


def _chunks(events: Sequence[dict], size: int) -> Iterable[Sequence[dict]]:
    for start in range(0, len(events), size):
        yield events[start : start + size]


def request_batch(
    host: str,
    token: str,
    events: Sequence[dict],
    timeout: float,
    *,
    opener: _Opener | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Synchronously post one bounded batch, retrying only transient failures."""
    body = _canonical_json({"api_key": token, "batch": events})
    request = urllib.request.Request(
        f"{host}/batch/",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    request_opener = opener or urllib.request.build_opener(NoRedirects())
    for attempt in range(MAX_RETRIES + 1):
        try:
            with request_opener.open(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"PostHog batch request returned HTTP {response.status}")
            return
        except urllib.error.HTTPError as error:
            if error.code not in RETRIABLE_STATUS_CODES or attempt == MAX_RETRIES:
                raise RuntimeError(f"PostHog batch request returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"PostHog batch request failed after {MAX_RETRIES + 1} attempts "
                    f"({type(error).__name__})"
                ) from None
        sleep(0.5 * (2**attempt))


def post_batches(
    profiles: Sequence[dict],
    behaviors: Sequence[dict],
    *,
    host: str,
    token: str,
    timeout: float,
    batch_size: int = BATCH_SIZE,
    request: Callable[[str, str, Sequence[dict], float], None] = request_batch,
) -> dict:
    """Post profile events first, then behavioral events, in bounded batches."""
    batch_count = 0
    for events in (profiles, behaviors):
        for batch in _chunks(events, batch_size):
            request(host, token, batch, timeout)
            batch_count += 1
    return {"submitted_event_count": len(profiles) + len(behaviors), "batch_count": batch_count}


def _translate_dataset(dataset: SyntheticDataset) -> tuple[list[dict], list[dict]]:
    profiles = [translate_event(event) for event in dataset.profile_events]
    behaviors = [translate_event(event) for event in dataset.behavioral_events]
    validate_translated_dataset(profiles, behaviors)
    return profiles, behaviors


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        raise ValueError("POSTHOG_TIMEOUT_SECONDS must be a positive number") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("POSTHOG_TIMEOUT_SECONDS must be a positive number")
    return timeout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--describe", action="store_true", help="print the source manifest only")
    modes.add_argument(
        "--dry-run", action="store_true", help="translate and validate without sending"
    )
    modes.add_argument("--send", action="store_true", help="send the validated dataset to PostHog")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if not (args.describe or args.dry_run or args.send):
        parser.print_usage(sys.stderr)
        print(
            "Choose exactly one of --describe, --dry-run, or --send; nothing was sent.",
            file=sys.stderr,
        )
        return 2

    dataset = build_dataset()
    manifest = build_manifest(dataset)
    if args.describe:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    profiles, behaviors = _translate_dataset(dataset)
    if args.dry_run:
        sample = [profiles[0], behaviors[0], behaviors[-1]]
        print(
            json.dumps(
                {"status": "valid", "manifest": manifest, "sample_events": sample},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    token = os.environ.get("POSTHOG_PROJECT_TOKEN", "")
    if not token or any(character in token for character in "\r\n"):
        print(
            "Set POSTHOG_PROJECT_TOKEN to a valid PostHog project capture token.",
            file=sys.stderr,
        )
        return 2
    try:
        host = validate_host(os.environ.get("POSTHOG_HOST", DEFAULT_HOST))
        timeout = _positive_timeout(
            os.environ.get("POSTHOG_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        )
        result = post_batches(profiles, behaviors, host=host, token=token, timeout=timeout)
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "submitted",
                **result,
                "dataset_sha256": manifest["dataset_sha256"],
                "note": (
                    "HTTP success confirms batch receipt, not immediate query availability or "
                    "transactional deduplication."
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

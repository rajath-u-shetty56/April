import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from scripts.mock_analytics_dataset import build_dataset
from scripts.posthog_mock_analytics_dataset import (
    _positive_timeout,
    build_manifest,
    post_batches,
    request_batch,
    translate_event,
    validate_host,
    validate_translated_dataset,
)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _SequencedOpener:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def open(self, _request, timeout):
        assert timeout == 4.0
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_behavioral_event_translation_preserves_envelope_and_maps_groups():
    source = {
        "uuid": "d8dc44b9-d7f7-4ca7-8898-78ae60b71705",
        "event": "ticket_created",
        "distinct_id": "helpdesk:acme:user:42",
        "timestamp": "2026-08-15T10:30:00Z",
        "groups": {"account": "acme"},
        "properties": {"product": "helpdesk", "version": 1, "ticket_id": "T-100"},
    }

    assert translate_event(source) == {
        "uuid": "d8dc44b9-d7f7-4ca7-8898-78ae60b71705",
        "event": "ticket_created",
        "distinct_id": "helpdesk:acme:user:42",
        "timestamp": "2026-08-15T10:30:00Z",
        "properties": {
            "product": "helpdesk",
            "version": 1,
            "ticket_id": "T-100",
            "$groups": {"account": "acme"},
        },
    }


def test_groupidentify_translation_preserves_group_fields_without_plain_groups():
    source = build_dataset().profile_events[0]

    translated = translate_event(source)

    assert translated["event"] == "$groupidentify"
    assert translated["uuid"] == source["uuid"]
    assert translated["timestamp"] == source["timestamp"]
    assert translated["distinct_id"] == "system:billing"
    assert translated["properties"] == source["properties"]
    assert "groups" not in translated
    assert "$groups" not in translated["properties"]


def test_every_translated_event_preserves_the_complete_source_contract():
    dataset = build_dataset()

    for source in (*dataset.profile_events, *dataset.behavioral_events):
        translated = translate_event(source)
        assert {key: translated[key] for key in ("uuid", "event", "distinct_id", "timestamp")} == {
            key: source[key] for key in ("uuid", "event", "distinct_id", "timestamp")
        }
        expected_properties = dict(source["properties"])
        if source["event"] != "$groupidentify":
            expected_properties["$groups"] = source["groups"]
        assert translated["properties"] == expected_properties


def test_manifest_is_stable_and_describes_the_complete_source_dataset():
    manifest = build_manifest(build_dataset())

    assert manifest == {
        "profile_event_count": 13,
        "behavioral_event_count": 3514,
        "total_event_count": 3527,
        "first_timestamp": "2026-07-05T00:00:00Z",
        "last_timestamp": "2026-09-14T09:02:00Z",
        "event_counts_by_product": {
            "bi": 100,
            "contact_center": 2661,
            "helpdesk": 633,
            "rise": 120,
        },
        "event_counts_by_name": {
            "$groupidentify": 13,
            "barge_started": 40,
            "call_connected": 474,
            "call_ended": 474,
            "call_hold_ended": 150,
            "call_hold_started": 150,
            "call_initiated": 503,
            "call_summary_generated": 239,
            "call_transfer_completed": 163,
            "call_transfer_initiated": 238,
            "callback_fulfilled": 44,
            "callback_requested": 102,
            "course_assigned": 40,
            "course_completed": 40,
            "course_started": 40,
            "dashboard_created": 20,
            "dashboard_viewed": 20,
            "listen_started": 43,
            "macro_applied": 90,
            "report_created": 30,
            "report_viewed": 30,
            "sla_policy_created": 40,
            "ticket_created": 253,
            "ticket_resolved": 250,
            "whisper_started": 41,
        },
        "account_count": 10,
        "dataset_sha256": "e58716ba6e38e497d5b04d8de06e5a2c473d31bb6f7bdbb2a1422b3ea82f50ed",
    }


def test_validation_rejects_naive_timestamps_and_missing_products():
    dataset = build_dataset()
    profiles = [translate_event(event) for event in dataset.profile_events]
    behaviors = [translate_event(event) for event in dataset.behavioral_events[:2]]
    behaviors[0]["timestamp"] = "2026-08-15T10:30:00"
    behaviors[1]["properties"].pop("product")

    with pytest.raises(ValueError, match="timezone-aware timestamp"):
        validate_translated_dataset(profiles, behaviors)


@pytest.mark.parametrize(
    "host",
    [
        "ftp://posthog.example",
        "https://token@posthog.example",
        "https://posthog.example/path",
        "https://posthog.example/?token=secret",
        "https://posthog.example:not-a-port",
    ],
)
def test_host_validation_rejects_unsafe_or_ambiguous_urls(host):
    with pytest.raises(ValueError, match="POSTHOG_HOST"):
        validate_host(host)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e999", "0", "-1"])
def test_timeout_validation_rejects_non_finite_and_non_positive_values(value):
    with pytest.raises(ValueError, match="POSTHOG_TIMEOUT_SECONDS"):
        _positive_timeout(value)


def test_batch_sender_sends_profile_batches_before_behavioral_batches():
    calls = []

    def request(host, token, events, timeout):
        calls.append((host, token, [event["event"] for event in events], timeout))

    post_batches(
        [{"event": "$groupidentify"}],
        [{"event": "ticket_created"}, {"event": "ticket_resolved"}],
        host="https://eu.i.posthog.com",
        token="phc_test",
        timeout=4.0,
        batch_size=1,
        request=request,
    )

    assert [events for _, _, events, _ in calls] == [
        ["$groupidentify"],
        ["ticket_created"],
        ["ticket_resolved"],
    ]


def test_batch_request_retries_transient_http_failures_with_same_payload():
    opener = _SequencedOpener(
        [
            urllib.error.HTTPError("https://example.test/batch/", 503, "", {}, None),
            _Response(),
        ]
    )
    delays = []

    request_batch(
        "https://example.test",
        "phc_test",
        [{"uuid": "event-1"}],
        4.0,
        opener=opener,
        sleep=delays.append,
    )

    assert opener.calls == 2
    assert delays == [0.5]


def test_batch_request_reports_failure_without_exposing_token():
    opener = _SequencedOpener(
        [urllib.error.HTTPError("https://example.test/batch/", 400, "", {}, None)]
    )

    with pytest.raises(RuntimeError, match="HTTP 400") as error:
        request_batch(
            "https://example.test",
            "super-secret-token",
            [{"uuid": "event-1"}],
            4.0,
            opener=opener,
        )

    assert "super-secret-token" not in str(error.value)


def test_script_without_mode_exits_without_reading_credentials():
    repository = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [sys.executable, "scripts/posthog_mock_analytics_dataset.py"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env={},
    )

    assert result.returncode == 2
    assert "--describe, --dry-run, or --send" in result.stderr
    assert result.stdout == ""


def test_dry_run_needs_no_token_and_prints_only_sanitized_sample():
    repository = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [sys.executable, "scripts/posthog_mock_analytics_dataset.py", "--dry-run"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env={"POSTHOG_PROJECT_TOKEN": "must-not-appear"},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "valid"
    assert payload["manifest"]["total_event_count"] == 3527
    assert len(payload["sample_events"]) == 3
    assert "must-not-appear" not in result.stdout

#!/usr/bin/env python3
"""Exercise the supplied analytics ingestion contract using only HTTP."""

import copy
import http.client
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Report redirects as responses instead of masking them or forwarding keys."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sanitize(value, secrets):
    """Redact known credentials, including echoes in JSON field names."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {sanitize(k, secrets): sanitize(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    return value


def reject_non_json_constant(value):
    raise ValueError("Non-JSON numeric constant")


def check_fields(body, expected, failures, label="response"):
    if not isinstance(body, dict):
        failures.append(f"{label}: expected a JSON object")
        return
    for field, value in expected.items():
        actual = body.get(field)
        # JSON false must not pass for 0, nor true for 1.
        if field not in body or type(actual) is not type(value) or actual != value:
            failures.append(f"{label}.{field}: expected {json.dumps(value)}")


def main():
    key = os.environ.get("ANALYTICS_INGESTION_KEY", "")
    if not key.strip():
        print("Configuration error: set ANALYTICS_INGESTION_KEY to an existing "
              "ingestion credential, then rerun this script.", file=sys.stderr)
        return 2
    if any(character in key for character in "\r\n"):
        print("Configuration error: ANALYTICS_INGESTION_KEY must not contain "
              "line breaks.", file=sys.stderr)
        return 2

    # Change exactly the final character; never print either credential.
    invalid_key = key[:-1] + ("A" if key[-1] != "A" else "B")
    secrets = (key, invalid_key)
    base_url = os.environ.get("ANALYTICS_BASE_URL", "http://localhost:8000").rstrip("/")
    account = os.environ.get("ANALYTICS_ACCOUNT", "smoke-test-account")
    try:
        parsed_url = urllib.parse.urlsplit(base_url)
        if (parsed_url.scheme not in ("http", "https") or not parsed_url.hostname
                or parsed_url.username is not None or parsed_url.password is not None
                or parsed_url.query or parsed_url.fragment):
            raise ValueError("ANALYTICS_BASE_URL must be an HTTP(S) URL without "
                             "embedded credentials, a query, or a fragment")
        # Accessing .port also validates malformed or out-of-range port values.
        _validated_port = parsed_url.port
        timeout = float(os.environ.get("ANALYTICS_TIMEOUT_SECONDS", "10"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("ANALYTICS_TIMEOUT_SECONDS must be finite and positive")
    except ValueError:
        print("Configuration error: check ANALYTICS_BASE_URL (HTTP(S), no embedded "
              "credentials/query/fragment), ANALYTICS_TIMEOUT_SECONDS (positive "
              "finite number).",
              file=sys.stderr)
        return 2

    opener = urllib.request.build_opener(NoRedirects())
    passed = 0
    failed = 0

    def check(name, path, expected_status, payload=None, expected=None,
              request_key=None, bulk_events=None):
        nonlocal passed, failed
        status = None
        body = None
        failures = []
        try:
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                base_url + path,
                data=data,
                method="GET" if payload is None else "POST",
                headers={
                    "Authorization": "Bearer " + (key if request_key is None else request_key),
                    "Content-Type": "application/json",
                },
            )
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                # HTTP error bodies carry contract results too.
                response = error
            with response:
                status = response.code
                raw_body = response.read()
            try:
                body = json.loads(raw_body, parse_constant=reject_non_json_constant)
            except (ValueError, UnicodeError, RecursionError):
                body = {"unparsed_body": raw_body.decode("utf-8", errors="replace")}
                failures.append("Response is not valid JSON")
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            # Avoid exception strings, which may contain request details or secrets.
            failures.append("Request failed: connection, timeout, URL, or HTTP transport error")

        if status != expected_status:
            failures.append(f"Expected HTTP {expected_status}")
        if not failures:
            if expected is not None:
                check_fields(body, expected, failures)
            if bulk_events is not None and isinstance(body, dict):
                results = body.get("results")
                if not isinstance(results, list) or len(results) != len(bulk_events):
                    failures.append("response.results: expected three results in input order")
                else:
                    for index, event in enumerate(bulk_events[:2]):
                        check_fields(results[index], {
                            "uuid": event["uuid"], "status": "accepted",
                        }, failures, f"response.results[{index}]")
                    check_fields(results[2], {
                        "status": "rejected", "code": "INVALID_DISTINCT_ID",
                    }, failures, "response.results[2]")
                    # The contract does not require a UUID on validation errors.
                    if isinstance(results[2], dict) and "uuid" in results[2]:
                        check_fields(results[2], {"uuid": bulk_events[2]["uuid"]},
                                     failures, "response.results[2]")

        print(f"\n{name}")
        print(f"HTTP status: {status if status is not None else 'unavailable'}")
        print("Response: " + json.dumps(sanitize(body, secrets), indent=2))
        if failures:
            failed += 1
            print("FAIL: " + sanitize("; ".join(failures), secrets))
        else:
            passed += 1
            print("PASS")
        sys.stdout.flush()

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def event(product, name, distinct_id):
        return {
            "uuid": str(uuid.uuid4()),
            "event": name,
            "distinct_id": distinct_id,
            "timestamp": timestamp,
            "groups": {"account": account},
            "properties": {"product": product, "smoke_test": True},
        }

    check("1. Health check", "/health/", 200)

    helpdesk = event("helpdesk", "ticket_created", "helpdesk:agent:42")
    helpdesk["properties"].update({
        "ticket_id": "SMOKE-TICKET-100", "channel": "email",
        "priority": "high", "version": 1,
    })
    check("2. New Helpdesk event", "/api/v1/capture/", 201, helpdesk, {
        "uuid": helpdesk["uuid"], "status": "accepted", "duplicate": False,
    })
    # The original object is unchanged, including UUID, timestamp, and field order.
    check("3. Identical retry", "/api/v1/capture/", 200, helpdesk, {
        "uuid": helpdesk["uuid"], "status": "accepted", "duplicate": True,
    })
    conflict = copy.deepcopy(helpdesk)
    conflict["properties"]["priority"] = "low"
    check("4. Conflicting retry", "/api/v1/capture/", 409, conflict, {
        "uuid": helpdesk["uuid"], "status": "rejected",
        "code": "UUID_CONFLICT", "field": "uuid",
    })

    bi = event("bi", "report_created", "bi:user:17")
    contact_center = event("contact_center", "call_completed", "contact_center:agent:91")
    workflows = event("workflows", "execution_completed", "workflows:user:24")
    del workflows["distinct_id"]
    bulk_events = [bi, contact_center, workflows]
    check("5. Cross-product bulk request", "/api/v1/bulk/", 200,
          {"events": bulk_events}, {"accepted": 2, "rejected": 1},
          bulk_events=bulk_events)

    unknown = event("helpdesk", "ticket_created", "helpdesk:agent:42")
    unknown["unexpected_field"] = "must be rejected"
    check("6. Unknown envelope field", "/api/v1/capture/", 400, unknown,
          {"code": "UNKNOWN_ENVELOPE_FIELD"})

    check("7. Invalid credential", "/api/v1/capture/", 401,
          event("helpdesk", "ticket_created", "helpdesk:agent:42"),
          {"code": "INVALID_CREDENTIAL"}, request_key=invalid_key)

    no_product = event("helpdesk", "ticket_created", "helpdesk:agent:42")
    del no_product["properties"]["product"]
    check("8a. Optional product context", "/api/v1/capture/", 201, no_product,
          {"status": "accepted", "duplicate": False})
    no_account = event("helpdesk", "ticket_created", "helpdesk:agent:42")
    no_account["groups"] = {}
    check("8b. Optional account context", "/api/v1/capture/", 201, no_account,
          {"status": "accepted", "duplicate": False})

    print(f"\nTotal passed: {passed}")
    print(f"Total failed: {failed}")
    print("All checks passed: " + ("YES" if failed == 0 else "NO"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

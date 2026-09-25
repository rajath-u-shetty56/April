#!/usr/bin/env python3
"""Exercise production-like ingestion edge cases using only the public HTTP API."""

import concurrent.futures
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
from dataclasses import dataclass
from datetime import UTC, datetime


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return redirects to the caller instead of forwarding credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class HTTPResult:
    status: int | None
    headers: dict[str, str]
    body: object
    raw_text: str
    transport_failed: bool = False


def reject_non_json_constant(_value):
    raise ValueError("Non-JSON numeric constant")


def sanitize(value, secrets):
    """Redact supplied credentials from all displayed values."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {sanitize(key, secrets): sanitize(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    return value


def nested_field(body, path):
    current = body
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


class EdgeCaseRunner:
    def __init__(self, *, base_url, key, timeout):
        self.base_url = base_url
        self.key = key
        self.timeout = timeout
        self.invalid_key = key[:-1] + ("A" if key[-1] != "A" else "B")
        self.secrets = (key, self.invalid_key)
        self.opener = urllib.request.build_opener(NoRedirects())
        self.passed = 0
        self.failed = 0

    def request(
        self,
        path,
        *,
        payload=None,
        raw_body=None,
        method="POST",
        authorization="default",
        content_type="application/json",
    ):
        if raw_body is not None:
            data = raw_body
        elif payload is not None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            data = None

        headers = {}
        if authorization == "default":
            headers["Authorization"] = "Bearer " + self.key
        elif authorization is not None:
            headers["Authorization"] = authorization
        if content_type is not None:
            headers["Content-Type"] = content_type

        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                response_headers = dict(response.headers.items())
                raw_response = response.read()
            raw_text = raw_response.decode("utf-8", errors="replace")
            try:
                body = json.loads(raw_text, parse_constant=reject_non_json_constant)
            except (ValueError, UnicodeError, RecursionError):
                body = {"unparsed_body": raw_text}
            return HTTPResult(status, response_headers, body, raw_text)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            return HTTPResult(None, {}, None, "", transport_failed=True)

    def response_failures(
        self,
        result,
        *,
        expected_status,
        expected=None,
        ingestion_response=True,
    ):
        failures = []
        if result.transport_failed:
            failures.append("HTTP request could not be completed")
        if result.status != expected_status:
            failures.append(f"expected HTTP {expected_status}, received {result.status}")
        if expected:
            for path, wanted in expected.items():
                present, actual = nested_field(result.body, path)
                if not present or type(actual) is not type(wanted) or actual != wanted:
                    failures.append(f"{path}: expected {wanted!r}, received {actual!r}")
        if ingestion_response:
            headers = {key.lower(): value for key, value in result.headers.items()}
            if not headers.get("x-request-id", "").strip():
                failures.append("missing X-Request-ID header")
            if "no-store" not in headers.get("cache-control", "").lower():
                failures.append("missing Cache-Control: no-store header")
        for secret in self.secrets:
            if secret and secret in result.raw_text:
                failures.append("response exposed an ingestion credential")
        return failures

    def report(self, name, result, failures):
        print(f"\n{name}")
        print(f"HTTP status: {result.status if result.status is not None else 'unavailable'}")
        print("Response: " + json.dumps(sanitize(result.body, self.secrets), indent=2))
        if failures:
            self.failed += 1
            print("FAIL: " + sanitize("; ".join(failures), self.secrets))
        else:
            self.passed += 1
            print("PASS")
        sys.stdout.flush()

    def check(self, name, path, expected_status, **kwargs):
        expected = kwargs.pop("expected", None)
        ingestion_response = kwargs.pop("ingestion_response", True)
        result = self.request(path, **kwargs)
        failures = self.response_failures(
            result,
            expected_status=expected_status,
            expected=expected,
            ingestion_response=ingestion_response,
        )
        self.report(name, result, failures)
        return result

    def custom(self, name, result, failures):
        self.report(name, result, failures)


def event(*, account, product, name, distinct_id, timestamp=None, event_uuid=None):
    return {
        "uuid": event_uuid or str(uuid.uuid4()),
        "event": name,
        "distinct_id": distinct_id,
        "timestamp": timestamp or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "groups": {"account": account},
        "properties": {"product": product, "edge_case_test": True},
    }


def validate_configuration():
    key = os.environ.get("ANALYTICS_INGESTION_KEY", "")
    if not key.strip() or any(character in key for character in "\r\n"):
        raise ValueError("ANALYTICS_INGESTION_KEY must be a nonempty single-line credential")

    base_url = os.environ.get("ANALYTICS_BASE_URL", "http://localhost:8000").rstrip("/")
    parsed_url = urllib.parse.urlsplit(base_url)
    if (
        parsed_url.scheme not in ("http", "https")
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("ANALYTICS_BASE_URL must be an HTTP(S) URL without credentials or query")
    _validated_port = parsed_url.port

    timeout = float(os.environ.get("ANALYTICS_TIMEOUT_SECONDS", "10"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("ANALYTICS_TIMEOUT_SECONDS must be finite and positive")

    required_context = os.environ.get("ANALYTICS_EXPECT_REQUIRED_CONTEXT", "true").lower()
    if required_context not in ("true", "false"):
        raise ValueError("ANALYTICS_EXPECT_REQUIRED_CONTEXT must be true or false")

    max_property_bytes = int(os.environ.get("ANALYTICS_MAX_PROPERTY_BYTES", "65536"))
    max_request_bytes = int(os.environ.get("ANALYTICS_MAX_REQUEST_BYTES", "1048576"))
    max_batch_events = int(os.environ.get("ANALYTICS_MAX_BATCH_EVENTS", "500"))
    if min(max_property_bytes, max_request_bytes, max_batch_events) <= 0:
        raise ValueError("ingestion limits must be positive")
    return {
        "key": key,
        "base_url": base_url,
        "timeout": timeout,
        "required_context": required_context == "true",
        "max_property_bytes": max_property_bytes,
        "max_request_bytes": max_request_bytes,
        "max_batch_events": max_batch_events,
    }


def run_success_and_idempotency_cases(runner, account):
    complex_event = event(
        account=account,
        product="helpdesk",
        name="edge_case_complex_event",
        distinct_id="helpdesk:agent:47",
        timestamp="2026-09-24T10:30:00Z",
    )
    complex_event["groups"]["region"] = "ap-south-1"
    complex_event["properties"].update(
        {
            "unicode": "ನಮಸ್ಕಾರ 👋 café",
            "nullable": None,
            "enabled": True,
            "score": 12.5,
            "tags": ["urgent", "vip"],
            "nested": {"channel": "email", "attempt": 1},
        }
    )
    runner.check(
        "2. Complex nested event",
        "/api/v1/capture/",
        201,
        payload=complex_event,
        expected={
            "uuid": complex_event["uuid"],
            "status": "accepted",
            "duplicate": False,
        },
    )
    runner.check(
        "3. Identical complex-event retry",
        "/api/v1/capture/",
        200,
        payload=complex_event,
        expected={"uuid": complex_event["uuid"], "status": "accepted", "duplicate": True},
    )

    equivalent = copy.deepcopy(complex_event)
    equivalent["timestamp"] = "2026-09-24T16:00:00+05:30"
    equivalent["properties"] = dict(reversed(list(equivalent["properties"].items())))
    runner.check(
        "4. Equivalent timezone and reordered JSON retry",
        "/api/v1/capture/",
        200,
        payload=equivalent,
        expected={"uuid": complex_event["uuid"], "status": "accepted", "duplicate": True},
    )

    conflict = copy.deepcopy(complex_event)
    conflict["properties"]["score"] = 99
    runner.check(
        "5. Conflicting content with reused UUID",
        "/api/v1/capture/",
        409,
        payload=conflict,
        expected={"status": "rejected", "code": "UUID_CONFLICT", "field": "uuid"},
    )

    generated = {
        "event": "edge_case_generated_identifiers",
        "distinct_id": "bi:user:2",
        "groups": {"account": account},
        "properties": {"product": "bi", "edge_case_test": True},
    }
    created = runner.check(
        "6. Server-generated UUID and timestamp",
        "/api/v1/capture/",
        201,
        payload=generated,
        expected={"status": "accepted", "duplicate": False},
    )
    generated_uuid = created.body.get("uuid") if isinstance(created.body, dict) else None
    retry = copy.deepcopy(generated)
    retry["uuid"] = generated_uuid
    if generated_uuid:
        runner.check(
            "7. Retry without timestamp preserves stored occurrence time",
            "/api/v1/capture/",
            200,
            payload=retry,
            expected={"uuid": generated_uuid, "status": "accepted", "duplicate": True},
        )
    else:
        runner.custom(
            "7. Retry without timestamp preserves stored occurrence time",
            HTTPResult(None, {}, None, ""),
            ["previous response did not contain a generated UUID"],
        )

    shared_name = "edge_case_shared_product_event"
    helpdesk = event(
        account=account,
        product="helpdesk",
        name=shared_name,
        distinct_id="helpdesk:agent:47",
    )
    bi = event(
        account=account,
        product="bi",
        name=shared_name,
        distinct_id="bi:user:2",
    )
    runner.check(
        "8a. Shared event name from Helpdesk",
        "/api/v1/capture/",
        201,
        payload=helpdesk,
        expected={"status": "accepted", "duplicate": False},
    )
    runner.check(
        "8b. Shared event name from BI",
        "/api/v1/capture/",
        201,
        payload=bi,
        expected={"status": "accepted", "duplicate": False},
    )


def run_envelope_validation_cases(runner, account):
    valid = event(
        account=account,
        product="helpdesk",
        name="edge_case_validation",
        distinct_id="helpdesk:agent:47",
    )
    cases = []

    def changed(label, field, value, code, response_field):
        payload = copy.deepcopy(valid)
        if value is _MISSING:
            payload.pop(field)
        else:
            payload[field] = value
        cases.append((label, payload, code, response_field))

    changed("missing event", "event", _MISSING, "INVALID_EVENT", "event")
    changed("blank event", "event", "   ", "INVALID_EVENT", "event")
    changed("missing distinct_id", "distinct_id", _MISSING, "INVALID_DISTINCT_ID", "distinct_id")
    changed("blank distinct_id", "distinct_id", " ", "INVALID_DISTINCT_ID", "distinct_id")
    changed("numeric UUID", "uuid", 42, "INVALID_UUID", "uuid")
    changed("malformed UUID", "uuid", "not-a-uuid", "INVALID_UUID", "uuid")
    changed("malformed timestamp", "timestamp", "yesterday", "INVALID_TIMESTAMP", "timestamp")
    changed("array groups", "groups", [], "INVALID_GROUPS", "groups")
    changed("array properties", "properties", [], "INVALID_PROPERTIES", "properties")

    unknown = copy.deepcopy(valid)
    unknown["unexpected"] = "value"
    cases.append(("unknown envelope field", unknown, "UNKNOWN_ENVELOPE_FIELD", None))
    project_id = copy.deepcopy(valid)
    project_id["project_id"] = str(uuid.uuid4())
    cases.append(("payload-supplied project_id", project_id, "UNKNOWN_ENVELOPE_FIELD", None))

    for index, (label, payload, code, response_field) in enumerate(cases, start=9):
        runner.check(
            f"{index}. Reject {label}",
            "/api/v1/capture/",
            400,
            payload=payload,
            expected={"status": "rejected", "code": code, "field": response_field},
        )

    runner.check(
        "20. Reject array as event envelope",
        "/api/v1/capture/",
        400,
        payload=[valid],
        expected={"status": "rejected", "code": "INVALID_ENVELOPE"},
    )
    runner.check(
        "21. Reject null as event envelope",
        "/api/v1/capture/",
        400,
        raw_body=b"null",
        expected={"status": "rejected", "code": "INVALID_ENVELOPE"},
    )


def run_context_cases(runner, account, required_context):
    base = event(
        account=account,
        product="helpdesk",
        name="edge_case_context",
        distinct_id="helpdesk:agent:47",
    )
    invalid_products = [
        ("blank", ""),
        ("padded", " helpdesk "),
        ("numeric", 17),
        ("long", "p" * 81),
    ]
    for suffix, product in invalid_products:
        payload = copy.deepcopy(base)
        payload["properties"]["product"] = product
        runner.check(
            f"22.{suffix}. Reject {suffix} product key",
            "/api/v1/capture/",
            400,
            payload=payload,
            expected={
                "status": "rejected",
                "code": "INVALID_PROPERTIES",
                "field": "properties.product",
            },
        )

    if required_context:
        no_product = copy.deepcopy(base)
        del no_product["properties"]["product"]
        runner.check(
            "23a. Enforce required product",
            "/api/v1/capture/",
            400,
            payload=no_product,
            expected={
                "status": "rejected",
                "code": "REQUIRED_PROPERTY",
                "field": "properties.product",
            },
        )
        for suffix, account_value in (("missing", _MISSING), ("blank", ""), ("numeric", 17)):
            payload = copy.deepcopy(base)
            if account_value is _MISSING:
                del payload["groups"]["account"]
            else:
                payload["groups"]["account"] = account_value
            runner.check(
                f"23.{suffix}. Enforce required {suffix} account",
                "/api/v1/capture/",
                400,
                payload=payload,
                expected={
                    "status": "rejected",
                    "code": "REQUIRED_PROPERTY",
                    "field": "groups.account",
                },
            )
    else:
        print("\n23. Required product/account checks: SKIPPED by configuration")


def run_transport_and_limit_cases(runner, account, config):
    minimal = event(
        account=account,
        product="helpdesk",
        name="edge_case_transport",
        distinct_id="helpdesk:agent:47",
    )
    runner.check(
        "24. Reject malformed JSON",
        "/api/v1/capture/",
        400,
        raw_body=b'{"event":',
        expected={"status": "rejected", "code": "INVALID_JSON"},
    )
    runner.check(
        "25. Reject non-finite JSON number",
        "/api/v1/capture/",
        400,
        raw_body=(
            b'{"event":"edge","distinct_id":"helpdesk:agent:47",'
            b'"properties":{"product":"helpdesk","number":NaN}}'
        ),
        expected={"status": "rejected", "code": "INVALID_JSON"},
    )
    runner.check(
        "26. Reject invalid UTF-8 JSON",
        "/api/v1/capture/",
        400,
        raw_body=b'{"event":"\xff"}',
        expected={"status": "rejected", "code": "INVALID_JSON"},
    )
    runner.check(
        "27. Reject unsupported content type",
        "/api/v1/capture/",
        415,
        raw_body=json.dumps(minimal).encode(),
        content_type="text/plain",
        expected={"status": "rejected", "code": "UNSUPPORTED_MEDIA_TYPE"},
    )

    too_large_property = copy.deepcopy(minimal)
    too_large_property["uuid"] = str(uuid.uuid4())
    too_large_property["properties"]["blob"] = "x" * config["max_property_bytes"]
    runner.check(
        "28. Reject oversized properties",
        "/api/v1/capture/",
        413,
        payload=too_large_property,
        expected={"status": "rejected", "code": "PROPERTIES_TOO_LARGE", "field": "properties"},
    )

    oversized_request = b'{}' + b" " * config["max_request_bytes"]
    runner.check(
        "29. Reject oversized HTTP request",
        "/api/v1/capture/",
        413,
        raw_body=oversized_request,
        expected={"status": "rejected", "code": "REQUEST_TOO_LARGE"},
    )
    runner.check(
        "30. Reject batch exceeding configured count",
        "/api/v1/bulk/",
        413,
        payload={"events": [{} for _ in range(config["max_batch_events"] + 1)]},
        expected={"status": "rejected", "code": "BATCH_TOO_LARGE", "field": "events"},
    )


def run_bulk_case(runner, account):
    first = event(
        account=account,
        product="bi",
        name="edge_case_bulk_valid",
        distinct_id="bi:user:2",
    )
    second = event(
        account=account,
        product="contact_center",
        name="edge_case_bulk_valid",
        distinct_id="contact_center:agent:91",
    )
    invalid = event(
        account=account,
        product="workflows",
        name="edge_case_bulk_invalid",
        distinct_id="workflows:user:24",
    )
    del invalid["distinct_id"]
    duplicate = copy.deepcopy(first)
    conflict = copy.deepcopy(first)
    conflict["properties"]["changed"] = True
    items = [first, invalid, second, duplicate, conflict]
    result = runner.request("/api/v1/bulk/", payload={"events": items})
    failures = runner.response_failures(
        result,
        expected_status=200,
        expected={"accepted": 3, "rejected": 2},
    )
    results = result.body.get("results") if isinstance(result.body, dict) else None
    if not isinstance(results, list) or len(results) != 5:
        failures.append("results must contain five entries in input order")
    else:
        expected = [
            ("accepted", None, False),
            ("rejected", "INVALID_DISTINCT_ID", None),
            ("accepted", None, False),
            ("accepted", None, True),
            ("rejected", "UUID_CONFLICT", None),
        ]
        for index, (status, code, duplicate_value) in enumerate(expected):
            item = results[index]
            if not isinstance(item, dict) or item.get("status") != status:
                failures.append(f"results[{index}] has incorrect status")
                continue
            if code is not None and item.get("code") != code:
                failures.append(f"results[{index}] has incorrect rejection code")
            if duplicate_value is not None and item.get("duplicate") is not duplicate_value:
                failures.append(f"results[{index}] has incorrect duplicate flag")
    runner.custom("31. Mixed bulk preserves order and partial commits", result, failures)


def run_authentication_cases(runner, account):
    payload = event(
        account=account,
        product="helpdesk",
        name="edge_case_authentication",
        distinct_id="helpdesk:agent:47",
    )
    runner.check(
        "32. Reject missing authorization",
        "/api/v1/capture/",
        401,
        payload=payload,
        authorization=None,
        expected={"status": "rejected", "code": "INVALID_CREDENTIAL"},
    )
    runner.check(
        "33. Reject incorrect bearer secret",
        "/api/v1/capture/",
        401,
        payload=payload,
        authorization="Bearer " + runner.invalid_key,
        expected={"status": "rejected", "code": "INVALID_CREDENTIAL"},
    )
    runner.check(
        "34. Reject unsupported authorization scheme",
        "/api/v1/capture/",
        401,
        payload=payload,
        authorization="Basic " + runner.key,
        expected={"status": "rejected", "code": "INVALID_CREDENTIAL"},
    )
    runner.check(
        "35. Reject unsupported capture method",
        "/api/v1/capture/",
        405,
        method="GET",
        expected={"status": "rejected", "code": "METHOD_NOT_ALLOWED"},
    )


def run_concurrency_case(runner, account):
    payload = event(
        account=account,
        product="helpdesk",
        name="edge_case_concurrent_retry",
        distinct_id="helpdesk:agent:47",
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(runner.request, "/api/v1/capture/", payload=payload) for _ in range(8)
        ]
        results = [future.result() for future in futures]

    failures = []
    statuses = [result.status for result in results]
    if statuses.count(201) != 1 or statuses.count(200) != 7:
        failures.append(f"expected one HTTP 201 and seven HTTP 200 responses, received {statuses}")
    for index, result in enumerate(results):
        item_failures = runner.response_failures(
            result,
            expected_status=result.status if result.status in (200, 201) else 200,
            expected={"uuid": payload["uuid"], "status": "accepted"},
        )
        failures.extend(f"request {index}: {failure}" for failure in item_failures)
    duplicate_values = [
        result.body.get("duplicate") for result in results if isinstance(result.body, dict)
    ]
    if duplicate_values.count(False) != 1 or duplicate_values.count(True) != 7:
        failures.append("expected one inserted event and seven duplicate responses")
    summary = HTTPResult(
        200 if not failures else None,
        {},
        {"statuses": sorted(status for status in statuses if status is not None)},
        "",
    )
    runner.custom("36. Concurrent identical requests are idempotent", summary, failures)


_MISSING = object()


def main():
    try:
        config = validate_configuration()
    except (ValueError, OverflowError):
        print(
            "Configuration error: check ANALYTICS_INGESTION_KEY, ANALYTICS_BASE_URL, "
            "ANALYTICS_TIMEOUT_SECONDS, ANALYTICS_EXPECT_REQUIRED_CONTEXT, and limit values.",
            file=sys.stderr,
        )
        return 2

    account = os.environ.get("ANALYTICS_ACCOUNT", "edge-case-test-account")
    runner = EdgeCaseRunner(
        base_url=config["base_url"],
        key=config["key"],
        timeout=config["timeout"],
    )
    runner.check("1. Health check", "/health/", 200, method="GET", ingestion_response=False)
    run_success_and_idempotency_cases(runner, account)
    run_envelope_validation_cases(runner, account)
    run_context_cases(runner, account, config["required_context"])
    run_transport_and_limit_cases(runner, account, config)
    run_bulk_case(runner, account)
    run_authentication_cases(runner, account)
    run_concurrency_case(runner, account)

    print(f"\nTotal passed: {runner.passed}")
    print(f"Total failed: {runner.failed}")
    print("All checks passed: " + ("YES" if runner.failed == 0 else "NO"))
    return 0 if runner.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

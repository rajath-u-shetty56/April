# Event contract

Events describe meaningful business actions. Every event belongs to the project
selected by its ingestion credential. Producers must not send `project_id`; like
any unrecognized top-level envelope field, it is rejected as
`UNKNOWN_ENVELOPE_FIELD`.

```json
{
  "uuid": "01994fad-c340-7000-8000-000000000001",
  "event": "ticket_created",
  "distinct_id": "helpdesk:agent:42",
  "timestamp": "2026-09-17T10:30:00Z",
  "groups": {"account": "acme"},
  "properties": {"product": "helpdesk", "ticket_id": "T-100", "version": 1}
}
```

## Envelope and identity

- `event`: required nonblank string, at most 200 characters.
- `distinct_id`: required nonblank actor identifier, at most 400 characters.
  Producers must namespace actor IDs by product: `helpdesk:agent:42`, `bi:user:17`,
  or `workflows:user:24`. For actions without a human actor use `system:workflows`.
  The generic platform validates a nonblank identifier, not product-specific ID syntax.
- `uuid`: optional UUID string. Producers that may retry should generate a UUIDv7
  before the first attempt (other valid UUID versions are accepted) and reuse it
  for every retry. If omitted, the platform generates a UUIDv4 and returns it, but
  a caller that loses the response cannot know that UUID; retrying may store a
  duplicate occurrence.
- `timestamp`: optional datetime. Offsets normalize to UTC; naive datetimes are
  interpreted as UTC. Omission uses the request receipt time.
- `groups`: optional object mapping a group type to a group key, default `{}`.
  For example, `{"account": "acme", "team": "support"}` associates that event
  with both groups. Types and keys must be trimmed, nonblank strings. The default
  limit is five groups per event. HappyFox producers can use the immutable
  Helpdesk subdomain as the `account` key.
- `properties`: optional JSON object, default `{}`. When supplied,
  `properties.product` is a trimmed, nonblank string of at most 80 characters,
  such as `helpdesk` or `contact_center`. It becomes the event definition's
  `product_key`. HappyFox producers include it on every event.

Account-level cross-product analysis is supported within a project. User analysis
remains within each product; no cross-product user matching or identity graph is
implemented. Product/account conventions do not add database models.

Business properties remain flexible, including nested objects and arrays. The
platform does not enforce product-specific schemas or versions. Unknown envelope
fields are rejected; flexible fields inside `properties` are accepted.
Nested values remain available to exact structured filters, but analytics grouping dimensions are
limited to scalar properties so MCP results do not echo nested objects as bucket labels.
JSON must be representable in PostgreSQL: non-finite numbers, NUL characters, and
unpaired Unicode surrogates are rejected.

Ingestion also updates an observational catalog for top-level event properties,
scoped to the event's `EventDefinition`. `$group_set` keys update a separate
catalog scoped to project and group type. Each catalog records observed non-null
JSON types, whether a null was seen, and first/last occurrence timestamps, but
never stores sample values. Null plus one concrete type remains nullable without
a conflict; a conflict requires multiple incompatible non-null types. This
catalog does not make ingestion reject mixed property shapes.

`version` is optional producer-controlled metadata. Both `1` and `"1"` are stored
without version validation, preserving the existing Contact Center convention.
Adding an optional property does not need a new version; increment it for breaking
meaning or property-type changes. A different business action gets a new event name.

## Group associations and current properties

`groups` on an ordinary event records which account, team, organization, or other
business entity participated in that event. This association is stored on the
immutable event and can be used for group-level filtering and aggregation.

Current descriptive information about a group is updated through the reserved
`$groupidentify` event on the same capture or bulk endpoints:

```json
{
  "event": "$groupidentify",
  "distinct_id": "system:group-profile",
  "groups": {},
  "properties": {
    "$group_type": "account",
    "$group_key": "acme",
    "$group_set": {"name": "Acme", "plan": "enterprise"}
  }
}
```

`$group_set` is merged into the group's current properties. Existing keys not
included in an update are preserved. The event itself is stored for retry and
audit behavior, but it is not added to the business `EventDefinition` catalog.
Its top-level `$group_set` keys update the observational group property catalog.

The two concepts are deliberately separate:

- `groups` on a business event associates that one event with a group.
- `$groupidentify` updates the group's current descriptive profile.

Identifying a group later never adds it to older events that arrived without a
group. Current profile properties are not a historical snapshot. If a plan change
or another transition matters historically, send it as a business event or include
the event-time value in that business event's properties.

## Authentication

Use `Authorization: Bearer <ingestion-key>` over HTTPS. The credential selects the
project. Missing/invalid/revoked credentials and inactive projects/workspaces are
rejected with HTTP 401 and `INVALID_CREDENTIAL`. Staff session/token authentication
is for catalog management and cannot substitute for an ingestion credential.

Staff-only management routes:

- `GET/POST /api/v1/projects/{project_id}/ingestion-credentials/`
- `POST /api/v1/projects/{project_id}/ingestion-credentials/{credential_id}/rotate/`
- `POST /api/v1/projects/{project_id}/ingestion-credentials/{credential_id}/revoke/`

Creation accepts a human-readable `name`. Creation and rotation return credential
metadata plus `secret` once, with `Cache-Control:
no-store`. List responses never return a secret or hash. Store the secret securely
at creation; it cannot be retrieved later.

Rotation creates a new credential with the same name.
The previous credential remains active for a producer rollout; revoke it afterward.
Revocation is idempotent and permanent through these APIs. There is no reactivation
endpoint. An active credential is one whose `revoked_at` is null. Credentials are
independent, so different products can use separate credentials for one project.

## Capture and retries

`POST /api/v1/capture/` accepts one envelope as `application/json`.

New events return HTTP 201:

```json
{"uuid": "01994fad-c340-7000-8000-000000000001", "status": "accepted", "duplicate": false}
```

An identical retry returns HTTP 200 with `duplicate: true`. Uniqueness is scoped to
`project + uuid`. Comparison includes the event name, actor, groups, properties,
and a supplied timestamp. JSON key order, equivalent timezone offsets, and JSON
numeric representations do not change the event; booleans are distinct from numbers.
Omitting timestamp on a retry preserves the stored timestamp. Reusing a UUID with
conflicting content returns HTTP 409, `UUID_CONFLICT`, and field `uuid`.

Acceptance means the database transaction has successfully committed, including
new event/group property observations, event-definition discovery, and its latest-seen
timestamp. It does not imply a queue handoff. Received time is distinct from producer
occurrence time and is unchanged on retry.

## Bulk ingestion

`POST /api/v1/bulk/` accepts `{"events": [<envelope>, ...]}`. Authentication happens
once. Each item validates and commits independently. Results retain input order;
accepted counts include identical duplicates. A well-formed batch returns HTTP
200 even when some or all items are rejected:

```json
{
  "accepted": 1,
  "rejected": 1,
  "results": [
    {"uuid": "01994fad-c340-7000-8000-000000000001", "status": "accepted", "duplicate": false},
    {"uuid": null, "status": "rejected", "code": "INVALID_TIMESTAMP", "field": "timestamp"}
  ]
}
```

Authentication errors, invalid JSON, a missing/non-array/empty `events` value, and
batch/request limits reject the complete request. Valid earlier items remain
committed if a later storage operation fails. Retry uncertain requests using the
original event UUIDs; a lost response cannot undo an already committed event.

## Limits and errors

Environment-configurable defaults:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `INGESTION_MAX_REQUEST_BYTES` | 1048576 | Raw UTF-8 HTTP body bytes |
| `INGESTION_MAX_PROPERTY_BYTES` | 65536 | Compact UTF-8 encoding of each properties object |
| `INGESTION_MAX_BATCH_EVENTS` | 500 | Items in one nonempty batch |
| `INGESTION_MAX_GROUPS` | 5 | Group associations on one event |

The parser reads at most the request limit plus one byte. Property limits are
per-item; group count is bounded separately. Exceeding a request/batch limit
returns HTTP 413; oversized properties return 413 for capture or an item rejection
in bulk. Configure the front proxy's request limit consistently.

Rejections return `uuid` (valid supplied UUID or null), `status: rejected`, `code`,
and `field` (null for request-wide errors). Responses carry a generated
`X-Request-ID`; user-supplied request IDs are not echoed.

Stable codes include `INVALID_CREDENTIAL`, `INVALID_JSON`, `INVALID_BATCH`,
`INVALID_ENVELOPE`, `UNKNOWN_ENVELOPE_FIELD`,
`INVALID_EVENT`, `INVALID_DISTINCT_ID`, `INVALID_UUID`, `INVALID_TIMESTAMP`,
`INVALID_GROUPS`, `INVALID_GROUP_IDENTIFY`, `INVALID_PROPERTIES`,
`REQUEST_TOO_LARGE`, `PROPERTIES_TOO_LARGE`, `BATCH_TOO_LARGE`, `UUID_CONFLICT`,
and `STORAGE_UNAVAILABLE`. Storage failures return HTTP 503 for capture and an
individual rejection in bulk; retry using the same UUID. Malformed envelope
values return HTTP 400. Unsupported content types return HTTP 415.

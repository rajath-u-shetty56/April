# Data model

```text
Workspace
└── Project
    ├── EventDefinition
    │   └── EventPropertyDefinition
    ├── IngestionCredential
    ├── GroupProfile
    ├── GroupPropertyDefinition
    └── Event
```

## Workspace and Project

Workspace represents an organization; its key is globally unique in one deployment.
Project represents a data/access boundary; its key is unique within its workspace.
Both have UUID primary keys, names, activity flags, and created/updated timestamps.
Inactive workspaces or projects cannot receive events.

A project can receive events from multiple products. Deployment environments remain
separate databases. Neither products nor environments are database models.

## EventDefinition

Documentation and discovery metadata for a product's event name. Fields are
`project`, optional `product_key`, `name`, `description`, `owner`, and status
(`visible`, `verified`, or `hidden`), plus UUID primary key and created/updated
timestamps. `last_seen_at` records the latest occurrence time observed for that
event without moving backwards when delayed data arrives. The combination of
`project`, `product_key`, and `name` is unique, so
two products can document the same event name independently. An empty product key
represents a project-wide event whose producer supplied no product classification.

Successful ingestion copies `properties.product` into `product_key` and discovers
missing definitions automatically with status `visible`. Existing metadata is
preserved. Staff can edit description, owner, and status without changing the
event identity. `hidden` removes an event from normal future discovery; it does
not reject later occurrences. Explicit event and product analytics selectors resolve only
visible or verified definitions, excluding hidden definitions and reporting unknown selectors as
input errors. `verified` marks reviewed definitions. Definitions contain neither occurrences
nor strict property schemas; events store their event name directly and do not
depend on a foreign key to mutable catalog metadata.

## EventPropertyDefinition

An observational definition for a top-level event property, scoped to one
`EventDefinition`. Its unique identity is the event definition plus a SHA-256
hash of the full property name; the original property name remains available for
discovery. The catalog stores description and status (`visible`, `verified`, or
`hidden`), sorted observed non-null JSON types, a separate `nullable` flag, and
first/last observation timestamps. It never stores sample property values.

Ingestion remains flexible and does not reject a property because its observed
type changes. Null plus one concrete type is nullable, not a conflict. A type
conflict means that more than one incompatible non-null type was observed.
Visible and verified properties are discoverable and can be queried explicitly;
hidden properties are omitted from discovery and rejected by explicit analytics
queries. An event-property selector requires an event and may omit product to match that property
across visible definitions for the event; query responses report coverage separately for each
matching event/product definition. Properties used as grouping dimensions must have observed scalar
values; nested objects and arrays remain stored but are not returned as dimension keys.

The schema migration creates this table but does not scan stored events. The
`backfill_property_catalog` management command merges event and group property
observations in bounded batches. Its `--after-event-pk` and `--through-event-pk`
cursors use `Event.id`, ordered by that database primary key, and can be resumed
from the last processed primary key. Repeated ranges are idempotent.

## Event

- `id`: internal UUID primary key.
- `project`: required project relation, protected against cascading project deletion.
- `uuid`: producer event UUID, unique with `project`.
- `event`: event name, up to 200 characters.
- `distinct_id`: actor identifier, up to 400 characters.
- `timestamp`: occurrence datetime, normalized to UTC.
- `received_at`: platform receipt time; defaults to request arrival for ingestion.
- `groups`: JSON object, including account identity when applicable.
- `properties`: flexible JSON object, including product/version metadata.

Events are immutable through normal application update paths. `received_at` supplies
the ingestion timestamp; a redundant `created_at` and mutable `updated_at` are not
needed. Controlled deletions remain possible, without database triggers.

B-tree indexes cover `(project, timestamp)`, `(project, event, timestamp)`, and
`(project, distinct_id, timestamp)`. The `(project, uuid)` constraint supports retry
lookup. Arbitrary JSON properties have no indexes.

## GroupProfile

A group profile stores current descriptive information for a business entity such
as an account, organization, or team. Its identity is the unique combination of
`project`, `group_type`, and `group_key`.

- `properties`: current merged JSON properties, such as name, plan, or region.
- `last_seen_at`: latest occurrence time of an ingested business event that was
  explicitly associated with the group.

An ordinary event's `groups` object creates or touches the matching profiles. The
reserved `$groupidentify` event merges current properties. Both operations happen
in the same transaction as event storage. A delayed event cannot move
`last_seen_at` backwards, and identifying a group does not modify older events.

## GroupPropertyDefinition

An observational definition for a top-level `$group_set` property, scoped to
`project + group_type + property_name`. It stores the same description, status,
observed non-null types, separate nullability, and first/last timestamps as an
event property definition, without retaining values. Visible and verified
properties are discoverable and explicitly queryable; hidden properties are
omitted from discovery and rejected by explicit state filters. Group state
remains in `GroupProfile` plus immutable `$groupidentify` events; this catalog
does not add historical snapshots.

## IngestionCredential

- UUID `id`, required `project`, and human-readable `name`.
- Unique indexed `prefix` for lookup; `secret_hash` for constant-time verification.
- `created_at`, `updated_at`, nullable `last_used_at`, and nullable `revoked_at`.

`revoked_at = null` means active. Revocation cannot be reversed through the API.
Rotation creates another row and leaves the previous credential active. Many active
credentials can belong to one project. Only creation/rotation responses disclose a
new full secret. `last_used_at` records successful authentication, even when the
subsequent event fails validation.

## Not implemented

No persisted person, person identity mapping, group-membership graph, historical
group-property table, rejected-payload, metric, or MCP models are present.
Rejection monitoring uses sanitized structured logs and log-derived counter samples.

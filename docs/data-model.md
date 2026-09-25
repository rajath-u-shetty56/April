# Data model

```text
Workspace
└── Project
    ├── EventDefinition
    ├── IngestionCredential
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
timestamps. The combination of `project`, `product_key`, and `name` is unique, so
two products can document the same event name independently. An empty product key
represents a project-wide event whose producer supplied no product classification.

Successful ingestion copies `properties.product` into `product_key` and discovers
missing definitions automatically with status `visible`. Existing metadata is
preserved. Definitions contain neither occurrences nor strict property schemas;
events store their event name directly and do not depend on a foreign key to
mutable catalog metadata.

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

## IngestionCredential

- UUID `id`, required `project`, and human-readable `name`.
- Unique indexed `prefix` for lookup; `secret_hash` for constant-time verification.
- Independent `require_product` and `require_account` validation flags, default false.
- `created_at`, `updated_at`, nullable `last_used_at`, and nullable `revoked_at`.

`revoked_at = null` means active. Revocation cannot be reversed through the API.
Rotation creates another row and leaves the previous credential active. Many active
credentials can belong to one project. Only creation/rotation responses disclose a
new full secret. `last_used_at` records successful authentication, even when the
subsequent event fails validation.

## Not implemented

No persisted person, account profile, identity mapping, rejected-payload, metric,
or MCP models are present. Rejection monitoring uses sanitized structured logs and
log-derived counter samples.

# Architecture

## Application structure

The application is divided into small Django apps. Each app has one main
responsibility:

```text
analytics_platform
├── catalog        # Workspaces and projects
├── event_catalog  # Known event names and their documentation
├── events         # Events that have been received and stored
├── ingestion      # Credentials, authentication, validation, and ingestion
└── common         # Shared model fields such as UUIDs and timestamps
```

The event envelope is defined in `ingestion/contracts.py`. This is the common
structure that every product uses when it sends an event. The import in
`event_catalog/contracts.py` remains only for backward compatibility with older
code.

## How an event is ingested

An event goes through the following steps:

```text
Request
  → authenticate the ingestion credential
  → validate the event envelope
  → check whether the event was already received
  → discover its event definition
  → store the event
  → commit the PostgreSQL transaction
  → return the result
```

Both `/capture/` and `/bulk/` use the same `ingest_event` service. `/capture/`
processes one event. `/bulk/` processes a list of events in order and returns a
separate result for each item.

The HTTP views are intentionally small. They authenticate the request, reject
malformed or oversized JSON, and pass valid request data to the ingestion
service. The service contains the shared validation and storage logic, so
another interface can reuse it later.

Ingestion is currently synchronous. When the API returns an accepted result,
the event has already been committed to PostgreSQL. There is no Celery, Redis,
or Kafka dependency. If a queue is introduced later, we must define whether
"accepted" means queued or fully stored because those are different guarantees.

## Transactions, retries, and concurrent requests

Each event is stored in its own database transaction. This also applies to bulk
requests: one event failing does not roll back events that were already accepted.

The transaction uses `transaction.atomic(durable=True)`. This prevents the
ingestion service from being wrapped inside another transaction that could roll
back after the API has already returned success.

Every stored event has a `uuid`. Producers should supply one and reuse it when
retrying an event. If a producer does not supply one, the platform generates
one, but that producer cannot safely retry unless it saves the returned UUID.
The combination of `project` and `uuid` is unique:

- Sending the same UUID with the same event data is treated as a safe retry. A
  second event is not created.
- Sending the same UUID with different data returns a conflict.
- The database constraint remains the final protection if two requests with the
  same UUID arrive at the same time.

Event names are discovered automatically. The combination of `project` and
event name is unique in `EventDefinition`. Concurrent requests can therefore
discover the same event without creating duplicate definitions. Ingestion never
overwrites an existing definition's status, owner, or description.

Storage errors during event processing roll back that event and return a
sanitized error. Database error details and event data are not returned to the
producer.

## Stored events are append-only

An accepted event represents something that already happened, so normal
application code must not edit it. The model blocks:

- Saving changes to an existing event.
- Queryset updates.
- Bulk updates.
- Bulk inserts that update an existing row on conflict.

There is no event-editing API or admin screen.

The database does not use triggers to block every possible update or deletion.
This is deliberate: controlled deletion may be needed later for retention,
privacy requests, or recovery work. A project cannot be deleted while it still
owns events, which protects against accidental cascading deletion.

## Ingestion credentials

Each ingestion credential belongs to one project. The credential tells the
platform which project should receive an event, so producers must not send a
`project_id` in the event body.

A credential has two parts:

```text
public prefix.random secret
```

The prefix is used to find the credential efficiently. The secret is generated
from 256 bits of secure random data. The complete credential is shown only when
it is created or rotated.

The database stores a SHA-256 hash of the complete credential, not the secret
itself. SHA-256 is suitable here because the secret is long and randomly
generated rather than chosen by a person. Authentication compares hashes using
`hmac.compare_digest`, which avoids ordinary string-comparison timing leaks.

A credential is rejected when:

- The secret is invalid.
- The credential has been revoked.
- Its project is inactive.
- Its workspace is inactive.

The application checks the credential during authentication and checks its
status again before storing each event. The second check matters for bulk
requests because a credential could be revoked while the request is still being
processed.

Rotating a credential creates a new credential. The old one stays active until
it is explicitly revoked, allowing producers to switch without downtime. A
revoked credential cannot be reactivated.

Only staff users can manage ingestion credentials. An ingestion credential can
send events, but it cannot access staff-only catalog or management APIs.

## Rejection logging and monitoring

Rejected requests and rejected bulk items are written to the
`analytics.ingestion` logger as structured JSON.

The log contains only information that is useful for troubleshooting:

- Project ID, when it is known.
- A valid event UUID, when supplied.
- A SHA-256 hash of the event name.
- Error code and field.
- Server timestamp.
- Request ID.

The raw event, properties, authorization header, actor ID, account ID, and
database exception details are not logged. The event name is hashed because it
is producer-controlled text and could accidentally contain sensitive data.

Each rejection log also contains:

```text
metric = ingestion_rejections_total
value = 1
```

A deployment's log collector can sum these values by error code and, when
appropriate, by project. Request IDs, event UUIDs, and event-name hashes should
not be used as metric labels because they create too many unique label values.

The application currently writes these records to standard error. It does not
include a metrics database, a scraping endpoint, or a separate table for invalid
events. The deployment is responsible for collecting and retaining the logs.

## Deployment model

Testing, staging, and production are separate deployments. Each deployment has
its own PostgreSQL database and ingestion credentials.

Multiple products can send events to the same project within one deployment.
The event's `properties.product` value distinguishes Helpdesk, Contact Center,
BI, Workflows, and other products. Account-level relationships can be expressed
through the event's `groups` data.

Queries must always include a project context. This prevents data from one
project from being mixed with another project accidentally.

## Not implemented yet

The current code covers the catalog and synchronous ingestion foundation. It
does not yet include:

- Product analytics queries.
- Metric or semantic definitions.
- MCP tools.
- A queue-based ingestion pipeline.
- A separate `Product` model.
- A `DeploymentEnvironment` model.
- A strict event-schema registry.
- Cross-product user identity matching.

These are intentionally outside the current phase. They can be added later
without changing the basic event envelope or the separation between the Django
apps.

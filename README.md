# Multi-Product Analytics

An internal product analytics platform for collecting business events across multiple products and making them available to analytics and AI interfaces.

The repository contains the catalog foundation, synchronous PostgreSQL event ingestion, reusable
analytics queries, and a local read-only MCP server.

## Current structure

```text
src/analytics_platform/
├── catalog/         # Workspaces and projects
├── event_catalog/   # Event names and per-event property definitions
├── events/          # Persisted event occurrences
├── group_analytics/ # Group profiles and project/group-type property definitions
├── ingestion/       # Credentials, capture, bulk, validation and deduplication
├── analytics/       # Deterministic read-only analytics services and semantics
├── mcp_adapter/     # Typed local MCP tools
└── common/          # Shared model behavior
```

See [architecture](docs/architecture.md), [data model](docs/data-model.md), and [event contract](docs/event-contract.md) for more context.

## Local setup

```bash
cp .env.example .env
uv sync
docker compose up -d postgres
uv run python manage.py migrate
uv run python manage.py runserver
```

The health endpoint is available at `http://localhost:8000/health/`.

## Existing event property backfill

Apply schema migrations first, then backfill events that were stored before observational property
discovery was added. The command reads bounded batches and merges catalog observations idempotently:

```bash
uv run python manage.py migrate
uv run python manage.py backfill_property_catalog \
  --project-id '<project UUID>' --batch-size 500
```

The command orders and filters by `Event.id`, the Event database primary key. It reports the last
processed primary key after every batch. To resume, pass that value as `--after-event-pk` and keep
the same `--through-event-pk` high-water value from the original run. If no high-water value is
provided, the command captures the largest Event primary key at startup. These cursors are not the
producer event UUID. Re-running completed ranges is safe; observations merge types, nullability,
and first/last timestamps without storing sample property values.

## Checks

```bash
uv run pytest
uv run ruff check .
uv run python manage.py makemigrations --check --dry-run
```

## Synthetic analytics validation

The deterministic validation dataset exercises ten weeks of account-level activity across
Helpdesk, Contact Center, Rise, and BI. Use a dedicated empty project and one of its ingestion
credentials so the expected-result assertions are not mixed with unrelated events.

```bash
export ANALYTICS_BASE_URL=http://localhost:8000
export ANALYTICS_INGESTION_KEY='<project ingestion credential>'
uv run python scripts/mock_analytics_dataset.py

# Run the same command again to verify every event is reported as a duplicate.
uv run python scripts/mock_analytics_dataset.py

export ANALYTICS_PROJECT_ID='<project UUID>'
uv run python scripts/mock_analytics_analysis.py
```

The producer communicates only through `/api/v1/capture/` and `/api/v1/bulk/`. The analysis
script reads the accepted PostgreSQL rows through Django, prints results for the validation
questions, and exits nonzero when an expected scenario is missing. Historical plan and entitlement
results are explicitly conditional on complete, correctly timestamped `$groupidentify` history;
`GroupProfile` remains the source for current account state.

The analysis script exercises structured event queries, group activity rules, explicit eligibility,
funnels, group state and transitions, and comparisons over the 30 days before the fixed cutoff.
These are generic query examples over a synthetic dataset; callers supply event rules, group
filters, dimensions, and aggregations. The sample user identifiers intentionally use separate
product namespaces in some cases. Any overlap output compares exact `distinct_id` strings only;
zero overlap does not establish that the underlying people are different, and no identity
resolution is performed.

### PostHog parity dataset

The PostHog adapter imports the same `build_dataset()` function as the April producer. It does not
generate a second dataset. Start with its offline modes; neither contacts PostHog or requires a
token:

```bash
uv run python scripts/posthog_mock_analytics_dataset.py --describe
uv run python scripts/posthog_mock_analytics_dataset.py --dry-run
```

`--describe` prints counts, timestamp bounds, product and event-name breakdowns, account count, and
a SHA-256 fingerprint of the canonical source dataset. `--dry-run` also translates and validates
all events, then prints three sanitized samples. Behavioral `groups.account` values become the
PostHog `$groups.account` property; group profile events retain `$group_type`, `$group_key`, and
`$group_set`.

Sending requires both an explicit mode and the dedicated PostHog project's public capture token:

```bash
export POSTHOG_PROJECT_TOKEN='<project capture token>'
export POSTHOG_HOST='https://us.i.posthog.com'  # EU Cloud or a self-hosted origin also works
export POSTHOG_TIMEOUT_SECONDS=30
uv run python scripts/posthog_mock_analytics_dataset.py --send
```

The token is placed only in the JSON request body, never in output, exception text, or a URL. The
adapter uses PostHog's public `/batch/` capture endpoint with bounded batches, transient-failure
retries, and profile events sent before behavioral events. This synchronous HTTP path is deliberate:
the official Python SDK queues events locally and documents that shutdown does not guarantee server
receipt, whereas this validation utility needs to surface batch HTTP failures. A successful run
means PostHog's capture service acknowledged every batch; it is not an immediate transactional
commit or a guarantee that events are already queryable. Repeated sends reuse identical UUIDs, but
PostHog deduplication and query availability can be eventual.

The mapping follows PostHog's official [event](https://posthog.com/docs/data/events) and
[group analytics](https://posthog.com/docs/product-analytics/group-analytics) contracts. Use a
dedicated PostHog project so parity queries are not mixed with unrelated events.

## Local analytics MCP

Set `ANALYTICS_PROJECT_ID` to the one project the process may query, then start the stdio server:

```bash
export ANALYTICS_PROJECT_ID='<project UUID>'
uv run python scripts/run_analytics_mcp.py
```

The project is fixed and validated at startup. Tool arguments cannot select another project, and
every response includes the resolved project UUID as scope evidence. The server exposes these eight
read-only tools:

- `discover_analytics_catalog`
- `query_events`
- `analyze_group_state`
- `query_group_activity`
- `analyze_group_adoption`
- `analyze_group_funnel`
- `analyze_group_transitions`
- `compare_group_activity`

The previous tools map to the structured surface as follows:

| Previous tool | Replacement |
| --- | --- |
| `describe_project` | `discover_analytics_catalog` |
| `get_account_profile` | `analyze_group_state` with an optional `group_key` |
| `summarize_account_activity` | `query_events` or `query_group_activity` |
| `find_cross_product_accounts` | `query_group_activity` with labeled rules and explicit `match` |
| `analyze_product_adoption`, `count_feature_users` | `analyze_group_adoption` with explicit eligibility, or `query_events` for event counts |
| `analyze_funnel` | `analyze_group_funnel` with caller-defined ordered event steps |
| `analyze_account_change`, `analyze_trial_outcomes` | Structured event queries, group transitions, or adoption inputs; no domain-specific result contract remains |

Date ranges require timezone-aware timestamps and use an inclusive start and exclusive end,
normalized to UTC. Bounded results report `returned_count`, `total_count`, and `truncated`; aggregate
totals use the full matching population. `analyze_group_state` always requires an explicit
`state_basis`: `current`, `period_start`, `period_end`, or `event_time`. Omitting `group_key` returns
a bounded list of matching groups; supplying it selects one group. Historical property state is
reconstructed from `$groupidentify` event history and is conditional on complete,
correctly timestamped history.

Event properties are scoped to one event definition, and group properties are scoped to project and
group type. Ingestion accepts flexible property JSON and records observed non-null types and
nullability separately. A conflict means multiple incompatible non-null types were observed. Visible
and verified definitions appear in discovery and can be queried explicitly; hidden definitions are
omitted from discovery and rejected by explicit property queries. Catalog responses contain no
sample values.

Event and product filters must resolve to visible or verified event definitions; unknown selectors
are errors. An event-property query requires an event name, while product may be omitted to query the
matching definitions across products. Such queries return per-definition base, present (including
JSON null), explicit-null, missing, and matched counts. Property dimensions distinguish concrete
scalar values, explicit `null`, and missing keys; nested objects and arrays cannot be grouping keys.
Reserved profile-update events are excluded from general event queries. Query windows are limited
to 366 days, `in` and `not_in` filters to 100 values, and comparison periods must have equal
duration. A missing comparison group uses zero for counts and sums, but `null` for averages, minima,
and maxima.

`query_group_activity` accepts multiple labeled event rules and requires `match=all` or `match=any`.
Its optional overlap compares exact identifier strings and never resolves identities. Adoption
requires caller-supplied eligibility filters and a selected `state_basis`, so a denominator is never
inferred. For event-time eligibility, the denominator includes each group that matched the filters
at any point in the period, while the numerator counts matching activity only when the group was
eligible. Adoption also reports total and median event depth per adopting group. Funnels correlate
ordered steps by group key, exact `distinct_id`, or a shared observed string or number property such
as `call_id`. Each step requires a distinct later event occurrence, and raw correlation values are
not returned. Funnel `state_basis` is required only when group-profile filters are supplied.
`query_events` supports an optional second
equal-length period; `compare_group_activity` compares the same structured group activity rules
across two equal-length periods through the shared comparison service. Funnels, transitions,
dimensions, and aggregations are expressed as structured inputs. No tool accepts SQL or defines
custom metrics.

`ANALYTICS_QUERY_TIMEOUT_MS` sets a transaction-local PostgreSQL statement timeout around analytics
MCP query execution. It does not configure a global database timeout and does not affect ingestion,
migrations, admin operations, or the property backfill command.

`ANALYTICS_MAX_RESPONSE_BYTES` limits every serialized MCP result and defaults to 1 MiB. Historical
group reconstruction rejects more than 100,000 matching `$groupidentify` events, and an unscoped
current-state query rejects more than 20,000 groups. Oversized work fails explicitly instead of
returning partial analytics.

This MCP server supports local stdio only. Its stdout is reserved for protocol messages, so launch
it from an MCP client rather than treating its output as a human-readable CLI. Diagnostics go to
stderr. It provides no arbitrary SQL, raw user-identifier export, write operations, HTTP transport,
or runtime project selector.

## API

The staff-only management API is mounted under `/api/v1/`:

- `GET/POST /api/v1/workspaces/`
- `GET/POST /api/v1/workspaces/{workspace_id}/projects/`
- `GET/POST /api/v1/projects/{project_id}/event-definitions/`
- `GET/PATCH /api/v1/projects/{project_id}/event-definitions/{definition_id}/`

- `GET/POST /api/v1/projects/{project_id}/ingestion-credentials/`
- `POST /api/v1/projects/{project_id}/ingestion-credentials/{credential_id}/rotate/`
- `POST /api/v1/projects/{project_id}/ingestion-credentials/{credential_id}/revoke/`

Producers use project ingestion credentials as `Authorization: Bearer <ingestion-key>`:

- `POST /api/v1/capture/` — one event, committed before acceptance;
- `POST /api/v1/bulk/` — independently committed items with ordered acceptance/rejection results.

Create a named credential through the staff API and store its returned `secret`:
it is only disclosed on creation or rotation. Rotation permits an overlap period;
explicitly revoke the old credential after migrating the producer.

See the [event contract](docs/event-contract.md) for payloads, retry semantics,
limits, and error codes, and [architecture](docs/architecture.md) for transaction
and operational monitoring details. The test suite requires PostgreSQL (including
real concurrent ingestion tests) and a database role allowed to create test databases.

Testing, staging, and production are separate deployments with separate databases and credentials. Each deployment can use the same logical workspace and project keys, such as `happyfox/main`.

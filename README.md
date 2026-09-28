# Multi-Product Analytics

An internal product analytics platform for collecting business events across multiple products and making them available to analytics and AI interfaces.

The repository contains the catalog foundation, synchronous PostgreSQL event ingestion, reusable
analytics queries, and a local read-only MCP server.

## Current structure

```text
src/analytics_platform/
├── catalog/         # Workspaces and projects
├── event_catalog/   # Known event names
├── events/          # Persisted event occurrences
├── group_analytics/ # Current account, organization, or team profiles
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

The analysis uses the 30 days before the fixed dataset cutoff for active cross-product usage,
feature adoption, feature depth, and distinct feature users. Contact Center feature metrics use an
explicit qualifying-event mapping rather than treating call lifecycle events as features. Plan
adoption reports an eligible-account denominator and attributes each qualifying event to the plan
effective at its timestamp. Synthetic user identifiers are scoped by user store and account.
Helpdesk, Contact Center, and Rise share the Helpdesk user namespace, while BI keeps its separate
user namespace because no verified cross-product user mapping exists.

## Local analytics MCP

Set `ANALYTICS_PROJECT_ID` to the one project the process may query, then start the stdio server:

```bash
export ANALYTICS_PROJECT_ID='<project UUID>'
uv run python scripts/run_analytics_mcp.py
```

The project is fixed and validated at startup. Tool arguments cannot select another project, and
every response includes the resolved project UUID as scope evidence. The server exposes these nine
read-only tools:

- `describe_project`
- `get_account_profile`
- `summarize_account_activity`
- `find_cross_product_accounts`
- `analyze_product_adoption`
- `count_feature_users`
- `analyze_funnel`
- `analyze_account_change`
- `analyze_trial_outcomes`

Date-bearing tools require timezone-aware timestamps and use an inclusive start and exclusive end,
normalized to UTC. Account and event-definition evidence is bounded to 50 items by default and 200
maximum; responses report `returned_count`, `total_count`, and `truncated`, while aggregate totals
continue to use the full matching population. Feature, funnel, entitlement, and trial meanings come
from reviewed application configuration rather than caller-supplied predicates.

`find_cross_product_accounts` uses two related populations. Its bounded account list and aggregate
event counts cover accounts observed in every requested product. Its `user_overlap` aggregates
cover all accounts with activity in any requested product, as declared by the response's
`population_basis`; this preserves meaningful pairwise overlap when no account used every product.
Raw distinct IDs are never returned.

Feature adoption classifies `high_adoption_low_depth` using period-end entitled adopters only. The
deterministic rule requires at least two entitled adopting accounts, an entitled adoption rate of
at least 50%, and an entitled-adopter median depth of at most two qualifying events. Each feature
result returns those configured thresholds, its numerator and denominator, actual rate and median,
and the final classification so callers can explain the result.

Product adoption deliberately uses two usage bases. `overall.usage_basis` is
`any_observed_product_event_in_period`, so lifecycle events count toward overall product usage.
Each feature result has `usage_basis` equal to `qualifying_feature_events_in_period`, so only events
mapped to that feature in deterministic semantics count toward its adoption and depth. The overall
and feature numerators therefore answer different questions by design.

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

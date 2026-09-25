# Multi-Product Analytics

An internal product analytics platform for collecting business events across multiple products and making them available to analytics and AI interfaces.

The repository contains the catalog foundation and synchronous PostgreSQL event ingestion. Analytics queries, business metrics, and MCP tools have not been implemented yet.

## Current structure

```text
src/analytics_platform/
├── catalog/         # Workspaces and projects
├── event_catalog/   # Known event names
├── events/          # Persisted event occurrences
├── group_analytics/ # Current account, organization, or team profiles
├── ingestion/       # Credentials, capture, bulk, validation and deduplication
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

## API

The staff-only management API is mounted under `/api/v1/`:

- `GET/POST /api/v1/workspaces/`
- `GET/POST /api/v1/workspaces/{workspace_id}/projects/`
- `GET/POST /api/v1/projects/{project_id}/event-definitions/`
- `GET /api/v1/projects/{project_id}/event-definitions/{definition_id}/`

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

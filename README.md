# Multi-Product Analytics

An internal product analytics platform for collecting business events across multiple products and making them available to analytics and AI interfaces.

The repository currently contains the catalog foundation only. Event ingestion, event storage, analytics queries, metrics, and MCP tools have not been implemented yet.

## Current structure

```text
src/analytics_platform/
├── catalog/         # Workspaces and projects
├── event_catalog/   # Known event names and the current event contract
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

The current staff-only API is mounted under `/api/v1/`:

- `GET/POST /api/v1/workspaces/`
- `GET/POST /api/v1/workspaces/{workspace_id}/projects/`
- `GET/POST /api/v1/projects/{project_id}/event-definitions/`
- `GET /api/v1/projects/{project_id}/event-definitions/{definition_id}/`

Testing, staging, and production are separate deployments with separate databases and credentials. Each deployment can use the same logical workspace and project keys, such as `happyfox/main`.

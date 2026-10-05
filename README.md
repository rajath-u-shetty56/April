# April — Multi-Product Analytics

April is an internal product analytics prototype for collecting business events from multiple
products and making that data available through deterministic analytics queries and an AI/MCP
interface.

## Demo

[Watch the April Analytics demo](https://github.com/rajath-u-shetty56/April/releases/download/demo-v1/april-demo.mp4)

The current implementation includes:

- Django and PostgreSQL application setup;
- workspace and project data boundaries;
- project-scoped ingestion credentials;
- single-event and bulk ingestion endpoints;
- immutable event storage and UUID-based deduplication;
- flexible event properties;
- event, event-property, group-profile, and group-property catalogs;
- generic event, adoption, funnel, transition, and comparison queries;
- a local read-only MCP server; and
- one deterministic dataset that can be loaded into both April and PostHog.

April does not currently provide an analytics dashboard. The validation interface is the local MCP,
the analysis script, or direct inspection of PostgreSQL.

## Repository structure

```text
src/analytics_platform/
├── catalog/         # Workspaces and projects
├── event_catalog/   # Event and event-property catalog entries
├── events/          # Immutable event occurrences
├── group_analytics/ # Current group profiles and group-property catalog entries
├── ingestion/       # Credentials, validation, capture, bulk and deduplication
├── analytics/       # Generic read-only analytics queries
├── mcp_adapter/     # Typed MCP tools
└── common/          # Shared model and property-catalog behavior
```

For the design details, see:

- [Architecture](docs/architecture.md)
- [Data model](docs/data-model.md)
- [Event contract](docs/event-contract.md)
- [Ingestion credential decision](docs/ingestion-credentials-decision.md)

## Prerequisites

Install the following before starting:

- Git
- Docker with Docker Compose
- [uv](https://docs.astral.sh/uv/)
- `curl`
- Python 3.12–3.14; `uv` will create and manage the project environment

PostHog is optional and is only needed for the parity comparison.

## 1. Start April locally

Clone the repository and install the Python dependencies:

```bash
git clone <repository-url>
cd april
cp .env.example .env
uv sync
```

Start PostgreSQL, wait for it to become healthy, and apply the migrations:

```bash
docker compose up -d --wait postgres
uv run python manage.py migrate
uv run python manage.py check
```

Start Django:

```bash
uv run python manage.py runserver
```

Verify the application from another terminal:

```bash
curl http://localhost:8000/health/
```

Expected response:

```json
{"status": "ok"}
```

## 2. Create a workspace, project, and ingestion credential

The management APIs require a Django staff user. Create one and generate its local API token:

```bash
uv run python manage.py createsuperuser
uv run python manage.py drf_create_token <username>
export APRIL_ADMIN_TOKEN='<token returned by drf_create_token>'
```

Create a workspace:

```bash
curl --fail-with-body --request POST http://localhost:8000/api/v1/workspaces/ \
  --header "Authorization: Token ${APRIL_ADMIN_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{"key":"happyfox","name":"HappyFox"}'
```

Copy the returned workspace `id`, then create a project:

```bash
export APRIL_WORKSPACE_ID='<workspace UUID>'

curl --fail-with-body --request POST \
  "http://localhost:8000/api/v1/workspaces/${APRIL_WORKSPACE_ID}/projects/" \
  --header "Authorization: Token ${APRIL_ADMIN_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{"key":"main","name":"Main analytics"}'
```

Copy the returned project `id` and create a named ingestion credential:

```bash
export ANALYTICS_PROJECT_ID='<project UUID>'

curl --fail-with-body --request POST \
  "http://localhost:8000/api/v1/projects/${ANALYTICS_PROJECT_ID}/ingestion-credentials/" \
  --header "Authorization: Token ${APRIL_ADMIN_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{"name":"mock-dataset-loader"}'
```

The response contains a `secret`. Save it when it is returned because April stores only its hash and
cannot display the secret again. Ingestion credentials can later be rotated or revoked through the
same staff API.

```bash
export ANALYTICS_INGESTION_KEY='<secret returned above>'
export ANALYTICS_BASE_URL='http://localhost:8000'
```

Do not commit either the staff token or ingestion credential.

## 3. Load and verify the mock dataset

Inspect the deterministic dataset without sending anything:

```bash
uv run python scripts/mock_analytics_dataset.py --describe
```

Load it into the project created above:

```bash
uv run python scripts/mock_analytics_dataset.py
```

Expected counts:

```text
13 group-profile events
3514 behavioral events
3527 total events
10 account group profiles
24 event definitions
```

The successful ingestion response should report `3527` created events and no rejected events. The
dataset uses deterministic UUIDs, so running the command again should report all events as
duplicates rather than inserting another copy.

Run the deterministic analytics validation:

```bash
uv run python scripts/mock_analytics_analysis.py
```

A successful result contains:

```json
"assertion_failures": []
```

The dataset covers Helpdesk, Contact Center, BI, and Rise between July 5 and September 14, 2026.
Its historical entitlement results depend on the included, correctly timestamped `$groupidentify`
history.

## 4. Connect the local MCP

The MCP is read-only and fixed to one project at startup. It reads PostgreSQL directly, so
PostgreSQL must remain running. Django's `runserver` process is not required after the dataset has
been loaded.

Find the absolute paths required by desktop MCP clients:

```bash
command -v uv
pwd
```

For Cursor, add a server to the project-level `.cursor/mcp.json` or the user-level
`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "april": {
      "command": "/absolute/path/to/uv",
      "args": [
        "--directory",
        "/absolute/path/to/april",
        "run",
        "python",
        "scripts/run_analytics_mcp.py"
      ],
      "env": {
        "ANALYTICS_PROJECT_ID": "<project UUID>"
      }
    }
  }
}
```

Use absolute paths because desktop applications may not inherit the shell's `PATH`. Reload the MCP
server or restart the client after changing its configuration.

A useful first verification prompt is:

> Use `discover_analytics_catalog` and summarize the available products, events, event properties,
> and group properties. Do not infer information that is not returned by the tool.

The MCP exposes these generic tools:

- `discover_analytics_catalog`
- `query_events`
- `analyze_group_state`
- `query_group_activity`
- `analyze_group_adoption`
- `analyze_group_funnel`
- `analyze_group_transitions`
- `compare_group_activity`

Tool date ranges require timezone-aware timestamps and use an inclusive start and exclusive end.
The MCP accepts structured analytics arguments only; it does not expose arbitrary SQL, raw user-ID
exports, write operations, or a runtime project selector.

## 5. Load the same dataset into PostHog

Create a dedicated empty PostHog project. Use its public project capture token—not a personal API
key—and select the ingestion host for its region.

First inspect and validate the translated payload locally:

```bash
uv run python scripts/posthog_mock_analytics_dataset.py --describe
uv run python scripts/posthog_mock_analytics_dataset.py --dry-run
```

The expected source fingerprint is:

```text
e58716ba6e38e497d5b04d8de06e5a2c473d31bb6f7bdbb2a1422b3ea82f50ed
```

Send the dataset once:

```bash
export POSTHOG_PROJECT_TOKEN='<PostHog project capture token>'
export POSTHOG_HOST='https://us.i.posthog.com' # Use the EU origin for an EU project
export POSTHOG_TIMEOUT_SECONDS=30

uv run python scripts/posthog_mock_analytics_dataset.py --send
unset POSTHOG_PROJECT_TOKEN
```

PostHog acknowledges ingestion before every event is necessarily available to queries. Allow a few
minutes for its event and property catalogs to update.

When building an insight, select a custom date range covering July 5 through September 14, 2026.
PostHog's default recent-date range will not include this fixed dataset.

April's `groups.account` association is translated to PostHog's `$groups.account`. It is not copied
into a normal event property named `account`. PostHog's group-level UI requires Group Analytics. For
this mock dataset only, `synthetic_scenario = acme` selects the same Acme event population when Group
Analytics is unavailable.

Use a dedicated PostHog project and avoid repeated sends. The translated events reuse deterministic
UUIDs, but PostHog deduplication and query availability can be eventual.

## 6. Run the project checks

PostgreSQL must be running because the test suite creates a PostgreSQL test database.

```bash
uv run ruff check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run pytest
```

The current suite contains 234 tests.

## API summary

Staff management endpoints:

- `GET/POST /api/v1/workspaces/`
- `GET/POST /api/v1/workspaces/{workspace_id}/projects/`
- `GET/POST /api/v1/projects/{project_id}/event-definitions/`
- `GET/PATCH /api/v1/projects/{project_id}/event-definitions/{definition_id}/`
- `GET/POST /api/v1/projects/{project_id}/ingestion-credentials/`
- `POST /api/v1/projects/{project_id}/ingestion-credentials/{credential_id}/rotate/`
- `POST /api/v1/projects/{project_id}/ingestion-credentials/{credential_id}/revoke/`

Producer endpoints use `Authorization: Bearer <ingestion credential>`:

- `POST /api/v1/capture/` — ingest one event;
- `POST /api/v1/bulk/` — ingest multiple independently committed events.

See the [event contract](docs/event-contract.md) for request examples, validation limits, retries,
deduplication, and error codes.

## Existing-data property backfill

Fresh projects do not need this command. After applying the current migrations to a database that
already contains events, populate the event and group property catalogs with:

```bash
uv run python manage.py backfill_property_catalog \
  --project-id '<project UUID>' \
  --batch-size 500
```

The command is bounded and idempotent. Use `--after-event-pk` and the original
`--through-event-pk` high-water value to resume an interrupted run.

## Resetting the local environment

Stop the containers while retaining PostgreSQL data:

```bash
docker compose down
```

To deliberately delete the local PostgreSQL volume and start from an empty database:

```bash
docker compose down -v
```

The second command permanently deletes the local April database. Run migrations and recreate the
workspace, project, and credentials afterward.

## Current limitations

- Ingestion writes synchronously to PostgreSQL; there is no Kafka or background ingestion queue.
- ClickHouse is not used yet.
- The MCP supports local stdio only and has no remote transport or end-user authentication.
- There is no production SDK or product analytics dashboard yet.
- Cross-product user overlap compares exact `distinct_id` values; April does not resolve different
  product-specific IDs to the same person.
- Product classification is carried in event properties rather than a separate Product model.
- Workspaces and projects are logical boundaries in one database. Separate databases or deployments
  for testing, staging, and production are deployment choices, not behavior enforced by these
  models.

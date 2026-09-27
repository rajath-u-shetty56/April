# Local Analytics MCP Design

## Purpose

April needs a small local Model Context Protocol server that lets an LLM inspect one configured
project and answer product analytics questions from stored PostgreSQL data. This phase validates
the usefulness of April's event schema. It is not a production, remotely hosted, or general-purpose
query service.

The server uses local stdio transport, exposes only reviewed read-only tools, and returns structured
evidence with explicit project and UTC time-range scope. It does not execute arbitrary SQL, accept a
project selector from tool callers, or expose secrets and unnecessary event properties.

## Scope and constraints

- The server project is fixed by `ANALYTICS_PROJECT_ID` at process startup.
- No MCP argument can select or override the project.
- Every result includes the resolved project UUID as scope evidence.
- Every event, profile, and definition query filters by the resolved project.
- The MCP surface is read-only and contains no create, update, delete, credential, or ingestion tool.
- Date-bearing tools require explicit timezone-aware start and end values and normalize them to UTC.
- The first version uses stdio only. It adds no HTTP transport, authentication, queue, frontend,
  chatbot, ClickHouse integration, or database model.
- Tool results contain structured evidence rather than only natural-language conclusions.
- Empty datasets and empty matches are valid successful results.
- Large lists are bounded and report whether results were truncated.

## SDK and transport

Use the official Python MCP SDK version 2 with `mcp>=2,<3`. The high-level `MCPServer` API derives
input and output schemas from Python type annotations, validates structured output, and uses stdio
when `run()` is called without another transport.

The executable configures Django and validates `ANALYTICS_PROJECT_ID` before starting the protocol
loop. It must not print during imports or startup. MCP protocol traffic owns stdout; diagnostics use
Python logging on stderr. The `run()` call remains under an `if __name__ == "__main__"` guard.

Plain synchronous tool functions are appropriate because the SDK runs them in worker threads and
April uses Django's synchronous ORM. Each tool carries `read_only_hint=True` and
`open_world_hint=False`. These annotations describe behavior to clients but are not treated as a
security boundary.

## Architecture

```text
ANALYTICS_PROJECT_ID
        |
        v
startup configuration and project resolution
        |
        v
project-scoped Django analytics services
        |                    |
        v                    v
validation CLI         stdio MCP adapter
synthetic assertions   typed read-only tools
```

The reusable query layer belongs in `src/analytics_platform/analytics/`. It contains no MCP
protocol code and no synthetic scenario assertions. The validation CLI and MCP adapter are separate
consumers of that layer.

The MCP adapter belongs in `src/analytics_platform/mcp_adapter/`. It validates protocol arguments,
calls one analytics service, converts known errors to sanitized tool errors, and returns typed
results. It does not assemble ORM queries or calculate analytics itself.

The existing `scripts/mock_analytics_analysis.py` remains a validation command. Its fixed cutoff,
expected counts, expected account names, and synthetic assertions stay outside the reusable query
layer.

## Deterministic semantic configuration

Analytics meanings must be defined in application-owned configuration. The LLM cannot invent,
infer, or override them.

Create a semantic registry in `src/analytics_platform/analytics/semantics.py` containing immutable,
typed definitions for:

- Product keys and their relevant current entitlement fields.
- Entitled statuses, initially `active` and `trial` for Contact Center.
- Qualifying feature names and the exact event predicates that represent successful use.
- Named funnels, their ordered start and completion events, and their correlation property.
- Trial, conversion, and expiry status meanings.
- Supported decline and abandonment rules and their defaults.

Initial Contact Center features are:

| Feature | Qualifying event | Additional predicate |
| --- | --- | --- |
| `call_transfer` | `call_transfer_completed` | none |
| `callback` | `callback_fulfilled` | none |
| `call_hold` | `call_hold_ended` | none |
| `ai_summary` | `call_summary_generated` | `success` is `true` |
| `supervisor_listen` | `listen_started` | none |
| `supervisor_whisper` | `whisper_started` | none |
| `supervisor_barge` | `barge_started` | none |

The initial named funnels are:

| Funnel | Start | Completion | Correlation property |
| --- | --- | --- | --- |
| `call_connection` | `call_initiated` | `call_connected` | `call_id` |
| `call_transfer` | `call_transfer_initiated` | `call_transfer_completed` | `call_id` |
| `callback` | `callback_requested` | `callback_fulfilled` | `call_id` |

Helpdesk, Rise, and BI feature definitions already validated by the synthetic harness move into the
same registry. The registry is ordinary reviewed Python data rather than a new database model. A
future editable semantic layer is outside this phase.

Tool arguments that name a product, feature, funnel, or analysis mode are validated against this
registry. Catalog discovery alone does not make an event a qualifying feature or funnel step.

## Time and entitlement semantics

Every date-bearing result includes:

- `start`, inclusive, in UTC.
- `end`, exclusive, in UTC.
- The resolved `project_id`.
- A short `interpretation` object describing the applied usage and entitlement semantics.

### Overall product and feature adoption

Overall adoption uses entitlement as of the period end:

```text
numerator   = accounts entitled at period end with qualifying observed usage in [start, end)
denominator = accounts entitled at period end
rate        = numerator / denominator * 100
```

The profile state effective at `end` is reconstructed from immutable `$groupidentify` events. A
transition timestamp equal to `end` is not included because the period is end-exclusive. The result
states `entitlement_basis: period_end` and returns numerator, denominator, rate, and bounded account
evidence.

Current `GroupProfile` may be returned separately as current state, but it cannot silently replace
period-end historical state when the requested period ends in the past.

### Adoption by plan

Plan attribution uses the profile state effective at each qualifying event timestamp. An account
that changes plan during a period can contribute usage to more than one plan. Each plan's eligible
population consists of accounts whose historical profile was entitled on that plan at any time in
the requested period.

For each plan and feature, return:

- Eligible accounts for that plan during the period.
- Accounts with qualifying feature use while that plan was effective.
- Numerator, denominator, and adoption rate.
- `plan_attribution_basis: event_time`.

This result is an event-time comparison and is intentionally distinct from the period-end overall
adoption result.

### Reliability statement

Every entitlement- or plan-based result includes this reliability condition in structured form:

```text
Historical entitlement and plan results require complete, correctly timestamped
$groupidentify history.
```

Observed usage is reported independently of entitlement. The services never infer entitlement or
subscription because an account emitted an event.

## Reusable service modules

### `analytics/contracts.py`

Defines UTC time ranges, bounded-list metadata, scope evidence, typed result objects, and sanitized
analytics exceptions. A `TimeRange` enforces timezone awareness and `start < end`.

### `analytics/semantics.py`

Contains the immutable semantic registry described above. Services accept semantic definition
objects rather than event-name guesses supplied by callers.

### `analytics/catalog.py`

Describes the configured project, products, event definitions, stored timestamp range, and aggregate
counts. Event-definition lists use deterministic ordering and bounded results.

### `analytics/accounts.py`

Retrieves current account profiles, summarizes account activity, finds accounts with observed use
of requested products, calculates aggregate cross-product user overlap, decline, and abandonment.

### `analytics/features.py`

Calculates feature adoption, depth, period-end entitlement usage, event-time plan comparison, and
distinct users per account, product, and feature.

### `analytics/funnels.py`

Calculates reviewed named funnels. A completion must occur after its matched start. Repeated starts
with the same correlation key remain separate attempts and each completion can satisfy at most one
start.

### `analytics/trials.py`

Reconstructs account profile timelines and finds observed product usage between trial start and
conversion or expiry transitions.

## MCP tools

### `describe_project`

Inputs: optional bounded `event_definition_limit`.

Returns the resolved project, event/profile/definition counts, earliest and latest event timestamps,
products, and event definitions grouped by product. Definition lists include `returned_count`,
`total_count`, and `truncated`.

### `get_account_profile`

Inputs: `account_key`.

Returns current account profile properties, `last_seen_at`, and scope evidence. A missing account is
a valid empty result. It explicitly labels the properties as current profile state.

### `summarize_account_activity`

Inputs: `account_key`, `start`, and `end`.

Returns event counts by product and event, active products, distinct-user counts by product, and the
applied UTC period. It does not return raw event rows or distinct IDs.

### `find_cross_product_accounts`

Inputs: two or more registry product keys, `start`, `end`, and optional bounded `limit`.

Returns matching accounts and per-account aggregate event evidence for every requested product.
Account lists use deterministic ordering and include truncation metadata.

The same result also includes aggregate user overlap for products that share a verified identity
namespace. It returns counts only, for example:

```json
{
  "products": ["helpdesk", "contact_center", "rise"],
  "users_by_product": {"helpdesk": 12, "contact_center": 8, "rise": 6},
  "users_in_all_products": 4,
  "pairwise_overlap": {
    "helpdesk|contact_center": 7,
    "helpdesk|rise": 5,
    "contact_center|rise": 4
  }
}
```

Raw `distinct_id` values are never returned. BI is excluded from shared-user overlap with the
Helpdesk identity namespace unless a verified mapping is added to the semantic registry.

### `analyze_product_adoption`

Inputs: product, `start`, `end`, optional feature, optional plan breakdown, and optional bounded
account evidence limit.

Returns observed product usage, period-end entitled population, numerator, denominator, rate,
feature adopting accounts, event count, median depth, and high-adoption/low-depth classification.
When requested, it includes event-time plan adoption results. The interpretation object explicitly
states both entitlement bases.

### `count_feature_users`

Inputs: product, `start`, `end`, optional feature, optional account, and optional bounded account
limit.

Returns account to product to feature distinct-user counts. It never returns raw user identifiers.
Account lists include truncation metadata.

### `analyze_funnel`

Inputs: named funnel, `start`, `end`, and optional account.

Returns the configured start event, completion event, correlation property, started, completed,
lost, completion rate, account counts, and applied UTC period.

### `analyze_account_change`

Inputs: analysis kind (`usage_decline` or `feature_abandonment`), product, `start`, `end`, optional
feature, optional decline threshold, and optional bounded account limit.

The requested period is compared with the immediately preceding equal-length period. Results
include the comparison periods, matching accounts, supporting aggregate counts, threshold, and
truncation metadata.

### `analyze_trial_outcomes`

Inputs: product, `start`, `end`, and optional bounded account limit.

Returns trial accounts with observed product usage before conversion and trial accounts with
observed product usage before expiry. Evidence contains transition and aggregate usage timestamps,
not raw events. Account lists include truncation metadata and the historical-profile reliability
condition.

## Bounded results

Tools that can return account or event-definition lists accept a `limit` constrained to `1..200`.
The default is 50. Results use stable ordering and include:

```json
{
  "returned_count": 50,
  "total_count": 137,
  "truncated": true
}
```

Aggregate totals and rates are calculated over the full matching population, not only the returned
evidence page. Truncation therefore affects displayed evidence but never changes a numerator,
denominator, or rate. Pagination is outside v1; callers can narrow their query instead.

## Configuration and startup

`scripts/run_analytics_mcp.py` performs these steps in order:

1. Put the repository and `src` directory on `sys.path`.
2. Set and initialize `DJANGO_SETTINGS_MODULE`.
3. Read `ANALYTICS_PROJECT_ID`.
4. Validate it as a UUID and resolve one active project with its active workspace.
5. Create the MCP server with that fixed project ID.
6. Start stdio with no stdout output before the protocol loop.

Missing, malformed, inactive, or unknown project configuration logs a sanitized startup error to
stderr and exits nonzero. Database connection strings, credentials, and exception details are not
written to stdout or tool results.

Each tool resolves the fixed project inside its worker thread before querying. The project ID is
closed over by the server factory and is not present in any tool input schema.

## Error handling

- MCP/Pydantic input schemas reject wrong types, unsupported enums, and out-of-range limits.
- The analytics layer rejects naive timestamps, reversed ranges, unsupported semantics, and invalid
  account keys with sanitized `AnalyticsInputError` messages.
- The adapter converts known input and lookup errors to `ToolError`, allowing the calling model to
  correct its request.
- Unexpected failures are logged with traceback information to stderr and returned as a generic
  `Analytics query failed` tool error.
- Empty query results return typed successful results with zero counts, empty lists, and
  `truncated: false`.

## Testing

### Analytics services

- Every query is scoped to its supplied project.
- A second project's events, definitions, and profiles never appear.
- UTC and offset timestamps normalize correctly; naive and reversed ranges fail.
- Empty projects and empty periods return valid empty results.
- Period-end entitlement differs correctly from current profile state.
- Event-time plan attribution follows historical profile transitions.
- Feature mappings and funnel definitions come only from the semantic registry.
- Funnel completions must follow starts and repeated attempts remain separate.
- Cross-product account matching uses the requested period.
- Shared-identity overlap includes Helpdesk, Contact Center, and Rise aggregates and excludes BI.
- Distinct-user output preserves account, product, and feature dimensions without exposing IDs.
- Limits preserve full aggregate totals while bounding evidence lists and setting `truncated`.

### MCP adapter

- Tool discovery exposes only the reviewed read-only tools.
- No tool input schema contains a project selector.
- Structured outputs contain project and time-range scope.
- Invalid inputs return sanitized tool errors.
- Tool annotations declare read-only, closed-world behavior.
- In-memory client tests cover every tool and output schema.

### Stdio and local validation

- A subprocess client starts the real server over stdio and successfully lists and calls tools.
- No import-time, startup, or diagnostic output corrupts stdout protocol traffic.
- The server is connected locally with `ANALYTICS_PROJECT_ID` set to the validation project.
- The twelve required product questions are answered through MCP tools.
- Results are compared with `scripts/mock_analytics_analysis.py` expectations.
- The full test suite, Ruff, and migration drift checks pass.

## Security and privacy

- There is no SQL tool, expression language, model mutation, or generic ORM endpoint.
- Tool inputs become bounded ORM filters, never SQL fragments.
- The configured project cannot be changed after startup or through an MCP call.
- Raw event properties are excluded unless a service explicitly selects a reviewed aggregate field.
- Raw distinct user identifiers, database settings, ingestion credentials, and secret hashes are
  never returned.
- Logs use project UUIDs, tool names, and sanitized error categories only.

## Out of scope

- Remote MCP transports and authentication.
- Multiple projects in one server process.
- Editable semantic definitions or metric models.
- User identity mapping between BI and the shared Helpdesk namespace.
- Raw event browsing or export.
- Arbitrary SQL, arbitrary event predicates, and arbitrary funnel construction.
- Frontend, chatbot, queue, caching, and ClickHouse support.

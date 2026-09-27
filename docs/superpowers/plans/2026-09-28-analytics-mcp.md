# Local Analytics MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, read-only stdio MCP server that answers reviewed analytics questions for the single project fixed by `ANALYTICS_PROJECT_ID`.

**Architecture:** Extract deterministic analytics semantics and project-scoped Django query services into `analytics_platform.analytics`, then expose those services through a thin typed `analytics_platform.mcp_adapter` layer. Keep synthetic expectations in the validation script, make every MCP result carry project/time interpretation evidence, and keep stdout exclusively for MCP protocol traffic.

**Tech Stack:** Python 3.12+, Django 5.2, PostgreSQL JSON queries plus bounded Python aggregation where required, official Python MCP SDK `mcp>=2,<3`, Pydantic-backed MCP structured output, pytest/pytest-django, Ruff, uv.

**Spec:** `docs/superpowers/specs/2026-09-28-analytics-mcp-design.md`

## Global Constraints

- The server project is fixed by `ANALYTICS_PROJECT_ID` at process startup; no MCP input may select or override it.
- Every tool result includes the resolved project UUID; every date-bearing result includes inclusive UTC `start`, exclusive UTC `end`, and a structured interpretation.
- Overall adoption uses entitlement at period end; plan attribution uses historical profile state at each qualifying event timestamp.
- Analytics meanings come only from the immutable registry: product keys, feature predicates, named funnels, entitled statuses, and trial/conversion/expiry meanings are never inferred by the LLM.
- Raw `distinct_id` values, raw event rows, secrets, database settings, and unnecessary event properties are never returned.
- Account and event-definition lists default to 50 items, accept limits only in `1..200`, use stable ordering, and return `returned_count`, `total_count`, and `truncated`; aggregate calculations use the full population.
- The MCP surface is read-only, closed-world, and stdio-only; stdout is reserved for protocol messages and logs go to stderr.
- Add no database model or migration, no arbitrary SQL or predicates, no remote transport, and no multi-project server mode.
- Pin the SDK as `mcp>=2,<3`; preserve Python `>=3.12,<3.15` and the existing Django dependency bounds.

## Review Focus

- A transition exactly at the exclusive period end must not change period-end entitlement; Task 2 pins the boundary behavior.
- A qualifying event emitted before an account becomes entitled, or while on a different plan, must not be attributed to a later profile state; Task 4 pins event-time attribution.
- Two products may contain the same actor-looking suffix without sharing an identity namespace; Task 3 pins aggregate overlap to verified namespaces and excludes BI.
- A truncated evidence list must not truncate aggregate numerators, denominators, totals, or rates; Tasks 2–5 pin full-population calculations independently of display limits.
- Startup failures and unexpected ORM failures must not corrupt stdio or expose secrets/tracebacks to callers; Tasks 6 and 7 pin sanitized errors and stderr-only diagnostics.

---

### Task 1: Semantic Registry and Shared Contracts

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/analytics_platform/analytics/__init__.py`
- Create: `src/analytics_platform/analytics/contracts.py`
- Create: `src/analytics_platform/analytics/semantics.py`
- Create: `tests/analytics/test_contracts.py`
- Create: `tests/analytics/test_semantics.py`

**Interfaces:**
- Consumes: Existing event property conventions: `properties.product`, `groups.account`, `$groupidentify` fields `$group_type`, `$group_key`, and `$group_set`.
- Produces: `TimeRange`, `Scope`, `BoundedList[T]`, `Interpretation`, `AnalyticsInputError`, `SemanticRegistry`, `ProductDefinition`, `FeatureDefinition`, `EventPredicate`, `FunnelDefinition`, `TrialDefinition`, and module constant `SEMANTICS`.

- [ ] **Step 1: Add failing contract tests**

  In `tests/analytics/test_contracts.py`, test that `TimeRange.create(start, end)` converts offset-aware inputs to UTC, rejects naive values, rejects `start >= end`, and that `BoundedList.from_items(items, limit=2)` returns two stable items with `returned_count=2`, `total_count=3`, and `truncated=True`. Test `validate_limit` accepts 1 and 200 and rejects 0 and 201 with `AnalyticsInputError`.

- [ ] **Step 2: Run the contract tests and verify failure**

  Run: `uv run pytest tests/analytics/test_contracts.py -v`

  Expected: FAIL because `analytics_platform.analytics.contracts` does not exist.

- [ ] **Step 3: Implement the shared contracts**

  In `contracts.py`, add frozen typed dataclasses with these public constructors/helpers:

  - `TimeRange.create(start: datetime, end: datetime) -> TimeRange`
  - `TimeRange.as_dict() -> dict[str, str]`
  - `Scope(project_id: UUID)`
  - `Interpretation(usage_window: str, entitlement_basis: str | None = None, plan_attribution_basis: str | None = None, historical_profile_reliability: str | None = None)`
  - `BoundedList.from_items(items: Sequence[T], *, limit: int, total_count: int | None = None) -> BoundedList[T]`
  - `validate_limit(limit: int) -> int`

  Serialize datetimes with a `Z` suffix and use the exact reliability value `conditional_on_complete_correctly_timestamped_groupidentify_history`.

- [ ] **Step 4: Run the contract tests and verify success**

  Run: `uv run pytest tests/analytics/test_contracts.py -v`

  Expected: PASS.

- [ ] **Step 5: Add failing semantic-registry tests**

  In `tests/analytics/test_semantics.py`, assert the seven Contact Center feature mappings and predicates from the spec, the three named ordered funnels and `call_id` correlation, entitled statuses `{"active", "trial"}`, trial status `trial`, conversion status `active`, expiry status `expired`, and the shared identity namespace for Helpdesk/Contact Center/Rise but not BI. Assert registry lookup rejects unknown products, features, and funnels with `AnalyticsInputError`.

- [ ] **Step 6: Run the semantic tests and verify failure**

  Run: `uv run pytest tests/analytics/test_semantics.py -v`

  Expected: FAIL because `semantics.py` does not exist.

- [ ] **Step 7: Implement the immutable semantic registry**

  In `semantics.py`, define frozen dataclasses and `SEMANTICS`. Include existing synthetic feature definitions for Helpdesk (`macro_applied`, `sla_policy_created`), Rise (`course_completed`), and BI (`report_created`, `report_viewed`, `dashboard_created`, `dashboard_viewed`) alongside the Contact Center definitions in the spec. Define product status/plan property names, identity namespaces, named funnels, and trial transitions explicitly; expose `get_product`, `get_feature`, and `get_funnel` lookup methods.

- [ ] **Step 8: Add the MCP dependency and refresh the lockfile**

  Add `"mcp>=2,<3"` to project dependencies and run `uv lock`.

- [ ] **Step 9: Run task verification**

  Run: `uv run pytest tests/analytics/test_contracts.py tests/analytics/test_semantics.py -v`

  Expected: PASS.

  Run: `uv run ruff check src/analytics_platform/analytics tests/analytics pyproject.toml`

  Expected: PASS.

- [ ] **Step 10: Commit**

  ```bash
  git add pyproject.toml uv.lock src/analytics_platform/analytics tests/analytics
  git commit -m "feat: define analytics semantics and contracts"
  ```

### Task 2: Historical Profiles and Project Catalog

**Files:**
- Create: `src/analytics_platform/analytics/profiles.py`
- Create: `src/analytics_platform/analytics/catalog.py`
- Create: `tests/analytics/test_profiles.py`
- Create: `tests/analytics/test_catalog.py`

**Interfaces:**
- Consumes: `TimeRange`, `BoundedList`, `SEMANTICS`; Django `Event`, `GroupProfile`, `EventDefinition`, and `Project` models.
- Produces: `ProfileTransition`, `ProfileTimeline.state_at(timestamp, *, inclusive=True)`, `load_profile_timelines(project)`, `describe_project(project, *, event_definition_limit)`, and `get_account_profile(project, account_key)`.

- [ ] **Step 1: Add failing profile timeline tests**

  Create `$groupidentify` events for one account at three UTC timestamps. Assert merge semantics preserve unchanged properties, `state_at()` returns the correct historical snapshot, deterministic UUID ordering resolves equal timestamps, and `state_at(period.end, inclusive=False)` excludes a transition exactly at `end`. Also assert malformed/non-account identify events are ignored.

- [ ] **Step 2: Run the profile tests and verify failure**

  Run: `uv run pytest tests/analytics/test_profiles.py -v`

  Expected: FAIL because `profiles.py` does not exist.

- [ ] **Step 3: Implement historical profile reconstruction**

  Implement:

  - `load_profile_timelines(project: Project) -> dict[str, ProfileTimeline]`
  - `ProfileTimeline.state_at(timestamp: datetime, *, inclusive: bool = True) -> Mapping[str, object]`
  - `ProfileTimeline.states_during(period: TimeRange) -> tuple[ProfileTransition, ...]`

  Query only the supplied project and `$groupidentify` events, order by `timestamp, uuid`, and merge `$group_set` changes without consulting current `GroupProfile`.

- [ ] **Step 4: Run the profile tests and verify success**

  Run: `uv run pytest tests/analytics/test_profiles.py -v`

  Expected: PASS.

- [ ] **Step 5: Add failing catalog/profile query tests**

  In `test_catalog.py`, create two projects with definitions and profiles. Assert `describe_project` returns only the requested project's counts, products, timestamp bounds, and a stably ordered bounded definition list. With three definitions and limit two, assert full `total_count=3`, two returned definitions, and `truncated=True`. Assert `get_account_profile` labels properties as current state, returns scope evidence, validates a nonblank bounded account key, and returns `found=False` for a missing account.

- [ ] **Step 6: Run catalog tests and verify failure**

  Run: `uv run pytest tests/analytics/test_catalog.py -v`

  Expected: FAIL because catalog query functions do not exist.

- [ ] **Step 7: Implement catalog and current-profile queries**

  Add exact service signatures:

  - `describe_project(project: Project, *, event_definition_limit: int = 50) -> ProjectDescription`
  - `get_account_profile(project: Project, account_key: str) -> AccountProfileResult`

  Keep outputs as frozen dataclasses that expose JSON-compatible `to_dict()` methods. Use aggregate ORM queries for counts/time bounds and select only reviewed definition/profile fields.

- [ ] **Step 8: Run task verification**

  Run: `uv run pytest tests/analytics/test_profiles.py tests/analytics/test_catalog.py -v`

  Expected: PASS.

- [ ] **Step 9: Commit**

  ```bash
  git add src/analytics_platform/analytics/profiles.py src/analytics_platform/analytics/catalog.py tests/analytics/test_profiles.py tests/analytics/test_catalog.py
  git commit -m "feat: add historical profiles and project catalog"
  ```

### Task 3: Account Activity and Cross-Product Overlap

**Files:**
- Create: `src/analytics_platform/analytics/accounts.py`
- Create: `tests/analytics/test_accounts.py`

**Interfaces:**
- Consumes: `TimeRange`, `BoundedList`, `SEMANTICS`, and project-scoped `Event` rows.
- Produces: `summarize_account_activity(project, account_key, period) -> AccountActivityResult` and `find_cross_product_accounts(project, products, period, *, limit) -> CrossProductResult`.

- [ ] **Step 1: Add failing account activity tests**

  Assert `summarize_account_activity` returns event counts by product/event, active products, and distinct-user counts by product for `[start,end)`; excludes events at `end`, other accounts, and another project; returns no raw rows or IDs; and includes scope and UTC range.

- [ ] **Step 2: Add failing cross-product and identity-overlap tests**

  Build three matching accounts plus nonmatching and out-of-period activity. Assert requested-product matching uses only the period, account evidence is stable and truncated at limit two while `total_count` remains three, and aggregate totals use all three matches. Assert users-by-product, all-product overlap, and pairwise overlap count shared Helpdesk/Contact Center/Rise IDs without returning identifiers. Add the Review Focus case where a BI ID has the same suffix: BI must have no shared-namespace overlap block.

- [ ] **Step 3: Run tests and verify failure**

  Run: `uv run pytest tests/analytics/test_accounts.py -v`

  Expected: FAIL because `accounts.py` does not exist.

- [ ] **Step 4: Implement account activity services**

  Add exact signatures:

  - `summarize_account_activity(project: Project, account_key: str, period: TimeRange) -> AccountActivityResult`
  - `find_cross_product_accounts(project: Project, products: Sequence[str], period: TimeRange, *, limit: int = 50) -> CrossProductResult`

  Validate at least two unique registry product keys. Filter `project`, timestamps, `groups__account`, and `properties__product` in ORM; perform only bounded aggregate shaping in Python. Compute user overlap across the full matching population and only when every included product has the same non-null registry identity namespace.

- [ ] **Step 5: Run task verification**

  Run: `uv run pytest tests/analytics/test_accounts.py -v`

  Expected: PASS.

  Run: `uv run ruff check src/analytics_platform/analytics/accounts.py tests/analytics/test_accounts.py`

  Expected: PASS.

- [ ] **Step 6: Commit**

  ```bash
  git add src/analytics_platform/analytics/accounts.py tests/analytics/test_accounts.py
  git commit -m "feat: query account and cross-product activity"
  ```

### Task 4: Feature Adoption and Distinct Users

**Files:**
- Create: `src/analytics_platform/analytics/features.py`
- Create: `tests/analytics/test_features.py`

**Interfaces:**
- Consumes: `TimeRange`, `BoundedList`, profile timelines from Task 2, and product/feature predicates from `SEMANTICS`.
- Produces: `analyze_product_adoption(project, product, period, *, feature, include_plan_breakdown, account_limit) -> AdoptionResult` and `count_feature_users(project, product, period, *, feature, account_key, account_limit) -> FeatureUsersResult`.

- [ ] **Step 1: Add failing qualifying-feature and user-count tests**

  Assert only registry predicates qualify: successful `call_summary_generated` counts as `ai_summary`, unsuccessful summaries and lifecycle events do not. Assert distinct users preserve account/product/feature dimensions, filters work, other projects are excluded, no IDs are serialized, and a limit truncates account evidence without changing total account/user aggregates.

- [ ] **Step 2: Add failing period-end entitlement tests**

  Create accounts that are active, trial, expired before end, and activated exactly at end. Assert overall denominator includes only `active` and `trial` state immediately before the exclusive end; numerator includes only those period-end-entitled accounts with qualifying use during the period. Assert output includes `entitlement_basis="period_end"`, full numerator/denominator/rate, the reliability condition, and bounded evidence whose truncation does not alter the rate.

- [ ] **Step 3: Add failing event-time plan attribution tests**

  Create an account changing Basic→Pro mid-period with qualifying events before and after the transition, plus an event before entitlement starts. Assert the account can adopt under both historical plans, the pre-entitlement event is excluded, each plan denominator is the population entitled on that plan at any point in the period, and the result states `plan_attribution_basis="event_time"`.

- [ ] **Step 4: Run tests and verify failure**

  Run: `uv run pytest tests/analytics/test_features.py -v`

  Expected: FAIL because `features.py` does not exist.

- [ ] **Step 5: Implement feature event selection and user counting**

  Implement a private query helper that applies registry event names in SQL and exact additional predicates in Python after selecting only `event`, `timestamp`, `distinct_id`, `groups`, and `properties`. Add:

  - `count_feature_users(project: Project, product: str, period: TimeRange, *, feature: str | None = None, account_key: str | None = None, account_limit: int = 50) -> FeatureUsersResult`

- [ ] **Step 6: Implement adoption and plan attribution**

  Add:

  - `analyze_product_adoption(project: Project, product: str, period: TimeRange, *, feature: str | None = None, include_plan_breakdown: bool = False, account_limit: int = 50) -> AdoptionResult`

  Report observed use independently from entitlement. Calculate per-feature event count, adopting accounts, median depth, and high-adoption/low-depth using the validation rule: adopting account count at least `max(2, entitled_count / 2)` and median depth at most 2. Use Task 2 timelines for both entitlement modes.

- [ ] **Step 7: Run task verification**

  Run: `uv run pytest tests/analytics/test_features.py -v`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add src/analytics_platform/analytics/features.py tests/analytics/test_features.py
  git commit -m "feat: analyze feature adoption and users"
  ```

### Task 5: Funnels, Account Change, and Trial Outcomes

**Files:**
- Create: `src/analytics_platform/analytics/funnels.py`
- Create: `src/analytics_platform/analytics/change.py`
- Create: `src/analytics_platform/analytics/trials.py`
- Create: `tests/analytics/test_funnels.py`
- Create: `tests/analytics/test_change.py`
- Create: `tests/analytics/test_trials.py`

**Interfaces:**
- Consumes: `TimeRange`, `BoundedList`, `SEMANTICS`, qualifying feature helpers, and Task 2 profile timelines.
- Produces: `analyze_funnel`, `analyze_account_change`, and `analyze_trial_outcomes` service functions.

- [ ] **Step 1: Add failing ordered-funnel tests**

  For `call_connection`, assert a completion before a start does not convert, two starts with one later completion produce one completion and one loss, one completion cannot satisfy two starts, account filtering works, and another project is excluded. Assert returned metadata names the configured events and `call_id` correlation property.

- [ ] **Step 2: Run funnel tests and verify failure**

  Run: `uv run pytest tests/analytics/test_funnels.py -v`

  Expected: FAIL because `funnels.py` does not exist.

- [ ] **Step 3: Implement named ordered funnels**

  Add `analyze_funnel(project: Project, funnel: str, period: TimeRange, *, account_key: str | None = None) -> FunnelResult`. Pair each chronologically sorted start with the first unused completion strictly after it for the same `(account, correlation_value)`, preserving repeated attempts.

- [ ] **Step 4: Run funnel tests and verify success**

  Run: `uv run pytest tests/analytics/test_funnels.py -v`

  Expected: PASS.

- [ ] **Step 5: Add failing account-change tests**

  Assert `usage_decline` compares the requested period with the immediately preceding equal-duration period and applies a default 50% threshold. Assert `feature_abandonment` uses qualifying feature events and identifies prior adopters absent in the requested period. Pin zero-baseline behavior to 0% decline, stable bounded evidence, full totals despite truncation, project isolation, and the exact comparison ranges.

- [ ] **Step 6: Implement account-change analysis**

  Add `analyze_account_change(project: Project, kind: Literal["usage_decline", "feature_abandonment"], product: str, period: TimeRange, *, feature: str | None = None, decline_threshold: float = 50.0, account_limit: int = 50) -> AccountChangeResult`. Validate threshold in `0..100` and require `feature` for abandonment.

- [ ] **Step 7: Add failing trial-outcome tests**

  Create conversion and expiry timelines with usage before and after the terminal transition. Assert only usage from trial start up to but excluding the first configured conversion/expiry counts, trial/conversion/expiry meanings come from the registry, unrelated products/projects are excluded, and results carry bounded metadata plus the historical reliability condition.

- [ ] **Step 8: Implement trial outcomes**

  Add `analyze_trial_outcomes(project: Project, product: str, period: TimeRange, *, account_limit: int = 50) -> TrialOutcomeResult`. A qualifying trial begins on the configured trial state during the requested period; associate the first later conversion or expiry transition and aggregate observed product usage within that trial interval.

- [ ] **Step 9: Run task verification**

  Run: `uv run pytest tests/analytics/test_funnels.py tests/analytics/test_change.py tests/analytics/test_trials.py -v`

  Expected: PASS.

- [ ] **Step 10: Commit**

  ```bash
  git add src/analytics_platform/analytics/funnels.py src/analytics_platform/analytics/change.py src/analytics_platform/analytics/trials.py tests/analytics/test_funnels.py tests/analytics/test_change.py tests/analytics/test_trials.py
  git commit -m "feat: analyze funnels changes and trials"
  ```

### Task 6: Typed Read-Only MCP Adapter

**Files:**
- Create: `src/analytics_platform/mcp_adapter/__init__.py`
- Create: `src/analytics_platform/mcp_adapter/schemas.py`
- Create: `src/analytics_platform/mcp_adapter/server.py`
- Create: `tests/mcp_adapter/test_server.py`

**Interfaces:**
- Consumes: All analytics service functions from Tasks 2–5 and `mcp.server.mcpserver.MCPServer`/`ToolError` APIs.
- Produces: `create_server(project_id: UUID) -> MCPServer` exposing exactly nine reviewed tools.

- [ ] **Step 1: Add failing tool-discovery tests**

  Using the SDK in-memory client/transport, call `list_tools()` and assert exactly these names: `describe_project`, `get_account_profile`, `summarize_account_activity`, `find_cross_product_accounts`, `analyze_product_adoption`, `count_feature_users`, `analyze_funnel`, `analyze_account_change`, `analyze_trial_outcomes`. Assert every tool has `readOnlyHint=true` and `openWorldHint=false`, has an output schema, and no input schema contains `project`, `project_id`, workspace, SQL, event predicate, or arbitrary funnel arguments.

- [ ] **Step 2: Run discovery tests and verify failure**

  Run: `uv run pytest tests/mcp_adapter/test_server.py -v -k discovery`

  Expected: FAIL because the MCP adapter does not exist.

- [ ] **Step 3: Define MCP response schemas**

  In `schemas.py`, define Pydantic-compatible typed response models matching each service result. Share `ScopeOutput`, `TimeRangeOutput`, `InterpretationOutput`, and `BoundedMetadataOutput`. Keep structured fields and counts; do not add narrative-only response types.

- [ ] **Step 4: Implement server creation and nine tools**

  In `server.py`, implement `create_server(project_id: UUID) -> MCPServer`. Close over the UUID, resolve the active project inside each synchronous tool invocation, validate date strings through `TimeRange`, invoke exactly one service per tool, and include the resolved ID in every response. Use registry-derived `Literal`/enum schemas where supported; otherwise validate names with the registry.

- [ ] **Step 5: Add failing call and error tests**

  Through the in-memory client, call every tool against fixture data and assert structured responses, scope, UTC periods, interpretations, and truncation metadata. Assert missing accounts produce successful empty results; unsupported names, naive/reversed dates, invalid limits, and inactive/missing projects produce sanitized tool errors. Monkeypatch one service to raise an unexpected exception containing a fake database secret and assert the client receives only `Analytics query failed` while `caplog` records the traceback without protocol output.

- [ ] **Step 6: Implement sanitized error translation**

  Convert `AnalyticsInputError`, project lookup failures, and validation failures to actionable `ToolError` messages. Log unexpected exceptions with `logger.exception` and return only the generic error. Do not catch `BaseException`.

- [ ] **Step 7: Run task verification**

  Run: `uv run pytest tests/mcp_adapter/test_server.py -v`

  Expected: PASS.

  Run: `uv run ruff check src/analytics_platform/mcp_adapter tests/mcp_adapter`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add src/analytics_platform/mcp_adapter tests/mcp_adapter
  git commit -m "feat: expose analytics through read-only MCP tools"
  ```

### Task 7: Startup Entrypoint and Real Stdio Integration

**Files:**
- Create: `scripts/run_analytics_mcp.py`
- Create: `tests/mcp_adapter/test_startup.py`
- Create: `tests/mcp_adapter/test_stdio.py`
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:**
- Consumes: `create_server(project_id)` from Task 6 and Django settings/bootstrap conventions from existing scripts.
- Produces: `scripts/run_analytics_mcp.py` executable entrypoint and documented local launch configuration.

- [ ] **Step 1: Add failing startup configuration tests**

  Test a side-effect-free `resolve_project_id(environ: Mapping[str, str]) -> UUID` helper for missing, malformed, unknown, inactive-project, inactive-workspace, and valid IDs. Capture stdout/stderr to assert imports and successful resolution write nothing, while the CLI returns nonzero and writes one sanitized stderr message for invalid startup configuration.

- [ ] **Step 2: Run startup tests and verify failure**

  Run: `uv run pytest tests/mcp_adapter/test_startup.py -v`

  Expected: FAIL because the entrypoint does not exist.

- [ ] **Step 3: Implement the startup entrypoint**

  Bootstrap repository/`src` paths and Django as existing scripts do. Read only `ANALYTICS_PROJECT_ID`, validate the active project/workspace, construct the server, and call `server.run()` under the main guard. Configure logging to stderr and never print before or during the protocol loop.

- [ ] **Step 4: Add a failing real stdio test**

  Mark the test `django_db(transaction=True)`, construct `DATABASE_URL` from Django's active test connection settings, and pass that URL plus the fixture project's ID to `uv run python scripts/run_analytics_mcp.py` through the SDK stdio client. Assert initialization succeeds, exactly nine tools list, `describe_project` can be called, its scope matches the environment ID, and captured stderr contains no secrets. Also assert an MCP call cannot pass a project selector. Never let the subprocess inherit the developer database URL from `.env`.

- [ ] **Step 5: Run the stdio test and verify failure, then complete subprocess wiring**

  Run: `uv run pytest tests/mcp_adapter/test_stdio.py -v`

  Expected before wiring: FAIL on subprocess initialization. Adjust only entrypoint/client lifecycle plumbing until it passes; do not add analytics behavior here.

- [ ] **Step 6: Document configuration and local connection**

  Add `ANALYTICS_PROJECT_ID=` to `.env.example`. In `README.md`, document the fixed-project security model, the `uv run python scripts/run_analytics_mcp.py` command, stdio-only behavior, nine tool names, UTC/end-exclusive periods, bounded evidence, and that stdout must remain protocol-only.

- [ ] **Step 7: Run task verification**

  Run: `uv run pytest tests/mcp_adapter/test_startup.py tests/mcp_adapter/test_stdio.py -v`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add scripts/run_analytics_mcp.py tests/mcp_adapter/test_startup.py tests/mcp_adapter/test_stdio.py .env.example README.md
  git commit -m "feat: run analytics MCP over local stdio"
  ```

### Task 8: Validation Harness Migration and End-to-End Evidence

**Files:**
- Modify: `scripts/mock_analytics_analysis.py`
- Modify: `tests/validation/test_mock_analysis.py`
- Create: `tests/validation/test_analytics_mcp.py`

**Interfaces:**
- Consumes: All reusable analytics services and `create_server(project_id)`.
- Produces: A compatibility validation CLI with synthetic assertions only, plus MCP-level validation of the twelve required analytics questions against the stored dataset.

- [ ] **Step 1: Add failing service-backed validation tests**

  Update `test_mock_analysis.py` so it verifies the CLI delegates reusable calculations to analytics services rather than defining duplicate feature mappings, profile timeline reconstruction, funnel matching, or account/product aggregation. Preserve fixed cutoff, expected account names/counts, and drift assertions in the script.

- [ ] **Step 2: Run the validation test and verify failure**

  Run: `uv run pytest tests/validation/test_mock_analysis.py -v`

  Expected: FAIL until the script delegates to the new services.

- [ ] **Step 3: Refactor the validation CLI**

  Keep `analyze_project(project, cutoff=ANALYSIS_CUTOFF)` and `assert_expected_results(result)` as compatibility interfaces. Replace reusable algorithms and duplicated constants with calls to Tasks 2–5; adapt their structured outputs to the existing assertion payload where needed. Keep JSON CLI output and `--no-assert` behavior unchanged.

- [ ] **Step 4: Add failing MCP validation coverage for all questions**

  In `test_analytics_mcp.py`, load the deterministic dataset through the existing real ingestion fixture and use the in-memory MCP client to validate: project/catalog description; account profile; account activity; two-product and three-product account use; Helpdesk/Contact Center/Rise user overlap; overall and plan feature adoption; high-adoption/low-depth classification; per-feature user counts; all three funnels; decline; abandonment; trial conversion; and trial expiry. Assert project IDs and interpretation fields on every relevant response and assert no raw distinct IDs occur anywhere in serialized output.

- [ ] **Step 5: Run MCP validation and verify failure, then align adapters only**

  Run: `uv run pytest tests/validation/test_analytics_mcp.py -v`

  Expected before final alignment: FAIL on any service/adapter shape not matching the approved spec. Correct service-to-schema adaptation without weakening expected synthetic outcomes.

- [ ] **Step 6: Run focused validation**

  Run: `uv run pytest tests/validation/test_mock_analysis.py tests/validation/test_analytics_mcp.py -v`

  Expected: PASS, including the existing 3,524-event and 24-definition assertions.

- [ ] **Step 7: Commit**

  ```bash
  git add scripts/mock_analytics_analysis.py tests/validation/test_mock_analysis.py tests/validation/test_analytics_mcp.py
  git commit -m "test: validate analytics questions through MCP"
  ```

### Task 9: Full Verification and Branch Handoff

**Files:**
- Modify only files required to resolve verification failures attributable to this feature.

**Interfaces:**
- Consumes: Completed Tasks 1–8.
- Produces: A clean, verified feature branch ready for review; no merge to `main`.

- [ ] **Step 1: Run the complete test suite**

  Run: `uv run pytest -v`

  Expected: all tests PASS.

- [ ] **Step 2: Run static checks**

  Run: `uv run ruff check .`

  Expected: PASS.

  Run: `uv run ruff format --check .`

  Expected: PASS.

- [ ] **Step 3: Verify no migration drift**

  Run: `uv run python manage.py makemigrations --check --dry-run`

  Expected: `No changes detected`.

- [ ] **Step 4: Run the stored validation project through the CLI and MCP server**

  Set `ANALYTICS_PROJECT_ID=96a53597-d0e4-4a48-8614-41e483b4fd31`, run `uv run python scripts/mock_analytics_analysis.py`, then connect an SDK client to `uv run python scripts/run_analytics_mcp.py` and call `describe_project` plus representative adoption, overlap, funnel, and trial tools.

  Expected: zero assertion failures; scope equals the configured project; event count is 3,524; definition count is 24; interpretations and truncation metadata are present; no raw IDs appear.

- [ ] **Step 5: Inspect the final branch diff**

  Run: `git status --short --branch && git diff --check main...HEAD && git diff --stat main...HEAD`

  Expected: clean working tree, no whitespace errors, and changes limited to the approved analytics MCP feature. Do not merge to `main`.

- [ ] **Step 6: Commit any verification-only fixes**

  If Step 1–5 required changes, commit only those verified corrections:

  ```bash
  git add <exact corrected files>
  git commit -m "fix: resolve analytics MCP verification findings"
  ```

- [ ] **Step 7: Request final code review**

  Use `superpowers:requesting-code-review` against the complete branch diff, address only verified findings, rerun Steps 1–5, and hand off the feature branch without merging it.

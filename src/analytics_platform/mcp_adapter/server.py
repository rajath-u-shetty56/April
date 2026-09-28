from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import connections
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from analytics_platform.analytics.accounts import (
    find_cross_product_accounts as find_cross_product_accounts_service,
)
from analytics_platform.analytics.accounts import (
    summarize_account_activity as summarize_account_activity_service,
)
from analytics_platform.analytics.catalog import (
    describe_project as describe_project_service,
)
from analytics_platform.analytics.catalog import (
    get_account_profile as get_account_profile_service,
)
from analytics_platform.analytics.change import (
    analyze_account_change as analyze_account_change_service,
)
from analytics_platform.analytics.contracts import AnalyticsInputError, TimeRange
from analytics_platform.analytics.features import (
    analyze_product_adoption as analyze_product_adoption_service,
)
from analytics_platform.analytics.features import (
    count_feature_users as count_feature_users_service,
)
from analytics_platform.analytics.funnels import analyze_funnel as analyze_funnel_service
from analytics_platform.analytics.trials import (
    analyze_trial_outcomes as analyze_trial_outcomes_service,
)
from analytics_platform.catalog.models import Project
from analytics_platform.mcp_adapter.schemas import (
    AccountActivityOutput,
    AccountChangeOutput,
    AccountProfileOutput,
    AdoptionOutput,
    CrossProductOutput,
    FeatureUsersOutput,
    FunnelOutput,
    ProjectDescriptionOutput,
    TrialOutcomeOutput,
)

logger = logging.getLogger("analytics.mcp")
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _project(project_id: UUID) -> Project:
    try:
        return Project.objects.select_related("workspace").get(
            pk=project_id,
            is_active=True,
            workspace__is_active=True,
        )
    except Project.DoesNotExist as exc:
        raise AnalyticsInputError("Configured analytics project is unavailable") from exc


def _period(start: str, end: str) -> TimeRange:
    try:
        parsed_start = datetime.fromisoformat(start)
        parsed_end = datetime.fromisoformat(end)
    except ValueError as exc:
        raise AnalyticsInputError("start and end must be ISO 8601 timestamps") from exc
    return TimeRange.create(parsed_start, parsed_end)


def _execute[ResultT](operation: Callable[[Project], ResultT], project_id: UUID) -> ResultT:
    try:
        return operation(_project(project_id))
    except (AnalyticsInputError, ValidationError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        logger.error(
            "Analytics MCP query failed project_id=%s exception_type=%s",
            project_id,
            type(exc).__name__,
        )
        raise ToolError("Analytics query failed") from exc
    finally:
        connections.close_all()


def create_server(project_id: UUID) -> MCPServer:
    server = MCPServer(
        name="april-analytics",
        description="Read-only analytics for one server-configured April project.",
    )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def describe_project(event_definition_limit: int = 50) -> ProjectDescriptionOutput:
        """Describe the configured project before deeper analysis.

        Use this to discover available products, observed event definitions, counts, and
        event-time coverage. ``event_definition_limit`` bounds the catalog list; its
        metadata reports truncation. Results describe stored observations, not entitlement.
        """
        return _execute(
            lambda project: ProjectDescriptionOutput.model_validate(
                describe_project_service(
                    project, event_definition_limit=event_definition_limit
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_account_profile(account_key: str) -> AccountProfileOutput:
        """Return the current merged profile state for one account.

        Use ``account_key`` to inspect current plan, status, and other group properties.
        This is the latest materialized profile, not state at a historical event time.
        """
        return _execute(
            lambda project: AccountProfileOutput.model_validate(
                get_account_profile_service(project, account_key).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def summarize_account_activity(account_key: str, start: str, end: str) -> AccountActivityOutput:
        """Summarize observed product events and distinct users for one account.

        Use this for an account's activity in the half-open ISO-8601 window ``[start, end)``.
        Counts represent observed usage only; they do not establish entitlement or profile state.
        """
        return _execute(
            lambda project: AccountActivityOutput.model_validate(
                summarize_account_activity_service(
                    project, account_key, _period(start, end)
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def find_cross_product_accounts(
        products: list[str], start: str, end: str, limit: int = 50
    ) -> CrossProductOutput:
        """Find accounts with observed use of every requested product in a time window.

        Supply at least two product keys and ISO-8601 ``start``/``end`` timestamps; ``limit``
        bounds matching account evidence. Account event totals cover matching accounts only.
        Aggregated user overlap returns no raw IDs and explicitly identifies its broader
        population: all accounts active in any requested product during the observed window.
        """
        return _execute(
            lambda project: CrossProductOutput.model_validate(
                find_cross_product_accounts_service(
                    project, products, _period(start, end), limit=limit
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def analyze_product_adoption(
        product: str,
        start: str,
        end: str,
        feature: str | None = None,
        include_plan_breakdown: bool = False,
        account_limit: int = 50,
    ) -> AdoptionOutput:
        """Analyze product and qualifying-feature adoption against historical entitlement.

        Use ``product`` and optional deterministic ``feature`` over ``[start, end)``.
        Overall product usage counts any observed product event, while feature adoption counts
        only qualifying feature events from deterministic semantics. Overall entitlement is profile
        state at period end; optional plan breakdown attributes each qualifying feature event to
        historical plan/status at event time. ``account_limit`` bounds evidence lists. The result
        exposes thresholds and entitled evidence for high-adoption/low-depth classification.
        """
        return _execute(
            lambda project: AdoptionOutput.model_validate(
                analyze_product_adoption_service(
                    project,
                    product,
                    _period(start, end),
                    feature=feature,
                    include_plan_breakdown=include_plan_breakdown,
                    account_limit=account_limit,
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def count_feature_users(
        product: str,
        start: str,
        end: str,
        feature: str | None = None,
        account_key: str | None = None,
        account_limit: int = 50,
    ) -> FeatureUsersOutput:
        """Count distinct users of deterministic qualifying features by account.

        Use ``product``, optional ``feature`` or ``account_key``, and the half-open ISO-8601
        window. ``account_limit`` bounds account rows. This is observed qualifying usage,
        returns aggregates without raw user IDs, and does not imply entitlement.
        """
        return _execute(
            lambda project: FeatureUsersOutput.model_validate(
                count_feature_users_service(
                    project,
                    product,
                    _period(start, end),
                    feature=feature,
                    account_key=account_key,
                    account_limit=account_limit,
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def analyze_funnel(
        funnel: str,
        start: str,
        end: str,
        account_key: str | None = None,
    ) -> FunnelOutput:
        """Measure ordered completion of a named deterministic funnel.

        Use a configured ``funnel`` with ISO-8601 ``start``/``end`` and optional account scope.
        Starts are matched to later completions by the configured correlation property; results
        represent observed event sequences rather than current profile or entitlement state.
        """
        return _execute(
            lambda project: FunnelOutput.model_validate(
                analyze_funnel_service(
                    project, funnel, _period(start, end), account_key=account_key
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def analyze_account_change(
        kind: str,
        product: str,
        start: str,
        end: str,
        feature: str | None = None,
        decline_threshold: float = 50.0,
        account_limit: int = 50,
    ) -> AccountChangeOutput:
        """Find accounts with observed usage decline or feature abandonment.

        ``kind`` selects usage decline or feature abandonment; ``product`` and optional
        deterministic ``feature`` define usage. ``[start, end)`` is compared with the preceding
        equal window, ``decline_threshold`` is a percent, and ``account_limit`` bounds evidence.
        This compares observed events, not profile or entitlement changes.
        """
        return _execute(
            lambda project: AccountChangeOutput.model_validate(
                analyze_account_change_service(
                    project,
                    kind,  # type: ignore[arg-type]
                    product,
                    _period(start, end),
                    feature=feature,
                    decline_threshold=decline_threshold,
                    account_limit=account_limit,
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def analyze_trial_outcomes(
        product: str, start: str, end: str, account_limit: int = 50
    ) -> TrialOutcomeOutput:
        """Classify historical trial episodes as converted or expired with prior usage.

        Use ``product`` and an ISO-8601 ``[start, end)`` transition window; ``account_limit``
        bounds account evidence. Trial, conversion, and expiry meanings come from deterministic
        semantics, and historical states depend on complete, correctly timestamped profile events.
        """
        return _execute(
            lambda project: TrialOutcomeOutput.model_validate(
                analyze_trial_outcomes_service(
                    project, product, _period(start, end), account_limit=account_limit
                ).to_dict()
            ),
            project_id,
        )

    return server

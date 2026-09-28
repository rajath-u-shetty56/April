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
        return TimeRange.create(datetime.fromisoformat(start), datetime.fromisoformat(end))
    except ValueError as exc:
        raise AnalyticsInputError("start and end must be ISO 8601 timestamps") from exc


def _execute[ResultT](operation: Callable[[Project], ResultT], project_id: UUID) -> ResultT:
    try:
        return operation(_project(project_id))
    except (AnalyticsInputError, ValidationError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected analytics MCP query failure for project %s", project_id)
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
        return _execute(
            lambda project: AccountProfileOutput.model_validate(
                get_account_profile_service(project, account_key).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def summarize_account_activity(account_key: str, start: str, end: str) -> AccountActivityOutput:
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
        return _execute(
            lambda project: TrialOutcomeOutput.model_validate(
                analyze_trial_outcomes_service(
                    project, product, _period(start, end), account_limit=account_limit
                ).to_dict()
            ),
            project_id,
        )

    return server

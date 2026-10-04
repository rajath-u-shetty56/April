from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from analytics_platform.analytics.contracts import AnalyticsInputError, TimeRange
from analytics_platform.analytics.group_queries import (
    analyze_group_adoption as analyze_group_adoption_service,
)
from analytics_platform.analytics.group_queries import (
    analyze_group_funnel as analyze_group_funnel_service,
)
from analytics_platform.analytics.group_queries import (
    analyze_group_state as analyze_group_state_service,
)
from analytics_platform.analytics.group_queries import (
    analyze_group_transitions as analyze_group_transitions_service,
)
from analytics_platform.analytics.group_queries import (
    compare_group_activity as compare_group_activity_service,
)
from analytics_platform.analytics.group_queries import (
    query_group_activity as query_group_activity_service,
)
from analytics_platform.analytics.query import query_events as query_events_service
from analytics_platform.analytics.query_catalog import (
    discover_analytics_catalog as discover_analytics_catalog_service,
)
from analytics_platform.catalog.models import Project
from analytics_platform.mcp_adapter.schemas import (
    ActivityRuleInput,
    AnalyticsCatalogOutput,
    EventAggregationInput,
    EventDimensionInput,
    EventFilterInput,
    EventQueryOutput,
    FunnelCorrelationInput,
    GroupActivityComparisonOutput,
    GroupActivityOutput,
    GroupAdoptionOutput,
    GroupFilterInput,
    GroupFunnelOutput,
    GroupStateOutput,
    GroupTransitionsOutput,
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
    except (TypeError, ValueError) as exc:
        raise ToolError("start and end must be ISO 8601 timestamps") from exc
    try:
        return TimeRange.create(parsed_start, parsed_end)
    except AnalyticsInputError as exc:
        raise ToolError(str(exc)) from exc


def _optional_period(start: str | None, end: str | None) -> TimeRange | None:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ToolError("start and end must be supplied together")
    return _period(start, end)


def _event_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ToolError("event_time must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolError("event_time must be timezone-aware")
    return parsed


def _plain(value: Any):
    if isinstance(value, BaseModel):
        return {key: _plain(item) for key, item in value.model_dump(exclude_unset=True).items()}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _set_analytics_statement_timeout() -> None:
    """Set the timeout for this transaction only; called from analytics MCP execution."""
    if connection.vendor != "postgresql":
        return
    if connection.get_autocommit():
        raise RuntimeError("analytics statement timeout requires an active transaction")
    timeout = settings.ANALYTICS_QUERY_TIMEOUT_MS
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            [f"{timeout}ms"],
        )


def _execute[ResultT](operation: Callable[[Project], ResultT], project_id: UUID) -> ResultT:
    try:
        with transaction.atomic():
            _set_analytics_statement_timeout()
            result = operation(_project(project_id))
            if isinstance(result, BaseModel):
                result_size = len(result.model_dump_json().encode("utf-8"))
                if result_size > settings.ANALYTICS_MAX_RESPONSE_BYTES:
                    raise AnalyticsInputError(
                        "Analytics result exceeds the configured response size; narrow the query"
                    )
            return result
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
        description="Read-only structured analytics for one server-configured project.",
    )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def discover_analytics_catalog(
        event_definition_limit: int = 50,
        event_property_limit: int = 100,
        group_property_limit: int = 100,
    ) -> AnalyticsCatalogOutput:
        """Discover visible and verified events and observed property definitions.

        Hidden definitions are omitted from discovery. Event properties are scoped by product
        and event; group properties are scoped by group type. No observed property values are
        returned, and each catalog section reports its own truncation metadata.
        """
        return _execute(
            lambda project: AnalyticsCatalogOutput.model_validate(
                discover_analytics_catalog_service(
                    project,
                    event_definition_limit=event_definition_limit,
                    event_property_limit=event_property_limit,
                    group_property_limit=group_property_limit,
                )
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def query_events(
        start: str,
        end: str,
        filters: list[EventFilterInput] | None = None,
        group_by: list[EventDimensionInput] | None = None,
        aggregations: list[EventAggregationInput] | None = None,
        limit: int = 50,
        comparison_start: str | None = None,
        comparison_end: str | None = None,
    ) -> EventQueryOutput:
        """Query observed events with structured filters, grouping, and aggregations.

        The window is half-open, ``[start, end)``. Event and product selectors must resolve to
        visible or verified catalog definitions. Property references are event scoped; product
        may be omitted to query matching definitions across products, with per-definition
        coverage. Optional comparison timestamps run the same query specification over an equal
        length explicit window. Distinct IDs use exact string equality and do not perform identity
        resolution.
        """
        period = _period(start, end)
        comparison_period = _optional_period(comparison_start, comparison_end)
        event_filters = _plain(filters or [])
        dimensions = _plain(group_by or [])
        aggregation_specs = (
            _plain(aggregations)
            if aggregations is not None
            else [{"kind": "event_count", "label": "events"}]
        )
        return _execute(
            lambda project: EventQueryOutput.model_validate(
                query_events_service(
                    project,
                    period=period,
                    filters=event_filters,
                    group_by=dimensions,
                    aggregations=aggregation_specs,
                    limit=limit,
                    comparison_period=comparison_period,
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def analyze_group_state(
        group_type: str,
        state_basis: Literal["current", "period_start", "period_end", "event_time"],
        group_key: str | None = None,
        start: str | None = None,
        end: str | None = None,
        event_time: str | None = None,
        group_filters: list[GroupFilterInput] | None = None,
        limit: int = 50,
    ) -> GroupStateOutput:
        """Inspect one group or a bounded list of matching groups at an explicit state time.

        ``state_basis`` is required: ``current``, ``period_start``, ``period_end``, or
        ``event_time``. Supplying ``group_key`` selects one group; omitting it returns bounded
        results with complete count and truncation metadata. Hidden properties are not returned.
        """
        period = _optional_period(start, end)
        return _execute(
            lambda project: GroupStateOutput.model_validate(
                analyze_group_state_service(
                    project,
                    group_type=group_type,
                    state_basis=state_basis,
                    group_key=group_key,
                    period=period,
                    event_time=_event_time(event_time),
                    group_filters=_plain(group_filters or []),
                    limit=limit,
                )
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def query_group_activity(
        group_type: str,
        start: str,
        end: str,
        state_basis: Literal["current", "period_start", "period_end", "event_time"],
        activity_rules: list[ActivityRuleInput],
        match: Literal["all", "any"],
        group_filters: list[GroupFilterInput] | None = None,
        include_distinct_id_overlap: bool = False,
        limit: int = 50,
    ) -> GroupActivityOutput:
        """Find groups matching multiple labeled activity rules with explicit all or any logic.

        Each rule scopes an event to a product and can add catalog-backed event property filters.
        The required ``state_basis`` declares the group property time basis for any state filters.
        Optional distinct ID overlap counts exact string equality only and does not resolve people.
        """
        period = _period(start, end)
        return _execute(
            lambda project: GroupActivityOutput.model_validate(
                query_group_activity_service(
                    project,
                    group_type=group_type,
                    period=period,
                    state_basis=state_basis,
                    activity_rules=_plain(activity_rules),
                    match=match,
                    group_filters=_plain(group_filters or []),
                    include_distinct_id_overlap=include_distinct_id_overlap,
                    limit=limit,
                ).to_dict()
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def analyze_group_adoption(
        group_type: str,
        start: str,
        end: str,
        state_basis: Literal["current", "period_start", "period_end", "event_time"],
        eligibility_filters: list[GroupFilterInput],
        activity_rule: ActivityRuleInput,
        limit: int = 50,
    ) -> GroupAdoptionOutput:
        """Measure adoption among groups matching an explicit eligibility denominator.

        Eligibility properties and the state time basis are required. Event-time eligibility
        includes a group in the denominator if it matched eligibility at any point in the period,
        and counts activity only while eligible. Event depth reports period event totals and the
        median count per adopting group. Activity is a structured event rule; this tool does not
        infer an eligible population when no filters are supplied.
        """
        period = _period(start, end)
        return _execute(
            lambda project: GroupAdoptionOutput.model_validate(
                analyze_group_adoption_service(
                    project,
                    group_type=group_type,
                    period=period,
                    state_basis=state_basis,
                    eligibility_filters=_plain(eligibility_filters),
                    activity_rule=_plain(activity_rule),
                    limit=limit,
                )
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def analyze_group_funnel(
        start: str,
        end: str,
        steps: list[ActivityRuleInput],
        group_type: str | None = None,
        correlation: FunnelCorrelationInput | None = None,
        group_filters: list[GroupFilterInput] | None = None,
        state_basis: Literal["current", "period_start", "period_end", "event_time"] | None = None,
    ) -> GroupFunnelOutput:
        """Measure ordered progression through labeled event steps.

        Correlate by ``group_key``, exact ``distinct_id``, or a shared observed string or number
        event
        property such as ``call_id``. Omitting correlation preserves group-key correlation and
        requires ``group_type``. Property correlations are checked against each step's event
        definition. Correlation values are not returned. ``state_basis`` is required only when
        group filters read profile state. Optional group filters use visible or verified group
        properties. This is observed progression and does not imply eligibility or entitlement.
        """
        period = _period(start, end)
        return _execute(
            lambda project: GroupFunnelOutput.model_validate(
                analyze_group_funnel_service(
                    project,
                    group_type=group_type,
                    period=period,
                    state_basis=state_basis,
                    steps=_plain(steps),
                    correlation=_plain(correlation) if correlation is not None else None,
                    group_filters=_plain(group_filters or []),
                )
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def analyze_group_transitions(
        group_type: str,
        property_name: str,
        start: str,
        end: str,
        state_basis: Literal["event_time"],
        group_key: str | None = None,
        limit: int = 50,
    ) -> GroupTransitionsOutput:
        """Return observed changes to one visible group property in event-time order.

        ``state_basis`` is explicitly ``event_time``. The optional group key narrows the result;
        hidden properties are rejected and transition evidence is bounded.
        """
        period = _period(start, end)
        return _execute(
            lambda project: GroupTransitionsOutput.model_validate(
                analyze_group_transitions_service(
                    project,
                    group_type=group_type,
                    property_name=property_name,
                    period=period,
                    state_basis=state_basis,
                    group_key=group_key,
                    limit=limit,
                )
            ),
            project_id,
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def compare_group_activity(
        group_type: str,
        start: str,
        end: str,
        comparison_start: str,
        comparison_end: str,
        state_basis: Literal["current", "period_start", "period_end", "event_time"],
        activity_rules: list[ActivityRuleInput],
        match: Literal["all", "any"],
        group_filters: list[GroupFilterInput] | None = None,
        limit: int = 50,
    ) -> GroupActivityComparisonOutput:
        """Compare the same labeled group activity query across two explicit windows.

        This shares structured activity rules and the common comparison engine with
        ``query_events``; match mode and group state basis are required.
        """
        period = _period(start, end)
        baseline = _period(comparison_start, comparison_end)
        return _execute(
            lambda project: GroupActivityComparisonOutput.model_validate(
                compare_group_activity_service(
                    project,
                    group_type=group_type,
                    period=period,
                    comparison_period=baseline,
                    state_basis=state_basis,
                    activity_rules=_plain(activity_rules),
                    match=match,
                    group_filters=_plain(group_filters or []),
                    limit=limit,
                )
            ),
            project_id,
        )

    return server

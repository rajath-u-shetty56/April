from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventFilterInput(InputModel):
    field: Literal["event", "product", "distinct_id", "group_key", "property"]
    operator: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "exists"]
    value: Any = None
    group_type: str | None = None
    event: str | None = None
    product: str | None = None
    property_name: str | None = None


class EventDimensionInput(InputModel):
    kind: Literal["event", "product", "group_key", "property", "day", "week", "month"]
    label: str
    group_type: str | None = None
    event: str | None = None
    product: str | None = None
    property_name: str | None = None


class EventAggregationInput(InputModel):
    kind: Literal[
        "event_count",
        "distinct_id_count",
        "distinct_group_count",
        "count_property",
        "sum_property",
        "avg_property",
        "min_property",
        "max_property",
    ]
    label: str
    group_type: str | None = None
    event: str | None = None
    product: str | None = None
    property_name: str | None = None


class GroupFilterInput(InputModel):
    property_name: str
    operator: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "exists"]
    value: Any = None


class ActivityPropertyFilterInput(InputModel):
    operator: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "exists"]
    property_name: str
    value: Any = None


class ActivityRuleInput(InputModel):
    label: str
    event: str
    product: str
    filters: list[ActivityPropertyFilterInput] | None = None


class FunnelCorrelationInput(InputModel):
    kind: Literal["group_key", "distinct_id", "property"]
    group_type: str | None = None
    property_name: str | None = None


class ScopeOutput(OutputModel):
    project_id: str


class BoundedMetadataOutput(OutputModel):
    items: list[Any]
    returned_count: int
    total_count: int
    truncated: bool


class PropertyCoverageOutput(OutputModel):
    current: BoundedMetadataOutput
    baseline: BoundedMetadataOutput | None = None


class AnalyticsCatalogOutput(OutputModel):
    scope: ScopeOutput
    products: list[str]
    counts: dict[str, int]
    event_definitions: BoundedMetadataOutput
    event_properties: BoundedMetadataOutput
    group_properties: BoundedMetadataOutput


class EventQueryOutput(OutputModel):
    scope: ScopeOutput
    period: dict[str, str]
    dimensions: list[str]
    aggregations: list[str]
    rows: BoundedMetadataOutput
    identifier_semantics: Literal["exact_distinct_id_equality"]
    comparison: dict[str, Any] | None = None
    property_coverage: PropertyCoverageOutput | None = None


class GroupStateOutput(OutputModel):
    scope: ScopeOutput
    group_type: str
    state_basis: Literal["current", "period_start", "period_end", "event_time"]
    state_as_of: str
    historical_profile_reliability: str | None
    period: dict[str, str] | None = None
    groups: BoundedMetadataOutput
    requested_group_key: str | None = None
    found: bool | None = None


class GroupActivityOutput(OutputModel):
    scope: ScopeOutput
    period: dict[str, str]
    group_type: str
    state_basis: Literal["current", "period_start", "period_end", "event_time"]
    match: Literal["all", "any"]
    activity_rules: list[dict[str, str]]
    groups: BoundedMetadataOutput
    historical_profile_reliability: str | None
    distinct_id_overlap: dict[str, Any] | None = None


class GroupAdoptionOutput(OutputModel):
    scope: ScopeOutput
    period: dict[str, str]
    group_type: str
    state_basis: Literal["current", "period_start", "period_end", "event_time"]
    eligibility_basis: str
    historical_profile_reliability: str | None
    eligibility_filters: list[dict[str, Any]]
    activity_rule: dict[str, str]
    numerator: int
    denominator: int
    adoption_rate: float
    event_depth: dict[str, Any]
    adopting_group_keys: BoundedMetadataOutput
    eligible_group_keys: BoundedMetadataOutput


class GroupFunnelOutput(OutputModel):
    scope: ScopeOutput
    period: dict[str, str]
    group_type: str | None
    correlation: dict[str, Any]
    state_basis: Literal["current", "period_start", "period_end", "event_time"] | None
    historical_profile_reliability: str | None
    started_cohort_count: int
    completed_cohort_count: int
    completion_rate: float
    steps: list[dict[str, Any]]


class GroupTransitionsOutput(OutputModel):
    scope: ScopeOutput
    period: dict[str, str]
    group_type: str
    property_name: str
    state_basis: Literal["event_time"]
    historical_profile_reliability: str
    transitions: BoundedMetadataOutput


class GroupActivityComparisonOutput(OutputModel):
    scope: ScopeOutput
    group_type: str
    state_basis: Literal["current", "period_start", "period_end", "event_time"]
    match: Literal["all", "any"]
    activity_rules: list[dict[str, str]]
    comparison: dict[str, Any]

from typing import Any

from pydantic import BaseModel, ConfigDict


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeOutput(OutputModel):
    project_id: str


class TimeRangeOutput(OutputModel):
    start: str
    end: str


class InterpretationOutput(OutputModel):
    usage_window: str
    entitlement_basis: str | None = None
    plan_attribution_basis: str | None = None
    historical_profile_reliability: str | None = None


class BoundedMetadataOutput(OutputModel):
    items: list[Any]
    returned_count: int
    total_count: int
    truncated: bool


class ProjectDescriptionOutput(OutputModel):
    scope: ScopeOutput
    workspace_key: str
    project_key: str
    project_name: str
    counts: dict[str, int]
    products: list[str]
    event_range: dict[str, str | None]
    event_definitions: BoundedMetadataOutput


class AccountProfileOutput(OutputModel):
    scope: ScopeOutput
    account_key: str
    found: bool
    state_basis: str
    properties: dict[str, Any]
    last_seen_at: str | None


class AccountActivityOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    interpretation: InterpretationOutput
    account_key: str
    active_products: list[str]
    event_counts: dict[str, dict[str, int]]
    distinct_users_by_product: dict[str, int]


class CrossProductOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    interpretation: InterpretationOutput
    products: list[str]
    accounts: BoundedMetadataOutput
    aggregate_event_counts: dict[str, int]
    user_overlap: dict[str, Any] | None


class AdoptionOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    interpretation: InterpretationOutput
    product: str
    features: dict[str, dict[str, Any]]
    plans: dict[str, dict[str, Any]] | None = None


class FeatureUsersOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    interpretation: InterpretationOutput
    product: str
    features: list[str]
    accounts: BoundedMetadataOutput
    totals_by_feature: dict[str, int]


class FunnelOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    interpretation: InterpretationOutput
    funnel: str
    configuration: dict[str, str]
    started: int
    completed: int
    lost: int
    completion_rate: float
    accounts_started: int
    accounts_completed: int


class AccountChangeOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    comparison_period: TimeRangeOutput
    interpretation: InterpretationOutput
    kind: str
    product: str
    feature: str | None
    threshold: float | None
    matching_account_count: int
    accounts: BoundedMetadataOutput


class TrialOutcomeOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    interpretation: InterpretationOutput
    product: str
    semantics: dict[str, str]
    used_before_conversion: BoundedMetadataOutput
    used_then_expired: BoundedMetadataOutput

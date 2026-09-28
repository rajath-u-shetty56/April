from typing import Any, Literal

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


class UserOverlapOutput(OutputModel):
    population_basis: str
    products: list[str]
    users_by_product: dict[str, int]
    users_in_all_products: int
    pairwise_overlap: dict[str, int]


class CrossProductOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    interpretation: InterpretationOutput
    products: list[str]
    accounts: BoundedMetadataOutput
    aggregate_event_counts: dict[str, int]
    user_overlap: UserOverlapOutput | None


class AdoptionRateOutput(OutputModel):
    numerator: int
    denominator: int
    rate: float


class HighAdoptionLowDepthOutput(OutputModel):
    adoption_numerator: int
    adoption_denominator: int
    adoption_rate: float
    adoption_rate_threshold: float
    minimum_account_threshold: int
    median_depth_threshold: float
    actual_median_depth: float
    classified: bool


class OverallAdoptionOutput(OutputModel):
    usage_basis: Literal["any_observed_product_event_in_period"]
    observed_account_count: int
    observed_accounts: BoundedMetadataOutput
    period_end_entitled_accounts: BoundedMetadataOutput
    period_end_entitled_adoption: AdoptionRateOutput
    entitled_without_usage: BoundedMetadataOutput


class FeatureAdoptionOutput(OutputModel):
    usage_basis: Literal["qualifying_feature_events_in_period"]
    observed_adopting_account_count: int
    observed_event_count: int
    median_events_per_adopting_account: float
    high_adoption_low_depth: HighAdoptionLowDepthOutput
    period_end_entitled_adoption: AdoptionRateOutput
    adopting_accounts: BoundedMetadataOutput


class AdoptionOutput(OutputModel):
    scope: ScopeOutput
    period: TimeRangeOutput
    interpretation: InterpretationOutput
    product: str
    overall: OverallAdoptionOutput
    features: dict[str, FeatureAdoptionOutput]
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

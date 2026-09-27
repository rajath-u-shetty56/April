from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from analytics_platform.analytics.contracts import AnalyticsInputError


@dataclass(frozen=True)
class EventPredicate:
    event: str
    properties_equal: Mapping[str, object] = MappingProxyType({})

    def matches(self, event_name: str, properties: Mapping[str, object]) -> bool:
        return event_name == self.event and all(
            properties.get(key) == value for key, value in self.properties_equal.items()
        )


@dataclass(frozen=True)
class FeatureDefinition:
    key: str
    predicate: EventPredicate


@dataclass(frozen=True)
class TrialDefinition:
    status: str
    conversion_status: str
    expiry_status: str


@dataclass(frozen=True)
class ProductDefinition:
    key: str
    status_property: str
    plan_property: str | None
    entitled_statuses: frozenset[str]
    identity_namespace: str
    features: tuple[FeatureDefinition, ...]
    trial: TrialDefinition


@dataclass(frozen=True)
class FunnelDefinition:
    key: str
    product: str
    start_event: str
    completion_event: str
    correlation_property: str


@dataclass(frozen=True)
class SemanticRegistry:
    products: Mapping[str, ProductDefinition]
    funnels: Mapping[str, FunnelDefinition]

    def get_product(self, product: str) -> ProductDefinition:
        try:
            return self.products[product]
        except KeyError as exc:
            raise AnalyticsInputError(f"Unsupported product: {product}") from exc

    def get_feature(self, product: str, feature: str) -> FeatureDefinition:
        definition = self.get_product(product)
        for candidate in definition.features:
            if candidate.key == feature:
                return candidate
        raise AnalyticsInputError(f"Unsupported feature for {product}: {feature}")

    def get_funnel(self, funnel: str) -> FunnelDefinition:
        try:
            return self.funnels[funnel]
        except KeyError as exc:
            raise AnalyticsInputError(f"Unsupported funnel: {funnel}") from exc


def _feature(
    key: str, event: str, properties_equal: Mapping[str, object] | None = None
) -> FeatureDefinition:
    return FeatureDefinition(
        key=key,
        predicate=EventPredicate(
            event=event,
            properties_equal=MappingProxyType(dict(properties_equal or {})),
        ),
    )


_TRIAL = TrialDefinition(status="trial", conversion_status="active", expiry_status="expired")

_PRODUCTS = {
    "helpdesk": ProductDefinition(
        key="helpdesk",
        status_property="helpdesk_status",
        plan_property="helpdesk_plan",
        entitled_statuses=frozenset({"active", "trial"}),
        identity_namespace="helpdesk_user",
        features=(
            _feature("helpdesk_macro", "macro_applied"),
            _feature("sla_policy", "sla_policy_created"),
        ),
        trial=_TRIAL,
    ),
    "contact_center": ProductDefinition(
        key="contact_center",
        status_property="contact_center_status",
        plan_property="contact_center_plan",
        entitled_statuses=frozenset({"active", "trial"}),
        identity_namespace="helpdesk_user",
        features=(
            _feature("call_transfer", "call_transfer_completed"),
            _feature("callback", "callback_fulfilled"),
            _feature("call_hold", "call_hold_ended"),
            _feature("ai_summary", "call_summary_generated", {"success": True}),
            _feature("supervisor_listen", "listen_started"),
            _feature("supervisor_whisper", "whisper_started"),
            _feature("supervisor_barge", "barge_started"),
        ),
        trial=_TRIAL,
    ),
    "rise": ProductDefinition(
        key="rise",
        status_property="rise_status",
        plan_property="rise_plan",
        entitled_statuses=frozenset({"active", "trial"}),
        identity_namespace="helpdesk_user",
        features=(_feature("course_completion", "course_completed"),),
        trial=_TRIAL,
    ),
    "bi": ProductDefinition(
        key="bi",
        status_property="bi_status",
        plan_property="bi_plan",
        entitled_statuses=frozenset({"active", "trial"}),
        identity_namespace="bi_user",
        features=(
            _feature("report_creation", "report_created"),
            _feature("report_viewing", "report_viewed"),
            _feature("dashboard_creation", "dashboard_created"),
            _feature("dashboard_viewing", "dashboard_viewed"),
        ),
        trial=_TRIAL,
    ),
}

_FUNNELS = {
    "call_connection": FunnelDefinition(
        key="call_connection",
        product="contact_center",
        start_event="call_initiated",
        completion_event="call_connected",
        correlation_property="call_id",
    ),
    "call_transfer": FunnelDefinition(
        key="call_transfer",
        product="contact_center",
        start_event="call_transfer_initiated",
        completion_event="call_transfer_completed",
        correlation_property="call_id",
    ),
    "callback": FunnelDefinition(
        key="callback",
        product="contact_center",
        start_event="callback_requested",
        completion_event="callback_fulfilled",
        correlation_property="call_id",
    ),
}

SEMANTICS = SemanticRegistry(
    products=MappingProxyType(_PRODUCTS),
    funnels=MappingProxyType(_FUNNELS),
)

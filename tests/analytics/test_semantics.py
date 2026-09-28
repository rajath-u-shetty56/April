import pytest

from analytics_platform.analytics.contracts import AnalyticsInputError
from analytics_platform.analytics.semantics import SEMANTICS


def test_contact_center_features_are_explicit_qualifying_events():
    expected = {
        "call_transfer": ("call_transfer_completed", {}),
        "callback": ("callback_fulfilled", {}),
        "call_hold": ("call_hold_ended", {}),
        "ai_summary": ("call_summary_generated", {"success": True}),
        "supervisor_listen": ("listen_started", {}),
        "supervisor_whisper": ("whisper_started", {}),
        "supervisor_barge": ("barge_started", {}),
    }

    actual = {
        feature.key: (feature.predicate.event, dict(feature.predicate.properties_equal))
        for feature in SEMANTICS.get_product("contact_center").features
    }

    assert actual == expected


def test_named_funnels_are_ordered_and_correlated_by_call():
    expected = {
        "call_connection": ("call_initiated", "call_connected", "call_id"),
        "call_transfer": (
            "call_transfer_initiated",
            "call_transfer_completed",
            "call_id",
        ),
        "callback": ("callback_requested", "callback_fulfilled", "call_id"),
    }

    assert {
        key: (funnel.start_event, funnel.completion_event, funnel.correlation_property)
        for key, funnel in SEMANTICS.funnels.items()
    } == expected


def test_entitlement_trials_and_shared_identity_are_deterministic():
    contact_center = SEMANTICS.get_product("contact_center")

    assert contact_center.entitled_statuses == frozenset({"active", "trial"})
    assert contact_center.trial.status == "trial"
    assert contact_center.trial.conversion_status == "active"
    assert contact_center.trial.expiry_status == "expired"
    assert {
        SEMANTICS.get_product(key).identity_namespace
        for key in ("helpdesk", "contact_center", "rise")
    } == {"helpdesk_user"}
    assert SEMANTICS.get_product("bi").identity_namespace == "bi_user"


@pytest.mark.parametrize(
    ("value", "matches"),
    [(True, True), (False, False), (1, False), (1.0, False), ("true", False)],
)
def test_boolean_feature_predicates_require_an_actual_boolean(value, matches):
    predicate = SEMANTICS.get_feature("contact_center", "ai_summary").predicate

    assert predicate.matches("call_summary_generated", {"success": value}) is matches


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("get_product", ("unknown",)),
        ("get_feature", ("contact_center", "unknown")),
        ("get_funnel", ("unknown",)),
    ],
)
def test_unknown_semantics_are_rejected(method, arguments):
    with pytest.raises(AnalyticsInputError, match="Unsupported"):
        getattr(SEMANTICS, method)(*arguments)

from datetime import UTC

from scripts.mock_analytics_dataset import (
    ANALYSIS_CUTOFF,
    build_dataset,
)


def test_dataset_is_deterministic_and_includes_the_schema_evolution_period():
    first = build_dataset()
    second = build_dataset()

    assert first == second
    assert 3500 <= len(first.behavioral_events) <= 4500
    assert len(first.profile_events) == 13
    timestamps = [event["timestamp"] for event in first.behavioral_events]
    assert min(timestamps) == "2026-07-06T09:00:00Z"
    assert max(timestamps) == "2026-09-14T09:02:00Z"


def test_behavioral_events_follow_product_and_account_identity_conventions():
    dataset = build_dataset()

    expected_user_store = {
        "helpdesk": "helpdesk",
        "contact_center": "helpdesk",
        "rise": "helpdesk",
        "bi": "bi",
    }

    assert {event["groups"]["account"] for event in dataset.behavioral_events} == {
        "acme",
        "bravo",
        "charlie",
        "delta",
        "echo",
        "foxtrot",
        "golf",
        "hotel",
        "india",
        "juliet",
    }
    for event in dataset.behavioral_events:
        product = event["properties"]["product"]
        account = event["groups"]["account"]
        assert event["distinct_id"].startswith(
            f"{expected_user_store[product]}:{account}:user:"
        )
        assert event["timestamp"].endswith("Z")
        assert event["uuid"]


def test_ticket_created_schema_evolves_without_rewriting_historical_events():
    ticket_created = [
        event
        for event in build_dataset().behavioral_events
        if event["event"] == "ticket_created"
        and event["properties"]["product"] == "helpdesk"
    ]
    version_one = [event for event in ticket_created if event["properties"]["version"] == 1]
    version_two = [event for event in ticket_created if event["properties"]["version"] == 2]

    assert len(version_one) == 250
    assert len(version_two) == 3
    assert max(event["timestamp"] for event in version_one) < min(
        event["timestamp"] for event in version_two
    )
    assert all(
        not {"channel", "priority", "requester_type"} & event["properties"].keys()
        for event in version_one
    )
    assert {
        (
            event["groups"]["account"],
            event["properties"]["channel"],
            event["properties"]["priority"],
            event["properties"]["requester_type"],
        )
        for event in version_two
    } == {
        ("acme", "email", "high", "contact"),
        ("charlie", "portal", "normal", "contact"),
        ("juliet", "email", "urgent", "agent"),
    }


def test_products_using_the_helpdesk_user_store_share_user_identity():
    events = build_dataset().behavioral_events

    acme_helpdesk_user = next(
        event
        for event in events
        if event["groups"]["account"] == "acme"
        and event["properties"]["product"] == "helpdesk"
        and event["distinct_id"].endswith(":user:1")
    )
    acme_contact_center_user = next(
        event
        for event in events
        if event["groups"]["account"] == "acme"
        and event["properties"]["product"] == "contact_center"
        and event["distinct_id"].endswith(":user:1")
    )

    assert acme_helpdesk_user["distinct_id"] == acme_contact_center_user["distinct_id"]


def test_hotel_one_time_feature_uses_the_same_call_identity_as_its_call():
    hotel = [
        event
        for event in build_dataset().behavioral_events
        if event["groups"]["account"] == "hotel"
    ]

    assert {event["properties"]["call_id"] for event in hotel} == {"hotel-w3-c1"}


def test_profile_events_cover_entitlement_and_trial_transitions():
    dataset = build_dataset()
    updates = [event["properties"] for event in dataset.profile_events]

    assert all(event["event"] == "$groupidentify" for event in dataset.profile_events)
    assert all(event["groups"] == {} for event in dataset.profile_events)
    assert any(
        update["$group_key"] == "echo"
        and update["$group_set"]["contact_center_status"] == "active"
        for update in updates
    )
    assert any(
        update["$group_key"] == "foxtrot"
        and update["$group_set"]["contact_center_status"] == "expired"
        for update in updates
    )
    assert any(
        update["$group_key"] == "charlie"
        and update["$group_set"]["contact_center_status"] == "active"
        for update in updates
    )


def test_all_event_uuids_are_unique_and_timezone_aware():
    dataset = build_dataset()
    events = (*dataset.profile_events, *dataset.behavioral_events)

    assert len({event["uuid"] for event in events}) == len(events)
    assert ANALYSIS_CUTOFF.tzinfo is UTC

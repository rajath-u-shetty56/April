from analytics_platform.event_catalog.contracts import EventPayloadSerializer


def test_event_payload_accepts_tracking_metadata_and_arbitrary_properties():
    serializer = EventPayloadSerializer(
        data={
            "event": "call_completed",
            "distinct_id": "acme",
            "timestamp": "2026-09-08T10:30:00Z",
            "properties": {
                "account_id": "acme",
                "product": "contact_center",
                "version": "1",
                "agent_id": 41,
                "new_property_added_by_product": {"nested": True},
            },
        }
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["properties"]["version"] == "1"
    assert serializer.validated_data["properties"]["new_property_added_by_product"] == {
        "nested": True
    }


def test_event_payload_requires_distinct_id():
    serializer = EventPayloadSerializer(
        data={
            "event": "call_completed",
            "properties": {"account_id": "acme"},
        }
    )

    assert not serializer.is_valid()
    assert serializer.errors.keys() == {"distinct_id"}


def test_event_payload_properties_default_to_empty_object():
    serializer = EventPayloadSerializer(
        data={"event": "call_completed", "distinct_id": "acme"}
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["properties"] == {}

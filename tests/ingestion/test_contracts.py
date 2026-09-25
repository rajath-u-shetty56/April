import pytest

from analytics_platform.ingestion.contracts import validate_event
from analytics_platform.ingestion.errors import IngestionError


def test_project_id_is_rejected_as_an_unknown_envelope_field():
    with pytest.raises(IngestionError) as error:
        validate_event({"project_id": "a-project"})

    assert error.value.code == "UNKNOWN_ENVELOPE_FIELD"
    assert error.value.field is None


@pytest.mark.parametrize(
    "groups",
    [
        {"": "acme"},
        {" account": "acme"},
        {1: "acme"},
        {"x" * 81: "acme"},
        {"account": ""},
        {"account": " acme"},
        {"account": 12},
        {"account": {"id": "acme"}},
        {"account": "x" * 401},
    ],
)
def test_groups_require_bounded_trimmed_string_types_and_keys(groups):
    with pytest.raises(IngestionError) as error:
        validate_event(
            {
                "event": "ticket_created",
                "distinct_id": "helpdesk:agent:47",
                "groups": groups,
            }
        )

    assert error.value.code == "INVALID_GROUPS"
    assert error.value.field == "groups"

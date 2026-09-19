from types import SimpleNamespace

import pytest

from analytics_platform.ingestion.contracts import validate_event
from analytics_platform.ingestion.errors import IngestionError


def test_project_id_is_rejected_as_an_unknown_envelope_field():
    credential = SimpleNamespace(require_product=False, require_account=False)

    with pytest.raises(IngestionError) as error:
        validate_event({"project_id": "a-project"}, credential)

    assert error.value.code == "UNKNOWN_ENVELOPE_FIELD"
    assert error.value.field is None

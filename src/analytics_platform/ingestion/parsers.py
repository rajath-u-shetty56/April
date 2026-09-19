import json

from django.conf import settings
from rest_framework.parsers import BaseParser

from analytics_platform.ingestion.errors import IngestionError


class BoundedJSONParser(BaseParser):
    media_type = "application/json"

    def parse(self, stream, media_type=None, parser_context=None):
        limit = settings.INGESTION_MAX_REQUEST_BYTES
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise IngestionError("REQUEST_TOO_LARGE", status_code=413)
        try:

            def invalid_constant(_value):
                raise ValueError("Non-finite JSON number")

            return json.loads(data.decode("utf-8"), parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise IngestionError("INVALID_JSON") from error


def validate_batch(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise IngestionError("INVALID_BATCH", "events")
    events = payload["events"]
    if not events:
        raise IngestionError("INVALID_BATCH", "events")
    if len(events) > settings.INGESTION_MAX_BATCH_EVENTS:
        raise IngestionError("BATCH_TOO_LARGE", "events", 413)
    return events

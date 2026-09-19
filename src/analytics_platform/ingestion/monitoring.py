import hashlib
import json
import logging
from uuid import UUID

from django.utils import timezone

logger = logging.getLogger("analytics.ingestion")


def safe_uuid(payload):
    try:
        return str(UUID(str(payload.get("uuid")))) if isinstance(payload, dict) else None
    except (ValueError, TypeError, AttributeError):
        return None


def record_rejection(error, *, project_id=None, payload=None, request_id):
    event = payload.get("event") if isinstance(payload, dict) else None
    event_hash = (
        hashlib.sha256(event.encode("utf-8", errors="replace")).hexdigest()
        if isinstance(event, str)
        else None
    )
    # One structured counter sample per rejection, suitable for log-derived metrics.
    metadata = {
        "metric": "ingestion_rejections_total",
        "value": 1,
        "project_id": str(project_id) if project_id else None,
        "event_name_sha256": event_hash,
        "uuid": safe_uuid(payload),
        "code": error.code,
        "field": error.field,
        "timestamp": timezone.now().isoformat(),
        "request_id": request_id,
    }
    logger.info(json.dumps(metadata), extra={"ingestion": metadata})

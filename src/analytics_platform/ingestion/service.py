from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from analytics_platform.event_catalog.models import EventDefinition
from analytics_platform.events.models import Event
from analytics_platform.ingestion.contracts import json_bytes, validate_event
from analytics_platform.ingestion.errors import IngestionError
from analytics_platform.ingestion.models import IngestionCredential
from analytics_platform.ingestion.monitoring import record_rejection, safe_uuid


def same_json(left, right):
    """Compare JSONB iteratively, without conflating booleans and numbers."""
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if isinstance(left, bool) or isinstance(right, bool):
            if type(left) is not type(right) or left != right:
                return False
        elif isinstance(left, (int, float)) and isinstance(right, (int, float)):
            # PostgreSQL expands exponent notation; compare decimal meanings.
            if Decimal(str(left)) != Decimal(str(right)):
                return False
        elif type(left) is not type(right):
            return False
        elif isinstance(left, dict):
            if left.keys() != right.keys():
                return False
            pending.extend((left[key], right[key]) for key in left)
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right, strict=True))
        elif left != right:
            return False
    return True


def same_event(stored, value):
    for field in ("event", "distinct_id", "groups", "properties"):
        if not same_json(getattr(stored, field), value[field]):
            return False
    return "timestamp" not in value or value["timestamp"] == stored.timestamp


def persist_event(credential, value, received_at):
    event_uuid = value.get("uuid", uuid4())
    # Durable prevents callers nesting ingestion inside a transaction that could
    # roll back after an acceptance has been returned. Each bulk item commits here.
    with transaction.atomic(durable=True):
        active = IngestionCredential.objects.filter(
            pk=credential.pk,
            project_id=credential.project_id,
            revoked_at__isnull=True,
            project__is_active=True,
            project__workspace__is_active=True,
        ).exists()
        if not active:
            raise IngestionError("INVALID_CREDENTIAL", status_code=401)
        stored = Event.objects.filter(project=credential.project, uuid=event_uuid).first()
        if stored is not None:
            if not same_event(stored, value):
                raise IngestionError("UUID_CONFLICT", "uuid", 409)
            return {"uuid": str(stored.uuid), "status": "accepted", "duplicate": True}
        EventDefinition.objects.get_or_create(project=credential.project, name=value["event"])
        stored, created = Event.objects.get_or_create(
            project=credential.project,
            uuid=event_uuid,
            defaults={
                "event": value["event"],
                "distinct_id": value["distinct_id"],
                "timestamp": value.get("timestamp", received_at),
                "received_at": received_at,
                "groups": value["groups"],
                "properties": value["properties"],
            },
        )
        if not created and not same_event(stored, value):
            # Also rolls back a new definition created by a losing conflicting request.
            raise IngestionError("UUID_CONFLICT", "uuid", 409)
    return {"uuid": str(stored.uuid), "status": "accepted", "duplicate": not created}


def ingest_event(credential, payload, *, request_id, received_at=None):
    received_at = received_at or timezone.now()
    try:
        value = validate_event(payload, credential)
        # Bound direct service calls as well as HTTP requests.
        if len(json_bytes(payload)) > settings.INGESTION_MAX_REQUEST_BYTES:
            raise IngestionError("REQUEST_TOO_LARGE", status_code=413)
        result = persist_event(credential, value, received_at)
        return result, 200 if result["duplicate"] else 201
    except DatabaseError:
        # Do not expose/log DB exception messages: they can contain event values.
        error = IngestionError("STORAGE_UNAVAILABLE", status_code=503)
        record_rejection(
            error, project_id=credential.project_id, payload=payload, request_id=request_id
        )
        return error.result(safe_uuid(payload)), error.status_code
    except IngestionError as error:
        record_rejection(
            error, project_id=credential.project_id, payload=payload, request_id=request_id
        )
        return error.result(safe_uuid(payload)), error.status_code

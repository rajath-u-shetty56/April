import hashlib
import hmac
import secrets

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from analytics_platform.ingestion.models import IngestionCredential


def create_credential(project, *, name):
    prefix = "ing_" + secrets.token_hex(12)
    secret = prefix + "." + secrets.token_urlsafe(32)
    credential = IngestionCredential.objects.create(
        project=project,
        name=name,
        prefix=prefix,
        secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
    )
    return credential, secret


def revoke_credential(credential):
    now = timezone.now()
    IngestionCredential.objects.filter(pk=credential.pk, revoked_at__isnull=True).update(
        revoked_at=now,
        updated_at=now,
    )
    credential.refresh_from_db()
    return credential


def rotate_credential(credential):
    # Rotation deliberately leaves the old credential's lifecycle unchanged.
    return create_credential(credential.project, name=credential.name)


def authenticate_secret(secret):
    prefix, separator, _ = secret.partition(".")
    credential = None
    if separator and len(secret) <= 128 and len(prefix) <= 32:
        credential = (
            IngestionCredential.objects.select_related("project__workspace")
            .filter(
                prefix=prefix,
            )
            .first()
        )
    expected = credential.secret_hash if credential else "0" * 64
    valid = hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), expected)
    if not valid or credential is None or credential.revoked_at is not None:
        raise AuthenticationFailed("Invalid ingestion credential.", code="INVALID_CREDENTIAL")
    if not credential.project.is_active or not credential.project.workspace.is_active:
        raise AuthenticationFailed("Invalid ingestion credential.", code="INVALID_CREDENTIAL")
    with transaction.atomic():
        IngestionCredential.objects.filter(pk=credential.pk).update(last_used_at=timezone.now())
    return credential

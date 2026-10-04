from hashlib import sha256

from django.db import models


class PropertyDefinitionStatus(models.TextChoices):
    VISIBLE = "visible", "Visible"
    VERIFIED = "verified", "Verified"
    HIDDEN = "hidden", "Hidden"


VISIBLE_PROPERTY_STATUSES = (
    PropertyDefinitionStatus.VISIBLE,
    PropertyDefinitionStatus.VERIFIED,
)


def hash_property_name(property_name: str) -> str:
    return sha256(property_name.encode("utf-8")).hexdigest()


def has_type_conflict(observed_non_null_types: list[str]) -> bool:
    return len(set(observed_non_null_types)) > 1

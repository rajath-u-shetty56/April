"""Compatibility import; the ingestion domain now owns the envelope contract."""

from analytics_platform.ingestion.contracts import EventPayloadSerializer

__all__ = ["EventPayloadSerializer"]

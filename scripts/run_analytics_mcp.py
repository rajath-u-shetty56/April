#!/usr/bin/env python3
"""Run the fixed-project April analytics MCP server over stdio."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPOSITORY), str(REPOSITORY / "src")]


class StartupConfigurationError(ValueError):
    """A sanitized analytics MCP startup configuration failure."""


def bootstrap_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()


def resolve_project_id(environ: Mapping[str, str]) -> UUID:
    raw_project_id = environ.get("ANALYTICS_PROJECT_ID")
    if not raw_project_id:
        raise StartupConfigurationError("ANALYTICS_PROJECT_ID is required")
    try:
        project_id = UUID(raw_project_id)
    except ValueError as exc:
        raise StartupConfigurationError("ANALYTICS_PROJECT_ID must be a valid UUID") from exc

    from analytics_platform.catalog.models import Project

    if not Project.objects.filter(
        pk=project_id,
        is_active=True,
        workspace__is_active=True,
    ).exists():
        raise StartupConfigurationError("Configured analytics project is unavailable")
    return project_id


def _log_startup_error(message: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("Analytics MCP startup failed: %(message)s"))
    logger = logging.getLogger("analytics.mcp.startup")
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    try:
        logger.error(message)
    finally:
        logger.removeHandler(handler)


def main(
    environ: Mapping[str, str] | None = None,
    *,
    run_protocol: bool = True,
) -> int:
    try:
        bootstrap_django()
        project_id = resolve_project_id(os.environ if environ is None else environ)
    except StartupConfigurationError as exc:
        _log_startup_error(str(exc))
        return 2
    except Exception:
        _log_startup_error("unexpected startup error")
        return 2
    if run_protocol:
        from analytics_platform.mcp_adapter.server import create_server

        create_server(project_id).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

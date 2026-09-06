from django.apps import apps


def test_foundation_excludes_persisted_identity_graph_models():
    assert not apps.is_installed("analytics_platform.identity")
    assert "relationshiptype" not in apps.get_app_config("event_catalog").models


def test_foundation_excludes_strict_schema_registry_models():
    event_catalog_models = apps.get_app_config("event_catalog").models

    assert set(event_catalog_models) == {"eventdefinition"}


def test_catalog_contains_only_workspace_and_project_boundaries():
    catalog_models = apps.get_app_config("catalog").models

    assert set(catalog_models) == {"workspace", "project"}

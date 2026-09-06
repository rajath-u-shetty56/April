from django.urls import path

from analytics_platform.event_catalog.views import (
    ProjectEventDefinitionDetailView,
    ProjectEventDefinitionListCreateView,
)

urlpatterns = [
    path(
        "projects/<uuid:project_id>/event-definitions/",
        ProjectEventDefinitionListCreateView.as_view(),
    ),
    path(
        "projects/<uuid:project_id>/event-definitions/<uuid:definition_id>/",
        ProjectEventDefinitionDetailView.as_view(),
    ),
]

from django.urls import path

from analytics_platform.catalog.views import (
    ProjectListCreateView,
    WorkspaceListCreateView,
)

urlpatterns = [
    path("workspaces/", WorkspaceListCreateView.as_view()),
    path("workspaces/<uuid:workspace_id>/projects/", ProjectListCreateView.as_view()),
]

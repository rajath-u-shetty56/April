from django.urls import path

from analytics_platform.ingestion.views import (
    BulkView,
    CaptureView,
    CredentialActionView,
    CredentialListCreateView,
)

urlpatterns = [
    path("capture/", CaptureView.as_view()),
    path("bulk/", BulkView.as_view()),
    path("projects/<uuid:project_id>/ingestion-credentials/", CredentialListCreateView.as_view()),
    path(
        "projects/<uuid:project_id>/ingestion-credentials/<uuid:credential_id>/revoke/",
        CredentialActionView.as_view(action="revoke"),
    ),
    path(
        "projects/<uuid:project_id>/ingestion-credentials/<uuid:credential_id>/rotate/",
        CredentialActionView.as_view(action="rotate"),
    ),
]

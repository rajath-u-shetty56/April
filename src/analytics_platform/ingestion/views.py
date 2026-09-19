from uuid import uuid4

from django.core.exceptions import RequestDataTooBig
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics_platform.catalog.models import Project
from analytics_platform.catalog.views import StaffAPIView
from analytics_platform.ingestion.authentication import IngestionAuthentication
from analytics_platform.ingestion.credentials import (
    create_credential,
    revoke_credential,
    rotate_credential,
)
from analytics_platform.ingestion.errors import IngestionError
from analytics_platform.ingestion.models import IngestionCredential
from analytics_platform.ingestion.monitoring import record_rejection
from analytics_platform.ingestion.parsers import BoundedJSONParser, validate_batch
from analytics_platform.ingestion.serializers import CredentialSerializer
from analytics_platform.ingestion.service import ingest_event


class CredentialListCreateView(StaffAPIView):
    def get(self, request, project_id):
        project = get_object_or_404(Project, pk=project_id)
        return Response(CredentialSerializer(project.ingestion_credentials.all(), many=True).data)

    def post(self, request, project_id):
        project = get_object_or_404(Project, pk=project_id)
        serializer = CredentialSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        credential, secret = create_credential(project, **serializer.validated_data)
        return Response(
            {**CredentialSerializer(credential).data, "secret": secret},
            status=201,
            headers={"Cache-Control": "no-store"},
        )


class CredentialActionView(StaffAPIView):
    action = None

    def post(self, request, project_id, credential_id):
        credential = get_object_or_404(IngestionCredential, pk=credential_id, project_id=project_id)
        if self.action == "revoke":
            return Response(CredentialSerializer(revoke_credential(credential)).data)
        created, secret = rotate_credential(credential)
        return Response(
            {**CredentialSerializer(created).data, "secret": secret},
            status=201,
            headers={"Cache-Control": "no-store"},
        )


@method_decorator(transaction.non_atomic_requests, name="dispatch")
class IngestionAPIView(APIView):
    authentication_classes = [IngestionAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [BoundedJSONParser]

    def initial(self, request, *args, **kwargs):
        self.request_id = str(uuid4())
        self.received_at = timezone.now()
        super().initial(request, *args, **kwargs)

    def handle_exception(self, exc):
        if isinstance(exc, RequestDataTooBig):
            exc = IngestionError("REQUEST_TOO_LARGE", status_code=413)
        if isinstance(exc, APIException):
            code = {
                401: "INVALID_CREDENTIAL",
                403: "PERMISSION_DENIED",
                415: "UNSUPPORTED_MEDIA_TYPE",
                405: "METHOD_NOT_ALLOWED",
            }.get(exc.status_code, "INVALID_REQUEST")
            exc = IngestionError(code, status_code=exc.status_code)
        if not isinstance(exc, IngestionError):
            return super().handle_exception(exc)
        credential = getattr(self.request, "auth", None)
        record_rejection(
            exc, project_id=getattr(credential, "project_id", None), request_id=self.request_id
        )
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {}
        return Response(exc.result(), status=exc.status_code, headers=headers)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["X-Request-ID"] = self.request_id
        response["Cache-Control"] = "no-store"
        return response


class CaptureView(IngestionAPIView):
    def post(self, request):
        result, status_code = ingest_event(
            request.auth, request.data, request_id=self.request_id, received_at=self.received_at
        )
        return Response(result, status=status_code)


class BulkView(IngestionAPIView):
    def post(self, request):
        payloads = validate_batch(request.data)
        results = [
            ingest_event(
                request.auth, payload, request_id=self.request_id, received_at=self.received_at
            )[0]
            for payload in payloads
        ]
        accepted = sum(result["status"] == "accepted" for result in results)
        return Response(
            {"accepted": accepted, "rejected": len(results) - accepted, "results": results}
        )

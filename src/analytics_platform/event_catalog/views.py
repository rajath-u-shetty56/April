from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics_platform.catalog.models import Project
from analytics_platform.event_catalog.models import EventDefinition
from analytics_platform.event_catalog.serializers import EventDefinitionSerializer


class StaffAPIView(APIView):
    permission_classes = [IsAdminUser]


class ProjectEventDefinitionListCreateView(StaffAPIView):
    def get(self, request, project_id):
        project = get_object_or_404(Project, pk=project_id)
        queryset = project.event_definitions.all()
        definition_status = request.query_params.get("status")
        if definition_status:
            queryset = queryset.filter(status=definition_status)
        return Response(EventDefinitionSerializer(queryset, many=True).data)

    def post(self, request, project_id):
        project = get_object_or_404(Project, pk=project_id)
        serializer = EventDefinitionSerializer(
            data=request.data,
            context={"project": project},
        )
        serializer.is_valid(raise_exception=True)
        definition = serializer.save(project=project)
        return Response(
            EventDefinitionSerializer(definition).data,
            status=status.HTTP_201_CREATED,
        )


class ProjectEventDefinitionDetailView(StaffAPIView):
    def get(self, _request, project_id, definition_id):
        definition = get_object_or_404(
            EventDefinition,
            project_id=project_id,
            pk=definition_id,
        )
        return Response(EventDefinitionSerializer(definition).data)

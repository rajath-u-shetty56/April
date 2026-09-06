from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics_platform.catalog.models import Workspace
from analytics_platform.catalog.serializers import (
    ProjectSerializer,
    WorkspaceSerializer,
)


class StaffAPIView(APIView):
    permission_classes = [IsAdminUser]


class WorkspaceListCreateView(StaffAPIView):
    def get(self, _request):
        return Response(WorkspaceSerializer(Workspace.objects.all(), many=True).data)

    def post(self, request):
        serializer = WorkspaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workspace = serializer.save()
        return Response(WorkspaceSerializer(workspace).data, status=status.HTTP_201_CREATED)


class ProjectListCreateView(StaffAPIView):
    def get(self, _request, workspace_id):
        workspace = get_object_or_404(Workspace, pk=workspace_id)
        return Response(ProjectSerializer(workspace.projects.all(), many=True).data)

    def post(self, request, workspace_id):
        workspace = get_object_or_404(Workspace, pk=workspace_id)
        serializer = ProjectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.save(workspace=workspace)
        return Response(ProjectSerializer(project).data, status=status.HTTP_201_CREATED)

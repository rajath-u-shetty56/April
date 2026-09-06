from django.contrib import admin

from analytics_platform.catalog.models import Project, Workspace

admin.site.register(Workspace)
admin.site.register(Project)

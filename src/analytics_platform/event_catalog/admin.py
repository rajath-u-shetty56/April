from django.contrib import admin

from analytics_platform.event_catalog.models import EventDefinition

admin.site.register(EventDefinition)

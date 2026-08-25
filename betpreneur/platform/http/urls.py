"""Platform-level routes. Mounted at /api/ from config/urls.py."""
from django.urls import path

from .health import HealthCheckView

urlpatterns = [
    path("health/", HealthCheckView.as_view(), name="health-check"),
]

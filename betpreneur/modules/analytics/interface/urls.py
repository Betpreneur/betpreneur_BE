"""Public record, market health and ops routes.

Mounted at /api/algo/ from config/urls.py.
"""
from django.urls import path

from .views import (
    MaintenanceRunView,
    MarketHealthView,
    ModelHealthView,
    PublicDatasetExportView,
    PublicDatasetView,
    PublicRecordView,
    PublicSummaryView,
    TaskStatusView,
)

urlpatterns = [
    path("model-health/", ModelHealthView.as_view(), name="algo-model-health"),
    path("public/dataset/", PublicDatasetView.as_view(), name="algo-public-dataset"),
    path("public/dataset/export/", PublicDatasetExportView.as_view(),
         name="algo-public-dataset-export"),
    path("public/summary/", PublicSummaryView.as_view(), name="algo-public-summary"),
    path("public/record/", PublicRecordView.as_view(), name="algo-public-record"),
    path("markets/health/", MarketHealthView.as_view(), name="algo-market-health"),
    path("maintenance/run/", MaintenanceRunView.as_view(), name="algo-maintenance-run"),
    path("tasks/<str:task_id>/", TaskStatusView.as_view(), name="algo-task-status"),
]

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AlgoRunViewSet,
    BackPickView,
    DailyPicksDownloadView,
    DailyPicksView,
    PublicRecordView,
    PublicSummaryView,
    TaskStatusView,
    TopPickView,
)


router = DefaultRouter()
router.register("runs", AlgoRunViewSet, basename="algo-run")

urlpatterns = [
    path("public/summary/", PublicSummaryView.as_view(), name="algo-public-summary"),
    path("public/record/", PublicRecordView.as_view(), name="algo-public-record"),
    path("picks/", DailyPicksView.as_view(), name="algo-picks"),
    path("picks/download/", DailyPicksDownloadView.as_view(), name="algo-picks-download"),
    path("picks/<int:pick_id>/back/", BackPickView.as_view(), name="algo-pick-back"),
    path("top-pick/", TopPickView.as_view(), name="algo-top-pick"),
    path("tasks/<str:task_id>/", TaskStatusView.as_view(), name="algo-task-status"),
    path("", include(router.urls)),
]

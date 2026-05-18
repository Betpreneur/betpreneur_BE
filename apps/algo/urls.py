from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AlgoRunViewSet,
    PublicDailyPicksView,
    PublicRecordView,
    PublicSummaryView,
    PublicTopPickView,
    TaskStatusView,
)


router = DefaultRouter()
router.register("runs", AlgoRunViewSet, basename="algo-run")

urlpatterns = [
    path("public/summary/", PublicSummaryView.as_view(), name="algo-public-summary"),
    path("public/picks/", PublicDailyPicksView.as_view(), name="algo-public-picks"),
    path("public/top-pick/", PublicTopPickView.as_view(), name="algo-public-top-pick"),
    path("public/record/", PublicRecordView.as_view(), name="algo-public-record"),
    path("tasks/<str:task_id>/", TaskStatusView.as_view(), name="algo-task-status"),
    path("", include(router.urls)),
]

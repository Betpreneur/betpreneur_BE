from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AlgoRunViewSet


router = DefaultRouter()
router.register("runs", AlgoRunViewSet, basename="algo-run")

urlpatterns = [
    path("", include(router.urls)),
]

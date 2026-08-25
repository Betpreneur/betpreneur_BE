"""Daily picks, games and backing routes.

Mounted at /api/algo/ from config/urls.py. Every path and URL name is
unchanged from before the refactor — the public API is frozen.
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AlgoRunViewSet,
    BackedGamesView,
    BackGameView,
    DailyPicksDownloadView,
    DailyPicksView,
    GameDetailView,
    GamesView,
    PickDetailView,
    TopPickView,
)

router = DefaultRouter()
router.register("runs", AlgoRunViewSet, basename="algo-run")

urlpatterns = [
    path("picks/", DailyPicksView.as_view(), name="algo-picks"),
    path("picks/download/", DailyPicksDownloadView.as_view(), name="algo-picks-download"),
    path("picks/<int:pick_id>/", PickDetailView.as_view(), name="algo-pick-detail"),
    path("games/", GamesView.as_view(), name="algo-games"),
    path("games/backed/", BackedGamesView.as_view(), name="algo-backed-games"),
    path("games/<str:match_id>/backed/", BackGameView.as_view(), name="algo-game-backed"),
    path("games/<str:match_id>/", GameDetailView.as_view(), name="algo-game-detail"),
    path("top-picks/", TopPickView.as_view(), name="algo-top-picks"),
    path("top-pick/", TopPickView.as_view(), name="algo-top-pick"),
    *router.urls,
]

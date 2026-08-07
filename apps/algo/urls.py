from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AlgoRunViewSet,
    BackGameView,
    BackedGamesView,
    BetanoSlipImportView,
    DailyPicksDownloadView,
    DailyPicksView,
    FixtureSearchView,
    GameDetailView,
    GamesView,
    ManualSlipReviewView,
    MarketHealthView,
    PickDetailView,
    PublicRecordView,
    PublicSummaryView,
    SlipReviewDetailView,
    SlipReviewListView,
    SlipReviewOptionsView,
    SportyBetSlipImportView,
    StatPalFixtureContextView,
    StatPalFixtureRefreshView,
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
    path("picks/<int:pick_id>/", PickDetailView.as_view(), name="algo-pick-detail"),
    path("fixtures/search/", FixtureSearchView.as_view(), name="algo-fixture-search"),
    path("slip-reviews/", SlipReviewListView.as_view(), name="algo-slip-reviews"),
    path("slip-reviews/options/", SlipReviewOptionsView.as_view(), name="algo-slip-review-options"),
    path("slip-reviews/manual/", ManualSlipReviewView.as_view(), name="algo-manual-slip-review"),
    path("slip-reviews/sportybet/", SportyBetSlipImportView.as_view(), name="algo-sportybet-slip-import"),
    path("slip-reviews/betano/", BetanoSlipImportView.as_view(), name="algo-betano-slip-import"),
    path("slip-reviews/<int:review_id>/", SlipReviewDetailView.as_view(), name="algo-slip-review-detail"),
    path("statpal/fixtures/context/", StatPalFixtureContextView.as_view(), name="algo-statpal-fixture-context"),
    path("statpal/fixtures/refresh/", StatPalFixtureRefreshView.as_view(), name="algo-statpal-fixture-refresh"),
    path("games/", GamesView.as_view(), name="algo-games"),
    path("games/backed/", BackedGamesView.as_view(), name="algo-backed-games"),
    path("games/<str:match_id>/backed/", BackGameView.as_view(), name="algo-game-backed"),
    path("games/<str:match_id>/", GameDetailView.as_view(), name="algo-game-detail"),
    path("markets/health/", MarketHealthView.as_view(), name="algo-market-health"),
    path("top-picks/", TopPickView.as_view(), name="algo-top-picks"),
    path("top-pick/", TopPickView.as_view(), name="algo-top-pick"),
    path("tasks/<str:task_id>/", TaskStatusView.as_view(), name="algo-task-status"),
    path("", include(router.urls)),
]

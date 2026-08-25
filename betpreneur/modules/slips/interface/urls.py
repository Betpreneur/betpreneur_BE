"""Slip review routes.

Mounted at /api/algo/ from config/urls.py. Every path and URL name is
unchanged from before the refactor — the public API is frozen.
"""
from django.urls import path

from .views import (
    BetanoSlipImportView,
    ManualSlipReviewView,
    SlipRepairView,
    SlipReviewDetailView,
    SlipReviewEventsView,
    SlipReviewListView,
    SlipReviewOptionsView,
    SlipReviewRandomizeView,
    SlipReviewRecapView,
    SlipReviewStreamTokenView,
    SportyBetSlipImportView,
)

urlpatterns = [
    path("slip-reviews/", SlipReviewListView.as_view(), name="algo-slip-reviews"),
    path("slip-reviews/options/", SlipReviewOptionsView.as_view(), name="algo-slip-review-options"),
    path("slip-reviews/recap/", SlipReviewRecapView.as_view(), name="algo-slip-review-recap"),
    path("slip-reviews/manual/", ManualSlipReviewView.as_view(), name="algo-manual-slip-review"),
    path("slip-reviews/sportybet/", SportyBetSlipImportView.as_view(), name="algo-sportybet-slip-import"),
    path("slip-reviews/betano/", BetanoSlipImportView.as_view(), name="algo-betano-slip-import"),
    path("slip-reviews/<int:review_id>/stream-token/", SlipReviewStreamTokenView.as_view(), name="algo-slip-review-stream-token"),
    path("slip-reviews/<int:review_id>/events/", SlipReviewEventsView.as_view(), name="algo-slip-review-events"),
    path("slip-reviews/<int:review_id>/randomize/", SlipReviewRandomizeView.as_view(), name="algo-slip-review-randomize"),
    path("slip-reviews/<int:review_id>/", SlipReviewDetailView.as_view(), name="algo-slip-review-detail"),
    path("slip-reviews/<int:review_id>/repair/", SlipRepairView.as_view(), name="algo-slip-review-repair"),
]

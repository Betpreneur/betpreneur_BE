"""Fixture search and provider-context routes.

Mounted at /api/algo/ from config/urls.py. Every path and URL name is
unchanged from before the refactor — the public API is frozen.
"""
from django.urls import path

from .views import (
    FixtureSearchView,
    StatPalFixtureContextView,
    StatPalFixtureRefreshView,
    StatPalReadinessView,
)

urlpatterns = [
    path("fixtures/search/", FixtureSearchView.as_view(), name="algo-fixture-search"),
    path("statpal/fixtures/context/", StatPalFixtureContextView.as_view(), name="algo-statpal-fixture-context"),
    path("statpal/fixtures/refresh/", StatPalFixtureRefreshView.as_view(), name="algo-statpal-fixture-refresh"),
    path("statpal/readiness/", StatPalReadinessView.as_view(), name="algo-statpal-readiness"),
]

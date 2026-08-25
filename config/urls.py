from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    # API
    path("api/", include("betpreneur.platform.http.urls")),
    path("api/auth/", include("betpreneur.modules.identity.interface.urls")),
    path("api/algo/tokens/", include("betpreneur.modules.billing.interface.urls")),
    # Four modules share the /api/algo/ prefix; Django tries each include in
    # order. The prefix is inherited from before the refactor and kept because
    # the public API is frozen.
    path("api/algo/", include("betpreneur.modules.analytics.interface.urls")),
    path("api/algo/", include("betpreneur.modules.slips.interface.urls")),
    path("api/algo/", include("betpreneur.modules.picks.interface.urls")),
    path("api/algo/", include("betpreneur.modules.catalog.interface.urls")),
    # Swagger
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
]

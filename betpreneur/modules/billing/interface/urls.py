"""Token routes.

Mounted at /api/algo/tokens/ from config/urls.py. The prefix is inherited from
before the refactor and kept deliberately — the public API is frozen, so the
paths and the URL names both stay exactly as clients and reverse() see them.
"""
from django.urls import path

from .views import (
    PayfonteWebhookView,
    TokenAdminAdjustmentView,
    TokenPackageListView,
    TokenPurchaseAdminCompleteView,
    TokenPurchaseAdminFailView,
    TokenPurchaseVerifyView,
    TokenPurchaseView,
    TokenWalletView,
)

urlpatterns = [
    path("", TokenWalletView.as_view(), name="algo-token-wallet"),
    path("packages/", TokenPackageListView.as_view(), name="algo-token-packages"),
    path("purchases/", TokenPurchaseView.as_view(), name="algo-token-purchases"),
    path("payfonte/webhook/", PayfonteWebhookView.as_view(), name="algo-token-payfonte-webhook"),
    path(
        "purchases/<int:purchase_id>/admin-complete/",
        TokenPurchaseAdminCompleteView.as_view(),
        name="algo-token-purchase-admin-complete",
    ),
    path(
        "purchases/<int:purchase_id>/verify/",
        TokenPurchaseVerifyView.as_view(),
        name="algo-token-purchase-verify",
    ),
    path(
        "purchases/<int:purchase_id>/admin-fail/",
        TokenPurchaseAdminFailView.as_view(),
        name="algo-token-purchase-admin-fail",
    ),
    path("admin/adjust/", TokenAdminAdjustmentView.as_view(), name="algo-token-admin-adjust"),
]

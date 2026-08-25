"""Admin for wallets, ledger, reservations and purchases."""
from django.contrib import admin

from betpreneur.modules.billing.models import (
    TokenPurchase,
    TokenReservation,
    TokenTransaction,
    TokenWallet,
)


@admin.register(TokenWallet)
class TokenWalletAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "free_tokens", "paid_tokens", "total_tokens_display", "last_free_refill_date", "updated_at")
    list_filter = ("last_free_refill_date", "created_at", "updated_at")
    search_fields = ("user__email", "user__username")
    readonly_fields = ("created_at", "updated_at", "total_tokens_display")

    @admin.display(description="Total tokens")
    def total_tokens_display(self, obj):
        return obj.total_tokens


@admin.register(TokenTransaction)
class TokenTransactionAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = (
        "id",
        "user",
        "amount",
        "free_tokens_delta",
        "paid_tokens_delta",
        "token_bucket",
        "reason",
        "reference_type",
        "reference_id",
        "created_at",
    )
    list_filter = ("token_bucket", "reason", "reference_type", "created_at")
    search_fields = ("user__email", "user__username", "reference_id")
    readonly_fields = ("created_at",)


@admin.register(TokenReservation)
class TokenReservationAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = (
        "id",
        "user",
        "amount",
        "free_tokens_reserved",
        "paid_tokens_reserved",
        "status",
        "reference_type",
        "reference_id",
        "expires_at",
        "created_at",
    )
    list_filter = ("status", "reference_type", "created_at", "expires_at")
    search_fields = ("user__email", "user__username", "reference_id")
    readonly_fields = ("created_at", "updated_at", "consumed_at", "released_at")


@admin.register(TokenPurchase)
class TokenPurchaseAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = (
        "id",
        "user",
        "package_id",
        "tokens",
        "amount",
        "currency",
        "status",
        "provider",
        "provider_reference",
        "paid_at",
        "created_at",
    )
    list_filter = ("status", "currency", "provider", "created_at", "paid_at")
    search_fields = ("user__email", "user__username", "package_id", "provider_reference")
    readonly_fields = ("created_at", "updated_at", "paid_at", "failed_at", "credited_transaction")

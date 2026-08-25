"""Admin for the settlement attempt log."""
from django.contrib import admin

from betpreneur.modules.settlement.models import SettlementRun


@admin.register(SettlementRun)
class SettlementRunAdmin(admin.ModelAdmin):
    list_display = ("target_date", "scope", "status", "started_at", "finished_at")
    list_filter = ("scope", "status", "target_date")
    search_fields = ("target_date",)
    readonly_fields = (
        "target_date", "scope", "status", "summary", "error", "started_at", "finished_at",
    )
    ordering = ("-started_at",)

    def has_add_permission(self, request):
        return False

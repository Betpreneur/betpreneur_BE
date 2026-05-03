from django.contrib import admin

from .models import AlgoRun, Pick


class PickInline(admin.TabularInline):
    model = Pick
    extra = 0


@admin.register(AlgoRun)
class AlgoRunAdmin(admin.ModelAdmin):
    list_display = (
        "target_date",
        "status",
        "total_scored",
        "picks_count",
        "bankers",
        "value_gems",
        "wild_cards",
        "created_at",
    )
    list_filter = ("status", "target_date")
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")
    inlines = [PickInline]


@admin.register(Pick)
class PickAdmin(admin.ModelAdmin):
    list_display = ("fixture", "tier", "market", "confidence", "odds", "ev", "source")
    list_filter = ("tier", "source")

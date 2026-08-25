"""Admin for slip reviews."""
import json

from django.contrib import admin
from django.utils.html import format_html

from betpreneur.modules.slips.models import SlipReview, SlipSelection


class SlipSelectionInline(admin.TabularInline):
    model = SlipSelection
    extra = 0
    can_delete = False
    fields = (
        "order",
        "submitted_match",
        "submitted_market",
        "fixture",
        "league",
        "status",
        "verdict",
        "message",
    )
    readonly_fields = fields
    show_change_link = True


@admin.register(SlipReview)
class SlipReviewAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = (
        "id",
        "user",
        "source",
        "status",
        "selection_count",
        "statpal_api_calls",
        "keep_count",
        "replace_count",
        "remove_count",
        "created_at",
    )
    list_filter = ("source", "status", "created_at")
    search_fields = ("user__email", "user__username", "title")
    readonly_fields = ("created_at", "updated_at", "api_usage_summary")
    inlines = [SlipSelectionInline]

    @admin.display(description="Selections")
    def selection_count(self, obj):
        return (obj.summary or {}).get("count", 0)

    def _api_usage(self, obj):
        summary = obj.summary or {}
        return summary.get("api_usage") or ((summary.get("intelligence") or {}).get("api_usage") or {})

    @admin.display(description="StatPal calls")
    def statpal_api_calls(self, obj):
        usage = self._api_usage(obj)
        attempted = int(usage.get("attempted_calls") or 0)
        skipped = int(usage.get("skipped_by_cache") or 0)
        failed = int(usage.get("failed_calls") or 0)
        if failed:
            return f"{attempted} calls, {failed} failed, {skipped} cache hits"
        return f"{attempted} calls, {skipped} cache hits"

    @admin.display(description="API usage")
    def api_usage_summary(self, obj):
        usage = self._api_usage(obj)
        if not usage:
            return "No API usage recorded."
        return format_html(
            "<pre>{}</pre>",
            json.dumps(usage, indent=2, default=str),
        )

    @admin.display(description="Keep")
    def keep_count(self, obj):
        return (obj.summary or {}).get("keep_count", 0)

    @admin.display(description="Replace")
    def replace_count(self, obj):
        return (obj.summary or {}).get("replace_count", 0)

    @admin.display(description="Remove")
    def remove_count(self, obj):
        return (obj.summary or {}).get("remove_count", 0)


@admin.register(SlipSelection)
class SlipSelectionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "review",
        "submitted_match",
        "submitted_market",
        "fixture",
        "status",
        "verdict",
        "created_at",
    )
    list_filter = ("status", "verdict", "league", "country")
    search_fields = (
        "submitted_match",
        "submitted_market",
        "fixture",
        "home_team",
        "away_team",
        "match_id",
        "league",
        "country",
    )
    readonly_fields = ("created_at",)

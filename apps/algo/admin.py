from django.contrib import admin
from django.contrib import messages
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone

from .models import AlgoRun, MarketPrediction, Pick, PickBack, StrategyReview
from .performance import performance_dashboard
from .tasks import generate_daily_picks, run_monthly_auditor, settle_daily_results


class PickInline(admin.TabularInline):
    model = Pick
    extra = 0
    can_delete = False
    fields = (
        "match_date",
        "fixture",
        "league",
        "kickoff",
        "tier",
        "market",
        "confidence",
        "odds",
        "ev",
        "stake",
        "status",
        "score",
        "pnl",
        "settled_at",
    )
    readonly_fields = (
        "match_date",
        "fixture",
        "league",
        "kickoff",
        "tier",
        "market",
        "confidence",
        "odds",
        "ev",
        "stake",
        "settled_at",
    )


class MarketPredictionInline(admin.TabularInline):
    model = MarketPrediction
    extra = 0
    can_delete = False
    fields = (
        "match_date",
        "fixture",
        "market",
        "confidence",
        "odds",
        "ev",
        "eligible",
        "published",
        "status",
        "score",
        "pnl_simulated",
    )
    readonly_fields = fields
    show_change_link = True


@admin.action(description="Queue pick generation for selected run dates")
def queue_pick_generation(modeladmin, request, queryset):
    task_ids = []
    for algo_run in queryset:
        task = generate_daily_picks.delay(algo_run.target_date.isoformat())
        task_ids.append(task.id)
    messages.success(
        request,
        f"Queued {len(task_ids)} pick generation task(s): {', '.join(task_ids)}",
    )


@admin.action(description="Queue result settlement for selected run dates")
def queue_result_settlement(modeladmin, request, queryset):
    task_ids = []
    for target_date in queryset.values_list("target_date", flat=True).distinct():
        task = settle_daily_results.delay(target_date.isoformat())
        task_ids.append(task.id)
    messages.success(
        request,
        f"Queued {len(task_ids)} settlement task(s): {', '.join(task_ids)}",
    )


@admin.action(description="Queue monthly auditor ending on selected run dates")
def queue_auditor(modeladmin, request, queryset):
    task_ids = []
    for algo_run in queryset:
        task = run_monthly_auditor.delay(None, algo_run.target_date.isoformat())
        task_ids.append(task.id)
    messages.success(
        request,
        f"Queued {len(task_ids)} auditor task(s): {', '.join(task_ids)}",
    )


@admin.register(AlgoRun)
class AlgoRunAdmin(admin.ModelAdmin):
    change_list_template = "admin/algo/algorun/change_list.html"
    date_hierarchy = "target_date"
    list_display = (
        "id",
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
    search_fields = ("error",)
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")
    actions = (queue_pick_generation, queue_result_settlement, queue_auditor)
    fieldsets = (
        (
            "Daily Run",
            {
                "fields": (
                    "target_date",
                    "status",
                    "triggered_by",
                    "started_at",
                    "finished_at",
                )
            },
        ),
        (
            "Counts",
            {
                "fields": (
                    "fd_fixtures",
                    "aps_fixtures",
                    "total_scored",
                    "picks_count",
                    "bankers",
                    "value_gems",
                    "wild_cards",
                    "bankroll",
                )
            },
        ),
        ("Result Payload", {"fields": ("result", "error"), "classes": ("collapse",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    inlines = [PickInline, MarketPredictionInline]

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "performance/",
                self.admin_site.admin_view(self.performance_view),
                name="algo_algorun_performance",
            ),
        ]
        return custom_urls + urls

    def performance_view(self, request):
        try:
            days = int(request.GET.get("days", 90))
        except (TypeError, ValueError):
            days = 90
        days = max(1, min(days, 365))
        context = {
            **self.admin_site.each_context(request),
            "title": "Algo Performance",
            "dashboard": performance_dashboard(days=days),
            "days": days,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/algo/algorun/performance.html", context)


@admin.register(Pick)
class PickAdmin(admin.ModelAdmin):
    date_hierarchy = "match_date"
    list_display = (
        "id",
        "match_date",
        "fixture",
        "league",
        "kickoff",
        "tier",
        "market",
        "confidence",
        "odds",
        "ev",
        "status",
        "score",
        "pnl",
        "source",
    )
    list_filter = ("tier", "status", "source", "match_date")
    search_fields = ("fixture", "league", "market")
    list_editable = ("status", "score", "pnl")
    readonly_fields = ("created_at", "settled_at")
    fieldsets = (
        (
            "Match",
            {
                "fields": (
                    "run",
                    "match_date",
                    "fixture",
                    "league",
                    "kickoff",
                    "match_id",
                    "source",
                )
            },
        ),
        (
            "Pick",
            {
                "fields": (
                    "tier",
                    "market",
                    "meaning",
                    "reasoning",
                    "risk_flags",
                    "insights",
                    "confidence",
                    "odds",
                    "ev",
                    "stake",
                )
            },
        ),
        (
            "Settlement",
            {
                "fields": (
                    "status",
                    "score",
                    "result",
                    "pnl",
                    "settled_at",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at",), "classes": ("collapse",)}),
    )

    @admin.action(description="Queue API-Football settlement for selected pick dates")
    def queue_settlement_for_pick_dates(self, request, queryset):
        task_ids = []
        dates = queryset.exclude(match_date__isnull=True).values_list("match_date", flat=True).distinct()
        for match_date in dates:
            task = settle_daily_results.delay(match_date.isoformat())
            task_ids.append(task.id)
        messages.success(
            request,
            f"Queued {len(task_ids)} settlement task(s): {', '.join(task_ids)}",
        )

    @admin.action(description="Mark selected picks as void")
    def mark_void(self, request, queryset):
        updated = queryset.update(status=Pick.Status.VOID, pnl=0, settled_at=timezone.now())
        messages.success(request, f"Marked {updated} pick(s) as void.")

    actions = ("queue_settlement_for_pick_dates", "mark_void")


@admin.register(MarketPrediction)
class MarketPredictionAdmin(admin.ModelAdmin):
    date_hierarchy = "match_date"
    list_display = (
        "id",
        "match_date",
        "fixture",
        "league",
        "market",
        "confidence",
        "odds",
        "ev",
        "eligible",
        "published",
        "market_state",
        "status",
        "score",
        "pnl_simulated",
    )
    list_filter = (
        "published",
        "eligible",
        "status",
        "odds_source",
        "match_date",
        "league",
        "market",
    )
    search_fields = (
        "fixture",
        "home_team",
        "away_team",
        "league",
        "market",
        "match_id",
        "rejection_reason",
    )
    list_editable = ("status", "score", "pnl_simulated")
    readonly_fields = (
        "run",
        "selected_pick",
        "created_at",
        "settled_at",
    )
    fieldsets = (
        (
            "Match",
            {
                "fields": (
                    "run",
                    "selected_pick",
                    "match_date",
                    "fixture",
                    "home_team",
                    "away_team",
                    "league",
                    "kickoff",
                    "match_id",
                )
            },
        ),
        (
            "Prediction",
            {
                "fields": (
                    "market",
                    "meaning",
                    "raw_confidence",
                    "confidence",
                    "odds",
                    "ev",
                    "odds_source",
                    "odds_meta",
                    "eligible",
                    "published",
                    "rejection_reason",
                    "risk_flags",
                    "insights",
                )
            },
        ),
        (
            "Context",
            {
                "fields": (
                    "home_recent_form",
                    "away_recent_form",
                    "fixture_context",
                    "team_news",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Settlement",
            {
                "fields": (
                    "status",
                    "score",
                    "result",
                    "pnl_simulated",
                    "settled_at",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at",), "classes": ("collapse",)}),
    )

    @admin.display(description="State")
    def market_state(self, obj):
        flags = set(obj.risk_flags or [])
        if "market_suppressed" in flags or "market_loss_streak" in flags or "market_recent_losses" in flags:
            return "suppressed"
        if "market_cooling" in flags or "market_recent_low_hit_rate" in flags:
            return "cooling"
        if "market_recovered" in flags:
            return "recovered"
        return "active"

    @admin.action(description="Queue settlement for selected prediction dates")
    def queue_settlement_for_prediction_dates(self, request, queryset):
        task_ids = []
        dates = queryset.values_list("match_date", flat=True).distinct()
        for match_date in dates:
            task = settle_daily_results.delay(match_date.isoformat())
            task_ids.append(task.id)
        messages.success(
            request,
            f"Queued {len(task_ids)} settlement task(s): {', '.join(task_ids)}",
        )

    @admin.action(description="Mark selected internal predictions as void")
    def mark_void(self, request, queryset):
        updated = queryset.update(status=MarketPrediction.Status.VOID, pnl_simulated=0, settled_at=timezone.now())
        messages.success(request, f"Marked {updated} internal prediction(s) as void.")

    actions = ("queue_settlement_for_prediction_dates", "mark_void")


@admin.register(PickBack)
class PickBackAdmin(admin.ModelAdmin):
    list_display = ("id", "pick", "user", "created_at")
    list_filter = ("created_at",)
    search_fields = ("pick__fixture", "pick__market", "user__username", "user__email")
    readonly_fields = ("created_at",)


@admin.register(StrategyReview)
class StrategyReviewAdmin(admin.ModelAdmin):
    date_hierarchy = "target_date"
    list_display = (
        "id",
        "target_date",
        "daily_policy",
        "suppressed_count",
        "cooling_count",
        "promoted_count",
        "league_warning_count",
        "updated_at",
    )
    list_filter = ("daily_policy", "target_date")
    search_fields = ("reason",)
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "Review",
            {
                "fields": (
                    "target_date",
                    "daily_policy",
                    "reason",
                )
            },
        ),
        (
            "Actions",
            {
                "fields": (
                    "markets_suppressed",
                    "markets_cooling",
                    "markets_promoted",
                    "league_market_actions",
                    "league_warnings",
                )
            },
        ),
        ("Profile Payload", {"fields": ("profile",), "classes": ("collapse",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Suppressed")
    def suppressed_count(self, obj):
        return len(obj.markets_suppressed or [])

    @admin.display(description="Cooling")
    def cooling_count(self, obj):
        return len(obj.markets_cooling or [])

    @admin.display(description="Promoted")
    def promoted_count(self, obj):
        return len(obj.markets_promoted or [])

    @admin.display(description="League warnings")
    def league_warning_count(self, obj):
        return len(obj.league_warnings or [])

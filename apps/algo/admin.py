from django.contrib import admin
from django.contrib import messages
from django.utils import timezone

from .models import AlgoRun, Pick, PickBack
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
    inlines = [PickInline]


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


@admin.register(PickBack)
class PickBackAdmin(admin.ModelAdmin):
    list_display = ("id", "pick", "user", "created_at")
    list_filter = ("created_at",)
    search_fields = ("pick__fixture", "pick__market", "user__username", "user__email")
    readonly_fields = ("created_at",)

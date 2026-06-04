from django.contrib import admin
from django.contrib import messages
from django.db.models import Avg, Count, Q
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils import timezone

from .models import AlgoFixture, AlgoRun, GameBack, MarketPrediction, Pick, PickBack, StrategyReview
from .performance import performance_dashboard
from .tasks import generate_daily_picks, recover_daily_run, run_monthly_auditor, settle_daily_results


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


class AlgoFixtureInline(admin.TabularInline):
    model = AlgoFixture
    extra = 0
    can_delete = False
    fields = (
        "match_date",
        "fixture",
        "country",
        "league",
        "kickoff",
        "match_id",
        "market_count",
        "markets_70_plus",
        "markets_65_plus",
        "status",
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


@admin.action(description="Recover/publish selected runs from stored fixture scores")
def queue_run_recovery(modeladmin, request, queryset):
    task_ids = []
    for algo_run in queryset:
        task = recover_daily_run.delay(algo_run.id, False)
        task_ids.append(task.id)
    messages.success(
        request,
        f"Queued {len(task_ids)} recovery task(s): {', '.join(task_ids)}",
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
        "data_center_link",
        "created_at",
    )
    list_filter = ("status", "target_date")
    search_fields = ("error",)
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")
    actions = (queue_pick_generation, queue_result_settlement, queue_auditor, queue_run_recovery)
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
    inlines = [PickInline, AlgoFixtureInline, MarketPredictionInline]

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "performance/",
                self.admin_site.admin_view(self.performance_view),
                name="algo_algorun_performance",
            ),
            path(
                "<path:object_id>/data-center/",
                self.admin_site.admin_view(self.data_center_view),
                name="algo_algorun_data_center",
            ),
        ]
        return custom_urls + urls

    @admin.display(description="Data Center")
    def data_center_link(self, obj):
        url = reverse("admin:algo_algorun_data_center", args=[obj.pk])
        return format_html('<a class="button" href="{}">Open</a>', url)

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

    def _rate(self, wins, losses):
        settled = wins + losses
        return round((wins / settled) * 100, 1) if settled else 0.0

    def _status_summary(self, queryset, pnl_field=None):
        aggregate = queryset.aggregate(
            total=Count("id"),
            wins=Count("id", filter=Q(status=MarketPrediction.Status.WIN)),
            losses=Count("id", filter=Q(status=MarketPrediction.Status.LOSS)),
            voids=Count("id", filter=Q(status=MarketPrediction.Status.VOID)),
            pending=Count("id", filter=Q(status=MarketPrediction.Status.PENDING)),
        )
        wins = aggregate["wins"] or 0
        losses = aggregate["losses"] or 0
        summary = {
            "total": aggregate["total"] or 0,
            "wins": wins,
            "losses": losses,
            "voids": aggregate["voids"] or 0,
            "pending": aggregate["pending"] or 0,
            "settled": wins + losses,
            "accuracy": self._rate(wins, losses),
        }
        if pnl_field:
            summary["pnl"] = queryset.aggregate(total=Avg(pnl_field)).get("total")
        return summary

    def _market_rows(self, predictions):
        rows = []
        aggregates = (
            predictions.values("market")
            .annotate(
                total=Count("id"),
                wins=Count("id", filter=Q(status=MarketPrediction.Status.WIN)),
                losses=Count("id", filter=Q(status=MarketPrediction.Status.LOSS)),
                voids=Count("id", filter=Q(status=MarketPrediction.Status.VOID)),
                pending=Count("id", filter=Q(status=MarketPrediction.Status.PENDING)),
                published_count=Count("id", filter=Q(published=True)),
                eligible_count=Count("id", filter=Q(eligible=True)),
                avg_confidence=Avg("confidence"),
                avg_ev=Avg("ev"),
            )
            .order_by("market")
        )
        for item in aggregates:
            wins = item["wins"] or 0
            losses = item["losses"] or 0
            total = item["total"] or 0
            rows.append({
                "market": item["market"],
                "total": total,
                "wins": wins,
                "losses": losses,
                "settled": wins + losses,
                "voids": item["voids"] or 0,
                "pending": item["pending"] or 0,
                "published_count": item["published_count"] or 0,
                "eligible_count": item["eligible_count"] or 0,
                "accuracy": self._rate(wins, losses),
                "avg_confidence": round(float(item["avg_confidence"] or 0), 1),
                "avg_ev": round(float(item["avg_ev"] or 0), 3),
            })
        return sorted(rows, key=lambda row: (row["settled"], row["accuracy"]), reverse=True)

    def _prediction_top_rank(self, prediction):
        from .views import _market_display_score

        payload = {
            "market": prediction.market,
            "confidence": prediction.confidence,
            "odds": float(prediction.odds or 0),
            "ev": float(prediction.ev) if prediction.ev is not None else None,
            "eligible": prediction.eligible,
            "risk_flags": prediction.risk_flags or [],
            "insights": prediction.insights or {},
        }
        return (
            1 if prediction.published else 0,
            1 if prediction.eligible else 0,
            *_market_display_score(payload),
        )

    def _top_predictions(self, predictions):
        top_by_game = {}
        for prediction in predictions:
            key = str(prediction.match_id or "").strip() or prediction.fixture
            current = top_by_game.get(key)
            if current is None or self._prediction_top_rank(prediction) > self._prediction_top_rank(current):
                top_by_game[key] = prediction
        return list(top_by_game.values())

    def _fixture_rows(self, algo_run, top_predictions):
        fixtures = {
            str(fixture.match_id or ""): fixture
            for fixture in AlgoFixture.objects.filter(run=algo_run).order_by("country", "league", "kickoff", "fixture")
        }
        rows = []
        for prediction in top_predictions:
            key = str(prediction.match_id or "")
            fixture = fixtures.get(key)
            rows.append({
                "match_id": key,
                "fixture": fixture.fixture if fixture else prediction.fixture,
                "country": fixture.country if fixture else "",
                "league": fixture.league if fixture else prediction.league,
                "kickoff": fixture.kickoff if fixture else prediction.kickoff,
                "score": prediction.score or "",
                "top_market": prediction,
            })
        return sorted(rows, key=lambda row: (row["country"], row["league"], row["kickoff"], row["fixture"]))

    def data_center_view(self, request, object_id):
        algo_run = self.get_object(request, object_id)
        if algo_run is None:
            from django.http import Http404

            raise Http404("Algo run not found")

        predictions = MarketPrediction.objects.filter(run=algo_run).select_related("selected_pick")
        all_prediction_count = predictions.count()
        top_prediction_list = self._top_predictions(list(predictions))
        top_prediction_ids = [prediction.id for prediction in top_prediction_list]
        top_predictions = MarketPrediction.objects.filter(id__in=top_prediction_ids).select_related("selected_pick")
        published_predictions = predictions.filter(published=True)
        picks = Pick.objects.filter(run=algo_run)
        market_rows = self._market_rows(top_predictions)
        fixture_rows = self._fixture_rows(algo_run, top_prediction_list)
        settled_market_rows = [row for row in market_rows if row["wins"] + row["losses"] > 0]
        best_market = max(settled_market_rows, key=lambda row: (row["accuracy"], row["wins"] + row["losses"], row["wins"]), default=None)
        worst_market = min(settled_market_rows, key=lambda row: (row["accuracy"], -(row["wins"] + row["losses"])), default=None)

        high_value_upsets = list(
            top_predictions.filter(
                status=MarketPrediction.Status.LOSS,
                confidence__gte=70,
            )
            .order_by("-confidence", "-ev")[:25]
        )
        hidden_wins = list(
            top_predictions.filter(
                status=MarketPrediction.Status.WIN,
                published=False,
                confidence__gte=70,
            )
            .order_by("-confidence", "-ev")[:25]
        )

        context = {
            **self.admin_site.each_context(request),
            "title": f"Daily Data Center - {algo_run.target_date}",
            "opts": self.model._meta,
            "algo_run": algo_run,
            "summary": self._status_summary(top_predictions),
            "published_summary": self._status_summary(published_predictions),
            "pick_summary": {
                "total": picks.count(),
                "wins": picks.filter(status=Pick.Status.WIN).count(),
                "losses": picks.filter(status=Pick.Status.LOSS).count(),
                "voids": picks.filter(status=Pick.Status.VOID).count(),
                "pending": picks.filter(status=Pick.Status.PENDING).count(),
            },
            "fixture_count": AlgoFixture.objects.filter(run=algo_run).count(),
            "all_prediction_count": all_prediction_count,
            "market_rows": market_rows,
            "fixture_rows": fixture_rows,
            "best_market": best_market,
            "worst_market": worst_market,
            "high_value_upsets": high_value_upsets,
            "hidden_wins": hidden_wins,
        }
        context["pick_summary"]["settled"] = context["pick_summary"]["wins"] + context["pick_summary"]["losses"]
        context["pick_summary"]["accuracy"] = self._rate(
            context["pick_summary"]["wins"],
            context["pick_summary"]["losses"],
        )
        return TemplateResponse(request, "admin/algo/algorun/data_center.html", context)


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


@admin.register(AlgoFixture)
class AlgoFixtureAdmin(admin.ModelAdmin):
    date_hierarchy = "match_date"
    list_display = (
        "id",
        "match_date",
        "fixture",
        "country",
        "league",
        "kickoff",
        "market_count",
        "markets_70_plus",
        "markets_65_plus",
        "status",
    )
    list_filter = ("status", "match_date", "country", "league")
    search_fields = ("fixture", "home_team", "away_team", "league", "country", "match_id")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "Fixture",
            {
                "fields": (
                    "run",
                    "match_date",
                    "fixture",
                    "home_team",
                    "away_team",
                    "home_logo",
                    "away_logo",
                    "league",
                    "league_logo",
                    "country",
                    "country_flag",
                    "round",
                    "league_type",
                    "kickoff",
                    "match_id",
                )
            },
        ),
        (
            "Scoring",
            {
                "fields": (
                    "market_count",
                    "markets_70_plus",
                    "markets_65_plus",
                    "status",
                    "error",
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
                    "corner_profile",
                    "insights",
                    "source_payload",
                ),
                "classes": ("collapse",),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )


@admin.register(PickBack)
class PickBackAdmin(admin.ModelAdmin):
    list_display = ("id", "pick", "user", "created_at")
    list_filter = ("created_at",)
    search_fields = ("pick__fixture", "pick__market", "user__username", "user__email")
    readonly_fields = ("created_at",)


@admin.register(GameBack)
class GameBackAdmin(admin.ModelAdmin):
    list_display = ("id", "match_id", "match_date", "fixture", "user", "created_at")
    list_filter = ("match_date", "created_at")
    search_fields = ("match_id", "fixture__fixture", "user__username", "user__email")
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

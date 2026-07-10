from django.contrib import admin
from django.contrib import messages
from django.db.models import Avg, Count, Q
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils import timezone

from .models import (
    AlgoFixture,
    AlgoRun,
    FixtureCache,
    GameBack,
    MarketPrediction,
    Pick,
    PickBack,
    SlipReview,
    SlipSelection,
    StrategyReview,
)
from .council import council_review
from .performance import performance_dashboard
from .recommendation_policy import assess_recommendation
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


@admin.register(FixtureCache)
class FixtureCacheAdmin(admin.ModelAdmin):
    date_hierarchy = "match_date"
    list_display = (
        "match_date",
        "fixture",
        "country",
        "league",
        "kickoff",
        "match_id",
        "updated_at",
    )
    list_filter = ("match_date", "country", "league")
    search_fields = (
        "fixture",
        "home_team",
        "away_team",
        "fixture_normalized",
        "match_id",
        "league",
        "country",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(SlipReview)
class SlipReviewAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = (
        "id",
        "user",
        "source",
        "status",
        "selection_count",
        "keep_count",
        "replace_count",
        "remove_count",
        "created_at",
    )
    list_filter = ("source", "status", "created_at")
    search_fields = ("user__email", "user__username", "title")
    readonly_fields = ("created_at", "updated_at")
    inlines = [SlipSelectionInline]

    @admin.display(description="Selections")
    def selection_count(self, obj):
        return (obj.summary or {}).get("count", 0)

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

    def _trust_status(self, prediction, key):
        return ((prediction.insights or {}).get(key) or {}).get("status") or "unknown"

    def _recommendation_assessment(self, prediction):
        return assess_recommendation({
            "confidence": prediction.confidence,
            "ev": prediction.ev,
            "odds_meta": prediction.odds_meta or {},
            "odds_source": prediction.odds_source,
            "league": prediction.league,
            "country": (prediction.fixture_context or {}).get("country", ""),
            "risk_flags": prediction.risk_flags or [],
            "eligible": prediction.eligible,
            "insights": prediction.insights or {},
        })

    def _council_review(self, prediction):
        existing = ((prediction.insights or {}).get("council_review") or {})
        if existing:
            return existing
        return council_review({
            "confidence": prediction.confidence,
            "ev": prediction.ev,
            "odds_meta": prediction.odds_meta or {},
            "odds_source": prediction.odds_source,
            "league": prediction.league,
            "country": (prediction.fixture_context or {}).get("country", ""),
            "risk_flags": prediction.risk_flags or [],
            "eligible": prediction.eligible,
            "insights": prediction.insights or {},
        })

    def _council_status_rows(self, predictions):
        grouped = {}
        for prediction in predictions:
            review = self._council_review(prediction)
            decision = review.get("decision") or "unknown"
            row = grouped.setdefault(decision, {
                "decision": decision,
                "total": 0,
                "published": 0,
                "wins": 0,
                "losses": 0,
                "avg_final_confidence": 0,
                "avg_consensus": 0,
                "avg_disagreement": 0,
            })
            row["total"] += 1
            row["published"] += 1 if prediction.published else 0
            row["wins"] += 1 if prediction.status == MarketPrediction.Status.WIN else 0
            row["losses"] += 1 if prediction.status == MarketPrediction.Status.LOSS else 0
            row["avg_final_confidence"] += float(review.get("final_confidence") or 0)
            row["avg_consensus"] += float(review.get("consensus_score") or 0)
            row["avg_disagreement"] += float(review.get("disagreement_score") or 0)
        rows = []
        order = {"approve": 0, "caution": 1, "reject": 2, "unknown": 3}
        for row in grouped.values():
            total = row["total"] or 1
            row["settled"] = row["wins"] + row["losses"]
            row["accuracy"] = self._rate(row["wins"], row["losses"])
            row["avg_final_confidence"] = round(row["avg_final_confidence"] / total, 1)
            row["avg_consensus"] = round(row["avg_consensus"] / total, 1)
            row["avg_disagreement"] = round(row["avg_disagreement"] / total, 1)
            rows.append(row)
        return sorted(rows, key=lambda row: (order.get(row["decision"], 99), -row["total"]))

    def _trust_rows(self, predictions, key):
        grouped = {}
        for prediction in predictions:
            status = self._trust_status(prediction, key)
            row = grouped.setdefault(status, {
                "status": status,
                "total": 0,
                "published": 0,
                "eligible": 0,
                "wins": 0,
                "losses": 0,
                "pending": 0,
            })
            row["total"] += 1
            row["published"] += 1 if prediction.published else 0
            row["eligible"] += 1 if prediction.eligible else 0
            if prediction.status == MarketPrediction.Status.WIN:
                row["wins"] += 1
            elif prediction.status == MarketPrediction.Status.LOSS:
                row["losses"] += 1
            elif prediction.status == MarketPrediction.Status.PENDING:
                row["pending"] += 1

        rows = []
        order = {"trusted": 0, "probation": 1, "restricted": 2, "unknown": 3}
        for row in grouped.values():
            row["settled"] = row["wins"] + row["losses"]
            row["accuracy"] = self._rate(row["wins"], row["losses"])
            rows.append(row)
        return sorted(rows, key=lambda row: (order.get(row["status"], 99), -row["total"]))

    def _rejection_rows(self, predictions):
        grouped = {}
        for prediction in predictions:
            assessment = self._recommendation_assessment(prediction)
            reasons = assessment.get("recommendation_reasons") or []
            reason = prediction.rejection_reason or (reasons[0] if reasons else "recommended_or_published")
            row = grouped.setdefault(reason, {
                "reason": reason,
                "total": 0,
                "published": 0,
                "eligible": 0,
            })
            row["total"] += 1
            row["published"] += 1 if prediction.published else 0
            row["eligible"] += 1 if prediction.eligible else 0
        return sorted(grouped.values(), key=lambda row: row["total"], reverse=True)

    def _confidence_band_rows(self, algo_run):
        bands = ((algo_run.result or {}).get("performance_profile") or {}).get("confidence_bands") or {}
        if not bands:
            review = StrategyReview.objects.filter(target_date=algo_run.target_date).first()
            bands = ((review.profile if review else {}) or {}).get("confidence_bands") or {}
        order = {"80+": 0, "75-79": 1, "70-74": 2, "65-69": 3, "Below 65": 4}
        rows = []
        for band, stats in bands.items():
            rows.append({
                "band": band,
                "count": stats.get("count", 0),
                "wins": stats.get("wins", 0),
                "losses": stats.get("losses", 0),
                "hit_rate": stats.get("hit_rate", 0),
                "roi_flat": stats.get("roi_flat", 0),
                "state": stats.get("state", "unknown"),
                "recent_10_hit_rate": stats.get("recent_10_hit_rate", 0),
                "loss_streak": stats.get("loss_streak", 0),
            })
        return sorted(rows, key=lambda row: order.get(row["band"], 99))

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

    def _prediction_top_rank_row(self, row):
        from .views import _market_display_score

        payload = {
            "market": row.get("market"),
            "confidence": row.get("confidence"),
            "odds": float(row.get("odds") or 0),
            "ev": float(row.get("ev")) if row.get("ev") is not None else None,
            "eligible": row.get("eligible"),
            "risk_flags": [],
            "insights": {},
        }
        return (
            1 if row.get("published") else 0,
            1 if row.get("eligible") else 0,
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

    def _top_prediction_ids_from_rows(self, rows):
        top_by_game = {}
        for row in rows:
            key = str(row.get("match_id") or "").strip() or row.get("fixture")
            current = top_by_game.get(key)
            if current is None or self._prediction_top_rank_row(row) > self._prediction_top_rank_row(current):
                top_by_game[key] = row
        return [row["id"] for row in top_by_game.values()]

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
                "league_trust": self._trust_status(prediction, "league_trust"),
                "calibration_trust": self._trust_status(prediction, "calibration_trust"),
                "recommendation": self._recommendation_assessment(prediction),
                "council": self._council_review(prediction),
            })
        return sorted(rows, key=lambda row: (row["country"], row["league"], row["kickoff"], row["fixture"]))

    def data_center_view(self, request, object_id):
        algo_run = self.get_object(request, object_id)
        if algo_run is None:
            from django.http import Http404

            raise Http404("Algo run not found")

        predictions = MarketPrediction.objects.filter(run=algo_run)
        all_prediction_count = predictions.count()
        ranking_rows = predictions.values(
            "id",
            "match_id",
            "fixture",
            "market",
            "confidence",
            "odds",
            "ev",
            "eligible",
            "published",
        )
        top_prediction_ids = self._top_prediction_ids_from_rows(ranking_rows)
        top_predictions = (
            MarketPrediction.objects.filter(id__in=top_prediction_ids)
            .select_related("selected_pick")
            .only(
                "id",
                "run_id",
                "selected_pick_id",
                "match_date",
                "fixture",
                "league",
                "kickoff",
                "match_id",
                "market",
                "meaning",
                "confidence",
                "odds",
                "ev",
                "odds_source",
                "eligible",
                "published",
                "rejection_reason",
                "risk_flags",
                "insights",
                "status",
                "score",
                "pnl_simulated",
            )
        )
        top_prediction_list = list(top_predictions)
        published_predictions = predictions.filter(published=True)
        picks = Pick.objects.filter(run=algo_run)
        market_rows = self._market_rows(top_predictions)
        fixture_rows = self._fixture_rows(algo_run, top_prediction_list)
        council_rows = self._council_status_rows(top_prediction_list)
        league_trust_rows = self._trust_rows(top_prediction_list, "league_trust")
        calibration_trust_rows = self._trust_rows(top_prediction_list, "calibration_trust")
        rejection_rows = self._rejection_rows(top_prediction_list)
        confidence_band_rows = self._confidence_band_rows(algo_run)
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
            "council_rows": council_rows,
            "league_trust_rows": league_trust_rows,
            "calibration_trust_rows": calibration_trust_rows,
            "rejection_rows": rejection_rows,
            "confidence_band_rows": confidence_band_rows,
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
        "council_decision",
        "council_final_confidence",
        "council_disagreement",
        "market_state",
        "league_trust_state",
        "calibration_state",
        "rejection_short",
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
        "council_summary",
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
                    "council_summary",
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

    @admin.display(description="League Trust")
    def league_trust_state(self, obj):
        return ((obj.insights or {}).get("league_trust") or {}).get("status", "unknown")

    @admin.display(description="Calibration")
    def calibration_state(self, obj):
        return ((obj.insights or {}).get("calibration_trust") or {}).get("status", "unknown")

    def _council_review(self, obj):
        existing = ((obj.insights or {}).get("council_review") or {})
        if existing:
            return existing
        return council_review({
            "confidence": obj.confidence,
            "ev": obj.ev,
            "odds_meta": obj.odds_meta or {},
            "odds_source": obj.odds_source,
            "league": obj.league,
            "country": (obj.fixture_context or {}).get("country", ""),
            "risk_flags": obj.risk_flags or [],
            "eligible": obj.eligible,
            "insights": obj.insights or {},
        })

    @admin.display(description="Council")
    def council_decision(self, obj):
        return self._council_review(obj).get("decision", "unknown")

    @admin.display(description="Council Conf")
    def council_final_confidence(self, obj):
        value = self._council_review(obj).get("final_confidence")
        return f"{value}%" if value is not None else "-"

    @admin.display(description="Disagree")
    def council_disagreement(self, obj):
        value = self._council_review(obj).get("disagreement_score")
        return f"{value}" if value is not None else "-"

    @admin.display(description="Council Summary")
    def council_summary(self, obj):
        review = self._council_review(obj)
        reviewers = review.get("reviewers") or []
        reviewer_lines = [
            (f"{item.get('reviewer')}: {item.get('score')} ({item.get('verdict')})",)
            for item in reviewers
        ]
        return format_html(
            "<strong>{}</strong> · final {}% · consensus {} · disagreement {}<br>{}<br><span style='color:#666'>{}</span>",
            review.get("decision", "unknown"),
            review.get("final_confidence", "-"),
            review.get("consensus_score", "-"),
            review.get("disagreement_score", "-"),
            format_html_join("", "{}<br>", reviewer_lines),
            ", ".join(review.get("reasons") or []),
        )

    @admin.display(description="Blocked By")
    def rejection_short(self, obj):
        if obj.published:
            return "published"
        return (obj.rejection_reason or "")[:80]

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
    list_display = ("id", "match_id", "market", "match_date", "fixture", "user", "created_at")
    list_filter = ("match_date", "market", "created_at")
    search_fields = ("match_id", "market", "meaning", "fixture__fixture", "user__username", "user__email")
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

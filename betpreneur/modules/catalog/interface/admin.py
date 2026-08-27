"""Admin for fixtures, provider maps and caches."""

# The market-cache build runs the daily-run pipeline, so the task belongs to
# picks, which sits above catalog. Dispatching by name keeps the layer order.
import json

from celery import current_app
from django.contrib import admin, messages
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from betpreneur.modules.catalog.models import (
    BookmakerLeagueMap,
    DataCoverage,
    FixtureCache,
    LeagueMarketProfile,
    ProviderFixtureMap,
    ProviderPlayerMap,
    ProviderTeamMap,
    SlipReviewMarketCache,
    StatPalFixtureCoverage,
    StatPalFixtureSnapshot,
    TeamAliasMap,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)
from betpreneur.modules.catalog.services.daily_build import (
    StatPalDailyBuildService,
    statpal_snapshot_usable_fields,
)
from betpreneur.modules.catalog.tasks import build_statpal_daily_cache

BUILD_CACHE_TASK = "betpreneur.modules.picks.tasks.build_slip_review_market_cache"
CLEANUP_CACHE_TASK = "betpreneur.modules.picks.tasks.cleanup_slip_review_market_cache"


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


@admin.register(TeamProfile)
class TeamProfileAdmin(admin.ModelAdmin):
    list_display = (
        "canonical_name",
        "country",
        "primary_league_name",
        "intelligence_coverage_status",
        "intelligence_last_refresh",
        "active",
        "updated_at",
    )
    list_filter = ("active", "country", "primary_league_key")
    search_fields = ("canonical_name", "canonical_normalized", "country", "primary_league_name", "provider_ids", "aliases")
    readonly_fields = ("created_at", "updated_at", "intelligence_coverage_status", "intelligence_last_refresh")

    @admin.display(description="Coverage")
    def intelligence_coverage_status(self, obj):
        row = (
            DataCoverage.objects.filter(team=obj)
            .order_by(
                "status",
                "-last_success_at",
                "-updated_at",
            )
            .first()
        )
        return row.status if row else "missing"

    @admin.display(description="Last intelligence refresh")
    def intelligence_last_refresh(self, obj):
        latest = (
            DataCoverage.objects.filter(team=obj)
            .exclude(last_success_at__isnull=True)
            .order_by("-last_success_at")
            .values_list("last_success_at", flat=True)
            .first()
        )
        if latest:
            return latest
        season_profile = TeamSeasonProfile.objects.filter(team=obj).order_by("-updated_at").first()
        if season_profile:
            return season_profile.computed_at or season_profile.fetched_at or season_profile.updated_at
        return obj.updated_at


@admin.register(TeamSeasonProfile)
class TeamSeasonProfileAdmin(admin.ModelAdmin):
    list_display = ("team", "league_name", "season", "matches_played", "data_quality", "updated_at")
    list_filter = ("league_key", "season", "country", "data_quality", "source")
    search_fields = ("team__canonical_name", "league_name", "country", "provider_ids", "stats")
    readonly_fields = ("created_at", "updated_at")


@admin.register(TeamRecentFormProfile)
class TeamRecentFormProfileAdmin(admin.ModelAdmin):
    list_display = ("team", "league_name", "season", "window", "scope", "matches", "updated_at")
    list_filter = ("league_key", "season", "window", "scope", "source")
    search_fields = ("team__canonical_name", "league_name", "stats", "form")
    readonly_fields = ("created_at", "updated_at")


@admin.register(TeamMarketProfile)
class TeamMarketProfileAdmin(admin.ModelAdmin):
    list_display = ("team", "league_name", "season", "market", "scope", "attempts", "hit_rate", "data_quality")
    list_filter = ("league_key", "season", "market_family", "scope", "data_quality", "source")
    search_fields = ("team__canonical_name", "league_name", "market", "market_family", "stats")
    readonly_fields = ("created_at", "updated_at")


@admin.register(LeagueMarketProfile)
class LeagueMarketProfileAdmin(admin.ModelAdmin):
    list_display = ("league_name", "season", "market", "attempts", "hit_rate", "fairness_score", "data_quality")
    list_filter = ("league_key", "season", "market_family", "data_quality", "source")
    search_fields = ("league_name", "country", "market", "market_family", "provider_ids", "stats")
    readonly_fields = ("created_at", "updated_at")


@admin.register(DataCoverage)
class DataCoverageAdmin(admin.ModelAdmin):
    list_display = ("subject_type", "subject_key", "provider", "coverage_key", "status", "last_success_at", "expires_at")
    list_filter = ("subject_type", "provider", "coverage_key", "status", "league_key", "season")
    search_fields = ("subject_key", "league_name", "team__canonical_name", "coverage_key", "error", "metadata")
    readonly_fields = ("created_at", "updated_at")


@admin.register(BookmakerLeagueMap)
class BookmakerLeagueMapAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "provider_competition_name",
        "provider_competition_id",
        "api_league_id",
        "api_league_name",
        "country",
        "current_api_season",
        "confidence",
        "active",
        "last_verified_at",
    )
    list_filter = ("provider", "active", "country", "source")
    search_fields = (
        "provider_competition_id",
        "provider_competition_name",
        "provider_competition_normalized",
        "api_league_name",
        "api_league_id",
        "country",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(TeamAliasMap)
class TeamAliasMapAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "alias",
        "canonical_name",
        "api_team_id",
        "country",
        "confidence",
        "active",
        "last_seen_at",
    )
    list_filter = ("provider", "active", "country", "source")
    search_fields = (
        "alias",
        "alias_normalized",
        "canonical_name",
        "canonical_normalized",
        "api_team_id",
        "country",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(ProviderFixtureMap)
class ProviderFixtureMapAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "provider_event_id",
        "provider_competition_name",
        "api_fixture_id",
        "api_league_name",
        "provider_home_team",
        "provider_away_team",
        "api_home_team",
        "api_away_team",
        "confidence",
        "active",
        "verified_at",
    )
    list_filter = ("provider", "active", "provider_competition_name", "api_league_name", "resolution_method")
    search_fields = (
        "provider_event_id",
        "provider_competition_id",
        "provider_competition_name",
        "api_fixture_id",
        "api_league_name",
        "provider_home_team",
        "provider_away_team",
        "api_home_team",
        "api_away_team",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(ProviderTeamMap)
class ProviderTeamMapAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "provider_team_name",
        "provider_team_id",
        "internal_team_name",
        "internal_team_id",
        "api_team_id",
        "country",
        "confidence",
        "active",
        "verified_at",
    )
    list_filter = ("provider", "active", "country", "resolution_method")
    search_fields = (
        "provider_team_id",
        "provider_team_name",
        "provider_team_normalized",
        "internal_team_id",
        "internal_team_name",
        "internal_team_normalized",
        "api_team_id",
        "country",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(ProviderPlayerMap)
class ProviderPlayerMapAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "provider_player_name",
        "provider_player_id",
        "provider_team_name",
        "internal_player_name",
        "internal_player_id",
        "position",
        "confidence",
        "active",
        "verified_at",
    )
    list_filter = ("provider", "active", "position", "nationality", "resolution_method")
    search_fields = (
        "provider_player_id",
        "provider_player_name",
        "provider_player_normalized",
        "internal_player_id",
        "internal_player_name",
        "internal_player_normalized",
        "provider_team_id",
        "provider_team_name",
        "internal_team_id",
        "internal_team_name",
        "nationality",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(StatPalFixtureSnapshot)
class StatPalFixtureSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "snapshot_type",
        "match_id",
        "provider_match_id",
        "provider_competition_id",
        "status",
        "source_endpoint",
        "usable_field_count",
        "payload_state",
        "fetched_at",
        "expires_at",
    )
    list_filter = ("snapshot_type", "status", "source_endpoint", "provider_competition_id")
    search_fields = (
        "match_id",
        "provider_match_id",
        "provider_competition_id",
        "source_endpoint",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "usable_fields_display",
        "summary_pretty",
        "payload_pretty",
    )

    @admin.display(description="Usable fields")
    def usable_field_count(self, obj):
        return len(statpal_snapshot_usable_fields(obj.snapshot_type, obj.summary or {}))

    @admin.display(description="Payload")
    def payload_state(self, obj):
        return "yes" if obj.payload else "empty"

    @admin.display(description="Usable fields")
    def usable_fields_display(self, obj):
        fields = statpal_snapshot_usable_fields(obj.snapshot_type, obj.summary or {})
        return ", ".join(fields) or "No usable fields detected"

    @admin.display(description="Summary JSON")
    def summary_pretty(self, obj):
        return format_html("<pre style='white-space:pre-wrap'>{}</pre>", json.dumps(obj.summary or {}, indent=2, sort_keys=True))

    @admin.display(description="Payload JSON")
    def payload_pretty(self, obj):
        return format_html("<pre style='white-space:pre-wrap'>{}</pre>", json.dumps(obj.payload or {}, indent=2, sort_keys=True)[:50000])


@admin.register(StatPalFixtureCoverage)
class StatPalFixtureCoverageAdmin(admin.ModelAdmin):
    change_list_template = "admin/algo/statpalfixturecoverage/change_list.html"
    date_hierarchy = "match_date"
    list_display = (
        "match_date",
        "fixture",
        "country",
        "league",
        "statpal_provider_match_id",
        "coverage_badge",
        "present_snapshot_count",
        "missing_snapshot_count",
        "usable_field_count",
        "updated_at",
    )
    list_filter = ("match_date", "country", "league", "source")
    search_fields = (
        "fixture",
        "home_team",
        "away_team",
        "match_id",
        "league",
        "country",
        "api_payload",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "statpal_identity_display",
        "coverage_detail",
        "missing_snapshot_types_display",
        "present_snapshot_types_display",
        "api_payload_pretty",
    )
    fieldsets = (
        (
            "Fixture",
            {
                "fields": (
                    "match_date",
                    "fixture",
                    "home_team",
                    "away_team",
                    "country",
                    "league",
                    "kickoff",
                    "kickoff_utc",
                    "match_id",
                    "source",
                )
            },
        ),
        (
            "StatPal Coverage",
            {
                "fields": (
                    "statpal_identity_display",
                    "coverage_detail",
                    "present_snapshot_types_display",
                    "missing_snapshot_types_display",
                )
            },
        ),
        ("Raw Fixture Payload", {"classes": ("collapse",), "fields": ("api_payload_pretty",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "queue-build/",
                self.admin_site.admin_view(self.queue_build_view),
                name="algo_statpalfixturecoverage_queue_build",
            ),
            path(
                "queue-force-build/",
                self.admin_site.admin_view(self.queue_force_build_view),
                name="algo_statpalfixturecoverage_queue_force_build",
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["statpal_queue_build_url"] = reverse("admin:algo_statpalfixturecoverage_queue_build")
        extra_context["statpal_queue_force_build_url"] = reverse("admin:algo_statpalfixturecoverage_queue_force_build")
        return super().changelist_view(request, extra_context=extra_context)

    def queue_build_view(self, request):
        task = build_statpal_daily_cache.delay(days=3)
        self.message_user(
            request,
            f"Queued StatPal daily cache build: {task.id}",
            level=messages.SUCCESS,
        )
        return self._redirect_to_changelist()

    def queue_force_build_view(self, request):
        task = build_statpal_daily_cache.delay(days=3, force=True)
        self.message_user(
            request,
            f"Queued forced StatPal daily cache refresh: {task.id}",
            level=messages.SUCCESS,
        )
        return self._redirect_to_changelist()

    def _redirect_to_changelist(self):
        from django.shortcuts import redirect

        return redirect(reverse("admin:algo_statpalfixturecoverage_changelist"))

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.filter(source="statpal")

    def _coverage(self, obj):
        cached = getattr(obj, "_statpal_admin_coverage", None)
        if cached is None:
            service = StatPalDailyBuildService()
            cached = service.coverage_for_fixture(self._fixture_dict(obj), include_optional=True)
            obj._statpal_admin_coverage = cached
        return cached

    @staticmethod
    def _fixture_dict(obj):
        payload = obj.api_payload if isinstance(obj.api_payload, dict) else {}
        return {
            "match_id": obj.match_id,
            "provider_match_id": payload.get("provider_match_id") or payload.get("main_id") or payload.get("statpal_provider_match_id") or "",
            "provider_competition_id": payload.get("provider_competition_id") or payload.get("statpal_provider_competition_id") or payload.get("code") or "",
            "home_team_id": payload.get("provider_home_team_id") or payload.get("statpal_home_team_id") or payload.get("hid") or "",
            "away_team_id": payload.get("provider_away_team_id") or payload.get("statpal_away_team_id") or payload.get("aid") or "",
            "home_team": obj.home_team,
            "away_team": obj.away_team,
            "fixture": obj.fixture,
            "date": obj.match_date,
            "league": obj.league,
            "country": obj.country,
            "api_payload": payload,
        }

    @admin.display(description="StatPal match")
    def statpal_provider_match_id(self, obj):
        identity = self._coverage(obj).get("identity") or {}
        return identity.get("match_id") or ""

    @admin.display(description="Coverage")
    def coverage_badge(self, obj):
        coverage = self._coverage(obj)
        status = coverage.get("status", "unknown")
        percent = coverage.get("coverage_percent", 0)
        color = {
            "complete": "#147d3f",
            "stale": "#9a6700",
            "partial": "#b42318",
            "identity_missing": "#b42318",
        }.get(status, "#667085")
        return format_html(
            "<strong style='color:{}'>{} {}</strong>",
            color,
            f"{percent}%",
            status.replace("_", " "),
        )

    @admin.display(description="Present")
    def present_snapshot_count(self, obj):
        snapshots = self._coverage(obj).get("snapshots") or {}
        return sum(1 for item in snapshots.values() if item.get("present"))

    @admin.display(description="Missing")
    def missing_snapshot_count(self, obj):
        return len(self._coverage(obj).get("missing_snapshot_types") or [])

    @admin.display(description="Usable fields")
    def usable_field_count(self, obj):
        return self._coverage(obj).get("usable_field_count", 0)

    @admin.display(description="StatPal identity")
    def statpal_identity_display(self, obj):
        identity = self._coverage(obj).get("identity") or {}
        present = identity.get("present") or {}
        rows = [
            ("Match", identity.get("match_id", ""), present.get("provider_match_id")),
            ("League", identity.get("league_id", ""), present.get("league_id")),
            ("Home team", identity.get("home_team_id", ""), present.get("home_team_id")),
            ("Away team", identity.get("away_team_id", ""), present.get("away_team_id")),
        ]
        return format_html_join(
            "",
            "<div><strong>{}</strong>: {} {}</div>",
            ((label, value or "missing", "ok" if ok else "missing") for label, value, ok in rows),
        )

    @admin.display(description="Coverage detail")
    def coverage_detail(self, obj):
        coverage = self._coverage(obj)
        rows = []
        for snapshot_type, item in (coverage.get("snapshots") or {}).items():
            if item.get("present"):
                fields = ", ".join(item.get("usable_fields") or [])
                state = "stale" if item.get("stale") else "available"
            else:
                fields = ""
                state = "missing"
            rows.append((snapshot_type, state, item.get("source_endpoint", ""), fields or ""))
        return format_html_join(
            "",
            "<div style='margin-bottom:6px'><strong>{}</strong>: {} <span style='color:#667085'>{}</span><br><small>{}</small></div>",
            rows,
        )

    @admin.display(description="Present snapshot types")
    def present_snapshot_types_display(self, obj):
        snapshots = self._coverage(obj).get("snapshots") or {}
        present = [key for key, item in snapshots.items() if item.get("present")]
        return ", ".join(present) or "None"

    @admin.display(description="Missing snapshot types")
    def missing_snapshot_types_display(self, obj):
        missing = self._coverage(obj).get("missing_snapshot_types") or []
        return ", ".join(missing) or "None"

    @admin.display(description="Fixture payload JSON")
    def api_payload_pretty(self, obj):
        return format_html("<pre style='white-space:pre-wrap'>{}</pre>", json.dumps(obj.api_payload or {}, indent=2, sort_keys=True))


@admin.register(SlipReviewMarketCache)
class SlipReviewMarketCacheAdmin(admin.ModelAdmin):
    date_hierarchy = "match_date"
    list_display = (
        "id",
        "match_date",
        "fixture",
        "league",
        "country",
        "market",
        "market_family",
        "confidence",
        "final_confidence",
        "odds",
        "odds_source",
        "eligible",
        "source",
        "data_quality",
        "expires_at",
        "updated_at",
    )
    list_filter = (
        "cache_scope",
        "source",
        "match_date",
        "country",
        "league",
        "market_family",
        "eligible",
        "odds_source",
        "data_quality",
        "cache_version",
    )
    search_fields = (
        "fixture",
        "home_team",
        "away_team",
        "league",
        "country",
        "market",
        "market_family",
        "match_id",
        "provider_match_id",
        "provider_competition_id",
        "home_team_id",
        "away_team_id",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "market_payload_pretty",
        "fixture_payload_pretty",
        "provider_merge_pretty",
        "insights_pretty",
        "risk_flags_pretty",
    )
    fieldsets = (
        (
            "Fixture",
            {
                "fields": (
                    "cache_scope",
                    "source",
                    "match_date",
                    "fixture",
                    "home_team",
                    "away_team",
                    "league",
                    "league_id",
                    "country",
                    "kickoff",
                    "match_id",
                    "provider_match_id",
                    "provider_competition_id",
                    "home_team_id",
                    "away_team_id",
                )
            },
        ),
        (
            "Market",
            {
                "fields": (
                    "market",
                    "market_family",
                    "meaning",
                    "raw_confidence",
                    "confidence",
                    "final_confidence",
                    "odds",
                    "ev",
                    "odds_source",
                    "eligible",
                    "data_quality",
                    "cache_version",
                    "expires_at",
                )
            },
        ),
        (
            "Debug Payloads",
            {
                "classes": ("collapse",),
                "fields": (
                    "risk_flags_pretty",
                    "insights_pretty",
                    "provider_merge_pretty",
                    "market_payload_pretty",
                    "fixture_payload_pretty",
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
    actions = ("queue_refresh_for_selected_dates", "queue_cleanup_expired", "delete_expired")

    def _pretty(self, value):
        return format_html("<pre style='white-space:pre-wrap'>{}</pre>", json.dumps(value or {}, indent=2, sort_keys=True, default=str)[:50000])

    @admin.display(description="Market Payload")
    def market_payload_pretty(self, obj):
        return self._pretty(obj.market_payload)

    @admin.display(description="Fixture Payload")
    def fixture_payload_pretty(self, obj):
        return self._pretty(obj.fixture_payload)

    @admin.display(description="Provider Merge")
    def provider_merge_pretty(self, obj):
        return self._pretty(obj.provider_merge)

    @admin.display(description="Insights")
    def insights_pretty(self, obj):
        return self._pretty(obj.insights)

    @admin.display(description="Risk Flags")
    def risk_flags_pretty(self, obj):
        return self._pretty(obj.risk_flags)

    @admin.action(description="Queue private cache rebuild for selected dates")
    def queue_refresh_for_selected_dates(self, request, queryset):
        task_ids = []
        dates = queryset.values_list("match_date", flat=True).distinct()
        for match_date in dates:
            task = current_app.send_task(
                BUILD_CACHE_TASK,
                kwargs={
                    "start_date": match_date.isoformat(),
                    "days": 0,
                    "sync_fixtures": False,
                    "force": True,
                },
            )
            task_ids.append(task.id)
        messages.success(request, f"Queued {len(task_ids)} private cache rebuild task(s): {', '.join(task_ids)}")

    @admin.action(description="Delete expired private cache rows")
    def delete_expired(self, request, queryset):
        deleted, _ = queryset.filter(expires_at__lte=timezone.now()).delete()
        messages.success(request, f"Deleted {deleted} expired private cache row(s).")

    @admin.action(description="Queue expired private cache cleanup")
    def queue_cleanup_expired(self, request, queryset):
        task = current_app.send_task(CLEANUP_CACHE_TASK)
        messages.success(request, f"Queued private cache cleanup task: {task.id}")

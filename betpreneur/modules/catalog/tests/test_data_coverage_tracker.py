from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.catalog.api import (
    DataCoverage,
    DataCoverageTracker,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)
from betpreneur.modules.catalog.domain.league_registry import TOP_EUROPEAN_INTELLIGENCE_LEAGUES
from betpreneur.modules.catalog.services.coverage_tracker import (
    REQUIRED_MARKET_FAMILIES,
    REQUIRED_RECENT_FORM_SCOPES,
    REQUIRED_RECENT_FORM_WINDOWS,
)


class DataCoverageTrackerTests(TestCase):
    def setUp(self):
        self.league = TOP_EUROPEAN_INTELLIGENCE_LEAGUES[0]
        self.team = TeamProfile.objects.create(
            canonical_name="Arsenal",
            canonical_normalized="arsenal",
            country="England",
            primary_league_key=self.league.key,
            primary_league_name=self.league.name,
            provider_ids={"statpal": {"team_id": "42", "league_id": self.league.statpal_league_id}},
        )
        self.season = self.league.current_season

    def _season_profile(self, *, quality=TeamSeasonProfile.DataQuality.STRONG):
        return TeamSeasonProfile.objects.create(
            team=self.team,
            league_key=self.league.key,
            league_name=self.league.name,
            country=self.league.country,
            season=self.season,
            provider_ids={"statpal": {"team_id": "42", "league_id": self.league.statpal_league_id}},
            matches_played=20,
            data_quality=quality,
            source="statpal",
            fetched_at=timezone.now(),
            computed_at=timezone.now(),
        )

    def _recent_form_profiles(self):
        for window in REQUIRED_RECENT_FORM_WINDOWS:
            for scope in REQUIRED_RECENT_FORM_SCOPES:
                TeamRecentFormProfile.objects.create(
                    team=self.team,
                    league_key=self.league.key,
                    league_name=self.league.name,
                    season=self.season,
                    window=window,
                    scope=scope,
                    matches=window,
                    wins=window,
                    goals_for=float(window),
                    goals_against=1.0,
                    computed_at=timezone.now(),
                )

    def _market_profiles(self):
        for family in REQUIRED_MARKET_FAMILIES:
            TeamMarketProfile.objects.create(
                team=self.team,
                league_key=self.league.key,
                league_name=self.league.name,
                season=self.season,
                market_family=family,
                market=f"{family} sample",
                scope=TeamMarketProfile.Scope.ALL,
                attempts=12,
                wins=8,
                losses=4,
                hit_rate=66.7,
                confidence=72.0,
                data_quality="medium",
                computed_at=timezone.now(),
            )

    def test_refresh_marks_complete_team_intelligence_as_fresh(self):
        self._season_profile()
        self._recent_form_profiles()
        self._market_profiles()

        result = DataCoverageTracker().refresh(
            league_keys=[self.league.key],
            seasons=[self.season],
        )

        self.assertEqual(result["teams_checked"], 1)
        coverage = DataCoverage.objects.get(
            team=self.team,
            coverage_key=DataCoverageTracker.TEAM_COVERAGE_KEY,
        )
        self.assertEqual(coverage.status, DataCoverage.Status.FRESH)
        self.assertEqual(coverage.missing_requirements, [])
        self.assertEqual(coverage.metadata["source_quality"], "strong")
        self.assertGreaterEqual(coverage.metadata["confidence"], 90)

    def test_refresh_marks_missing_form_and_markets_as_partial(self):
        self._season_profile(quality=TeamSeasonProfile.DataQuality.MEDIUM)

        DataCoverageTracker().refresh(
            league_keys=[self.league.key],
            seasons=[self.season],
        )

        coverage = DataCoverage.objects.get(
            team=self.team,
            coverage_key=DataCoverageTracker.TEAM_COVERAGE_KEY,
        )
        self.assertEqual(coverage.status, DataCoverage.Status.PARTIAL)
        self.assertIn("recent_form_all_5", coverage.missing_requirements)
        self.assertIn("market_profile_total_goals", coverage.missing_requirements)
        self.assertEqual(coverage.metadata["component_scores"]["recent_form"], 0.0)

    def test_refresh_writes_league_aggregate(self):
        self._season_profile()
        self._recent_form_profiles()
        self._market_profiles()

        DataCoverageTracker().refresh(
            league_keys=[self.league.key],
            seasons=[self.season],
        )

        coverage = DataCoverage.objects.get(
            subject_type=DataCoverage.SubjectType.LEAGUE,
            subject_key=f"{self.league.key}:{self.season}",
            coverage_key=DataCoverageTracker.LEAGUE_COVERAGE_KEY,
        )
        self.assertEqual(coverage.status, DataCoverage.Status.PARTIAL)
        self.assertEqual(coverage.metadata["teams_checked"], 1)
        self.assertEqual(coverage.metadata["status_counts"][DataCoverage.Status.FRESH], 1)

from datetime import date

from django.test import TestCase

from betpreneur.modules.catalog.api import (
    DataCoverage,
    FixtureCache,
    RecentFormBuilder,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)
from betpreneur.modules.catalog.domain.league_registry import TOP_EUROPEAN_INTELLIGENCE_LEAGUES
from betpreneur.modules.catalog.domain.text import normalize_fixture_text


class RecentFormBuilderTests(TestCase):
    def setUp(self):
        self.league = TOP_EUROPEAN_INTELLIGENCE_LEAGUES[0]
        self.team = TeamProfile.objects.create(
            canonical_name="Arsenal",
            canonical_normalized="arsenal",
            country="England",
            primary_league_key=self.league.key,
            primary_league_name=self.league.name,
            provider_ids={"statpal": {"team_id": "42", "league_id": "3037"}},
            aliases=["Arsenal FC"],
        )
        TeamSeasonProfile.objects.create(
            team=self.team,
            league_key=self.league.key,
            league_name=self.league.name,
            country="England",
            season="2026-2027",
            provider_ids={"statpal": {"team_id": "42", "league_id": "3037"}},
            matches_played=20,
            data_quality=TeamSeasonProfile.DataQuality.STRONG,
        )

    def _fixture(self, day, home, away, home_id, away_id, hg, ag, *, status="Finished", team_stats=None):
        FixtureCache.objects.create(
            match_date=date(2026, 8, day),
            fixture=f"{home} vs {away}",
            home_team=home,
            away_team=away,
            home_team_normalized=normalize_fixture_text(home),
            away_team_normalized=normalize_fixture_text(away),
            fixture_normalized=normalize_fixture_text(f"{home} vs {away}"),
            league=self.league.name,
            country="England",
            match_id=f"statpal:m{day}",
            source="statpal",
            api_payload={
                "provider_competition_id": "3037",
                "provider_match_id": f"m{day}",
                "provider_home_team_id": str(home_id),
                "provider_away_team_id": str(away_id),
                "status": status,
                "home_goals": hg,
                "away_goals": ag,
                "team_stats": team_stats or {},
            },
        )

    def test_builds_last_windows_for_all_home_and_away_scopes(self):
        self._fixture(1, "Arsenal", "Chelsea", "42", "50", 2, 0)
        self._fixture(2, "Liverpool", "Arsenal", "51", "42", 1, 1)
        self._fixture(3, "Arsenal FC", "Everton", "42", "52", 0, 1)
        self._fixture(4, "Tottenham", "Arsenal", "53", "42", 0, 3)
        self._fixture(5, "Arsenal", "Leeds", "42", "54", 1, 0)
        self._fixture(6, "Arsenal", "Future FC", "42", "55", "", "", status="Not Started")

        result = RecentFormBuilder().build(
            league_keys=[self.league.key],
            seasons=["2026-2027"],
            windows=[5],
            sync_matches=False,
        )

        self.assertEqual(result["profiles_saved"], 3)
        all_form = TeamRecentFormProfile.objects.get(team=self.team, window=5, scope=TeamRecentFormProfile.Scope.ALL)
        home_form = TeamRecentFormProfile.objects.get(team=self.team, window=5, scope=TeamRecentFormProfile.Scope.HOME)
        away_form = TeamRecentFormProfile.objects.get(team=self.team, window=5, scope=TeamRecentFormProfile.Scope.AWAY)
        self.assertEqual(all_form.matches, 5)
        self.assertEqual(all_form.form, ["W", "W", "L", "D", "W"])
        self.assertEqual(all_form.wins, 3)
        self.assertEqual(all_form.draws, 1)
        self.assertEqual(all_form.losses, 1)
        self.assertEqual(home_form.matches, 3)
        self.assertEqual(home_form.form, ["W", "L", "W"])
        self.assertEqual(away_form.matches, 2)
        self.assertEqual(away_form.form, ["W", "D"])

    def test_averages_fixture_specific_context_metrics(self):
        self._fixture(
            1,
            "Arsenal",
            "Chelsea",
            "42",
            "50",
            2,
            0,
            team_stats={
                "home": {
                    "expected_goals": {"total": 1.9},
                    "corners": {"total": 7},
                    "yellowcards": {"total": 1},
                    "shots_on_goal": {"total": 5},
                },
                "away": {
                    "expected_goals": {"total": 0.7},
                    "corners": {"total": 3},
                    "yellowcards": {"total": 3},
                    "shots_on_goal": {"total": 2},
                },
            },
        )

        RecentFormBuilder().build(
            league_keys=[self.league.key],
            seasons=["2026-2027"],
            windows=[5],
            sync_matches=False,
        )

        profile = TeamRecentFormProfile.objects.get(team=self.team, window=5, scope=TeamRecentFormProfile.Scope.ALL)
        self.assertEqual(profile.xg_for, 1.9)
        self.assertEqual(profile.xg_against, 0.7)
        self.assertEqual(profile.corners_for, 7.0)
        self.assertEqual(profile.cards_against, 3.0)
        self.assertEqual(profile.shots_on_target_for, 5.0)
        self.assertTrue(
            DataCoverage.objects.filter(
                team=self.team,
                coverage_key=RecentFormBuilder.COVERAGE_KEY,
                status=DataCoverage.Status.PARTIAL,
            ).exists()
        )

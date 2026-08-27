from django.test import SimpleTestCase, TestCase

from betpreneur.modules.catalog.api import (
    DataCoverage,
    HistoricalTeamHydrator,
    TeamProfile,
    TeamSeasonProfile,
)
from betpreneur.modules.catalog.domain.league_registry import TOP_EUROPEAN_INTELLIGENCE_LEAGUES


class DummyHistoricalClient:
    def __init__(self, *, team_failures=None):
        self.calls = []
        self.team_failures = set(team_failures or [])

    def soccer_league_standings(self, league_id, params=None):
        self.calls.append(("standings", str(league_id), params or {}))
        return {
            "standings": {
                "country": "England",
                "tournament": {
                    "id": str(league_id),
                    "season": (params or {}).get("season") or "2026-2027",
                    "league": "English Premier League",
                    "team": [
                        {
                            "id": "42",
                            "name": "Arsenal",
                            "position": "1",
                            "recent_form": "WWDWW",
                            "overall": {
                                "games_played": "24",
                                "wins": "17",
                                "draws": "5",
                                "losses": "2",
                                "goals_scored": "51",
                                "goals_allowed": "18",
                            },
                            "home": {"games_played": "12", "goals_scored": "28", "goals_allowed": "8"},
                            "away": {"games_played": "12", "goals_scored": "23", "goals_allowed": "10"},
                            "total": {"points": "56"},
                        }
                    ],
                },
            }
        }

    def soccer_team(self, team_id):
        self.calls.append(("team", str(team_id), {}))
        if str(team_id) in self.team_failures:
            raise RuntimeError("team endpoint failed")
        return {
            "team": {
                "id": str(team_id),
                "name": "Arsenal",
                "country": "England",
                "league_stats": {
                    "league": {
                        "id": "3037",
                        "name": "English Premier League",
                        "season": "2026-2027",
                        "fulltime": {
                            "avg_corners": {"total": "6.1", "home": "6.8", "away": "5.4"},
                            "avg_yellowcards": {"total": "1.7", "home": "1.5", "away": "1.9"},
                            "shots_total": {"total": "360"},
                            "shots_on_goal": {"total": "144"},
                            "clean_sheet": {"total": "10"},
                        },
                    }
                },
            }
        }


class HistoricalHydratorPureTests(SimpleTestCase):
    def test_scope_defaults_to_registry_current_and_previous_seasons(self):
        hydrator = HistoricalTeamHydrator(client=DummyHistoricalClient())

        scopes = hydrator._scopes(league_keys=["england-premier-league"], seasons=None)

        self.assertEqual(len(scopes), 2)
        self.assertEqual(scopes[0].league.key, "england-premier-league")
        self.assertEqual(
            {scope.season for scope in scopes},
            {
                TOP_EUROPEAN_INTELLIGENCE_LEAGUES[0].current_season,
                TOP_EUROPEAN_INTELLIGENCE_LEAGUES[0].previous_season,
            },
        )


class HistoricalHydratorPersistenceTests(TestCase):
    def test_hydrate_scope_stores_team_identity_profile_and_coverage(self):
        hydrator = HistoricalTeamHydrator(client=DummyHistoricalClient())

        result = hydrator.hydrate(
            league_keys=["england-premier-league"],
            seasons=["2026-2027"],
        )

        self.assertEqual(result["profiles_saved"], 1)
        team = TeamProfile.objects.get(canonical_normalized="arsenal")
        self.assertEqual(team.provider_ids["statpal"]["team_id"], "42")
        profile = TeamSeasonProfile.objects.get(team=team, league_key="england-premier-league", season="2026-2027")
        self.assertEqual(profile.matches_played, 24)
        self.assertEqual(profile.goals_for, 51)
        self.assertEqual(profile.corners_for, 6.1)
        self.assertEqual(profile.shots_for, 15.0)
        self.assertEqual(profile.data_quality, TeamSeasonProfile.DataQuality.STRONG)
        coverage = DataCoverage.objects.get(team=team, coverage_key=HistoricalTeamHydrator.COVERAGE_KEY)
        self.assertEqual(coverage.status, DataCoverage.Status.FRESH)

    def test_team_endpoint_failure_still_saves_limited_standings_profile(self):
        hydrator = HistoricalTeamHydrator(client=DummyHistoricalClient(team_failures={"42"}))

        result = hydrator.hydrate(
            league_keys=["england-premier-league"],
            seasons=["2026-2027"],
        )

        self.assertEqual(result["profiles_saved"], 1)
        self.assertEqual(result["coverage_failed"], 1)
        profile = TeamSeasonProfile.objects.get(team__canonical_normalized="arsenal")
        self.assertEqual(profile.matches_played, 24)
        self.assertEqual(profile.data_quality, TeamSeasonProfile.DataQuality.LIMITED)
        coverage = DataCoverage.objects.get(team=profile.team, coverage_key=HistoricalTeamHydrator.COVERAGE_KEY)
        self.assertEqual(coverage.status, DataCoverage.Status.PARTIAL)
        self.assertEqual(coverage.missing_requirements, ["team_stats"])

    def test_league_without_statpal_id_is_skipped(self):
        hydrator = HistoricalTeamHydrator(client=DummyHistoricalClient())

        result = hydrator.hydrate(
            league_keys=["spain-la-liga"],
            seasons=["2026-2027"],
        )

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["profiles_saved"], 0)

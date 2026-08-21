"""
Team strength must survive the season boundary.

In August every team's current-season record is empty, so shrinkage pulled every factor
to exactly 1.0 and `expected_goals` returned the bare league baseline. Two different
Saudi fixtures came back with identical expected goals -- 1.4748 home, 1.2999 away --
each presented as "derived from a fitted goal model". Because the baseline carries home
advantage, the home side was rated higher in *every* fixture in the league, which is how
a Galatasaray or Al-Nassr away trip was told to back the home team.

Two defences: shrink toward last season's strength rather than toward "average team",
and where the model still cannot separate two sides, decline the result market instead
of publishing a league average as a fixture-specific read.
"""

from django.test import SimpleTestCase, TestCase

from apps.algo.evaluators import score_matrix_evaluator
from apps.algo.market_taxonomy import describe_market
from apps.algo.models import LeagueScoreModel, TeamStrength
from apps.algo.scoring.fitting import (
    MIN_TEAM_MATCHES_FOR_RESULT,
    MODEL_VERSION,
    expected_goals,
    fit_league_from_standings,
)
from apps.algo.scoring.service import score_model_service
from apps.algo.tasks import prior_season_candidates


def _fixture():
    """Keys as the evaluator reads them off a resolved fixture."""
    return {"code": "L1", "hname": "Home FC", "aname": "Away FC"}


def _standings(rows):
    return {
        "standings": {
            "tournament": {
                "team": [
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "home": {
                            "games_played": str(row["hg"]),
                            "goals_scored": str(row["hs"]),
                            "goals_allowed": str(row["ha"]),
                        },
                        "away": {
                            "games_played": str(row["ag"]),
                            "goals_scored": str(row["as"]),
                            "goals_allowed": str(row["aa"]),
                        },
                    }
                    for row in rows
                ]
            }
        }
    }


# A finished season: one dominant side, one poor side.
PRIOR_ROWS = [
    {"id": "1", "name": "Strong FC", "hg": 19, "hs": 45, "ha": 12, "ag": 19, "as": 38, "aa": 18},
    {"id": "2", "name": "Weak United", "hg": 19, "hs": 14, "ha": 38, "ag": 19, "as": 10, "aa": 44},
    {"id": "3", "name": "Mid City", "hg": 19, "hs": 26, "ha": 25, "ag": 19, "as": 22, "aa": 28},
]
# The new season, nothing played yet -- exactly what StatPal returns in August.
EMPTY_ROWS = [
    {"id": row["id"], "name": row["name"], "hg": 0, "hs": 0, "ha": 0, "ag": 0, "as": 0, "aa": 0}
    for row in PRIOR_ROWS
]


class PriorSeasonFitTests(SimpleTestCase):
    def setUp(self):
        self.prior = fit_league_from_standings(
            _standings(PRIOR_ROWS), league_id="L1", season="2024-2025"
        )

    def test_without_a_prior_every_team_collapses_to_the_league_average(self):
        """The defect: an empty season makes all teams identical."""
        fit = fit_league_from_standings(_standings(EMPTY_ROWS), league_id="L1", season="2025-2026")

        factors = {(t.home_attack, t.home_defence, t.away_attack, t.away_defence) for t in fit.teams}
        self.assertEqual(factors, {(1.0, 1.0, 1.0, 1.0)})

    def test_a_prior_keeps_the_teams_apart_before_a_ball_is_kicked(self):
        fit = fit_league_from_standings(
            _standings(EMPTY_ROWS), league_id="L1", season="2025-2026", prior_fit=self.prior
        )
        by_name = {team.team_name: team for team in fit.teams}

        self.assertGreater(by_name["Strong FC"].home_attack, by_name["Weak United"].home_attack)
        self.assertLess(by_name["Strong FC"].home_defence, by_name["Weak United"].home_defence)

    def test_two_fixtures_in_one_league_no_longer_share_expected_goals(self):
        """The Al Riyadh / Al-Hazm symptom, stated directly."""
        fit = fit_league_from_standings(
            _standings(EMPTY_ROWS), league_id="L1", season="2025-2026", prior_fit=self.prior
        )
        by_name = {team.team_name: team for team in fit.teams}

        def rates(home, away):
            return expected_goals(
                home_attack=by_name[home].home_attack,
                home_defence=by_name[home].home_defence,
                away_attack=by_name[away].away_attack,
                away_defence=by_name[away].away_defence,
                home_baseline=fit.home_goal_baseline,
                away_baseline=fit.away_goal_baseline,
            )

        self.assertNotEqual(rates("Strong FC", "Weak United"), rates("Weak United", "Strong FC"))

    def test_a_strong_away_side_is_rated_above_a_weak_home_side(self):
        """Home advantage alone must not decide the fixture."""
        fit = fit_league_from_standings(
            _standings(EMPTY_ROWS), league_id="L1", season="2025-2026", prior_fit=self.prior
        )
        by_name = {team.team_name: team for team in fit.teams}
        home_rate, away_rate = expected_goals(
            home_attack=by_name["Weak United"].home_attack,
            home_defence=by_name["Weak United"].home_defence,
            away_attack=by_name["Strong FC"].away_attack,
            away_defence=by_name["Strong FC"].away_defence,
            home_baseline=fit.home_goal_baseline,
            away_baseline=fit.away_goal_baseline,
        )

        self.assertGreater(away_rate, home_rate)

    def test_current_form_still_takes_over_as_the_season_builds(self):
        """The prior is a starting point, not an anchor."""
        played = [
            {"id": "1", "name": "Strong FC", "hg": 10, "hs": 4, "ha": 18, "ag": 10, "as": 3, "aa": 20},
            {"id": "2", "name": "Weak United", "hg": 10, "hs": 22, "ha": 6, "ag": 10, "as": 20, "aa": 5},
            {"id": "3", "name": "Mid City", "hg": 10, "hs": 12, "ha": 12, "ag": 10, "as": 11, "aa": 13},
        ]
        fit = fit_league_from_standings(
            _standings(played), league_id="L1", season="2025-2026", prior_fit=self.prior
        )
        by_name = {team.team_name: team for team in fit.teams}

        # Collapsed champions, resurgent strugglers: the table has overtaken the prior.
        self.assertLess(by_name["Strong FC"].home_attack, by_name["Weak United"].home_attack)

    def test_a_promoted_team_gets_no_borrowed_strength(self):
        rows = EMPTY_ROWS + [
            {"id": "99", "name": "Promoted Rovers", "hg": 0, "hs": 0, "ha": 0, "ag": 0, "as": 0, "aa": 0}
        ]
        fit = fit_league_from_standings(
            _standings(rows), league_id="L1", season="2025-2026", prior_fit=self.prior
        )
        promoted = next(t for t in fit.teams if t.team_name == "Promoted Rovers")

        self.assertEqual(promoted.prior_matches, 0)
        self.assertEqual(promoted.effective_matches, 0)
        self.assertEqual(promoted.home_attack, 1.0)

    def test_evidence_counts_the_prior_season(self):
        fit = fit_league_from_standings(
            _standings(EMPTY_ROWS), league_id="L1", season="2025-2026", prior_fit=self.prior
        )
        team = fit.teams[0]

        self.assertEqual(team.matches, 0)
        self.assertEqual(team.prior_matches, 38)
        self.assertEqual(team.effective_matches, 38)
        self.assertEqual(team.prior_season, "2024-2025")


class PriorSeasonSelectionTests(SimpleTestCase):
    def test_the_season_in_progress_is_never_used_as_its_own_prior(self):
        seasons = ["2023-2024", "2024-2025", "2025-2026", "2026-2027"]

        self.assertEqual(
            prior_season_candidates(seasons, "2026/2027"),
            ("2025-2026", "2024-2025", "2023-2024"),
        )

    def test_slash_and_dash_season_formats_are_treated_as_the_same_season(self):
        """The standings body says `2026/2027`; the seasons list says `2026-2027`."""
        self.assertNotIn("2026-2027", prior_season_candidates(["2025-2026", "2026-2027"], "2026/2027"))

    def test_a_league_with_no_history_yields_no_candidates(self):
        self.assertEqual(prior_season_candidates([], "2026/2027"), ())


class DifferentiationGateTests(TestCase):
    """Where the model cannot separate two teams, it must decline the result market."""

    def _model(self, *, home_matches, away_matches, prior_matches=0):
        model = LeagueScoreModel.objects.create(
            provider="statpal",
            league_id="L1",
            model_version=MODEL_VERSION,
            home_goal_baseline=1.45,
            away_goal_baseline=1.20,
            data_quality="medium",
        )
        for team_id, name, played in (("1", "Home FC", home_matches), ("2", "Away FC", away_matches)):
            TeamStrength.objects.create(
                model=model,
                team_id=team_id,
                team_name=name,
                team_name_normalized=name.lower(),
                home_attack=1.2,
                home_defence=0.9,
                away_attack=1.1,
                away_defence=0.95,
                matches=played,
                prior_matches=prior_matches,
            )
        return model

    def _rates(self):
        return score_model_service.rates_for_fixture(
            league_id="L1", home_team_name="Home FC", away_team_name="Away FC"
        )

    def test_thin_team_history_is_not_differentiated(self):
        self._model(home_matches=0, away_matches=0)

        rates = self._rates()

        self.assertTrue(rates.usable)
        self.assertFalse(rates.differentiated)

    def test_a_carried_prior_counts_toward_differentiation(self):
        self._model(home_matches=0, away_matches=0, prior_matches=38)

        rates = self._rates()

        self.assertTrue(rates.differentiated)
        self.assertEqual(rates.home_matches, 38)

    def test_one_thin_side_is_enough_to_block_it(self):
        self._model(home_matches=30, away_matches=1)

        self.assertFalse(self._rates().differentiated)

    def test_thin_team_history_caps_the_reported_quality(self):
        """A league-average fixture must not be sold as a medium-quality fit."""
        self._model(home_matches=0, away_matches=0)

        self.assertEqual(self._rates().data_quality, "limited")

    def test_a_result_market_declines_rather_than_publishing_a_league_average(self):
        self._model(home_matches=0, away_matches=0)

        result = score_matrix_evaluator.evaluate(
            describe_market("Home Win"), fixture=_fixture()
        )

        self.assertFalse(result["available"])
        self.assertIsNone(result["score"])
        self.assertEqual(result["basis"], "score_matrix_undifferentiated_teams")
        self.assertIn("insufficient_team_history", result["warnings"])
        self.assertEqual(result["evidence"]["required_team_matches"], MIN_TEAM_MATCHES_FOR_RESULT)

    def test_a_totals_market_still_runs_on_a_league_average(self):
        """A league-average total is a real estimate, just not a sharp one."""
        self._model(home_matches=0, away_matches=0)

        result = score_matrix_evaluator.evaluate(
            describe_market("Over 2.5"), fixture=_fixture()
        )

        self.assertTrue(result["available"])
        self.assertIn("league_average_team_strength", result["warnings"])

    def test_a_differentiated_fixture_produces_a_result_market(self):
        self._model(home_matches=20, away_matches=20)

        result = score_matrix_evaluator.evaluate(
            describe_market("Home Win"), fixture=_fixture()
        )

        self.assertTrue(result["available"])
        self.assertNotIn("league_average_team_strength", result["warnings"])

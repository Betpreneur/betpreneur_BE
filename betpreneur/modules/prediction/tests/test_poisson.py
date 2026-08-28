from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    FixtureFeatureSet,
    PredictionDiagnostics,
    TeamStrengthSnapshot,
    goal_distribution,
)


class PoissonGoalModelTests(SimpleTestCase):
    def _features(self, *, home_attack=1.8, home_defence=0.9, away_attack=1.1, away_defence=1.4):
        return FixtureFeatureSet(
            fixture_id="fixture-poisson",
            fixture_name="Home FC vs Away FC",
            league_key="england-premier-league",
            season="2026-2027",
            home_team=TeamStrengthSnapshot(
                team_id="home",
                team_name="Home FC",
                attack_rating=home_attack,
                defence_rating=home_defence,
                recent_form_score=2.0,
                data_quality="strong",
            ),
            away_team=TeamStrengthSnapshot(
                team_id="away",
                team_name="Away FC",
                attack_rating=away_attack,
                defence_rating=away_defence,
                recent_form_score=1.0,
                data_quality="medium",
            ),
            features={
                "goal_model": {
                    "home_expected_goals": 1.9,
                    "away_expected_goals": 0.9,
                    "home_baseline": 1.45,
                    "away_baseline": 1.15,
                    "data_quality": "medium",
                    "usable": True,
                },
                "home": {
                    "recent_form": {
                        "all": {"5": {"matches": 5, "goals_for": 11, "goals_for_per_match": 2.2}},
                        "home": {"5": {"matches": 5, "goals_for": 13, "goals_for_per_match": 2.6}},
                    }
                },
                "away": {
                    "recent_form": {
                        "all": {"5": {"matches": 5, "goals_for": 5, "goals_for_per_match": 1.0}},
                        "away": {"5": {"matches": 5, "goals_for": 4, "goals_for_per_match": 0.8}},
                    }
                },
                "league": {
                    "scoring_environment": {
                        "home_goal_baseline": 1.45,
                        "away_goal_baseline": 1.15,
                        "expected_total_goals": 2.6,
                    }
                },
            },
            diagnostics=PredictionDiagnostics(data_quality="medium"),
        )

    def test_goal_distribution_outputs_scoreline_and_goal_markets(self):
        output = goal_distribution(self._features())

        self.assertGreater(output.home_expected_goals, output.away_expected_goals)
        self.assertEqual(len(output.scoreline_matrix), 81)
        self.assertAlmostEqual(sum(output.scoreline_matrix.values()), 1.0, places=5)
        self.assertGreater(output.over_1_5_probability, output.over_2_5_probability)
        self.assertGreater(output.under_3_5_probability, 0)
        self.assertGreater(output.btts_probability, 0)
        self.assertIn("over_1_5", output.team_goal_probabilities["home"])
        self.assertIn("home_win", output.result_probabilities)
        self.assertGreater(output.result_probabilities["home_win"], output.result_probabilities["away_win"])
        self.assertNotIn("odds", output.diagnostics.metadata)

    def test_feature_derived_expected_goals_work_when_score_model_is_weak(self):
        features = self._features()
        payload = dict(features.features)
        payload["goal_model"] = {
            "home_expected_goals": 1.35,
            "away_expected_goals": 1.1,
            "home_baseline": 1.45,
            "away_baseline": 1.15,
            "data_quality": "poor",
            "usable": False,
        }
        features = FixtureFeatureSet(
            fixture_id=features.fixture_id,
            fixture_name=features.fixture_name,
            league_key=features.league_key,
            season=features.season,
            home_team=features.home_team,
            away_team=features.away_team,
            features=payload,
            diagnostics=features.diagnostics,
        )

        output = goal_distribution(features)

        self.assertGreater(output.home_expected_goals, 1.45)
        self.assertIn("using_feature_derived_expected_goals", output.diagnostics.warnings)
        self.assertEqual(output.diagnostics.data_quality, "limited")

    def test_low_total_expectation_reduces_over_goals(self):
        low = goal_distribution(self._features(home_attack=0.7, home_defence=0.8, away_attack=0.7, away_defence=0.8))
        high = goal_distribution(self._features(home_attack=2.2, home_defence=1.2, away_attack=1.9, away_defence=1.3))

        self.assertGreater(high.over_2_5_probability, low.over_2_5_probability)
        self.assertGreater(high.btts_probability, low.btts_probability)

    def test_contract_rejects_invalid_goal_probability_fields(self):
        with self.assertRaises(ValueError):
            goal_distribution(self._features()).__class__(
                over_1_5_probability=1.2,
            )

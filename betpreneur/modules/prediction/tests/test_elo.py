from dataclasses import replace

from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    FixtureFeatureSet,
    PredictionDiagnostics,
    TeamStrengthSnapshot,
    result_probabilities,
)


class EloRatingTests(SimpleTestCase):
    def _features(self, *, home_attack=1.7, home_defence=0.9, away_attack=1.2, away_defence=1.3):
        return FixtureFeatureSet(
            fixture_id="fixture-elo",
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
                "home": {
                    "season_profile": {"matches_played": 10},
                    "coverage": {"status": "fresh"},
                    "recent_form": {
                        "all": {"5": {"matches": 5, "wins": 3, "draws": 1}},
                        "home": {"5": {"matches": 5, "wins": 4, "draws": 0}},
                    },
                    "market_profiles_by_family": {},
                },
                "away": {
                    "season_profile": {"matches_played": 10},
                    "coverage": {"status": "fresh"},
                    "recent_form": {
                        "all": {"5": {"matches": 5, "wins": 1, "draws": 1}},
                        "away": {"5": {"matches": 5, "wins": 1, "draws": 2}},
                    },
                    "market_profiles_by_family": {},
                },
                "league": {
                    "market_profiles_by_family": {
                        "match_result": [
                            {"market": "Home Win", "hit_rate": 45},
                            {"market": "Draw", "hit_rate": 27},
                            {"market": "Away Win", "hit_rate": 28},
                        ]
                    }
                },
            },
            diagnostics=PredictionDiagnostics(data_quality="medium"),
        )

    def test_elo_outputs_team_strength_and_result_probabilities(self):
        result = result_probabilities(self._features())

        self.assertIsNotNone(result.home_elo)
        self.assertIsNotNone(result.away_elo)
        self.assertGreater(result.elo_gap, 0)
        self.assertGreater(result.home_result_probability, result.away_result_probability)
        self.assertEqual(result.home_result_probability, result.home_win)
        self.assertEqual(result.draw_probability, result.draw)
        self.assertEqual(result.away_result_probability, result.away_win)
        self.assertAlmostEqual(result.home_win + result.draw + result.away_win, 1.0, places=5)
        self.assertNotIn("odds", result.diagnostics.metadata)

    def test_away_quality_can_overcome_home_advantage(self):
        result = result_probabilities(
            self._features(home_attack=0.9, home_defence=1.8, away_attack=2.1, away_defence=0.7)
        )

        self.assertLess(result.elo_gap, 0)
        self.assertGreater(result.away_win, result.home_win)

    def test_draw_probability_shrinks_when_elo_gap_is_wide(self):
        close = result_probabilities(
            self._features(home_attack=1.25, home_defence=1.25, away_attack=1.25, away_defence=1.25)
        )
        wide = result_probabilities(self._features(home_attack=2.4, home_defence=0.5, away_attack=0.8, away_defence=2.0))

        self.assertLess(wide.draw_probability, close.draw_probability)

    def test_weak_or_missing_evidence_decays_rating_toward_baseline(self):
        base = self._features()
        payload = dict(base.features)
        payload["home"] = {
            **payload["home"],
            "season_profile": {"matches_played": 0},
            "coverage": {"status": "missing"},
        }
        features = replace(
            base,
            home_team=TeamStrengthSnapshot(
                team_id="home",
                team_name="Home FC",
                attack_rating=2.4,
                defence_rating=0.4,
                recent_form_score=None,
                data_quality="missing",
            ),
            features=payload,
        )

        result = result_probabilities(features)

        self.assertLess(result.home_elo, 1550)
        self.assertIn("home_weak_or_missing_quality", result.diagnostics.warnings)
        self.assertIn("home_no_current_match_evidence", result.diagnostics.warnings)

    def test_missing_team_identity_returns_unavailable_result(self):
        result = result_probabilities(
            FixtureFeatureSet(
                fixture_id="fixture-elo",
                diagnostics=PredictionDiagnostics(data_quality="missing"),
            )
        )

        self.assertIsNone(result.home_win)
        self.assertIn("missing_team_strength", result.diagnostics.warnings)

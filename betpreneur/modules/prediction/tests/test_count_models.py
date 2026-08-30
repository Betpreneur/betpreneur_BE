from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    CountModelOutput,
    FixtureFeatureSet,
    PredictionDiagnostics,
    TeamStrengthSnapshot,
    count_distributions,
    predict_fixture,
)


class CountModelTests(SimpleTestCase):
    def _features(self, *, home_corners=6.2, away_corners=4.4, home_cards=1.7, away_cards=2.3):
        return FixtureFeatureSet(
            fixture_id="fixture-counts",
            fixture_name="Home FC vs Away FC",
            league_key="england-premier-league",
            season="2026-2027",
            home_team=TeamStrengthSnapshot(team_id="home", team_name="Home FC", data_quality="strong"),
            away_team=TeamStrengthSnapshot(team_id="away", team_name="Away FC", data_quality="medium"),
            features={
                "referee": {
                    "available": True,
                    "name": "Michael Salisbury",
                    "sample_matches": 12,
                    "avg_cards_per_match": 4.8,
                },
                "home": {
                    "rate_profile": {
                        "available": True,
                        "corners_home": home_corners,
                        "cards_home": home_cards,
                        "shots_on_target_home": 6.1,
                        "matches": 10,
                    },
                    "season_profile": {
                        "matches_played": 10,
                        "corners_for": 58,
                        "cards_for": 18,
                        "shots_on_target_for": 57,
                    },
                    "recent_form": {
                        "home": {
                            "5": {
                                "matches": 5,
                                "corners_for": 34,
                                "corners_for_per_match": 6.8,
                                "cards_for_per_match": 1.4,
                                "shots_on_target_for_per_match": 6.4,
                            }
                        }
                    },
                },
                "away": {
                    "rate_profile": {
                        "available": True,
                        "corners_away": away_corners,
                        "cards_away": away_cards,
                        "shots_on_target_away": 3.8,
                        "matches": 10,
                    },
                    "season_profile": {
                        "matches_played": 10,
                        "corners_against": 52,
                        "cards_against": 17,
                        "shots_on_target_against": 54,
                    },
                    "recent_form": {
                        "away": {
                            "5": {
                                "matches": 5,
                                "corners_for": 23,
                                "corners_for_per_match": 4.6,
                                "cards_for_per_match": 2.5,
                                "shots_on_target_for_per_match": 3.5,
                            }
                        }
                    },
                },
            },
            diagnostics=PredictionDiagnostics(data_quality="medium"),
        )

    def test_count_models_output_practical_sportybet_lines(self):
        output = count_distributions(self._features())

        self.assertIsInstance(output, CountModelOutput)
        self.assertGreater(output.expected_total_corners, 9)
        self.assertGreater(output.expected_total_cards, 3)
        self.assertGreater(output.expected_total_sot, 8)
        self.assertIn("over_7_5", output.line_probabilities["corners"])
        self.assertIn("over_8_5", output.line_probabilities["corners"])
        self.assertIn("over_3_5", output.line_probabilities["cards"])
        self.assertIn("over_7_5", output.line_probabilities["sot"])
        self.assertIn("over_2_5", output.team_line_probabilities["corners"]["home"])
        self.assertIn("over_2_5", output.team_line_probabilities["corners"]["away"])
        self.assertIn("referee_card_profile", output.diagnostics.metadata["sources"]["cards"])
        self.assertNotIn("odds", output.diagnostics.metadata)

    def test_referee_card_history_changes_card_expectation_only(self):
        lenient = self._features(home_cards=1.7, away_cards=2.3)
        strict = FixtureFeatureSet(
            fixture_id=lenient.fixture_id,
            fixture_name=lenient.fixture_name,
            league_key=lenient.league_key,
            season=lenient.season,
            home_team=lenient.home_team,
            away_team=lenient.away_team,
            features={
                **lenient.features,
                "referee": {
                    "available": True,
                    "name": "Strict Referee",
                    "sample_matches": 15,
                    "avg_cards_per_match": 6.8,
                },
            },
            diagnostics=lenient.diagnostics,
        )

        lenient_output = count_distributions(lenient)
        strict_output = count_distributions(strict)

        self.assertGreater(strict_output.expected_total_cards, lenient_output.expected_total_cards)
        self.assertEqual(strict_output.expected_total_corners, lenient_output.expected_total_corners)
        self.assertEqual(strict_output.expected_total_sot, lenient_output.expected_total_sot)

    def test_stronger_corner_rates_raise_corner_probabilities(self):
        low = count_distributions(self._features(home_corners=3.0, away_corners=2.8))
        high = count_distributions(self._features(home_corners=7.0, away_corners=6.0))

        self.assertGreater(high.expected_total_corners, low.expected_total_corners)
        self.assertGreater(
            high.line_probabilities["corners"]["over_8_5"],
            low.line_probabilities["corners"]["over_8_5"],
        )

    def test_unrealistic_count_rates_are_ignored(self):
        output = count_distributions(self._features(home_corners=15.35, away_corners=13.05))

        self.assertLess(output.expected_total_corners, 12)
        self.assertNotIn(
            "team_rate_profile",
            output.diagnostics.metadata["sources"]["corners"],
        )

    def test_missing_event_data_falls_back_with_warnings(self):
        features = self._features()
        features = FixtureFeatureSet(
            fixture_id=features.fixture_id,
            fixture_name=features.fixture_name,
            league_key=features.league_key,
            season=features.season,
            home_team=features.home_team,
            away_team=features.away_team,
            features={"home": {}, "away": {}},
            diagnostics=features.diagnostics,
        )

        output = count_distributions(features)

        self.assertEqual(output.diagnostics.data_quality, "poor")
        self.assertIn("home_corners_using_league_average", output.diagnostics.warnings)
        self.assertIn("away_sot_using_league_average", output.diagnostics.warnings)

    def test_predict_fixture_includes_count_output(self):
        prediction = predict_fixture("fixture-counts", fixture=self._features())

        self.assertIsInstance(prediction.counts, CountModelOutput)

from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    CountModelOutput,
    FixtureFeatureSet,
    FixturePrediction,
    GoalModelOutput,
    PredictionDiagnostics,
    ResultProbabilityOutput,
    TeamStrengthSnapshot,
    evaluate_market,
    evaluate_market_probability,
)


class MarketProbabilityEngineTests(SimpleTestCase):
    def _prediction(self):
        goals = GoalModelOutput(
            home_expected_goals=1.8,
            away_expected_goals=1.3,
            scoreline_matrix={
                "0-0": 0.06,
                "1-0": 0.13,
                "0-1": 0.09,
                "1-1": 0.13,
                "2-0": 0.11,
                "0-2": 0.05,
                "2-1": 0.16,
                "1-2": 0.08,
                "2-2": 0.07,
                "3-0": 0.05,
                "0-3": 0.02,
                "3-1": 0.05,
            },
            over_1_5_probability=0.72,
            over_2_5_probability=0.48,
            under_3_5_probability=0.68,
            btts_probability=0.44,
            team_goal_probabilities={
                "home": {"over_0_5": 0.8, "over_1_5": 0.44, "over_2_5": 0.1},
                "away": {"over_0_5": 0.7, "over_1_5": 0.22, "over_2_5": 0.02},
            },
            diagnostics=PredictionDiagnostics(data_quality="medium"),
        )
        result = ResultProbabilityOutput(
            home_win=0.46,
            draw=0.26,
            away_win=0.28,
            home_elo=1540,
            away_elo=1490,
            elo_gap=115,
            diagnostics=PredictionDiagnostics(data_quality="medium"),
        )
        counts = CountModelOutput(
            expected_total_corners=10.2,
            expected_total_cards=4.1,
            expected_total_sot=9.7,
            line_probabilities={
                "corners": {"over_7_5": 0.78, "over_8_5": 0.68, "over_9_5": 0.55},
                "cards": {"over_3_5": 0.61},
                "sot": {"over_7_5": 0.73},
            },
            team_line_probabilities={
                "corners": {
                    "home": {"over_2_5": 0.84},
                    "away": {"over_2_5": 0.76},
                },
                "cards": {
                    "home": {"over_1_5": 0.55},
                    "away": {"over_1_5": 0.62},
                },
                "sot": {
                    "home": {"over_2_5": 0.81},
                    "away": {"over_2_5": 0.57},
                },
            },
            expected_team_counts={
                "corners": {"home": 5.8, "away": 4.4},
                "cards": {"home": 1.8, "away": 2.3},
                "sot": {"home": 5.6, "away": 4.1},
            },
            diagnostics=PredictionDiagnostics(data_quality="medium"),
        )
        return FixturePrediction(
            fixture_id="fixture-market",
            fixture_name="Home FC vs Away FC",
            features=FixtureFeatureSet(
                fixture_id="fixture-market",
                home_team=TeamStrengthSnapshot(team_id="home", team_name="Home FC"),
                away_team=TeamStrengthSnapshot(team_id="away", team_name="Away FC"),
                features={
                    "referee": {
                        "available": True,
                        "name": "Michael Salisbury",
                        "sample_matches": 12,
                        "avg_cards_per_match": 4.8,
                    }
                },
            ),
            goals=goals,
            counts=counts,
            result=result,
            diagnostics=PredictionDiagnostics(data_quality="medium"),
        )

    def test_total_goals_market_uses_poisson_goals(self):
        probability = evaluate_market_probability(self._prediction(), "Over 2.5")

        self.assertEqual(probability.market, "Over 2.5")
        self.assertEqual(probability.raw_probability, 0.48)
        self.assertEqual(probability.model, "poisson_goals")
        self.assertIn("Projected total goals: 3.10.", probability.supporting_facts)
        self.assertIn("Home average: 1.80 xG.", probability.supporting_facts)
        self.assertIn(
            "Line 2.5 is below the model projection of 3.10 goals.",
            probability.supporting_facts,
        )

    def test_result_market_uses_elo(self):
        probability = evaluate_market_probability(self._prediction(), "Home Win")

        self.assertEqual(probability.raw_probability, 0.46)
        self.assertEqual(probability.model, "elo_result")
        self.assertIn("Home win probability: 46%.", probability.supporting_facts)
        self.assertIn("Draw probability: 26%.", probability.supporting_facts)
        self.assertIn(
            "Elo gap after home advantage: 115, supporting home.",
            probability.supporting_facts,
        )

    def test_double_chance_and_dnb_use_elo_result_probabilities(self):
        prediction = self._prediction()
        double_chance = evaluate_market_probability(prediction, "DC: 1X")
        dnb = evaluate_market_probability(prediction, "DNB Home")

        self.assertEqual(double_chance.model, "elo_result")
        self.assertEqual(dnb.model, "elo_result")
        self.assertAlmostEqual(double_chance.raw_probability, 0.72)
        self.assertAlmostEqual(dnb.raw_probability, 0.621622)
        self.assertIn("Home win probability: 46%.", double_chance.supporting_facts)
        self.assertIn("Draw probability: 26%.", dnb.supporting_facts)
        self.assertIn(
            "Elo gap after home advantage: 115, supporting home.",
            dnb.supporting_facts,
        )

    def test_double_chance_and_dnb_fall_back_to_score_matrix_without_elo(self):
        prediction = self._prediction()
        prediction = FixturePrediction(
            fixture_id=prediction.fixture_id,
            fixture_name=prediction.fixture_name,
            features=prediction.features,
            goals=prediction.goals,
            counts=prediction.counts,
            result=ResultProbabilityOutput(
                diagnostics=PredictionDiagnostics(data_quality="unavailable")
            ),
            diagnostics=prediction.diagnostics,
        )

        double_chance = evaluate_market_probability(prediction, "DC: 1X")
        dnb = evaluate_market_probability(prediction, "DNB Home")

        self.assertEqual(double_chance.model, "poisson_goals")
        self.assertAlmostEqual(double_chance.raw_probability, 0.76)
        self.assertAlmostEqual(dnb.raw_probability, 0.675676)
        self.assertIn("Home win probability: 50%.", double_chance.supporting_facts)
        self.assertIn("Draw probability: 26%.", dnb.supporting_facts)
        self.assertIn(
            "Result probabilities are derived from the scoreline distribution.",
            dnb.supporting_facts,
        )
        self.assertIn("Projected total goals: 3.10.", dnb.supporting_facts)

    def test_common_asian_handicap_lines_use_elo_when_result_equivalent(self):
        prediction = self._prediction()

        home_plus_half = evaluate_market_probability(prediction, "AH Home +0.5")
        away_minus_half = evaluate_market_probability(prediction, "AH Away -0.5")

        self.assertEqual(home_plus_half.model, "elo_result")
        self.assertEqual(away_minus_half.model, "elo_result")
        self.assertAlmostEqual(home_plus_half.raw_probability, 0.72)
        self.assertAlmostEqual(away_minus_half.raw_probability, 0.28)

    def test_team_goals_and_btts_use_goal_distribution(self):
        prediction = self._prediction()
        team_goals = evaluate_market_probability(prediction, "Home Team Over 1.5")
        btts = evaluate_market_probability(prediction, "GG / BTTS Yes")

        self.assertEqual(team_goals.raw_probability, 0.44)
        self.assertEqual(btts.raw_probability, 0.44)

    def test_count_markets_use_count_models(self):
        prediction = self._prediction()
        corners = evaluate_market_probability(prediction, "Corners Over 7.5")
        home_corners = evaluate_market_probability(prediction, "Home Corners Over 2.5")
        cards = evaluate_market_probability(prediction, "Cards Over 3.5")
        sot = evaluate_market_probability(prediction, "Shots On Target Over 7.5")

        self.assertEqual(corners.raw_probability, 0.78)
        self.assertEqual(home_corners.raw_probability, 0.84)
        self.assertEqual(cards.raw_probability, 0.61)
        self.assertEqual(sot.raw_probability, 0.73)
        self.assertEqual(corners.model, "poisson_counts")
        self.assertIn("Projected corners: 10.20.", corners.supporting_facts)
        self.assertIn(
            "Michael Salisbury averages 4.80 cards across 12 tracked matches.",
            cards.supporting_facts,
        )
        self.assertIn(
            "Line 7.5 is below the model projection of 10.20 corners.",
            corners.supporting_facts,
        )
        self.assertIn(
            "Line 2.5 is below the home team projection of 5.80 corners.",
            home_corners.supporting_facts,
        )

    def test_borderline_goal_unders_are_flagged_as_risky(self):
        prediction = self._prediction()
        prediction = FixturePrediction(
            fixture_id=prediction.fixture_id,
            fixture_name=prediction.fixture_name,
            features=prediction.features,
            goals=GoalModelOutput(
                home_expected_goals=1.1,
                away_expected_goals=1.06,
                scoreline_matrix=prediction.goals.scoreline_matrix,
                diagnostics=prediction.goals.diagnostics,
            ),
            counts=prediction.counts,
            result=prediction.result,
            diagnostics=prediction.diagnostics,
        )

        probability = evaluate_market_probability(prediction, "Under 2.5")

        self.assertIn("goal_line_boundary", probability.warnings)
        self.assertIn("under25_goal_volatility", probability.warnings)

    def test_high_projected_under45_is_flagged_as_volatile(self):
        prediction = self._prediction()
        prediction = FixturePrediction(
            fixture_id=prediction.fixture_id,
            fixture_name=prediction.fixture_name,
            features=prediction.features,
            goals=GoalModelOutput(
                home_expected_goals=1.35,
                away_expected_goals=2.28,
                scoreline_matrix=prediction.goals.scoreline_matrix,
                diagnostics=prediction.goals.diagnostics,
            ),
            counts=prediction.counts,
            result=prediction.result,
            diagnostics=prediction.diagnostics,
        )

        probability = evaluate_market_probability(prediction, "Under 4.5")

        self.assertIn("under45_high_goal_volatility", probability.warnings)

    def test_bundesliga_under_goal_markets_are_blocked(self):
        prediction = self._prediction()
        prediction = FixturePrediction(
            fixture_id=prediction.fixture_id,
            fixture_name=prediction.fixture_name,
            features=FixtureFeatureSet(
                fixture_id=prediction.features.fixture_id,
                fixture_name=prediction.features.fixture_name,
                league_key="germany-bundesliga",
                home_team=prediction.features.home_team,
                away_team=prediction.features.away_team,
                features={
                    **prediction.features.features,
                    "league": {"league_name": "Bundesliga", "league_key": "germany-bundesliga"},
                    "fixture": {"league": "Bundesliga", "country": "Germany"},
                },
                diagnostics=prediction.features.diagnostics,
            ),
            goals=prediction.goals,
            counts=prediction.counts,
            result=prediction.result,
            diagnostics=prediction.diagnostics,
        )

        total_under = evaluate_market_probability(prediction, "Under 3.5")
        team_under = evaluate_market_probability(prediction, "Home Team Under 1.5")

        self.assertIn("german_under_goals_market_blocked", total_under.warnings)
        self.assertIn("german_under_goals_market_blocked", team_under.warnings)

    def test_borderline_corner_lines_are_flagged_as_risky(self):
        probability = evaluate_market_probability(self._prediction(), "Corners Over 9.5")

        self.assertEqual(probability.raw_probability, 0.55)
        self.assertIn("corner_line_boundary", probability.warnings)
        self.assertIn("corner_over_margin_risk", probability.warnings)

    def test_early_payout_uses_score_matrix_and_increases_home_win_probability(self):
        prediction = self._prediction()
        normal = evaluate_market_probability(prediction, "Home Win")
        early = evaluate_market_probability(prediction, "Home Win 1UP")

        self.assertEqual(early.model, "poisson_goals")
        self.assertGreater(early.raw_probability, normal.raw_probability)
        self.assertEqual(early.diagnostics.metadata["early_payout"], "1UP")

    def test_evaluate_market_accepts_prediction_input(self):
        probability = evaluate_market(self._prediction(), "Corners Over 8.5")

        self.assertEqual(probability.raw_probability, 0.68)

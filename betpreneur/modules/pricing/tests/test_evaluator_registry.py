"""
Evaluator dispatch, capability vocabulary, and the assessment-type invariant.

The rule under test: the market *family* selects the evaluator. A data-requirement flag
must never influence dispatch — that is what sent `First to Score H`, a team market,
into the player-props model.
"""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from betpreneur.modules.markets.api import (
    HEURISTIC,
    NONE,
    QUANTITATIVE,
    DataCapability,
    assessment_type_for,
    capabilities_from_snapshots,
    coverage,
    describe_market,
    evaluator_for,
    missing,
    modelled_families,
    required_capabilities,
    snapshots_for_capabilities,
)
from betpreneur.modules.pricing.services.advisory import statpal_market_advisory
from betpreneur.modules.scoring.api import capability_for_descriptor


class AssessmentTypeTests(SimpleTestCase):
    def test_modelled_goal_markets_are_quantitative(self):
        for family in ["total_goals", "team_total_goals", "corners_total", "cards_total"]:
            self.assertEqual(assessment_type_for(family), QUANTITATIVE, family)

    def test_result_families_are_quantitative_since_the_score_matrix_landed(self):
        # These were heuristic (a constant plus nudges) until ADR-001 shipped.
        for family in [
            "match_result", "double_chance", "draw_no_bet", "btts", "clean_sheet",
            "result_total_goals", "result_btts", "total_btts",
            "double_chance_btts", "double_chance_total_goals",
            "result_or_total_goals", "result_or_btts", "result_or_clean_sheet",
        ]:
            self.assertEqual(assessment_type_for(family), QUANTITATIVE, family)

    def test_unmodelled_families_report_no_model(self):
        for family in ["correct_score", "exact_goals", "winning_margin", "last_to_score"]:
            self.assertEqual(assessment_type_for(family), NONE, family)

    def test_only_quantitative_evaluators_may_publish_a_probability(self):
        for family, spec in [(f, evaluator_for(f)) for f in modelled_families()]:
            self.assertTrue(spec.publishes_probability, family)
        self.assertIsNone(evaluator_for("correct_score"))

    def test_a_heuristic_entry_would_not_publish_a_probability(self):
        # No family is heuristic today; the invariant must still hold if one returns.
        from betpreneur.modules.markets.api import EvaluatorSpec

        spec = EvaluatorSpec("demo", "_handler", HEURISTIC)

        self.assertFalse(spec.publishes_probability)


class DispatchTests(TestCase):
    """These touch the DB only through the player-mapping lookup, hence TestCase."""

    def _evaluate(self, market):
        return statpal_market_advisory.evaluate_market(describe_market(market), fixture={})

    def test_first_to_score_is_a_team_market_not_a_player_prop(self):
        descriptor = describe_market("First to Score H")

        self.assertEqual(descriptor.family, "first_to_score")
        self.assertEqual(descriptor.side, "home")
        self.assertFalse(descriptor.requires_player_stats)

    def test_first_to_score_reaches_the_score_matrix_not_the_player_model(self):
        result = self._evaluate("First to Score H")

        self.assertIn(result["basis"], {"score_matrix", "score_matrix_no_fit"})

    def test_away_first_to_score_resolves_its_side(self):
        self.assertEqual(describe_market("First to Score A").side, "away")

    def test_goal_market_is_derived_from_the_score_matrix(self):
        result = self._evaluate("Over 2.5")

        self.assertEqual(result["assessment_type"], QUANTITATIVE)
        self.assertIn(result["basis"], {"score_matrix", "score_matrix_no_fit"})

    def test_result_market_declines_when_no_fit_exists_rather_than_defaulting(self):
        # With no fitted league model, a placeholder number would be indistinguishable
        # from a modelled one, so the evaluator must decline instead.
        result = self._evaluate("Home Win")

        self.assertFalse(result["available"])
        self.assertEqual(result["basis"], "score_matrix_no_fit")

    def test_unmodelled_family_is_reported_rather_than_approximated(self):
        result = self._evaluate("Correct Score 2-1")

        self.assertEqual(result["assessment_type"], NONE)
        self.assertEqual(result["basis"], "no_model_for_family")
        self.assertFalse(result["available"])

    def test_team_shots_on_target_is_count_model_not_player_routed(self):
        descriptor = describe_market("Home Team Shots on Target Over 9.5")
        fixture = {
            "statpal_context": {
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_shots_on_target": 7,
                            "away_shots_on_target": 4,
                        }
                    }
                }
            }
        }
        result = statpal_market_advisory.evaluate_market(descriptor, fixture=fixture)

        self.assertEqual(result["assessment_type"], QUANTITATIVE)
        self.assertEqual(result["basis"], "shots_on_target_count_model")
        self.assertEqual(result["market_family"], "team_shots_on_target")
        self.assertTrue(result["available"])

    def test_every_result_states_its_family(self):
        self.assertEqual(self._evaluate("Over 2.5")["market_family"], "total_goals")


class CapabilityVocabularyTests(SimpleTestCase):
    def test_team_stats_snapshot_supplies_the_goal_model_inputs(self):
        available = capabilities_from_snapshots(["team_stats"])

        self.assertIn(DataCapability.TEAM_GOALS_FOR, available)
        self.assertIn(DataCapability.TEAM_GOALS_AGAINST, available)
        self.assertIn(DataCapability.TEAM_CORNERS, available)

    def test_referee_comes_from_detailed_stats(self):
        self.assertIn(DataCapability.REFEREE, capabilities_from_snapshots(["detailed_stats"]))

    def test_predictions_snapshot_supplies_no_analytical_capability(self):
        # StatPal predictions is a single tip string, not probabilities.
        self.assertEqual(capabilities_from_snapshots(["predictions"]), set())

    def test_coverage_is_a_set_operation_not_a_string_comparison(self):
        required = [DataCapability.TEAM_GOALS_FOR, DataCapability.TEAM_SHOTS]

        self.assertEqual(coverage(required, [DataCapability.TEAM_GOALS_FOR]), 50.0)
        self.assertEqual(coverage(required, required), 100.0)
        self.assertEqual(coverage([], []), 100.0)

    def test_missing_capabilities_are_reported(self):
        gaps = missing([DataCapability.TEAM_CARDS, DataCapability.REFEREE], [DataCapability.TEAM_CARDS])

        self.assertEqual(gaps, [DataCapability.REFEREE])

    def test_snapshots_needed_for_a_capability_set(self):
        needed = snapshots_for_capabilities([DataCapability.MARKET_ODDS, DataCapability.INJURIES])

        self.assertIn("prematch_odds", needed)
        self.assertIn("injuries_suspensions", needed)


class CapabilityPlannerTests(SimpleTestCase):
    def test_planner_unions_requirements_across_a_fixtures_legs(self):
        needed = required_capabilities(["total_goals", "cards_total"])

        self.assertIn(DataCapability.TEAM_GOALS_FOR, needed)
        self.assertIn(DataCapability.TEAM_CARDS, needed)

    def test_planner_ignores_families_with_no_evaluator(self):
        self.assertEqual(required_capabilities(["correct_score"]), set())

    def test_planner_is_empty_for_no_legs(self):
        self.assertEqual(required_capabilities([]), set())

    def test_player_markets_request_lineups(self):
        needed = required_capabilities(["player_goal"])

        self.assertIn(DataCapability.LINEUP_CONFIRMED, needed)
        self.assertIn(DataCapability.PLAYER_SEASON_STATS, needed)

    def test_count_market_capability_accepts_detailed_stats_snapshot_fallback(self):
        context = {
            "snapshots": {
                "detailed_stats": {
                    "summary": {
                        "home_corners": 6,
                        "away_corners": 5,
                    }
                }
            }
        }
        with patch("betpreneur.modules.scoring.services.rate_profiles.team_rate_profile_service.profile_for", return_value=None):
            capability = capability_for_descriptor(describe_market("Corners Over 9.5"), fixture={}, statpal_context=context)

        self.assertTrue(capability["scoreable"])
        self.assertEqual(capability["data_quality"], "limited")

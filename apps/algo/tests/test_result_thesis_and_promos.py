"""
Three defects from one live 21-leg slip.

* Three fixtures were told to swap `DC: 1X` for `DC: X2` -- to back the other team. The
  thesis guard only inspected `match_result` selections, so any double-chance pick was
  unguarded.
* Every pick in that slip was a SportyBet `1UP`/`2UP` early-payout market. Those are
  scored with the underlying result model, which is a *floor*: the bet also wins in games
  the side led and failed to see out. Recommending a replacement by comparing against a
  floor cannot be sound.
* One fixture reported "expected goals: home 2.17, away 8.0" and scored a leg from it.
  The fitted model bounds its rates; the snapshot path did not.
"""

from django.test import SimpleTestCase

from apps.algo.market_taxonomy import describe_market
from apps.algo.statpal_advisory import (
    MAX_PLAUSIBLE_TEAM_EXPECTED_GOALS,
    statpal_market_advisory as advisory,
)
from apps.algo.views import (
    _is_early_payout_market,
    _replacement_is_meaningfully_better,
    _result_replacement_preserves_user_thesis,
    _result_thesis_side,
)


def _m(name):
    return {"market": name, "market_taxonomy": describe_market(name).to_dict()}


class ThesisSideTests(SimpleTestCase):
    def test_each_result_market_reports_the_team_it_backs(self):
        for name, side in (
            ("Home Win", "home"),
            ("Away Win", "away"),
            ("Draw", "draw"),
            ("DC: 1X", "home"),
            ("DC: X2", "away"),
            ("DNB Home", "home"),
            ("DNB Away", "away"),
        ):
            with self.subTest(market=name):
                self.assertEqual(_result_thesis_side(_m(name)), side)

    def test_a_market_backing_both_sides_has_no_direction(self):
        """`DC: 12` wins if either side wins, so it cannot stand in for one of them."""
        self.assertEqual(_result_thesis_side(_m("DC: 12")), "")


class ThesisPreservationTests(SimpleTestCase):
    def test_a_double_chance_is_not_flipped_to_the_other_side(self):
        """The live bug: 1X backed home-or-draw, X2 was recommended."""
        self.assertFalse(_result_replacement_preserves_user_thesis(_m("DC: 1X"), _m("DC: X2")))
        self.assertFalse(_result_replacement_preserves_user_thesis(_m("DC: X2"), _m("DC: 1X")))

    def test_a_draw_no_bet_is_not_flipped_either(self):
        self.assertFalse(_result_replacement_preserves_user_thesis(_m("DNB Home"), _m("DC: X2")))

    def test_a_double_chance_may_move_to_a_market_on_the_same_side(self):
        self.assertTrue(_result_replacement_preserves_user_thesis(_m("DC: 1X"), _m("DNB Home")))

    def test_match_result_behaviour_is_unchanged(self):
        self.assertTrue(_result_replacement_preserves_user_thesis(_m("Home Win"), _m("DC: 1X")))
        self.assertTrue(_result_replacement_preserves_user_thesis(_m("Home Win"), _m("AH Home +0.5")))
        self.assertFalse(_result_replacement_preserves_user_thesis(_m("Home Win"), _m("DC: X2")))
        self.assertFalse(_result_replacement_preserves_user_thesis(_m("Home Win"), _m("DC: 12")))

    def test_a_draw_pick_only_moves_to_another_draw(self):
        self.assertFalse(_result_replacement_preserves_user_thesis(_m("Draw"), _m("DC: 1X")))

    def test_a_directionless_pick_only_keeps_its_own_market(self):
        self.assertFalse(_result_replacement_preserves_user_thesis(_m("DC: 12"), _m("DC: 1X")))


class EarlyPayoutTests(SimpleTestCase):
    def test_the_promo_modifier_survives_classification(self):
        for name, modifier in (
            ("Home Win 1UP", "1UP"),
            ("Home Win 2UP", "2UP"),
            ("Away Win 1UP", "1UP"),
            ("DC: 1X 1UP", "1UP"),
        ):
            with self.subTest(market=name):
                self.assertEqual(describe_market(name).early_payout, modifier)

    def test_a_plain_market_carries_no_modifier(self):
        self.assertEqual(describe_market("Home Win").early_payout, "")

    def test_the_underlying_family_is_still_used_for_scoring(self):
        """The result model is the right model; its number is just a lower bound."""
        self.assertEqual(describe_market("Home Win 1UP").family, "match_result")
        self.assertEqual(describe_market("DC: 1X 1UP").family, "double_chance")

    def test_an_early_payout_pick_is_recognised(self):
        self.assertTrue(_is_early_payout_market(_m("Home Win 1UP")))
        self.assertFalse(_is_early_payout_market(_m("Home Win")))

    def test_an_early_payout_pick_is_not_swapped_on_an_understated_score(self):
        selected = {**_m("Home Win 1UP"), "advisory_score": 63, "odds": 1.26}
        replacement = {**_m("DNB Home"), "advisory_score": 88, "odds": 1.125}

        self.assertFalse(_replacement_is_meaningfully_better(selected, replacement))


class ExpectedGoalSanityTests(SimpleTestCase):
    def _fixture(self, home, away):
        return {
            "statpal_context": {
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "teams": [
                                {"fixture_side": "home", "sample_size": 12, "avg_goals_for": home, "avg_goals_against": 1.0},
                                {"fixture_side": "away", "sample_size": 12, "avg_goals_for": away, "avg_goals_against": 1.0},
                            ]
                        }
                    }
                }
            }
        }

    def test_a_normal_fixture_passes_through(self):
        home, away, _evidence, warnings = advisory._expected_score_rates(self._fixture(1.4, 1.24))

        self.assertEqual((home, away), (1.4, 1.24))
        self.assertEqual(warnings, [])

    def test_an_impossible_rate_is_refused_not_clamped(self):
        """Clamping 8.0 to a ceiling would still model a match nobody is playing."""
        home, away, evidence, warnings = advisory._expected_score_rates(self._fixture(2.17, 8.0))

        self.assertEqual((home, away), (0.0, 0.0))
        self.assertIn("implausible_expected_goals", warnings)
        self.assertEqual(evidence["implausible_expected_goals"]["away"], 8.0)

    def test_the_ceiling_still_allows_an_extreme_but_real_favourite(self):
        home, away, _evidence, warnings = advisory._expected_score_rates(self._fixture(3.8, 0.6))

        self.assertEqual(home, 3.8)
        self.assertNotIn("implausible_expected_goals", warnings)
        self.assertLess(home, MAX_PLAUSIBLE_TEAM_EXPECTED_GOALS)

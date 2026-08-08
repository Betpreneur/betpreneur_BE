"""
Regression tests for SportyBet market identity resolution.

Every case here is a real (marketId, specifier, outcomeId) triple observed on a booked
slip, paired with the market the bookmaker says it is. The old text-based path got 42
of 54 of these wrong.
"""

from django.test import SimpleTestCase

from apps.algo.normalize.canonical import Period, Resolution, Settlement, Subject
from apps.algo.normalize.sportybet import parse_specifier, resolve


class SpecifierGrammarTests(SimpleTestCase):
    def test_simple_and_compound_specifiers(self):
        self.assertEqual(parse_specifier("total=2.5"), {"total": "2.5"})
        self.assertEqual(
            parse_specifier("minsnr=10|total=1.5"), {"minsnr": "10", "total": "1.5"}
        )
        self.assertEqual(parse_specifier(""), {})

    def test_player_variant_specifier(self):
        parsed = parse_specifier("variant=pre:playerprops:72041034:35208")
        self.assertEqual(parsed["variant"], "pre:playerprops:72041034:35208")


class LineAndSignTests(SimpleTestCase):
    def test_asian_handicap_keeps_its_negative_sign(self):
        market = resolve(market_id="16", outcome_id="1714", specifier="hcp=-3")

        self.assertEqual(market.family, "asian_handicap")
        self.assertEqual(market.line, -3.0)

    def test_european_goal_start_is_normalised_to_the_home_perspective(self):
        # hcp=0:3 gives the away side a three-goal start, i.e. home -3.
        market = resolve(market_id="14", outcome_id="1711", specifier="hcp=0:3")

        self.assertEqual(market.family, "handicap")
        self.assertEqual(market.line, -3.0)

    def test_half_line_wins_or_loses_and_whole_line_can_push(self):
        half = resolve(market_id="18", outcome_id="12", specifier="total=2.5")
        whole = resolve(market_id="18", outcome_id="12", specifier="total=2")

        self.assertEqual(half.settlement, Settlement.WIN_LOSE)
        self.assertEqual(whole.settlement, Settlement.WIN_LOSE_VOID)

    def test_quarter_line_splits_the_stake(self):
        market = resolve(market_id="16", outcome_id="1714", specifier="hcp=-0.75")

        self.assertEqual(market.settlement, Settlement.ASIAN_QUARTER)


class MarketIdentityTests(SimpleTestCase):
    def test_match_total_and_team_total_are_different_markets(self):
        match_total = resolve(market_id="18", outcome_id="12", specifier="total=2.5")
        home_total = resolve(market_id="19", outcome_id="12", specifier="total=2.5")

        self.assertEqual(match_total.family, "total_goals")
        self.assertEqual(match_total.subject, Subject.MATCH)
        self.assertEqual(home_total.family, "team_total_goals")
        self.assertEqual(home_total.subject, Subject.HOME)

    def test_shots_on_target_is_a_team_market_not_a_player_prop(self):
        market = resolve(market_id="900546", outcome_id="12", specifier="total=9.5")

        self.assertEqual(market.family, "team_shots_on_target")
        self.assertEqual(market.subject, Subject.HOME)

    def test_bookings_1x2_is_not_the_match_result(self):
        market = resolve(market_id="136", outcome_id="2")

        self.assertEqual(market.family, "cards_result")
        self.assertEqual(market.side, "draw")

    def test_nth_goal_is_not_the_match_result(self):
        market = resolve(market_id="8", outcome_id="6", specifier="goalnr=1")

        self.assertEqual(market.family, "nth_goal")
        self.assertEqual(market.side, "home")
        self.assertEqual(market.goal_number, 1)

    def test_btts_is_recognised(self):
        yes = resolve(market_id="29", outcome_id="74")
        no = resolve(market_id="29", outcome_id="76")

        self.assertEqual(yes.family, "btts")
        self.assertEqual(yes.side, "yes")
        self.assertEqual(no.side, "no")

    def test_team_cards_is_not_team_goals(self):
        market = resolve(market_id="800060", outcome_id="800060:00000002")

        self.assertEqual(market.family, "team_cards")

    def test_early_payout_market_is_flagged_not_treated_as_a_plain_total(self):
        market = resolve(market_id="60180", outcome_id="12", specifier="minsnr=10|total=1.5")

        self.assertEqual(market.settlement, Settlement.EARLY_PAYOUT)
        self.assertIn("early_payout_market", market.warnings)


class PeriodTests(SimpleTestCase):
    def test_period_comes_from_the_market_id(self):
        cases = [
            ("18", Period.FULL_MATCH),
            ("68", Period.FIRST_HALF),
            ("90", Period.SECOND_HALF),
        ]
        for market_id, period in cases:
            market = resolve(market_id=market_id, outcome_id="12", specifier="total=0.5")
            self.assertEqual(market.period, period, market_id)

    def test_first_half_corners_are_distinct_from_full_match_corners(self):
        full = resolve(market_id="166", outcome_id="12", specifier="total=9.5")
        first = resolve(market_id="177", outcome_id="12", specifier="total=3.5")

        self.assertEqual(full.period, Period.FULL_MATCH)
        self.assertEqual(first.period, Period.FIRST_HALF)
        self.assertEqual(full.family, first.family)

    def test_second_half_result_is_not_a_full_match_result(self):
        market = resolve(market_id="83", outcome_id="1")

        self.assertEqual(market.family, "match_result")
        self.assertEqual(market.period, Period.SECOND_HALF)


class PlayerMarketTests(SimpleTestCase):
    def test_goalscorer_player_id_comes_from_the_outcome_id(self):
        market = resolve(market_id="40", outcome_id="sr:player:149731", specifier="type=prematch")

        self.assertEqual(market.family, "goalscorer_anytime")
        self.assertEqual(market.subject, Subject.PLAYER)
        self.assertEqual(market.subject_player_id, "149731")

    def test_player_card_player_id_comes_from_the_variant_specifier(self):
        market = resolve(
            market_id="1191",
            outcome_id="pre:playerprops:72041034:35208:1",
            specifier="variant=pre:playerprops:72041034:35208",
        )

        self.assertEqual(market.family, "player_card")
        self.assertEqual(market.subject_player_id, "35208")

    def test_missing_player_id_is_flagged(self):
        market = resolve(market_id="40", outcome_id="unexpected")

        self.assertEqual(market.subject_player_id, "")
        self.assertIn("player_id_not_resolved", market.warnings)


class UnknownMarketTests(SimpleTestCase):
    def test_unmapped_market_is_unresolved_rather_than_guessed(self):
        market = resolve(
            market_id="999999",
            outcome_id="12",
            specifier="total=2.5",
            market_label="Some New Market",
            outcome_label="Over 2.5",
        )

        self.assertEqual(market.resolution, Resolution.UNRESOLVED)
        self.assertEqual(market.family, "unknown")
        self.assertFalse(market.assessable)
        self.assertIn("unmapped_bookmaker_market:999999", market.warnings)

    def test_unmapped_outcome_on_a_known_market_is_flagged(self):
        market = resolve(market_id="18", outcome_id="9999", specifier="total=2.5")

        self.assertEqual(market.family, "total_goals")
        self.assertEqual(market.side, "")
        self.assertIn("unmapped_outcome:18:9999", market.warnings)

    def test_mapped_markets_are_assessable(self):
        self.assertTrue(resolve(market_id="1", outcome_id="1").assessable)

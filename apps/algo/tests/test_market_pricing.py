"""
Alternatives carry a price, and value decides the recommendation.

Generated markets used to be published with `odds: null`, so "replace this with a double
chance" was advice nobody could check, and the ranking had nothing but raw probability to
work with -- which always prefers whichever market has the highest base rate. StatPal
prices 90-odd markets per fixture across fourteen bookmakers; this wires them in.
"""

from django.test import SimpleTestCase

from apps.algo.market_taxonomy import describe_market
from apps.algo.statpal_advisory import statpal_market_advisory as advisory
from apps.algo.views import (
    MINIMUM_EV_LIFT,
    _market_expected_value,
    _rank_replacement_candidates,
    _replacement_is_meaningfully_better,
)


def _book(name, odds):
    return {"name": name, "value": str(odds)}


def _fixture():
    """Shaped as `normalize_prematch_odds` emits, trimmed to what pricing reads."""
    payload = {
        "markets": [
            {
                "name": "1x2",
                "bookmakers": [
                    {
                        "name": book,
                        "odds": [_book("Home", h), _book("Draw", d), _book("Away", a)],
                    }
                    for book, h, d, a in (("bet365", 1.22, 6.50, 12.00), ("Unibet", 1.20, 6.80, 13.00))
                ],
            },
            {
                "name": "Double Chance",
                "bookmakers": [
                    {
                        "name": "bet365",
                        "odds": [
                            _book("Home/Draw", 1.04),
                            _book("Home/Away", 1.11),
                            _book("Draw/Away", 4.50),
                        ],
                    }
                ],
            },
            {
                "name": "Over/Under",
                "bookmakers": [
                    {
                        "name": "bet365",
                        "totals": [
                            {"line": "2.5", "odds": [_book("Over", 1.57), _book("Under", 2.40)]},
                            {"line": "1.5", "odds": [_book("Over", 1.17), _book("Under", 4.75)]},
                        ],
                    }
                ],
            },
            {
                "name": "Asian Handicap",
                "bookmakers": [
                    {
                        "name": "bet365",
                        "handicaps": [
                            {"line": "1", "odds": [_book("Home", 4.00), _book("Away", 1.25)]},
                            {"line": "-1", "odds": [_book("Home", 1.25), _book("Away", 4.00)]},
                        ],
                    }
                ],
            },
        ]
    }
    return {"statpal_context": {"snapshots": {"prematch_odds": {"payload": payload}}}}


class PricingTests(SimpleTestCase):
    def test_a_result_market_is_priced_from_the_median_across_books(self):
        reference = advisory.reference_price(describe_market("Home Win"), fixture=_fixture())

        self.assertEqual(reference["odds"], 1.21)
        self.assertEqual(reference["bookmaker_count"], 2)

    def test_a_double_chance_is_priced(self):
        """`_market_matches_descriptor` had no double-chance branch at all."""
        reference = advisory.reference_price(describe_market("DC: 1X"), fixture=_fixture())

        self.assertEqual(reference["odds"], 1.04)

    def test_a_total_is_priced_from_its_own_line(self):
        fixture = _fixture()

        self.assertEqual(advisory.reference_price(describe_market("Over 2.5"), fixture=fixture)["odds"], 1.57)
        self.assertEqual(advisory.reference_price(describe_market("Over 1.5"), fixture=fixture)["odds"], 1.17)

    def test_handicaps_are_refused_rather_than_priced_from_the_wrong_side(self):
        """
        `describe_market("Handicap -1 Home")` parses as line 1 with the sign dropped, so a
        lookup can return the +1 bucket -- the opposite bet. No price beats a wrong one.
        """
        for name in ("Asian Handicap -1.5 Home", "Handicap -1 Home"):
            with self.subTest(market=name):
                self.assertEqual(advisory.reference_price(describe_market(name), fixture=_fixture()), {})

    def test_an_unpriced_market_returns_nothing_rather_than_guessing(self):
        self.assertEqual(
            advisory.reference_price(describe_market("Corners Over 9.5"), fixture=_fixture()), {}
        )


class DevigTests(SimpleTestCase):
    def test_the_bookmaker_margin_is_removed(self):
        reference = advisory.reference_price(describe_market("Home Win"), fixture=_fixture())

        self.assertLess(advisory.devigged_probability(reference), 100 / reference["odds"])

    def test_result_outcomes_sum_to_about_one_hundred(self):
        fixture = _fixture()
        total = sum(
            advisory.devigged_probability(advisory.reference_price(describe_market(name), fixture=fixture))
            for name in ("Home Win", "Draw", "Away Win")
        )

        self.assertAlmostEqual(total, 100.0, delta=1.5)

    def test_double_chance_outcomes_sum_to_about_two_hundred(self):
        """Each covers two of the three results, so a fair book sums to 2.0, not 1.0."""
        fixture = _fixture()
        total = sum(
            advisory.devigged_probability(advisory.reference_price(describe_market(name), fixture=fixture))
            for name in ("DC: 1X", "DC: X2", "DC: 12")
        )

        self.assertAlmostEqual(total, 200.0, delta=2.0)

    def test_a_double_chance_agrees_with_the_parts_it_covers(self):
        fixture = _fixture()

        def devig(name):
            return advisory.devigged_probability(advisory.reference_price(describe_market(name), fixture=fixture))

        self.assertAlmostEqual(devig("DC: 1X"), devig("Home Win") + devig("Draw"), delta=2.0)

    def test_a_partial_book_is_not_devigged(self):
        """Dividing by an incomplete outcome set would understate the margin."""
        payload = {"markets": [{"name": "1x2", "bookmakers": [{"name": "b", "odds": [_book("Home", 1.22)]}]}]}
        fixture = {"statpal_context": {"snapshots": {"prematch_odds": {"payload": payload}}}}
        reference = advisory.reference_price(describe_market("Home Win"), fixture=fixture)

        self.assertIsNone(reference.get("overround"))
        self.assertEqual(advisory.devigged_probability(reference), round(100 / 1.22, 1))


class ExpectedValueTests(SimpleTestCase):
    def test_expected_value_is_per_unit_staked(self):
        self.assertEqual(_market_expected_value({"advisory_score": 50.0, "odds": 2.20}), 0.1)

    def test_an_unpriced_market_has_no_expected_value(self):
        self.assertIsNone(_market_expected_value({"advisory_score": 88.0, "odds": None}))

    def test_the_short_price_that_looks_safe_is_negative_value(self):
        """Under 4.5 at 88% into 1.10 is a losing bet; probability alone preferred it."""
        self.assertLess(_market_expected_value({"advisory_score": 88.0, "odds": 1.10}), 0)

    def test_value_beats_probability_in_the_ranking(self):
        ranked = _rank_replacement_candidates(
            [
                {"market": "Under 4.5", "advisory_score": 88.0, "odds": 1.10},
                {"market": "Away Win", "advisory_score": 43.0, "odds": 2.60},
            ]
        )

        self.assertEqual(ranked[0]["market"], "Away Win")

    def test_a_priced_market_outranks_an_unpriced_one(self):
        ranked = _rank_replacement_candidates(
            [
                {"market": "Corners Over 9.5", "advisory_score": 80.0, "odds": None},
                {"market": "Away Win", "advisory_score": 43.0, "odds": 2.60},
            ]
        )

        self.assertEqual(ranked[0]["market"], "Away Win")

    def test_a_swap_that_raises_probability_but_kills_the_price_is_refused(self):
        """The swap that turned a 20.05 ticket into a 3.24 one."""
        selected = {"market": "Away Win", "advisory_score": 62.0, "odds": 2.60}
        replacement = {"market": "Under 4.5", "advisory_score": 88.0, "odds": 1.10}

        self.assertFalse(_replacement_is_meaningfully_better(selected, replacement))

    def test_a_genuinely_better_priced_market_still_replaces(self):
        # A candidate with no evidence behind it is not eligible to be recommended at all,
        # so a realistic one carries some.
        selected = {"market": "Home Win", "advisory_score": 60.0, "odds": 1.55}
        replacement = {
            "market": "DC: 1X",
            "advisory_score": 85.0,
            "odds": 1.35,
            "advisory_evidence": {"expected_goals_home": 1.8, "expected_goals_away": 0.9},
        }

        self.assertTrue(_replacement_is_meaningfully_better(selected, replacement))

    def test_a_rounding_error_is_not_an_improvement(self):
        selected = {"market": "Home Win", "advisory_score": 60.0, "odds": 1.80}
        replacement = {"market": "DC: 1X", "advisory_score": 61.0, "odds": 1.80}

        self.assertLess(
            _market_expected_value(replacement) - _market_expected_value(selected), MINIMUM_EV_LIFT
        )
        self.assertFalse(_replacement_is_meaningfully_better(selected, replacement))

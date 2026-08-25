"""
Replacements are ranked by edge, not by raw probability.

A raw probability is not comparable across market families. A double chance covers two of
three outcomes and sits near 70% in almost any fixture; an Under 4.5 sits near 88%; a home
win near 40%. Ranked on the raw number, the market with the highest base rate wins every
time regardless of whether the fixture suits it -- which is how a thirteen-leg slip came
back with eight legs "improved" into double chances and unders and its combined odds cut
from 20.05 to 3.24. That is walking down the odds ladder, not finding value.

The reference is the same market evaluated with team strengths switched off, so it comes
from the fitted league rather than a hand-written table of base rates.
"""

from django.test import SimpleTestCase

from betpreneur.modules.pricing.api import market_edge
from betpreneur.modules.slips.domain.slip_analysis import (
    _rank_replacement_candidates,
    _replacement_is_meaningfully_better,
)


def _market(name, score, edge=None, *, family=None):
    market = {
        "market": name,
        "advisory_score": score,
        "advisory_evidence": {} if edge is None else {"edge_points": edge},
    }
    if family:
        market["market_taxonomy"] = {"family": family}
    return market


class EdgeExtractionTests(SimpleTestCase):
    def test_edge_is_read_from_advisory_evidence(self):
        self.assertEqual(market_edge(_market("Away Win", 42.9, 14.1)), 14.1)

    def test_edge_is_read_from_a_nested_statpal_advisory(self):
        market = {
            "market": "Away Win",
            "statpal_advisory": {"evidence": {"edge_points": 9.5}},
        }

        self.assertEqual(market_edge(market), 9.5)

    def test_a_market_without_a_reference_reports_no_edge(self):
        self.assertIsNone(market_edge(_market("Corners Over 9.5", 71)))


class RankingTests(SimpleTestCase):
    # Real Premier League numbers: Everton vs Liverpool, away side much stronger.
    EVERTON_LIVERPOOL = [
        _market("Under 4.5", 87.9, -1.9),
        _market("Over 1.5", 74.7, 2.8),
        _market("DC: X2", 72.2, 13.0),
        _market("DC: 12", 70.7, 1.1),
        _market("DC: 1X", 57.1, -14.1),
        _market("Away Win", 42.9, 14.1),
    ]

    def test_the_highest_edge_wins_not_the_highest_probability(self):
        ranked = _rank_replacement_candidates(self.EVERTON_LIVERPOOL)

        self.assertEqual(ranked[0]["market"], "Away Win")

    def test_the_safest_looking_market_is_demoted_when_it_beats_no_average(self):
        """Under 4.5 at 87.9% is *below* what a typical fixture in this league returns."""
        ranked = [item["market"] for item in _rank_replacement_candidates(self.EVERTON_LIVERPOOL)]

        self.assertGreater(ranked.index("Under 4.5"), ranked.index("Away Win"))
        self.assertGreater(ranked.index("Under 4.5"), ranked.index("DC: X2"))

    def test_the_wrong_side_double_chance_ranks_last(self):
        """Backing the weak home side to avoid defeat is the worst read here."""
        ranked = [item["market"] for item in _rank_replacement_candidates(self.EVERTON_LIVERPOOL)]

        self.assertEqual(ranked[-1], "DC: 1X")

    def test_measured_markets_outrank_unmeasured_ones(self):
        """An unmeasured market must not be treated as zero edge and beat a measured loss."""
        ranked = _rank_replacement_candidates(
            [_market("Corners Over 9.5", 80), _market("Away Win", 43, 14.1)]
        )

        self.assertEqual(ranked[0]["market"], "Away Win")

    def test_ordering_is_unchanged_when_nothing_has_a_reference(self):
        ranked = _rank_replacement_candidates(
            [_market("Cards Over 3.5", 61), _market("Corners Over 9.5", 74)]
        )

        self.assertEqual(ranked[0]["market"], "Corners Over 9.5")


class ReplacementTests(SimpleTestCase):
    def test_a_more_probable_market_with_less_edge_is_not_an_improvement(self):
        """The exact swap the slip kept making: a result market into an under."""
        selected = _market("Away Win", 42.9, 14.1)
        replacement = _market("Under 4.5", 87.9, -1.9)

        self.assertFalse(_replacement_is_meaningfully_better(selected, replacement))

    def test_a_genuinely_better_read_still_replaces(self):
        # Originally written as DC: 1X -> DC: X2, which is a *side reversal* -- backing
        # home-or-draw and being told to switch to draw-or-away. The thesis guard rightly
        # refuses that now, so the example moves to a market on the same side.
        selected = _market("Home Win", 27.8, -13.0)
        replacement = _market("DC: 1X", 68.0, 13.0)

        self.assertTrue(_replacement_is_meaningfully_better(selected, replacement))

    def test_a_swap_to_the_other_side_is_never_an_improvement(self):
        """However much better it scores, it is a bet on the other team."""
        selected = _market("DC: 1X", 57.1, -14.1)
        replacement = _market("DC: X2", 72.2, 13.0)

        self.assertFalse(_replacement_is_meaningfully_better(selected, replacement))

    def test_a_market_below_the_absolute_floor_is_never_offered(self):
        """High edge does not excuse a market we would not stand behind on its own."""
        selected = _market("Over 1.5", 74.7, 2.8)
        replacement = _market("Correct Score 2-1", 12.0, 40.0)

        self.assertFalse(_replacement_is_meaningfully_better(selected, replacement))

    def test_markets_without_a_shared_reference_fall_back_to_raw_scores(self):
        # No league reference exists for counts, so the comparison falls back to raw
        # scores. Both still need real evidence behind them to be offered at all.
        selected = {
            "market": "Corners Over 9.5",
            "advisory_score": 55,
            "advisory_evidence": {"expected_corners": 9.1},
        }
        replacement = {
            "market": "Corners Over 8.5",
            "advisory_score": 72,
            "advisory_evidence": {"expected_corners": 9.1},
        }

        self.assertTrue(_replacement_is_meaningfully_better(selected, replacement))

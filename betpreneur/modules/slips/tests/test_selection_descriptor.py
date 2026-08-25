"""
The analysis step must use the identity resolved at import time, not re-parse text.

Re-deriving the descriptor from the canonical string is how period markets were lost:
the importer resolves market 60 to `match_result / first_half` and writes `1H Home Win`,
which text parsing cannot read back. Those legs then reported `unknown`, were never
assessed, and still paid the cost of core on-demand scoring.
"""

from django.test import SimpleTestCase

from betpreneur.modules.markets.api import describe_market
from betpreneur.modules.slips.domain.slip_analysis import (
    _market_can_skip_core_on_demand,
    _resolved_taxonomy,
)
from betpreneur.modules.slips.interface.views import _selection_market_descriptor


def _taxonomy(**overrides):
    base = {
        "raw": "Home", "canonical": "1H Home Win", "code": "match_result:home",
        "family": "match_result", "category": "match_result", "side": "home",
        "line": "", "team": "", "player": "", "subject": "match", "period": "1st_half",
        "recognized": True, "core_supported": False, "data_requirements": [],
        "requires_player_stats": False, "requires_card_stats": False,
        "requires_corner_stats": False, "requires_team_goal_stats": False,
    }
    base.update(overrides)
    return base


class TaxonomySourceTests(SimpleTestCase):
    def test_a_bookmaker_import_nests_the_taxonomy_under_provider_payload(self):
        selection = {"provider_payload": {"market_taxonomy": _taxonomy()}}

        self.assertEqual(_resolved_taxonomy(selection)["family"], "match_result")

    def test_the_manual_path_carries_it_at_the_top_level(self):
        selection = {"market_taxonomy": _taxonomy()}

        self.assertEqual(_resolved_taxonomy(selection)["family"], "match_result")

    def test_an_unrecognised_taxonomy_is_not_trusted(self):
        selection = {"provider_payload": {"market_taxonomy": _taxonomy(recognized=False)}}

        self.assertEqual(_resolved_taxonomy(selection), {})

    def test_a_taxonomy_without_a_family_is_not_trusted(self):
        selection = {"provider_payload": {"market_taxonomy": _taxonomy(family="")}}

        self.assertEqual(_resolved_taxonomy(selection), {})


class DescriptorTests(SimpleTestCase):
    def test_a_first_half_market_keeps_its_period_through_analysis(self):
        selection = {"provider_payload": {"market_taxonomy": _taxonomy()}}

        descriptor = _selection_market_descriptor(selection, "1H Home Win")

        self.assertEqual(descriptor.family, "match_result")
        self.assertEqual(descriptor.period, "1st_half")

    def test_the_period_prefixed_string_alone_would_not_resolve(self):
        # Demonstrates why the stored identity is used rather than the text.

        self.assertEqual(describe_market("1H Home Win").family, "unknown")

    def test_early_payout_result_text_uses_base_result_model(self):
        descriptor = describe_market("Home Win 1UP")

        self.assertEqual(descriptor.family, "match_result")
        self.assertEqual(descriptor.canonical, "Home Win 1UP")
        self.assertEqual(descriptor.code, "result_home_1up")
        self.assertEqual(descriptor.side, "home")
        self.assertTrue(_market_can_skip_core_on_demand(descriptor))

    def test_early_payout_double_chance_text_uses_base_dc_model(self):
        descriptor = describe_market("DC: X2 1UP")

        self.assertEqual(descriptor.family, "double_chance")
        self.assertEqual(descriptor.canonical, "DC: X2 1UP")
        self.assertEqual(descriptor.code, "double_chance_draw_or_away_1up")
        self.assertEqual(descriptor.side, "draw_or_away")
        self.assertTrue(_market_can_skip_core_on_demand(descriptor))

    def test_a_resolved_half_market_can_skip_core_scoring(self):
        selection = {"provider_payload": {"market_taxonomy": _taxonomy()}}

        descriptor = _selection_market_descriptor(selection, "1H Home Win")

        self.assertTrue(_market_can_skip_core_on_demand(descriptor))

    def test_data_requirements_survive_json_round_tripping(self):
        selection = {"provider_payload": {
            "market_taxonomy": _taxonomy(data_requirements=["team_stats", "odds"])
        }}

        descriptor = _selection_market_descriptor(selection, "1H Home Win")

        self.assertEqual(descriptor.data_requirements, ("team_stats", "odds"))

    def test_unknown_keys_in_a_stored_taxonomy_are_ignored(self):
        selection = {"provider_payload": {
            "market_taxonomy": _taxonomy(some_future_field="ignored")
        }}

        self.assertEqual(_selection_market_descriptor(selection, "1H Home Win").family, "match_result")

    def test_text_parsing_is_still_used_when_nothing_was_resolved(self):
        selection = {"provider_payload": {}}

        descriptor = _selection_market_descriptor(selection, "Over 2.5")

        self.assertEqual(descriptor.family, "total_goals")

    def test_an_unmapped_market_falls_back_rather_than_failing(self):
        selection = {"provider_payload": {"market_taxonomy": {"family": "", "recognized": False}}}

        descriptor = _selection_market_descriptor(selection, "Corners Over 9.5")

        self.assertEqual(descriptor.family, "corners_total")

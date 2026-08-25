"""Bookmaker market-string parsing.

These drive the taxonomy through the SportyBet/Betano importers, so they are
importer tests rather than taxonomy tests — which is why they live with slips
rather than with markets.
"""
from django.test import SimpleTestCase

from betpreneur.modules.slips.services.importers import (
    BetanoBetslipImporter,
    SportyBetShareImporter,
)


class BookmakerMarketParsingTests(SimpleTestCase):
    def test_sportybet_parses_over_under_line(self):
        importer = SportyBetShareImporter()
        market = importer._canonical_market("Over/Under", "Over", "total=1.5")

        self.assertEqual(market, "Over 1.5")

    def test_sportybet_recognizes_cards_market(self):
        importer = SportyBetShareImporter()
        market = importer._canonical_market("Yellow Cards Over/Under", "Over", "total=3.5")

        self.assertEqual(market, "Cards Over 3.5")

    def test_sportybet_recognizes_goalscorer_outcome_as_player_score_market(self):
        importer = SportyBetShareImporter()
        market = importer._canonical_market("Anytime Goalscorer", "Haller, Sebastian (Sanfrecce Hiroshima)", "")

        self.assertEqual(market, "Haller, Sebastian (Sanfrecce Hiroshima) To Score")

    def test_betano_parses_match_result_using_event_sides(self):
        importer = BetanoBetslipImporter()
        market = importer._canonical_market(
            {
                "eventName": "Norway - England",
                "description": "England",
                "market": "Match Result",
                "marketSort": "MRES",
            }
        )

        self.assertEqual(market, "Away Win")

    def test_betano_recognizes_player_market(self):
        importer = BetanoBetslipImporter()
        market = importer._canonical_market(
            {
                "description": "Kylian Mbappe To Score",
                "market": "Player To Score",
                "marketSort": "PLAYER",
            }
        )

        self.assertEqual(market, "Kylian Mbappe To Score")

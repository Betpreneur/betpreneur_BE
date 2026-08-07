from django.test import SimpleTestCase

from apps.algo.market_taxonomy import describe_market, market_matches, market_options
from apps.algo.services import BetanoBetslipImporter, SportyBetShareImporter


class MarketTaxonomyTests(SimpleTestCase):
    def test_core_goal_market_is_canonical_and_supported(self):
        descriptor = describe_market("Over 0.5 Goals")

        self.assertEqual(descriptor.canonical, "Over 0.5")
        self.assertEqual(descriptor.family, "total_goals")
        self.assertTrue(descriptor.recognized)
        self.assertFalse(descriptor.core_supported)

    def test_cards_corners_and_player_markets_are_recognized(self):
        cards = describe_market("Yellow Cards Over 3.5")
        corners = describe_market("Corners Under 10.5")
        scorer = describe_market("Erling Haaland To Score")

        self.assertEqual(cards.family, "cards_total")
        self.assertTrue(cards.requires_card_stats)
        self.assertEqual(corners.family, "corners_total")
        self.assertTrue(corners.requires_corner_stats)
        self.assertEqual(scorer.family, "player_goal")
        self.assertTrue(scorer.requires_player_stats)

    def test_market_matching_uses_canonical_aliases(self):
        self.assertTrue(market_matches("Both Teams To Score", "GG / BTTS Yes"))
        self.assertTrue(market_matches("12", "DC: 12"))

    def test_market_options_include_wider_match_checker_families(self):
        groups = {item["group"] for item in market_options()}

        self.assertIn("Player", groups)
        self.assertIn("Cards", groups)
        self.assertIn("Corners", groups)


class BookmakerMarketParsingTests(SimpleTestCase):
    def test_sportybet_parses_over_under_line(self):
        importer = SportyBetShareImporter()
        market = importer._canonical_market("Over/Under", "Over", "total=1.5")

        self.assertEqual(market, "Over 1.5")

    def test_sportybet_recognizes_cards_market(self):
        importer = SportyBetShareImporter()
        market = importer._canonical_market("Yellow Cards Over/Under", "Over", "total=3.5")

        self.assertEqual(market, "Cards Over 3.5")

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

from django.test import SimpleTestCase

from betpreneur.modules.markets.domain.taxonomy import (
    describe_market,
    market_matches,
    market_options,
)


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
        self.assertEqual(cards.market_type, "cards_total")
        self.assertEqual(cards.selection, "over")
        self.assertEqual(cards.line, "3.5")
        self.assertEqual(cards.support_level, "medium")
        self.assertTrue(cards.requires_card_stats)
        self.assertEqual(corners.family, "corners_total")
        self.assertEqual(corners.selection, "under")
        self.assertIn("corner_stats", corners.data_requirements)
        self.assertTrue(corners.requires_corner_stats)
        self.assertEqual(scorer.family, "player_goal")
        self.assertEqual(scorer.player, "Erling Haaland To Score")
        self.assertEqual(scorer.support_level, "weak")
        self.assertTrue(scorer.requires_player_stats)

    def test_team_goals_and_two_plus_markets_are_recognized(self):
        home_total = describe_market("HOME Over/Under", outcome_name="Over 1.5")
        team_two_plus = describe_market("Vitoria Guimaraes 2+")

        self.assertEqual(home_total.family, "team_total_goals")
        self.assertEqual(home_total.team, "home")
        self.assertEqual(home_total.selection, "over")
        self.assertEqual(home_total.line, "1.5")
        self.assertTrue(home_total.requires_team_goal_stats)
        self.assertEqual(team_two_plus.family, "team_total_goals")
        self.assertEqual(team_two_plus.selection, "over")
        self.assertEqual(team_two_plus.line, "1.5")

    def test_half_scoreline_and_time_window_markets_are_recognized(self):
        first_half = describe_market("1st Half - Over/Under", outcome_name="Over 1.5")
        correct_score = describe_market("Correct Score", outcome_name="2:1")
        first_15 = describe_market("Total Goals O/U - First 15 Minutes", outcome_name="Under 0.5")

        self.assertEqual(first_half.family, "total_goals")
        self.assertEqual(first_half.period, "first_half")
        self.assertEqual(first_half.support_level, "medium")
        self.assertEqual(correct_score.family, "correct_score")
        self.assertEqual(correct_score.selection, "2:1")
        self.assertEqual(correct_score.support_level, "weak")
        self.assertEqual(first_15.family, "total_goals")
        self.assertEqual(first_15.period, "first_15m")

    def test_booking_points_and_player_card_markets_are_recognized(self):
        booking_points = describe_market("Total Booking Points", outcome_name="Over 45.5")
        player_card = describe_market("Player To Be Booked", outcome_name="Virgil van Dijk")
        named_player_card = describe_market("Florian Wirtz To Be Booked")

        self.assertEqual(booking_points.family, "booking_points")
        self.assertEqual(booking_points.category, "Booking Points")
        self.assertEqual(booking_points.selection, "over")
        self.assertTrue(booking_points.requires_card_stats)
        self.assertEqual(player_card.family, "player_card")
        self.assertEqual(player_card.player, "Virgil van Dijk")
        self.assertTrue(player_card.requires_player_stats)
        self.assertEqual(named_player_card.family, "player_card")
        self.assertTrue(named_player_card.requires_player_stats)

    def test_market_matching_uses_canonical_aliases(self):
        self.assertTrue(market_matches("Both Teams To Score", "GG / BTTS Yes"))
        self.assertTrue(market_matches("12", "DC: 12"))

    def test_market_options_include_wider_match_checker_families(self):
        groups = {item["group"] for item in market_options()}

        self.assertIn("Player", groups)
        self.assertIn("Cards", groups)
        self.assertIn("Corners", groups)

    def test_team_shots_on_target_is_not_a_player_market(self):
        market = describe_market("Home Team Shots on Target Over 9.5")

        self.assertEqual(market.family, "team_shots_on_target")
        self.assertEqual(market.team, "home")
        self.assertEqual(market.side, "over")
        self.assertEqual(market.line, "9.5")
        self.assertFalse(market.requires_player_stats)
        self.assertEqual(market.support_level, "unsupported")

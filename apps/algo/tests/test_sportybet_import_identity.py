"""
Import-path tests: market identity survives, and so do same-fixture legs.

SportyBet emits one `outcomes` entry per selection. Both behaviours pinned here were
broken: identity was collapsed to display text and re-derived, and all but one leg per
fixture was dropped before it was ever analysed.
"""

from django.test import SimpleTestCase

from apps.algo.services import SportyBetShareImporter


def _outcome(event_id, market, home="NEC Nijmegen", away="SC Telstar"):
    return {
        "eventId": event_id,
        "homeTeamName": home,
        "awayTeamName": away,
        "estimateStartTime": 1786199400000,
        "sport": {"category": {"tournament": {"name": "Eredivisie"}}},
        "markets": [market],
    }


def _market(market_id, desc, outcome_id, outcome_desc, specifier="", odds="2.00"):
    payload = {
        "id": market_id,
        "desc": desc,
        "name": desc,
        "outcomes": [{"id": outcome_id, "desc": outcome_desc, "odds": odds}],
    }
    if specifier:
        payload["specifier"] = specifier
    return payload


def _share(selections, outcomes):
    return {
        "bizCode": 10000,
        "isAvailable": True,
        "data": {"shareCode": "TEST01", "ticket": {"selections": selections}, "outcomes": outcomes},
    }


class SameFixtureLegsTests(SimpleTestCase):
    """A slip with several markets on one match must keep every leg."""

    def _multi_market_share(self):
        event = "sr:match:72041034"
        selections = [
            {"eventId": event, "marketId": "18", "specifier": "total=2.5", "outcomeId": "12"},
            {"eventId": event, "marketId": "18", "specifier": "total=3.5", "outcomeId": "12"},
            {"eventId": event, "marketId": "19", "specifier": "total=0.5", "outcomeId": "12"},
            {"eventId": event, "marketId": "19", "specifier": "total=0.5", "outcomeId": "13"},
            {"eventId": event, "marketId": "29", "outcomeId": "74"},
        ]
        outcomes = [
            _outcome(event, _market("18", "Over/Under", "12", "Over 2.5", "total=2.5")),
            _outcome(event, _market("18", "Over/Under", "12", "Over 3.5", "total=3.5")),
            _outcome(event, _market("19", "Home O/U", "12", "Over 0.5", "total=0.5")),
            _outcome(event, _market("19", "Home O/U", "13", "Under 0.5", "total=0.5")),
            _outcome(event, _market("29", "GG/NG", "74", "Yes")),
        ]
        return _share(selections, outcomes)

    def test_every_leg_on_one_fixture_is_imported(self):
        parsed = SportyBetShareImporter().import_share(payload=self._multi_market_share())

        self.assertEqual(parsed["selection_count"], 5)

    def test_legs_differing_only_by_line_are_kept_apart(self):
        parsed = SportyBetShareImporter().import_share(payload=self._multi_market_share())

        markets = [item["market"] for item in parsed["selections"]]

        self.assertIn("Over 2.5", markets)
        self.assertIn("Over 3.5", markets)

    def test_legs_differing_only_by_outcome_are_kept_apart(self):
        parsed = SportyBetShareImporter().import_share(payload=self._multi_market_share())

        sides = [
            item["canonical_market"]["side"]
            for item in parsed["selections"]
            if item["canonical_market"]["family"] == "team_total_goals"
        ]

        self.assertEqual(sorted(sides), ["over", "under"])


class MarketIdentityThroughImportTests(SimpleTestCase):
    def _single(self, market_id, outcome_id, desc, outcome_desc, specifier=""):
        event = "sr:match:1"
        selection = {"eventId": event, "marketId": market_id, "outcomeId": outcome_id}
        if specifier:
            selection["specifier"] = specifier
        share = _share(
            [selection],
            [_outcome(event, _market(market_id, desc, outcome_id, outcome_desc, specifier))],
        )
        return SportyBetShareImporter().import_share(payload=share)["selections"][0]

    def test_home_team_total_is_not_read_as_a_match_total(self):
        leg = self._single("19", "12", "Home O/U", "Over 2.5", "total=2.5")

        self.assertEqual(leg["market_taxonomy"]["family"], "team_total_goals")
        self.assertEqual(leg["canonical_market"]["subject"], "home")
        self.assertEqual(leg["provider_market_text"], "Over 2.5")

    def test_first_half_total_keeps_its_period(self):
        leg = self._single("68", "12", "1st Half - Over/Under", "Over 0.5", "total=0.5")

        self.assertEqual(leg["market_taxonomy"]["period"], "1st_half")
        self.assertEqual(leg["market"], "1H Over 0.5")

    def test_bookings_1x2_is_not_read_as_the_match_result(self):
        leg = self._single("136", "2", "Bookings 1X2", "Draw")

        self.assertNotEqual(leg["market_taxonomy"]["family"], "match_result")
        self.assertEqual(leg["canonical_market"]["family"], "cards_result")

    def test_team_shots_on_target_is_not_read_as_a_player_prop(self):
        leg = self._single("900546", "12", "Home Team Shots on Target Over/Under", "Over 9.5", "total=9.5")

        self.assertEqual(leg["canonical_market"]["family"], "team_shots_on_target")
        self.assertFalse(leg["market_taxonomy"]["requires_player_stats"])

    def test_asian_handicap_keeps_its_sign(self):
        leg = self._single("16", "1714", "Asian Handicap", "Home (-3.0)", "hcp=-3")

        self.assertEqual(leg["canonical_market"]["line"], -3.0)

    def test_btts_is_recognised_through_the_importer(self):
        leg = self._single("29", "74", "GG/NG", "Yes")

        self.assertEqual(leg["market_taxonomy"]["family"], "btts")
        self.assertTrue(leg["market_taxonomy"]["recognized"])

    def test_unmapped_market_falls_back_to_text_and_is_flagged(self):
        leg = self._single("999999", "12", "Brand New Market", "Over 2.5", "total=2.5")

        self.assertEqual(leg["canonical_market"]["resolution"], "unresolved")
        self.assertIn("unmapped_bookmaker_market:999999", leg["canonical_market"]["warnings"])

    def test_provider_text_is_preserved_for_display(self):
        leg = self._single("139", "12", "Bookings - Over/Under", "Over 3.5", "total=3.5")

        self.assertEqual(leg["provider_market_text"], "Cards Over 3.5")
        self.assertEqual(leg["canonical_market"]["family"], "cards_total")


class MergeOutcomesTests(SimpleTestCase):
    def test_markets_are_merged_rather_than_overwritten(self):
        event = "sr:match:1"
        merged = SportyBetShareImporter._merge_outcomes_by_event([
            _outcome(event, _market("18", "Over/Under", "12", "Over 2.5", "total=2.5")),
            _outcome(event, _market("29", "GG/NG", "74", "Yes")),
        ])

        ids = {market["id"] for market in merged[event]["markets"]}

        self.assertEqual(ids, {"18", "29"})

    def test_outcomes_of_the_same_market_are_merged(self):
        event = "sr:match:1"
        merged = SportyBetShareImporter._merge_outcomes_by_event([
            _outcome(event, _market("19", "Home O/U", "12", "Over 0.5", "total=0.5")),
            _outcome(event, _market("19", "Home O/U", "13", "Under 0.5", "total=0.5")),
        ])

        market = merged[event]["markets"][0]

        self.assertEqual({item["id"] for item in market["outcomes"]}, {"12", "13"})

    def test_distinct_fixtures_stay_separate(self):
        merged = SportyBetShareImporter._merge_outcomes_by_event([
            _outcome("sr:match:1", _market("1", "1X2", "1", "Home")),
            _outcome("sr:match:2", _market("1", "1X2", "3", "Away")),
        ])

        self.assertEqual(len(merged), 2)

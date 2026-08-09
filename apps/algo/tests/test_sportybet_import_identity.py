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


def _market(market_id, desc, outcome_id, outcome_desc, specifier="", odds="2.00", guide=""):
    payload = {
        "id": market_id,
        "desc": desc,
        "name": desc,
        "outcomes": [{"id": outcome_id, "desc": outcome_desc, "odds": odds}],
    }
    if specifier:
        payload["specifier"] = specifier
    if guide:
        payload["marketGuide"] = guide
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
    def _single(self, market_id, outcome_id, desc, outcome_desc, specifier="", guide=""):
        event = "sr:match:1"
        selection = {"eventId": event, "marketId": market_id, "outcomeId": outcome_id}
        if specifier:
            selection["specifier"] = specifier
        share = _share(
            [selection],
            [_outcome(event, _market(market_id, desc, outcome_id, outcome_desc, specifier, guide=guide))],
        )
        return SportyBetShareImporter().import_share(payload=share)["selections"][0]

    def test_home_team_total_is_not_read_as_a_match_total(self):
        leg = self._single("19", "12", "Home O/U", "Over 2.5", "total=2.5")

        self.assertEqual(leg["market_taxonomy"]["family"], "team_total_goals")
        self.assertEqual(leg["canonical_market"]["subject"], "home")
        self.assertEqual(leg["provider_market_text"], "Over 2.5")

    def test_home_and_away_team_totals_import_with_side_and_line(self):
        cases = [
            ("19", "12", "Hammarby IF Over/Under", "Over 0.5", "total=0.5", "Home Team Goals Over 0.5", "home", "over"),
            ("19", "13", "Hammarby IF Over/Under", "Under 4.5", "total=4.5", "Home Team Goals Under 4.5", "home", "under"),
            ("20", "12", "BK Hacken Over/Under", "Over 0.5", "total=0.5", "Away Team Goals Over 0.5", "away", "over"),
            ("20", "13", "BK Hacken Over/Under", "Under 3.5", "total=3.5", "Away Team Goals Under 3.5", "away", "under"),
        ]

        for market_id, outcome_id, desc, outcome_desc, specifier, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, specifier=specifier):
                leg = self._single(
                    market_id,
                    outcome_id,
                    desc,
                    outcome_desc,
                    specifier,
                    guide="Predict whether the total number of goals scored by team in regular time is over/under a given line.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_total_goals")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["provider_market_guide"], "Predict whether the total number of goals scored by team in regular time is over/under a given line.")

    def test_first_half_home_and_away_team_totals_import_with_period_side_and_line(self):
        cases = [
            ("69", "12", "1st half - Sparta Rotterdam Over/Under", "Over 0.5", "total=0.5", "1H Home Team Goals Over 0.5", "home", "over"),
            ("69", "13", "1st half - Sparta Rotterdam Over/Under", "Under 1.5", "total=1.5", "1H Home Team Goals Under 1.5", "home", "under"),
            ("70", "12", "1st half - Feyenoord Rotterdam Over/Under", "Over 0.5", "total=0.5", "1H Away Team Goals Over 0.5", "away", "over"),
            ("70", "13", "1st half - Feyenoord Rotterdam Over/Under", "Under 2.5", "total=2.5", "1H Away Team Goals Under 2.5", "away", "under"),
        ]

        for market_id, outcome_id, desc, outcome_desc, specifier, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, specifier=specifier):
                leg = self._single(
                    market_id,
                    outcome_id,
                    desc,
                    outcome_desc,
                    specifier,
                    guide="Predict whether the total number of goals scored by team in the 1st Half is over/under a given line.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_total_goals")
                self.assertEqual(leg["canonical_market"]["period"], "first_half")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_second_half_home_and_away_team_totals_import_with_period_side_and_line(self):
        cases = [
            ("91", "12", "2nd Half - Sparta Rotterdam Over/Under", "Over 0.5", "total=0.5", "2H Home Team Goals Over 0.5", "home", "over"),
            ("91", "13", "2nd Half - Sparta Rotterdam Over/Under", "Under 1.5", "total=1.5", "2H Home Team Goals Under 1.5", "home", "under"),
            ("92", "12", "2nd Half - Feyenoord Rotterdam Over/Under", "Over 0.5", "total=0.5", "2H Away Team Goals Over 0.5", "away", "over"),
            ("92", "13", "2nd Half - Feyenoord Rotterdam Over/Under", "Under 2.5", "total=2.5", "2H Away Team Goals Under 2.5", "away", "under"),
        ]

        for market_id, outcome_id, desc, outcome_desc, specifier, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, specifier=specifier):
                leg = self._single(
                    market_id,
                    outcome_id,
                    desc,
                    outcome_desc,
                    specifier,
                    guide="Predict whether the total number of goals scored by team in the 2nd Half is over/under a given line.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_total_goals")
                self.assertEqual(leg["canonical_market"]["period"], "second_half")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_both_halves_total_goals_import_supports_over_under_yes_no(self):
        cases = [
            ("58", "Both Halves Over 1.5", "74", "Yes", "Both Halves Over 1.5 - Yes", "over_yes"),
            ("58", "Both Halves Over 1.5", "76", "No", "Both Halves Over 1.5 - No", "over_no"),
            ("59", "Both Halves Under 1.5", "74", "Yes", "Both Halves Under 1.5 - Yes", "under_yes"),
            ("59", "Both Halves Under 1.5", "76", "No", "Both Halves Under 1.5 - No", "under_no"),
        ]

        for market_id, desc, outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                leg = self._single(
                    market_id,
                    outcome_id,
                    desc,
                    outcome_desc,
                    "total=1.5",
                    guide="Predict whether the number of goals scored in each Half is over/under the given line.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "both_halves_total_goals")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["canonical_market"]["line"], 1.5)
                self.assertEqual(leg["provider_market_guide"], "Predict whether the number of goals scored in each Half is over/under the given line.")

    def test_half_btts_pair_import_supports_all_pairs(self):
        cases = [
            ("806", "No/No", "1H BTTS No / 2H BTTS No", "no_no"),
            ("808", "Yes/No", "1H BTTS Yes / 2H BTTS No", "yes_no"),
            ("810", "Yes/Yes", "1H BTTS Yes / 2H BTTS Yes", "yes_yes"),
            ("812", "No/Yes", "1H BTTS No / 2H BTTS Yes", "no_yes"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single(
                    "55",
                    outcome_id,
                    "1st/2nd Half GG/NG",
                    outcome_desc,
                    guide="Will both teams score at least one goal each during 1st/2nd Half.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "half_btts_pair")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["provider_market_guide"], "Will both teams score at least one goal each during 1st/2nd Half.")

    def test_first_half_btts_import_supports_yes_no(self):
        cases = [
            ("74", "Yes", "1H GG / BTTS Yes", "yes"),
            ("76", "No", "1H BTTS No", "no"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single(
                    "75",
                    outcome_id,
                    "1st Half - GG/NG",
                    outcome_desc,
                    guide="Will both teams score at least one goal each in the 1st Half.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "btts")
                self.assertEqual(leg["canonical_market"]["period"], "first_half")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["provider_market_guide"], "Will both teams score at least one goal each in the 1st Half.")

    def test_second_half_btts_import_supports_yes_no(self):
        cases = [
            ("74", "Yes", "2H GG / BTTS Yes", "yes"),
            ("76", "No", "2H BTTS No", "no"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single(
                    "95",
                    outcome_id,
                    "2nd Half - GG/NG",
                    outcome_desc,
                    guide="Will both teams score at least one goal each in the 2nd Half.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "btts")
                self.assertEqual(leg["canonical_market"]["period"], "second_half")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["provider_market_guide"], "Will both teams score at least one goal each in the 2nd Half.")

    def test_team_scores_both_halves_import_supports_home_away_yes_no(self):
        cases = [
            ("56", "Home Team to Score In Both Halves", "74", "Yes", "Home Team To Score In Both Halves - Yes", "home", "yes"),
            ("56", "Home Team to Score In Both Halves", "76", "No", "Home Team To Score In Both Halves - No", "home", "no"),
            ("57", "Away Team to Score In Both Halves", "74", "Yes", "Away Team To Score In Both Halves - Yes", "away", "yes"),
            ("57", "Away Team to Score In Both Halves", "76", "No", "Away Team To Score In Both Halves - No", "away", "no"),
        ]

        for market_id, desc, outcome_id, outcome_desc, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                leg = self._single(market_id, outcome_id, desc, outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_scores_both_halves")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_first_half_clean_sheet_import_supports_home_away_yes_no(self):
        cases = [
            ("76", "1st Half - Home Team Clean Sheet", "74", "Yes", "1H Home Team Clean Sheet - Yes", "home", "yes"),
            ("76", "1st Half - Home Team Clean Sheet", "76", "No", "1H Home Team Clean Sheet - No", "home", "no"),
            ("77", "1st Half - Away Team Clean Sheet", "74", "Yes", "1H Away Team Clean Sheet - Yes", "away", "yes"),
            ("77", "1st Half - Away Team Clean Sheet", "76", "No", "1H Away Team Clean Sheet - No", "away", "no"),
        ]

        for market_id, desc, outcome_id, outcome_desc, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                leg = self._single(
                    market_id,
                    outcome_id,
                    desc,
                    outcome_desc,
                    guide=f"Will {expected_subject.title()} team keep a clean sheet in the 1st Half.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_clean_sheet")
                self.assertEqual(leg["canonical_market"]["period"], "first_half")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_second_half_clean_sheet_import_supports_home_away_yes_no(self):
        cases = [
            ("96", "2nd Half - Home Team Clean Sheet", "74", "Yes", "2H Home Team Clean Sheet - Yes", "home", "yes"),
            ("96", "2nd Half - Home Team Clean Sheet", "76", "No", "2H Home Team Clean Sheet - No", "home", "no"),
            ("97", "2nd Half - Away Team Clean Sheet", "74", "Yes", "2H Away Team Clean Sheet - Yes", "away", "yes"),
            ("97", "2nd Half - Away Team Clean Sheet", "76", "No", "2H Away Team Clean Sheet - No", "away", "no"),
        ]

        for market_id, desc, outcome_id, outcome_desc, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                leg = self._single(
                    market_id,
                    outcome_id,
                    desc,
                    outcome_desc,
                    guide=f"Will the {expected_subject.title()} team keep a clean sheet in the 2nd Half.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_clean_sheet")
                self.assertEqual(leg["canonical_market"]["period"], "second_half")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_full_match_clean_sheet_import_supports_home_away_yes_no(self):
        cases = [
            ("31", "Home Team Clean Sheet", "74", "Yes", "Home Team Clean Sheet - Yes", "home", "yes"),
            ("31", "Home Team Clean Sheet", "76", "No", "Home Team Clean Sheet - No", "home", "no"),
            ("32", "Away Team Clean Sheet", "74", "Yes", "Away Team Clean Sheet - Yes", "away", "yes"),
            ("32", "Away Team Clean Sheet", "76", "No", "Away Team Clean Sheet - No", "away", "no"),
        ]

        for market_id, desc, outcome_id, outcome_desc, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                leg = self._single(market_id, outcome_id, desc, outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_clean_sheet")
                self.assertEqual(leg["canonical_market"]["period"], "full_match")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_first_half_total_keeps_its_period(self):
        leg = self._single("68", "12", "1st Half - Over/Under", "Over 0.5", "total=0.5")

        self.assertEqual(leg["market_taxonomy"]["period"], "1st_half")
        self.assertEqual(leg["market"], "1H Over 0.5")

    def test_result_total_goals_import_supports_result_and_line(self):
        cases = [
            ("794", "Home & Under 1.5", "total=1.5", "Home Win & Under 1.5", "home_under"),
            ("796", "Home & Over 2.5", "total=2.5", "Home Win & Over 2.5", "home_over"),
            ("798", "Draw & Under 3.5", "total=3.5", "Draw & Under 3.5", "draw_under"),
            ("800", "Draw & Over 4.5", "total=4.5", "Draw & Over 4.5", "draw_over"),
            ("802", "Away & Under 2.5", "total=2.5", "Away Win & Under 2.5", "away_under"),
            ("804", "Away & Over 3.5", "total=3.5", "Away Win & Over 3.5", "away_over"),
        ]

        for outcome_id, outcome_desc, specifier, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id, specifier=specifier):
                leg = self._single("37", outcome_id, "1X2 & Over/Under", outcome_desc, specifier)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "result_total_goals")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["market_taxonomy"]["family"], "result_total_goals")
                self.assertEqual(leg["market_taxonomy"]["requires_team_goal_stats"], False)

    def test_result_btts_import_supports_result_and_yes_no(self):
        cases = [
            ("78", "Home & yes", "Home Win & GG / BTTS Yes", "home_yes"),
            ("80", "Home & no", "Home Win & BTTS No", "home_no"),
            ("82", "Draw & yes", "Draw & GG / BTTS Yes", "draw_yes"),
            ("84", "Draw & no", "Draw & BTTS No", "draw_no"),
            ("86", "Away & yes", "Away Win & GG / BTTS Yes", "away_yes"),
            ("88", "Away & no", "Away Win & BTTS No", "away_no"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("35", outcome_id, "1X2 & GG/NG", outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "result_btts")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["market_taxonomy"]["family"], "result_btts")

    def test_total_btts_import_supports_line_and_yes_no(self):
        cases = [
            ("90", "Over 2.5 & Yes", "Over 2.5 & GG / BTTS Yes", "over_yes"),
            ("92", "Under 2.5 & Yes", "Under 2.5 & GG / BTTS Yes", "under_yes"),
            ("94", "Over 2.5 & No", "Over 2.5 & BTTS No", "over_no"),
            ("96", "Under 2.5 & No", "Under 2.5 & BTTS No", "under_no"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("36", outcome_id, "Over/Under & GG/NG", outcome_desc, "total=2.5")

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "total_btts")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["canonical_market"]["line"], 2.5)
                self.assertEqual(leg["market_taxonomy"]["family"], "total_btts")

    def test_double_chance_btts_import_supports_double_chance_and_yes_no(self):
        cases = [
            ("1718", "Home/Draw & Yes", "Home/Draw & GG / BTTS Yes", "home_or_draw_yes"),
            ("1719", "Home/Draw & No", "Home/Draw & BTTS No", "home_or_draw_no"),
            ("1720", "Home/Away & Yes", "Home/Away & GG / BTTS Yes", "home_or_away_yes"),
            ("1721", "Home/Away & No", "Home/Away & BTTS No", "home_or_away_no"),
            ("1722", "Draw/Away & Yes", "Draw/Away & GG / BTTS Yes", "draw_or_away_yes"),
            ("1723", "Draw/Away & No", "Draw/Away & BTTS No", "draw_or_away_no"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("546", outcome_id, "Double Chance & GG/NG", outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "double_chance_btts")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["market_taxonomy"]["family"], "double_chance_btts")

    def test_double_chance_total_import_supports_dynamic_lines(self):
        cases = [
            ("1724", "Home/Draw & Under 2.5", "Home/Draw & Under 2.5", "home_or_draw_under"),
            ("1727", "Home/Draw & Over 2.5", "Home/Draw & Over 2.5", "home_or_draw_over"),
            ("1725", "Home/Away & Under 3.5", "Home/Away & Under 3.5", "home_or_away_under"),
            ("1728", "Home/Away & Over 3.5", "Home/Away & Over 3.5", "home_or_away_over"),
            ("1726", "Draw/Away & Under 4.5", "Draw/Away & Under 4.5", "draw_or_away_under"),
            ("1729", "Draw/Away & Over 4.5", "Draw/Away & Over 4.5", "draw_or_away_over"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            line = outcome_desc.rsplit(" ", 1)[-1]
            with self.subTest(outcome_id=outcome_id, line=line):
                leg = self._single(
                    "547",
                    outcome_id,
                    f"Double Chance & Over/Under {line}",
                    outcome_desc,
                    f"total={line}",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "double_chance_total_goals")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["canonical_market"]["line"], float(line))
                self.assertEqual(leg["market_taxonomy"]["family"], "double_chance_total_goals")

    def test_result_or_total_import_supports_yes_no_and_dynamic_lines(self):
        cases = [
            ("854", "Home Team or Over 2.5", "74", "Home Win or Over 2.5 - Yes", "home_over_yes"),
            ("854", "Home Team or Over 2.5", "76", "Home Win or Over 2.5 - No", "home_over_no"),
            ("857", "Draw or Under 3.5", "74", "Draw or Under 3.5 - Yes", "draw_under_yes"),
            ("859", "Away or Under 4.5", "76", "Away Win or Under 4.5 - No", "away_under_no"),
        ]

        for market_id, market_desc, outcome_id, expected_market, expected_side in cases:
            line = market_desc.rsplit(" ", 1)[-1]
            outcome_desc = "Yes" if outcome_id == "74" else "No"
            with self.subTest(market_id=market_id, outcome_id=outcome_id, line=line):
                leg = self._single(market_id, outcome_id, market_desc, outcome_desc, f"total={line}")

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "result_or_total_goals")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["canonical_market"]["line"], float(line))
                self.assertEqual(leg["market_taxonomy"]["family"], "result_or_total_goals")

    def test_result_or_btts_import_supports_yes_no(self):
        cases = [
            ("860", "Home Team or GG", "74", "Home Win or GG / BTTS Yes - Yes", "home_btts_yes"),
            ("861", "Draw or GG", "76", "Draw or GG / BTTS Yes - No", "draw_btts_no"),
            ("862", "Away Team or GG", "74", "Away Win or GG / BTTS Yes - Yes", "away_btts_yes"),
        ]

        for market_id, market_desc, outcome_id, expected_market, expected_side in cases:
            outcome_desc = "Yes" if outcome_id == "74" else "No"
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                leg = self._single(market_id, outcome_id, market_desc, outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "result_or_btts")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["market_taxonomy"]["family"], "result_or_btts")

    def test_result_or_clean_sheet_import_supports_yes_no(self):
        cases = [
            ("863", "Home Team or Any Clean Sheet", "74", "Home Win or Any Clean Sheet - Yes", "home_clean_sheet_yes"),
            ("864", "Draw or Any Clean Sheet", "76", "Draw or Any Clean Sheet - No", "draw_clean_sheet_no"),
            ("865", "Away Team or Any Clean Sheet", "74", "Away Win or Any Clean Sheet - Yes", "away_clean_sheet_yes"),
        ]

        for market_id, market_desc, outcome_id, expected_market, expected_side in cases:
            outcome_desc = "Yes" if outcome_id == "74" else "No"
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                leg = self._single(market_id, outcome_id, market_desc, outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "result_or_clean_sheet")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["market_taxonomy"]["family"], "result_or_clean_sheet")

    def test_bookings_1x2_is_not_read_as_the_match_result(self):
        leg = self._single("136", "2", "Bookings 1X2", "Draw")

        self.assertNotEqual(leg["market_taxonomy"]["family"], "match_result")
        self.assertEqual(leg["canonical_market"]["family"], "cards_result")
        self.assertEqual(leg["market_taxonomy"]["family"], "cards_result")

    def test_full_match_corner_totals_import_with_dynamic_lines(self):
        cases = [
            ("12", "Over 8.5", "total=8.5", "Corners Over 8.5", "over"),
            ("13", "Under 8.5", "total=8.5", "Corners Under 8.5", "under"),
            ("12", "Over 10.5", "total=10.5", "Corners Over 10.5", "over"),
            ("13", "Under 12.5", "total=12.5", "Corners Under 12.5", "under"),
        ]

        for outcome_id, outcome_desc, specifier, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id, specifier=specifier):
                leg = self._single(
                    "166",
                    outcome_id,
                    "Corners - Over/Under",
                    outcome_desc,
                    specifier,
                    guide="Predict whether the total number of corners at regular time is over/under a given line.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "corners_total")
                self.assertEqual(leg["canonical_market"]["period"], "full_match")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["provider_market_guide"], "Predict whether the total number of corners at regular time is over/under a given line.")

    def test_home_and_away_team_corner_totals_import_with_dynamic_lines(self):
        cases = [
            ("900300", "30", "Home Team Total Corners", "Over 3.5", "total=3.5", "Home Team Corners Over 3.5", "home", "over"),
            ("900300", "31", "Home Team Total Corners", "Under 7.5", "total=7.5", "Home Team Corners Under 7.5", "home", "under"),
            ("900301", "30", "Away Team Total Corners", "Over 2.5", "total=2.5", "Away Team Corners Over 2.5", "away", "over"),
            ("900301", "31", "Away Team Total Corners", "Under 6.5", "total=6.5", "Away Team Corners Under 6.5", "away", "under"),
        ]

        for market_id, outcome_id, desc, outcome_desc, specifier, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, specifier=specifier):
                leg = self._single(market_id, outcome_id, desc, outcome_desc, specifier)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_corners")
                self.assertEqual(leg["canonical_market"]["period"], "full_match")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_first_half_home_and_away_team_corner_totals_import_with_dynamic_lines(self):
        cases = [
            ("900302", "30", "1st Half Home Team Corners", "Over 0.5", "total=0.5", "1H Home Team Corners Over 0.5", "home", "over"),
            ("900302", "31", "1st Half Home Team Corners", "Under 4.5", "total=4.5", "1H Home Team Corners Under 4.5", "home", "under"),
            ("900303", "30", "1st Half Away Team Corners", "Over 1.5", "total=1.5", "1H Away Team Corners Over 1.5", "away", "over"),
            ("900303", "31", "1st Half Away Team Corners", "Under 3.5", "total=3.5", "1H Away Team Corners Under 3.5", "away", "under"),
        ]

        for market_id, outcome_id, desc, outcome_desc, specifier, expected_market, expected_subject, expected_side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, specifier=specifier):
                leg = self._single(market_id, outcome_id, desc, outcome_desc, specifier)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "team_corners")
                self.assertEqual(leg["canonical_market"]["period"], "first_half")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["market_taxonomy"]["requires_corner_stats"], True)

    def test_corner_ranges_import_with_selected_bucket(self):
        cases = [
            ("169", "Corner Range", "sr:point_range:12+:1141", "0-8", "Corner Range 0-8", "match", "corner_range"),
            ("169", "Corner Range", "sr:point_range:12+:1143", "12+", "Corner Range 12+", "match", "corner_range"),
            ("170", "Home Corner Range", "sr:point_range:7+:1145", "3-4", "Home Corner Range 3-4", "home", "team_corner_range"),
            ("171", "Away Corner Range", "sr:point_range:7+:1147", "7+", "Away Corner Range 7+", "away", "team_corner_range"),
        ]

        for market_id, desc, outcome_id, outcome_desc, expected_market, expected_subject, expected_family in cases:
            with self.subTest(market_id=market_id, outcome_desc=outcome_desc):
                leg = self._single(
                    market_id,
                    outcome_id,
                    desc,
                    outcome_desc,
                    "variant=sr:point_range:7+",
                    guide="Predict the range of corners at regular time.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], expected_family)
                self.assertEqual(leg["canonical_market"]["period"], "full_match")
                self.assertEqual(leg["canonical_market"]["subject"], expected_subject)
                self.assertEqual(leg["canonical_market"]["side"], outcome_desc)
                self.assertEqual(leg["market_taxonomy"]["requires_corner_stats"], True)
                self.assertEqual(leg["provider_market_guide"], "Predict the range of corners at regular time.")

    def test_corners_1x2_import_supports_home_draw_away(self):
        cases = [
            ("1", "Home", "Corners 1X2 - Home Win", "home"),
            ("2", "Draw", "Corners 1X2 - Draw", "draw"),
            ("3", "Away", "Corners 1X2 - Away Win", "away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("162", outcome_id, "Corners - 1X2", outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "corners_result")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_first_half_corners_1x2_import_supports_home_draw_away(self):
        cases = [
            ("1", "Home", "1H Corners 1X2 - Home Win", "home"),
            ("2", "Draw", "1H Corners 1X2 - Draw", "draw"),
            ("3", "Away", "1H Corners 1X2 - Away Win", "away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("173", outcome_id, "1st Half - Corner 1X2", outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "corners_result")
                self.assertEqual(leg["canonical_market"]["period"], "first_half")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["market_taxonomy"]["requires_corner_stats"], True)

    def test_first_corner_import_supports_home_none_away(self):
        cases = [
            ("6", "Home", "1st Corner - Home", "home"),
            ("7", "None", "1st Corner - No Corner", "none"),
            ("8", "Away", "1st Corner - Away", "away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single(
                    "163",
                    outcome_id,
                    "1st Corner",
                    outcome_desc,
                    "cornernr=1",
                    guide="Predict which team will take the Nth corner at regular time.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "nth_corner")
                self.assertEqual(leg["canonical_market"]["goal_number"], 1)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_first_half_first_corner_import_supports_home_none_away(self):
        cases = [
            ("6", "Home", "1H 1st Corner - Home", "home"),
            ("7", "None", "1H 1st Corner - No Corner", "none"),
            ("8", "Away", "1H 1st Corner - Away", "away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single(
                    "174",
                    outcome_id,
                    "1st Half - 1st Corner",
                    outcome_desc,
                    "cornernr=1",
                    guide="Predict which team will take the Xth corner in the 1st Half.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "nth_corner")
                self.assertEqual(leg["canonical_market"]["period"], "first_half")
                self.assertEqual(leg["canonical_market"]["goal_number"], 1)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_last_corner_import_supports_home_none_away(self):
        cases = [
            ("6", "Home", "Last Corner - Home", "home"),
            ("7", "None", "Last Corner - No Corner", "none"),
            ("8", "Away", "Last Corner - Away", "away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("164", outcome_id, "Last Corner", outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "last_corner")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_first_half_last_corner_import_supports_home_none_away(self):
        cases = [
            ("6", "Home", "1H Last Corner - Home", "home"),
            ("7", "None", "1H Last Corner - No Corner", "none"),
            ("8", "Away", "1H Last Corner - Away", "away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("175", outcome_id, "1st Half - Last Corner", outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "last_corner")
                self.assertEqual(leg["canonical_market"]["period"], "first_half")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_first_half_corner_handicap_import_keeps_line_and_side(self):
        cases = [
            ("1714", "Home (-1.5)", "hcp=-1.5", "1H Corner Handicap Home -1.5", "home", -1.5),
            ("1715", "Away (+1.5)", "hcp=-1.5", "1H Corner Handicap Away -1.5", "away", -1.5),
            ("1714", "Home (+0.5)", "hcp=0.5", "1H Corner Handicap Home 0.5", "home", 0.5),
            ("1715", "Away (-0.5)", "hcp=0.5", "1H Corner Handicap Away 0.5", "away", 0.5),
        ]

        for outcome_id, outcome_desc, specifier, expected_market, expected_side, expected_line in cases:
            with self.subTest(outcome_id=outcome_id, specifier=specifier):
                leg = self._single("176", outcome_id, "1st Half - Corner Handicap", outcome_desc, specifier)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "corner_handicap")
                self.assertEqual(leg["canonical_market"]["period"], "first_half")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["canonical_market"]["line"], expected_line)
                self.assertEqual(leg["market_taxonomy"]["requires_corner_stats"], True)

    def test_team_shots_on_target_is_not_read_as_a_player_prop(self):
        leg = self._single("900546", "12", "Home Team Shots on Target Over/Under", "Over 9.5", "total=9.5")

        self.assertEqual(leg["canonical_market"]["family"], "team_shots_on_target")
        self.assertFalse(leg["market_taxonomy"]["requires_player_stats"])

    def test_asian_handicap_keeps_its_sign(self):
        leg = self._single("16", "1714", "Asian Handicap", "Home (-3.0)", "hcp=-3")

        self.assertEqual(leg["canonical_market"]["line"], -3.0)

    def test_european_handicap_import_keeps_line_and_outcome(self):
        cases = [
            ("hcp=0:1", "1711", "Home (0:1)", -1.0, "home", "EH Home -1"),
            ("hcp=0:2", "1712", "Draw (0:2)", -2.0, "draw", "EH Draw -2"),
            ("hcp=0:3", "1713", "Away (0:3)", -3.0, "away", "EH Away -3"),
            ("hcp=1:0", "1711", "Home (1:0)", 1.0, "home", "EH Home 1"),
            ("hcp=2:0", "1713", "Away (2:0)", 2.0, "away", "EH Away 2"),
        ]

        for specifier, outcome_id, outcome_desc, expected_line, expected_side, expected_market in cases:
            with self.subTest(specifier=specifier, outcome_id=outcome_id):
                leg = self._single("14", outcome_id, f"Handicap {specifier.removeprefix('hcp=')}", outcome_desc, specifier)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "handicap")
                self.assertEqual(leg["canonical_market"]["line"], expected_line)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["canonical_market"]["settlement"], "three_way")

    def test_sportybet_market_guide_is_preserved_for_debugging(self):
        guide = "Predict the winner in regular time taking in consideration the handicap in brackets."
        leg = self._single("14", "1711", "Handicap 0:1", "Home (0:1)", "hcp=0:1", guide=guide)

        self.assertEqual(leg["provider_market_guide"], guide)
        nested_market = leg["provider_payload"]["outcome"]["markets"][0]
        self.assertEqual(nested_market["marketGuide"], guide)

    def test_first_goal_import_supports_home_none_away(self):
        cases = [
            ("6", "Home", "1st Goal - Home", "home"),
            ("7", "None", "1st Goal - No Goal", "none"),
            ("8", "Away", "1st Goal - Away", "away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single(
                    "8",
                    outcome_id,
                    "1st Goal",
                    outcome_desc,
                    "goalnr=1",
                    guide="Predict which team will score the Nth goal in the match. Overtime not included.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "nth_goal")
                self.assertEqual(leg["canonical_market"]["goal_number"], 1)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["provider_market_guide"], "Predict which team will score the Nth goal in the match. Overtime not included.")

    def test_second_half_first_goal_import_supports_home_none_away(self):
        cases = [
            ("6", "Home", "2H 1st Goal - Home", "home"),
            ("7", "None", "2H 1st Goal - No Goal", "none"),
            ("8", "Away", "2H 1st Goal - Away", "away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single(
                    "84",
                    outcome_id,
                    "2nd Half - 1st Goal",
                    outcome_desc,
                    "goalnr=1",
                    guide="Predict which team will score the Nth goal in the 2nd Half.",
                )

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "nth_goal")
                self.assertEqual(leg["canonical_market"]["period"], "second_half")
                self.assertEqual(leg["canonical_market"]["goal_number"], 1)
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertEqual(leg["provider_market_guide"], "Predict which team will score the Nth goal in the 2nd Half.")

    def test_btts_is_recognised_through_the_importer(self):
        leg = self._single("29", "74", "GG/NG", "Yes")

        self.assertEqual(leg["market_taxonomy"]["family"], "btts")
        self.assertTrue(leg["market_taxonomy"]["recognized"])

    def test_btts_yes_and_no_import_labels_are_distinct(self):
        yes = self._single("29", "74", "GG/NG", "Yes")
        no = self._single("29", "76", "GG/NG", "No")

        self.assertEqual(yes["market"], "GG / BTTS Yes")
        self.assertEqual(no["market"], "BTTS No")
        self.assertEqual(yes["canonical_market"]["family"], "btts")
        self.assertEqual(no["canonical_market"]["family"], "btts")

    def test_teams_to_score_import_supports_all_outcomes(self):
        cases = [
            ("784", "None", "No Team To Score", "none"),
            ("788", "Only Home", "Only Home Team To Score", "only_home"),
            ("790", "Only Away", "Only Away Team To Score", "only_away"),
            ("792", "Both teams", "Both Teams To Score", "both"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("30", outcome_id, "Teams to Score", outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "teams_to_score")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)
                self.assertTrue(leg["market_taxonomy"]["recognized"])

    def test_btts_2_plus_import_stays_distinct_from_plain_btts(self):
        yes = self._single("60000", "74", "GG/NG 2+", "Yes")
        no = self._single("60000", "76", "GG/NG 2+", "No")

        self.assertEqual(yes["market"], "GG / BTTS 2+ Yes")
        self.assertEqual(no["market"], "BTTS 2+ No")
        self.assertEqual(yes["canonical_market"]["family"], "btts_n_plus")
        self.assertEqual(no["canonical_market"]["family"], "btts_n_plus")
        self.assertNotEqual(yes["canonical_market"]["family"], "btts")

    def test_no_draw_btts_import_stays_distinct_from_plain_btts(self):
        yes = self._single("900041", "39", "No Draw Both Teams To Score Yes/No", "Yes")
        no = self._single("900041", "40", "No Draw Both Teams To Score Yes/No", "No")

        self.assertEqual(yes["market"], "No Draw BTTS - Yes")
        self.assertEqual(no["market"], "No Draw BTTS - No")
        self.assertEqual(yes["canonical_market"]["family"], "no_draw_btts")
        self.assertEqual(no["canonical_market"]["family"], "no_draw_btts")
        self.assertNotEqual(yes["canonical_market"]["family"], "btts")

    def test_team_scores_in_a_row_import_labels_keep_subject_and_threshold(self):
        cases = [
            ("60010", "Any Team To Score 2 or More Goals in a Row", "Any Team To Score 2+ Goals in a Row"),
            ("60011", "Home Team To Score 2 or More Goals in a Row", "Home Team To Score 2+ Goals in a Row"),
            ("60012", "Away Team To Score 2 or More Goals in a Row", "Away Team To Score 2+ Goals in a Row"),
            ("60020", "Any Team To Score 3 or More Goals in a Row", "Any Team To Score 3+ Goals in a Row"),
            ("60021", "Home Team To Score 3 or More Goals in a Row", "Home Team To Score 3+ Goals in a Row"),
            ("60022", "Away Team To Score 3 or More Goals in a Row", "Away Team To Score 3+ Goals in a Row"),
        ]

        for market_id, desc, expected in cases:
            with self.subTest(market_id=market_id):
                yes = self._single(market_id, "74", desc, "Yes")
                no = self._single(market_id, "76", desc, "No")

                self.assertEqual(yes["market"], f"{expected} - Yes")
                self.assertEqual(no["market"], f"{expected} - No")
                self.assertEqual(yes["canonical_market"]["family"], "team_scores_n_plus")
                self.assertEqual(no["canonical_market"]["family"], "team_scores_n_plus")

    def test_1up_result_import_stays_distinct_from_home_win(self):
        leg = self._single("60200", "1", "1X2 - 1UP", "Home")

        self.assertEqual(leg["market"], "Home Win 1UP")
        self.assertEqual(leg["canonical_market"]["family"], "match_result_1up")
        self.assertEqual(leg["canonical_market"]["settlement"], "early_payout")
        self.assertIn("enhanced_result_market", leg["canonical_market"]["warnings"])
        self.assertEqual(leg["provider_market_text"], "Home Win 1UP")

    def test_1up_double_chance_import_stays_distinct_from_plain_dc(self):
        leg = self._single("60110", "11", "Double Chance - 1UP", "Draw or Away")

        self.assertEqual(leg["market"], "DC: X2 1UP")
        self.assertEqual(leg["canonical_market"]["family"], "double_chance_1up")
        self.assertEqual(leg["canonical_market"]["settlement"], "early_payout")
        self.assertIn("enhanced_double_chance_market", leg["canonical_market"]["warnings"])

    def test_1up_double_chance_import_supports_all_dc_outcomes(self):
        cases = [
            ("9", "Home or Draw", "DC: 1X 1UP", "home_or_draw"),
            ("10", "Home or Away", "DC: 12 1UP", "home_or_away"),
            ("11", "Draw or Away", "DC: X2 1UP", "draw_or_away"),
        ]

        for outcome_id, outcome_desc, expected_market, expected_side in cases:
            with self.subTest(outcome_id=outcome_id):
                leg = self._single("60110", outcome_id, "Double Chance - 1UP", outcome_desc)

                self.assertEqual(leg["market"], expected_market)
                self.assertEqual(leg["canonical_market"]["family"], "double_chance_1up")
                self.assertEqual(leg["canonical_market"]["side"], expected_side)

    def test_1up_result_import_supports_away_win(self):
        leg = self._single("60200", "3", "1X2 - 1UP", "Away")

        self.assertEqual(leg["market"], "Away Win 1UP")
        self.assertEqual(leg["canonical_market"]["family"], "match_result_1up")
        self.assertEqual(leg["canonical_market"]["side"], "away")

    def test_goalscorer_import_keeps_selected_player_name(self):
        leg = self._single(
            "40",
            "sr:player:1728609",
            "Anytime Goalscorer",
            "Abraham, Paulos (Hammarby IF)",
            "type=prematch",
        )

        self.assertEqual(leg["market"], "Abraham, Paulos (Hammarby IF) To Score")
        self.assertEqual(leg["market_taxonomy"]["family"], "player_goal")
        self.assertEqual(leg["market_taxonomy"]["subject"], "Abraham, Paulos (Hammarby IF)")
        self.assertTrue(leg["market_taxonomy"]["requires_player_stats"])
        self.assertEqual(leg["canonical_market"]["subject_player_id"], "1728609")

    def test_player_shots_import_keeps_selected_player_name(self):
        leg = self._single(
            "776",
            "pre:playerprops:72041042:1954418:1",
            "Player shots (incl. overtime)",
            "Terho, Casper (Sparta Rotterdam) 1+",
            "variant=pre:playerprops:72041042:1954418",
        )

        self.assertIn("Terho, Casper", leg["market"])
        self.assertEqual(leg["market_taxonomy"]["family"], "player_shots")
        self.assertEqual(leg["canonical_market"]["subject_player_id"], "1954418")

    def test_player_shots_on_goal_import_keeps_selected_player_name(self):
        leg = self._single(
            "777",
            "pre:playerprops:72041042:2847809:1",
            "Player shots on goal (incl. overtime)",
            "Diarra, Gaoussou (Feyenoord Rotterdam) 1+",
            "variant=pre:playerprops:72041042:2847809",
        )

        self.assertIn("Diarra, Gaoussou", leg["market"])
        self.assertEqual(leg["market_taxonomy"]["family"], "player_shots_on_target")
        self.assertEqual(leg["canonical_market"]["subject_player_id"], "2847809")

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

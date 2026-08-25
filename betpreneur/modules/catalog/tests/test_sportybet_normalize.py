"""
Regression tests for SportyBet market identity resolution.

Every case here is a real (marketId, specifier, outcomeId) triple observed on a booked
slip, paired with the market the bookmaker says it is. The old text-based path got 42
of 54 of these wrong.
"""

from django.test import SimpleTestCase

from betpreneur.modules.catalog.domain.sportybet_normalize import parse_specifier, resolve
from betpreneur.modules.markets.api import Period, Resolution, Settlement, Subject


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

    def test_european_handicap_scorelines_are_normalised_from_home_perspective(self):
        cases = [
            ("hcp=0:1", -1.0),
            ("hcp=0:2", -2.0),
            ("hcp=0:3", -3.0),
            ("hcp=1:0", 1.0),
            ("hcp=2:0", 2.0),
        ]

        for specifier, line in cases:
            with self.subTest(specifier=specifier):
                market = resolve(market_id="14", outcome_id="1711", specifier=specifier)

                self.assertEqual(market.family, "handicap")
                self.assertEqual(market.line, line)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_european_handicap_outcomes_are_home_draw_away(self):
        cases = [("1711", "home"), ("1712", "draw"), ("1713", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="14", outcome_id=outcome_id, specifier="hcp=0:2")

                self.assertEqual(market.family, "handicap")
                self.assertEqual(market.side, side)

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

    def test_home_and_away_team_totals_keep_side_line_and_outcome(self):
        cases = [
            ("19", Subject.HOME, "12", "over", 0.5),
            ("19", Subject.HOME, "13", "under", 4.5),
            ("20", Subject.AWAY, "12", "over", 0.5),
            ("20", Subject.AWAY, "13", "under", 3.5),
        ]

        for market_id, subject, outcome_id, side, line in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, line=line):
                market = resolve(market_id=market_id, outcome_id=outcome_id, specifier=f"total={line}")

                self.assertEqual(market.family, "team_total_goals")
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, line)

    def test_first_half_home_and_away_team_totals_keep_period_side_line(self):
        cases = [
            ("69", Subject.HOME, "12", "over", 0.5),
            ("69", Subject.HOME, "13", "under", 1.5),
            ("70", Subject.AWAY, "12", "over", 0.5),
            ("70", Subject.AWAY, "13", "under", 2.5),
        ]

        for market_id, subject, outcome_id, side, line in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, line=line):
                market = resolve(market_id=market_id, outcome_id=outcome_id, specifier=f"total={line}")

                self.assertEqual(market.family, "team_total_goals")
                self.assertEqual(market.period, Period.FIRST_HALF)
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, line)

    def test_second_half_home_and_away_team_totals_keep_period_side_line(self):
        cases = [
            ("91", Subject.HOME, "12", "over", 0.5),
            ("91", Subject.HOME, "13", "under", 1.5),
            ("92", Subject.AWAY, "12", "over", 0.5),
            ("92", Subject.AWAY, "13", "under", 2.5),
        ]

        for market_id, subject, outcome_id, side, line in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, line=line):
                market = resolve(market_id=market_id, outcome_id=outcome_id, specifier=f"total={line}")

                self.assertEqual(market.family, "team_total_goals")
                self.assertEqual(market.period, Period.SECOND_HALF)
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, line)

    def test_both_halves_total_goals_keeps_direction_line_and_answer(self):
        cases = [
            ("58", "74", "over_yes"),
            ("58", "76", "over_no"),
            ("59", "74", "under_yes"),
            ("59", "76", "under_no"),
        ]

        for market_id, outcome_id, side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                market = resolve(market_id=market_id, outcome_id=outcome_id, specifier="total=1.5")

                self.assertEqual(market.family, "both_halves_total_goals")
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, 1.5)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_half_btts_pair_outcomes_are_mapped(self):
        cases = [
            ("806", "no_no"),
            ("808", "yes_no"),
            ("810", "yes_yes"),
            ("812", "no_yes"),
        ]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="55", outcome_id=outcome_id)

                self.assertEqual(market.family, "half_btts_pair")
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_team_scores_both_halves_keeps_team_and_answer(self):
        cases = [
            ("56", Subject.HOME, "74", "yes"),
            ("56", Subject.HOME, "76", "no"),
            ("57", Subject.AWAY, "74", "yes"),
            ("57", Subject.AWAY, "76", "no"),
        ]

        for market_id, subject, outcome_id, side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                market = resolve(market_id=market_id, outcome_id=outcome_id)

                self.assertEqual(market.family, "team_scores_both_halves")
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_first_half_clean_sheet_keeps_team_and_answer(self):
        cases = [
            ("76", Subject.HOME, "74", "yes"),
            ("76", Subject.HOME, "76", "no"),
            ("77", Subject.AWAY, "74", "yes"),
            ("77", Subject.AWAY, "76", "no"),
        ]

        for market_id, subject, outcome_id, side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                market = resolve(market_id=market_id, outcome_id=outcome_id)

                self.assertEqual(market.family, "team_clean_sheet")
                self.assertEqual(market.period, Period.FIRST_HALF)
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)

    def test_second_half_clean_sheet_keeps_team_and_answer(self):
        cases = [
            ("96", Subject.HOME, "74", "yes"),
            ("96", Subject.HOME, "76", "no"),
            ("97", Subject.AWAY, "74", "yes"),
            ("97", Subject.AWAY, "76", "no"),
        ]

        for market_id, subject, outcome_id, side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                market = resolve(market_id=market_id, outcome_id=outcome_id)

                self.assertEqual(market.family, "team_clean_sheet")
                self.assertEqual(market.period, Period.SECOND_HALF)
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)

    def test_full_match_clean_sheet_keeps_team_and_answer(self):
        cases = [
            ("31", Subject.HOME, "74", "yes"),
            ("31", Subject.HOME, "76", "no"),
            ("32", Subject.AWAY, "74", "yes"),
            ("32", Subject.AWAY, "76", "no"),
        ]

        for market_id, subject, outcome_id, side in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id):
                market = resolve(market_id=market_id, outcome_id=outcome_id)

                self.assertEqual(market.family, "team_clean_sheet")
                self.assertEqual(market.period, Period.FULL_MATCH)
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)

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

    def test_first_goal_outcomes_are_mapped(self):
        cases = [("6", "home"), ("7", "none"), ("8", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="8", outcome_id=outcome_id, specifier="goalnr=1")

                self.assertEqual(market.family, "nth_goal")
                self.assertEqual(market.side, side)
                self.assertEqual(market.goal_number, 1)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_second_half_first_goal_outcomes_are_mapped(self):
        cases = [("6", "home"), ("7", "none"), ("8", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="84", outcome_id=outcome_id, specifier="goalnr=1")

                self.assertEqual(market.family, "nth_goal")
                self.assertEqual(market.period, Period.SECOND_HALF)
                self.assertEqual(market.side, side)
                self.assertEqual(market.goal_number, 1)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_btts_is_recognised(self):
        yes = resolve(market_id="29", outcome_id="74")
        no = resolve(market_id="29", outcome_id="76")

        self.assertEqual(yes.family, "btts")
        self.assertEqual(yes.side, "yes")
        self.assertEqual(no.side, "no")

    def test_result_total_goals_outcomes_are_mapped(self):
        cases = [
            ("794", "home_under"),
            ("796", "home_over"),
            ("798", "draw_under"),
            ("800", "draw_over"),
            ("802", "away_under"),
            ("804", "away_over"),
        ]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="37", outcome_id=outcome_id, specifier="total=2.5")

                self.assertEqual(market.family, "result_total_goals")
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, 2.5)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_result_btts_outcomes_are_mapped(self):
        cases = [
            ("78", "home_yes"),
            ("80", "home_no"),
            ("82", "draw_yes"),
            ("84", "draw_no"),
            ("86", "away_yes"),
            ("88", "away_no"),
        ]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="35", outcome_id=outcome_id)

                self.assertEqual(market.family, "result_btts")
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_total_btts_outcomes_are_mapped(self):
        cases = [
            ("90", "over_yes"),
            ("92", "under_yes"),
            ("94", "over_no"),
            ("96", "under_no"),
        ]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="36", outcome_id=outcome_id, specifier="total=2.5")

                self.assertEqual(market.family, "total_btts")
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, 2.5)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_double_chance_btts_outcomes_are_mapped(self):
        cases = [
            ("1718", "home_or_draw_yes"),
            ("1719", "home_or_draw_no"),
            ("1720", "home_or_away_yes"),
            ("1721", "home_or_away_no"),
            ("1722", "draw_or_away_yes"),
            ("1723", "draw_or_away_no"),
        ]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="546", outcome_id=outcome_id)

                self.assertEqual(market.family, "double_chance_btts")
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_double_chance_total_goals_outcomes_are_mapped(self):
        cases = [
            ("1724", "home_or_draw_under"),
            ("1725", "home_or_away_under"),
            ("1726", "draw_or_away_under"),
            ("1727", "home_or_draw_over"),
            ("1728", "home_or_away_over"),
            ("1729", "draw_or_away_over"),
        ]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="547", outcome_id=outcome_id, specifier="total=2.5")

                self.assertEqual(market.family, "double_chance_total_goals")
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, 2.5)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_result_or_total_goals_outcomes_are_mapped(self):
        cases = [
            ("854", "home_over"),
            ("855", "home_under"),
            ("856", "draw_over"),
            ("857", "draw_under"),
            ("858", "away_over"),
            ("859", "away_under"),
        ]

        for market_id, prefix in cases:
            for outcome_id, answer in [("74", "yes"), ("76", "no")]:
                with self.subTest(market_id=market_id, outcome_id=outcome_id):
                    market = resolve(market_id=market_id, outcome_id=outcome_id, specifier="total=2.5")

                    self.assertEqual(market.family, "result_or_total_goals")
                    self.assertEqual(market.side, f"{prefix}_{answer}")
                    self.assertEqual(market.line, 2.5)
                    self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_result_or_btts_outcomes_are_mapped(self):
        cases = [
            ("860", "home_btts"),
            ("861", "draw_btts"),
            ("862", "away_btts"),
        ]

        for market_id, prefix in cases:
            for outcome_id, answer in [("74", "yes"), ("76", "no")]:
                with self.subTest(market_id=market_id, outcome_id=outcome_id):
                    market = resolve(market_id=market_id, outcome_id=outcome_id)

                    self.assertEqual(market.family, "result_or_btts")
                    self.assertEqual(market.side, f"{prefix}_{answer}")
                    self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_result_or_clean_sheet_outcomes_are_mapped(self):
        cases = [
            ("863", "home_clean_sheet"),
            ("864", "draw_clean_sheet"),
            ("865", "away_clean_sheet"),
        ]

        for market_id, prefix in cases:
            for outcome_id, answer in [("74", "yes"), ("76", "no")]:
                with self.subTest(market_id=market_id, outcome_id=outcome_id):
                    market = resolve(market_id=market_id, outcome_id=outcome_id)

                    self.assertEqual(market.family, "result_or_clean_sheet")
                    self.assertEqual(market.side, f"{prefix}_{answer}")
                    self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_first_half_btts_is_distinct_from_full_match_btts(self):
        yes = resolve(market_id="75", outcome_id="74")
        no = resolve(market_id="75", outcome_id="76")

        self.assertEqual(yes.family, "btts")
        self.assertEqual(yes.period, Period.FIRST_HALF)
        self.assertEqual(yes.side, "yes")
        self.assertEqual(no.period, Period.FIRST_HALF)
        self.assertEqual(no.side, "no")

    def test_second_half_btts_is_distinct_from_full_match_btts(self):
        yes = resolve(market_id="95", outcome_id="74")
        no = resolve(market_id="95", outcome_id="76")

        self.assertEqual(yes.family, "btts")
        self.assertEqual(yes.period, Period.SECOND_HALF)
        self.assertEqual(yes.side, "yes")
        self.assertEqual(no.period, Period.SECOND_HALF)
        self.assertEqual(no.side, "no")

    def test_teams_to_score_outcomes_are_mapped(self):
        cases = [
            ("784", "none"),
            ("788", "only_home"),
            ("790", "only_away"),
            ("792", "both"),
        ]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="30", outcome_id=outcome_id)

                self.assertEqual(market.family, "teams_to_score")
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_btts_2_plus_is_not_plain_btts(self):
        yes = resolve(market_id="60000", outcome_id="74")
        no = resolve(market_id="60000", outcome_id="76")

        self.assertEqual(yes.family, "btts_n_plus")
        self.assertEqual(yes.side, "yes")
        self.assertEqual(yes.goal_number, 2)
        self.assertEqual(no.family, "btts_n_plus")
        self.assertEqual(no.side, "no")
        self.assertEqual(no.goal_number, 2)

    def test_no_draw_btts_is_not_plain_btts(self):
        yes = resolve(market_id="900041", outcome_id="39")
        no = resolve(market_id="900041", outcome_id="40")

        self.assertEqual(yes.family, "no_draw_btts")
        self.assertEqual(yes.side, "yes")
        self.assertEqual(no.family, "no_draw_btts")
        self.assertEqual(no.side, "no")

    def test_team_scores_in_a_row_markets_keep_team_and_threshold(self):
        cases = [
            ("60010", "either", 2),
            ("60011", "home", 2),
            ("60012", "away", 2),
            ("60020", "either", 3),
            ("60021", "home", 3),
            ("60022", "away", 3),
        ]

        for market_id, subject, threshold in cases:
            with self.subTest(market_id=market_id):
                yes = resolve(market_id=market_id, outcome_id="74")
                no = resolve(market_id=market_id, outcome_id="76")

                self.assertEqual(yes.family, "team_scores_n_plus")
                self.assertEqual(str(yes.subject), subject)
                self.assertEqual(yes.side, "yes")
                self.assertEqual(yes.goal_number, threshold)
                self.assertEqual(no.side, "no")
                self.assertEqual(no.goal_number, threshold)

    def test_1up_result_is_not_plain_match_result(self):
        market = resolve(market_id="60200", outcome_id="1")

        self.assertEqual(market.family, "match_result_1up")
        self.assertEqual(market.side, "home")
        self.assertEqual(market.settlement, Settlement.EARLY_PAYOUT)
        self.assertIn("early_payout_market", market.warnings)

    def test_1up_double_chance_is_not_plain_double_chance(self):
        market = resolve(market_id="60110", outcome_id="11")

        self.assertEqual(market.family, "double_chance_1up")
        self.assertEqual(market.side, "draw_or_away")
        self.assertEqual(market.settlement, Settlement.EARLY_PAYOUT)
        self.assertIn("enhanced_double_chance_market", market.warnings)

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

    def test_full_match_corner_totals_support_dynamic_lines(self):
        cases = [
            ("12", "over", 8.5),
            ("13", "under", 8.5),
            ("12", "over", 10.5),
            ("13", "under", 12.5),
        ]

        for outcome_id, side, line in cases:
            with self.subTest(outcome_id=outcome_id, line=line):
                market = resolve(market_id="166", outcome_id=outcome_id, specifier=f"total={line}")

                self.assertEqual(market.family, "corners_total")
                self.assertEqual(market.period, Period.FULL_MATCH)
                self.assertEqual(market.subject, Subject.MATCH)
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, line)

    def test_home_and_away_team_corner_totals_support_dynamic_lines(self):
        cases = [
            ("900300", Subject.HOME, "30", "over", 3.5),
            ("900300", Subject.HOME, "31", "under", 7.5),
            ("900301", Subject.AWAY, "30", "over", 2.5),
            ("900301", Subject.AWAY, "31", "under", 6.5),
        ]

        for market_id, subject, outcome_id, side, line in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, line=line):
                market = resolve(market_id=market_id, outcome_id=outcome_id, specifier=f"total={line}")

                self.assertEqual(market.family, "team_corners")
                self.assertEqual(market.period, Period.FULL_MATCH)
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, line)

    def test_first_half_home_and_away_team_corner_totals_support_dynamic_lines(self):
        cases = [
            ("900302", Subject.HOME, "30", "over", 0.5),
            ("900302", Subject.HOME, "31", "under", 4.5),
            ("900303", Subject.AWAY, "30", "over", 1.5),
            ("900303", Subject.AWAY, "31", "under", 3.5),
        ]

        for market_id, subject, outcome_id, side, line in cases:
            with self.subTest(market_id=market_id, outcome_id=outcome_id, line=line):
                market = resolve(market_id=market_id, outcome_id=outcome_id, specifier=f"total={line}")

                self.assertEqual(market.family, "team_corners")
                self.assertEqual(market.period, Period.FIRST_HALF)
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, line)

    def test_corner_ranges_keep_selected_bucket(self):
        cases = [
            ("169", Subject.MATCH, "sr:point_range:12+:1141", "0-8", "corner_range"),
            ("169", Subject.MATCH, "sr:point_range:12+:1143", "12+", "corner_range"),
            ("170", Subject.HOME, "sr:point_range:7+:1145", "3-4", "team_corner_range"),
            ("171", Subject.AWAY, "sr:point_range:7+:1147", "7+", "team_corner_range"),
        ]

        for market_id, subject, outcome_id, outcome_label, family in cases:
            with self.subTest(market_id=market_id, outcome_label=outcome_label):
                market = resolve(
                    market_id=market_id,
                    outcome_id=outcome_id,
                    specifier="variant=sr:point_range:7+",
                    outcome_label=outcome_label,
                )

                self.assertEqual(market.family, family)
                self.assertEqual(market.period, Period.FULL_MATCH)
                self.assertEqual(market.subject, subject)
                self.assertEqual(market.side, outcome_label)
                self.assertEqual(market.settlement, Settlement.WIN_LOSE)

    def test_corners_1x2_is_not_match_result(self):
        cases = [("1", "home"), ("2", "draw"), ("3", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="162", outcome_id=outcome_id)

                self.assertEqual(market.family, "corners_result")
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_first_half_corner_1x2_keeps_period(self):
        cases = [("1", "home"), ("2", "draw"), ("3", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="173", outcome_id=outcome_id)

                self.assertEqual(market.family, "corners_result")
                self.assertEqual(market.period, Period.FIRST_HALF)
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_first_corner_outcomes_are_mapped(self):
        cases = [("6", "home"), ("7", "none"), ("8", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="163", outcome_id=outcome_id, specifier="cornernr=1")

                self.assertEqual(market.family, "nth_corner")
                self.assertEqual(market.side, side)
                self.assertEqual(market.goal_number, 1)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_first_half_first_corner_outcomes_are_mapped(self):
        cases = [("6", "home"), ("7", "none"), ("8", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="174", outcome_id=outcome_id, specifier="cornernr=1")

                self.assertEqual(market.family, "nth_corner")
                self.assertEqual(market.period, Period.FIRST_HALF)
                self.assertEqual(market.side, side)
                self.assertEqual(market.goal_number, 1)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_last_corner_outcomes_are_mapped(self):
        cases = [("6", "home"), ("7", "none"), ("8", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="164", outcome_id=outcome_id)

                self.assertEqual(market.family, "last_corner")
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_first_half_last_corner_outcomes_are_mapped(self):
        cases = [("6", "home"), ("7", "none"), ("8", "away")]

        for outcome_id, side in cases:
            with self.subTest(outcome_id=outcome_id):
                market = resolve(market_id="175", outcome_id=outcome_id)

                self.assertEqual(market.family, "last_corner")
                self.assertEqual(market.period, Period.FIRST_HALF)
                self.assertEqual(market.side, side)
                self.assertEqual(market.settlement, Settlement.THREE_WAY)

    def test_first_half_corner_handicap_keeps_line_and_side(self):
        cases = [
            ("1714", "home", -1.5),
            ("1715", "away", -1.5),
            ("1714", "home", 0.5),
            ("1715", "away", 0.5),
        ]

        for outcome_id, side, line in cases:
            with self.subTest(outcome_id=outcome_id, line=line):
                market = resolve(market_id="176", outcome_id=outcome_id, specifier=f"hcp={line}")

                self.assertEqual(market.family, "corner_handicap")
                self.assertEqual(market.period, Period.FIRST_HALF)
                self.assertEqual(market.side, side)
                self.assertEqual(market.line, line)

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

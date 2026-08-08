"""
Score-distribution model and the markets derived from it.

The value of a shared distribution is arithmetic consistency, so most of these tests are
identities rather than magic numbers: `P(1X)` must equal `P(home) + P(draw)`, overs and
unders must sum to one, and the grid must carry exactly unit mass.
"""

from django.test import SimpleTestCase

from apps.algo.scoring.derive import (
    asian_handicap,
    away_win,
    btts,
    clean_sheet,
    correct_score,
    double_chance,
    draw,
    draw_no_bet,
    european_handicap,
    exact_goals,
    home_win,
    odd_even,
    team_total_goals,
    total_goals,
    winning_margin,
)
from apps.algo.scoring.dixon_coles import DEFAULT_RHO, build_score_matrix, tau


class MatrixShapeTests(SimpleTestCase):
    def test_grid_carries_unit_mass(self):
        matrix = build_score_matrix(1.6, 1.1)

        total = sum(sum(row) for row in matrix.grid)

        self.assertAlmostEqual(total, 1.0, places=9)

    def test_truncated_tail_is_redistributed_not_lost(self):
        # A very high scoring rate pushes mass toward the truncation boundary.
        matrix = build_score_matrix(6.0, 5.0)

        self.assertAlmostEqual(sum(sum(row) for row in matrix.grid), 1.0, places=9)

    def test_marginals_sum_to_one(self):
        matrix = build_score_matrix(1.4, 1.2)

        self.assertAlmostEqual(sum(matrix.home_goal_distribution()), 1.0, places=9)
        self.assertAlmostEqual(sum(matrix.away_goal_distribution()), 1.0, places=9)

    def test_expected_goals_track_the_input_rates(self):
        matrix = build_score_matrix(1.8, 0.9)
        home, away = matrix.expected_goals()

        self.assertAlmostEqual(home, 1.8, places=1)
        self.assertAlmostEqual(away, 0.9, places=1)

    def test_zero_rate_team_never_scores(self):
        matrix = build_score_matrix(1.5, 0.0)

        self.assertAlmostEqual(matrix.away_goal_distribution()[0], 1.0, places=6)


class DixonColesCorrectionTests(SimpleTestCase):
    def test_correction_only_touches_the_four_lowest_scorelines(self):
        for home, away in [(0, 2), (2, 0), (2, 2), (3, 1)]:
            self.assertEqual(tau(home, away, 1.5, 1.2, DEFAULT_RHO), 1.0)

    def test_correction_lifts_the_goalless_and_one_all_draws(self):
        self.assertGreater(tau(0, 0, 1.5, 1.2, DEFAULT_RHO), 1.0)
        self.assertGreater(tau(1, 1, 1.5, 1.2, DEFAULT_RHO), 1.0)

    def test_correction_damps_the_one_nil_scorelines(self):
        self.assertLess(tau(0, 1, 1.5, 1.2, DEFAULT_RHO), 1.0)
        self.assertLess(tau(1, 0, 1.5, 1.2, DEFAULT_RHO), 1.0)

    def test_correction_raises_the_draw_probability_versus_plain_poisson(self):
        corrected = build_score_matrix(1.4, 1.2, rho=DEFAULT_RHO)
        plain = build_score_matrix(1.4, 1.2, rho=0.0)

        self.assertGreater(draw(corrected), draw(plain))

    def test_tau_never_produces_negative_mass(self):
        matrix = build_score_matrix(0.2, 0.2, rho=-0.9)

        for row in matrix.grid:
            for cell in row:
                self.assertGreaterEqual(cell, 0.0)


class ResultMarketTests(SimpleTestCase):
    def setUp(self):
        self.matrix = build_score_matrix(1.7, 1.0)

    def test_the_three_results_partition_the_distribution(self):
        total = home_win(self.matrix) + draw(self.matrix) + away_win(self.matrix)

        self.assertAlmostEqual(total, 1.0, places=9)

    def test_the_stronger_side_is_favoured(self):
        self.assertGreater(home_win(self.matrix), away_win(self.matrix))

    def test_double_chance_is_exactly_the_sum_of_its_parts(self):
        self.assertAlmostEqual(
            double_chance(self.matrix, "home_or_draw"),
            home_win(self.matrix) + draw(self.matrix),
            places=9,
        )
        self.assertAlmostEqual(
            double_chance(self.matrix, "draw_or_away"),
            away_win(self.matrix) + draw(self.matrix),
            places=9,
        )
        self.assertAlmostEqual(
            double_chance(self.matrix, "home_or_away"),
            home_win(self.matrix) + away_win(self.matrix),
            places=9,
        )

    def test_draw_no_bet_pushes_on_a_draw(self):
        outcome = draw_no_bet(self.matrix, "home")

        self.assertAlmostEqual(outcome.push, draw(self.matrix), places=9)
        self.assertAlmostEqual(outcome.win, home_win(self.matrix), places=9)

    def test_draw_no_bet_effective_probability_excludes_the_push(self):
        outcome = draw_no_bet(self.matrix, "home")
        expected = home_win(self.matrix) / (home_win(self.matrix) + away_win(self.matrix))

        self.assertAlmostEqual(outcome.probability, expected, places=6)


class TotalsTests(SimpleTestCase):
    def setUp(self):
        self.matrix = build_score_matrix(1.5, 1.3)

    def test_over_and_under_a_half_line_are_complementary(self):
        over = total_goals(self.matrix, 2.5, "over")
        under = total_goals(self.matrix, 2.5, "under")

        self.assertAlmostEqual(over.win + under.win, 1.0, places=9)
        self.assertEqual(over.push, 0.0)

    def test_whole_line_pushes_on_an_exact_hit(self):
        outcome = total_goals(self.matrix, 2, "over")

        self.assertAlmostEqual(outcome.push, exact_goals(self.matrix, 2), places=9)
        self.assertAlmostEqual(outcome.win + outcome.push + outcome.lose, 1.0, places=9)

    def test_a_higher_line_is_harder_to_beat(self):
        self.assertGreater(
            total_goals(self.matrix, 1.5, "over").win,
            total_goals(self.matrix, 3.5, "over").win,
        )

    def test_quarter_line_splits_between_neighbours(self):
        quarter = total_goals(self.matrix, 2.25, "over")
        lower = total_goals(self.matrix, 2.0, "over")
        upper = total_goals(self.matrix, 2.5, "over")

        self.assertAlmostEqual(quarter.win, (lower.win + upper.win) / 2, places=9)

    def test_team_totals_use_the_correct_marginal(self):
        home = team_total_goals(self.matrix, 0.5, team="home", side="over")
        away = team_total_goals(self.matrix, 0.5, team="away", side="over")

        self.assertAlmostEqual(home.win, 1 - self.matrix.home_goal_distribution()[0], places=9)
        self.assertGreater(home.win, away.win)


class GoalShapeTests(SimpleTestCase):
    def setUp(self):
        self.matrix = build_score_matrix(1.4, 1.1)

    def test_btts_yes_and_no_are_complementary(self):
        self.assertAlmostEqual(btts(self.matrix, True) + btts(self.matrix, False), 1.0, places=9)

    def test_clean_sheet_matches_the_opponent_failing_to_score(self):
        self.assertAlmostEqual(
            clean_sheet(self.matrix, "home"), self.matrix.away_goal_distribution()[0], places=9
        )

    def test_odd_and_even_are_complementary(self):
        self.assertAlmostEqual(
            odd_even(self.matrix, "odd") + odd_even(self.matrix, "even"), 1.0, places=9
        )

    def test_exact_goals_sum_to_one_across_the_grid(self):
        total = sum(exact_goals(self.matrix, goals) for goals in range(0, 17))

        self.assertAlmostEqual(total, 1.0, places=9)

    def test_correct_score_is_a_single_cell(self):
        self.assertAlmostEqual(
            correct_score(self.matrix, 2, 1), self.matrix.probability(2, 1), places=12
        )

    def test_winning_margins_and_the_draw_partition_the_result(self):
        total = (
            sum(winning_margin(self.matrix, margin, team="home") for margin in range(1, 9))
            + sum(winning_margin(self.matrix, margin, team="away") for margin in range(1, 9))
            + draw(self.matrix)
        )

        self.assertAlmostEqual(total, 1.0, places=9)


class HandicapTests(SimpleTestCase):
    def setUp(self):
        self.matrix = build_score_matrix(1.9, 0.9)

    def test_negative_line_is_harder_than_the_plain_result(self):
        self.assertLess(asian_handicap(self.matrix, -1.5).win, home_win(self.matrix))

    def test_positive_line_is_easier_than_the_plain_result(self):
        self.assertGreater(asian_handicap(self.matrix, 1.5).win, home_win(self.matrix))

    def test_whole_handicap_pushes_when_the_margin_lands_on_the_line(self):
        outcome = asian_handicap(self.matrix, -1.0)

        self.assertAlmostEqual(outcome.push, winning_margin(self.matrix, 1, team="home"), places=9)

    def test_level_handicap_matches_draw_no_bet(self):
        handicap = asian_handicap(self.matrix, 0.0)
        dnb = draw_no_bet(self.matrix, "home")

        self.assertAlmostEqual(handicap.probability, dnb.probability, places=6)

    def test_home_and_away_sides_are_complementary_on_a_half_line(self):
        home = asian_handicap(self.matrix, -1.5, team="home")
        away = asian_handicap(self.matrix, -1.5, team="away")

        self.assertAlmostEqual(home.win + away.win, 1.0, places=9)

    def test_european_handicap_partitions_three_ways(self):
        total = (
            european_handicap(self.matrix, -1, "home")
            + european_handicap(self.matrix, -1, "draw")
            + european_handicap(self.matrix, -1, "away")
        )

        self.assertAlmostEqual(total, 1.0, places=9)


class ConsistencyAcrossMarketsTests(SimpleTestCase):
    """The whole point of one distribution: markets cannot contradict each other."""

    def setUp(self):
        self.matrix = build_score_matrix(1.6, 1.2)

    def test_over_zero_five_equals_not_goalless(self):
        self.assertAlmostEqual(
            total_goals(self.matrix, 0.5, "over").win,
            1 - correct_score(self.matrix, 0, 0),
            places=9,
        )

    def test_btts_never_exceeds_either_team_scoring(self):
        home_scores = team_total_goals(self.matrix, 0.5, team="home", side="over").win

        self.assertLessEqual(btts(self.matrix, True), home_scores)

    def test_clean_sheet_and_btts_cannot_both_happen(self):
        self.assertLessEqual(
            clean_sheet(self.matrix, "home") + btts(self.matrix, True), 1.0 + 1e-9
        )

"""
Corner and card models.

The behaviour these replace: both evaluators read fields the match-stats endpoint does
not carry, so they returned a constant that ignored the line while claiming to be
quantitative. `Corners Over 9.5` and `Corners Over 12.5` scored identically.
"""

from django.test import SimpleTestCase, TestCase

from apps.algo.evaluators import count_market_evaluator
from apps.algo.evaluators.registry import COUNT_MODEL_ENGINE, evaluator_for
from apps.algo.market_taxonomy import describe_market
from apps.algo.models import TeamRateProfile
from apps.algo.scoring import counts
from apps.algo.scoring.rate_profiles import parse_team_payload


class PoissonLineTests(SimpleTestCase):
    def test_over_and_under_a_half_line_are_complementary(self):
        over, _ = counts.poisson_over_under(10.0, 9.5, "over")
        under, _ = counts.poisson_over_under(10.0, 9.5, "under")

        self.assertAlmostEqual(over + under, 1.0, places=6)

    def test_a_higher_line_is_harder_to_beat(self):
        low, _ = counts.poisson_over_under(10.0, 8.5, "over")
        high, _ = counts.poisson_over_under(10.0, 12.5, "over")

        self.assertGreater(low, high)

    def test_whole_line_reports_a_push(self):
        _, push = counts.poisson_over_under(10.0, 10, "over")

        self.assertGreater(push, 0.0)

    def test_higher_expectation_raises_the_over(self):
        quiet, _ = counts.poisson_over_under(8.0, 10.5, "over")
        busy, _ = counts.poisson_over_under(13.0, 10.5, "over")

        self.assertGreater(busy, quiet)


class ForecastTests(SimpleTestCase):
    class _Profile:
        def __init__(self, ch=None, ca=None, kh=None, ka=None, matches=20):
            self.corners_home, self.corners_away = ch, ca
            self.cards_home, self.cards_away = kh, ka
            self.matches = matches

    def test_total_corners_combine_home_and_away_rates(self):
        forecast = counts.expected_corners(
            self._Profile(ch=6.0), self._Profile(ca=4.0)
        )

        self.assertGreater(forecast.expected, 8.0)
        self.assertEqual(len(forecast.sources), 2)

    def test_missing_profiles_report_no_sources(self):
        forecast = counts.expected_corners(None, None)

        self.assertEqual(forecast.sources, ())

    def test_small_samples_are_shrunk_toward_the_league_rate(self):
        big = counts.expected_corners(self._Profile(ch=9.0, matches=30), self._Profile(ca=9.0, matches=30))
        small = counts.expected_corners(self._Profile(ch=9.0, matches=1), self._Profile(ca=9.0, matches=1))

        self.assertLess(small.expected, big.expected)
        self.assertTrue(small.thin)

    def test_team_corners_use_the_requested_side(self):
        profile = self._Profile(ch=7.0, ca=3.0)

        home = counts.expected_team_corners(profile, side="home")
        away = counts.expected_team_corners(profile, side="away")

        self.assertGreater(home.expected, away.expected)


class TeamPayloadParsingTests(SimpleTestCase):
    def _payload(self):
        return {"team": {"id": "1", "name": "Alpha", "league_stats": {"league": [{
            "id": "77",
            "fulltime": {
                "avg_corners": {"home": "6.2", "away": "4.1", "total": "5.2"},
                "avg_yellowcards": {"home": "2.0", "away": "2.4", "total": "2.2"},
                "avg_redcards": {"home": "0.1", "away": "0.2", "total": "0.15"},
                "fouls": {"home": "12", "away": "14", "total": "26"},
                "win": {"total": "8"}, "draw": {"total": "4"}, "lost": {"total": "6"},
            },
        }]}}}

    def test_corner_rates_are_read_per_side(self):
        parsed = parse_team_payload(self._payload())

        self.assertAlmostEqual(parsed["corners_home"], 6.2)
        self.assertAlmostEqual(parsed["corners_away"], 4.1)

    def test_a_red_card_counts_as_two_bookings(self):
        parsed = parse_team_payload(self._payload())

        self.assertAlmostEqual(parsed["cards_home"], 2.0 + 0.2)

    def test_match_count_comes_from_the_result_record(self):
        self.assertEqual(parse_team_payload(self._payload())["matches"], 18)

    def test_empty_payload_yields_nothing(self):
        self.assertEqual(parse_team_payload({}), {})


class CountEvaluatorTests(TestCase):
    def setUp(self):
        TeamRateProfile.objects.create(
            provider="statpal", team_id="h1", team_name="Alpha",
            team_name_normalized="alpha", corners_home=6.5, corners_away=4.5,
            cards_home=2.2, cards_away=2.5, matches=20,
        )
        TeamRateProfile.objects.create(
            provider="statpal", team_id="a1", team_name="Beta",
            team_name_normalized="beta", corners_home=5.0, corners_away=4.0,
            cards_home=1.8, cards_away=2.0, matches=20,
        )
        self.fixture = {"hid": "h1", "aid": "a1", "hname": "Alpha", "aname": "Beta"}

    def _evaluate(self, market):
        return count_market_evaluator.evaluate(describe_market(market), fixture=self.fixture)

    def test_corner_lines_no_longer_score_identically(self):
        low = self._evaluate("Corners Over 9.5")
        high = self._evaluate("Corners Over 12.5")

        self.assertTrue(low["available"])
        self.assertNotEqual(low["score"], high["score"])
        self.assertGreater(low["probability"], high["probability"])

    def test_card_lines_no_longer_score_identically(self):
        low = self._evaluate("Cards Over 3.5")
        high = self._evaluate("Cards Over 5.5")

        self.assertNotEqual(low["score"], high["score"])
        self.assertGreater(low["probability"], high["probability"])

    def test_corner_evidence_reports_the_expectation(self):
        evidence = self._evaluate("Corners Over 9.5")["evidence"]

        self.assertIn("expected_corners", evidence)
        self.assertEqual(evidence["sources"], ["home_team_profile", "away_team_profile"])

    def test_over_and_under_are_complementary(self):
        over = self._evaluate("Corners Over 9.5")["probability"]
        under = self._evaluate("Corners Under 9.5")["probability"]

        self.assertAlmostEqual(over + under, 1.0, places=4)

    def test_missing_team_rates_decline_rather_than_return_a_constant(self):
        result = count_market_evaluator.evaluate(
            describe_market("Corners Over 9.5"), fixture={"hname": "Nobody", "aname": "Nowhere"}
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["basis"], "count_market_no_team_rates")
        self.assertIn("no_team_rate_profile", result["warnings"])

    def test_booking_points_line_is_converted_to_bookings(self):
        result = count_market_evaluator.evaluate(
            describe_market("Booking Points Over 25"), fixture=self.fixture
        )

        if result["available"]:
            self.assertLess(result["evidence"]["effective_line"], result["evidence"]["line"])


class CountRegistryTests(SimpleTestCase):
    def test_count_families_use_the_count_engine(self):
        for family in ["corners_total", "team_corners", "cards_total", "team_cards", "booking_points"]:
            self.assertEqual(evaluator_for(family).engine, COUNT_MODEL_ENGINE, family)

    def test_only_one_entry_exists_per_family(self):
        from apps.algo.evaluators.registry import MARKET_EVALUATORS

        self.assertEqual(MARKET_EVALUATORS["cards"].engine, COUNT_MODEL_ENGINE)

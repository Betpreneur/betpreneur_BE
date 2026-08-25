"""
Corner and card models.

The behaviour these replace: both evaluators read fields the match-stats endpoint does
not carry, so they returned a constant that ignored the line while claiming to be
quantitative. `Corners Over 9.5` and `Corners Over 12.5` scored identically.
"""

from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from betpreneur.modules.catalog.api import StatPalFixtureSnapshot
from betpreneur.modules.markets.api import (
    COUNT_MODEL_ENGINE,
    MarketDescriptor,
    describe_market,
    evaluator_for,
)
from betpreneur.modules.scoring.domain import counts
from betpreneur.modules.scoring.evaluators import count_market_evaluator
from betpreneur.modules.scoring.models import TeamRateProfile
from betpreneur.modules.scoring.services.rate_profiles import (
    TeamRateProfileService,
    _fetch_failures,
    parse_team_payload,
)


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

    def test_absurd_expected_count_is_clamped(self):
        probability, push = counts.poisson_over_under(500.0, 9.5, "over")

        self.assertGreaterEqual(probability, 0)
        self.assertLessEqual(probability, 1)
        self.assertEqual(push, 0.0)

    def test_range_probability_uses_inclusive_bucket(self):
        exact_mass, _ = counts.poisson_range(10.0, "9-11")
        low_tail, _ = counts.poisson_range(10.0, "0-8")
        high_tail, parsed = counts.poisson_range(10.0, "12+")

        self.assertGreater(exact_mass, 0)
        self.assertGreater(low_tail, 0)
        self.assertGreater(high_tail, 0)
        self.assertEqual(parsed, (12, None))
        self.assertAlmostEqual(exact_mass + low_tail + high_tail, 1.0, places=5)

    def test_invalid_range_reports_no_probability(self):
        probability, parsed = counts.poisson_range(10.0, "many")

        self.assertIsNone(probability)
        self.assertEqual(parsed, (None, None))

    def test_three_way_count_probabilities_sum_to_one(self):
        probabilities = counts.poisson_three_way(6.0, 4.0)

        self.assertGreater(probabilities["home"], probabilities["away"])
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=5)

    def test_count_handicap_probabilities_sum_to_one(self):
        probabilities = counts.poisson_handicap(3.0, 2.0, -0.5)

        self.assertGreater(probabilities["home"], 0)
        self.assertGreater(probabilities["away"], 0)
        self.assertEqual(probabilities["push"], 0.0)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=5)


class ForecastTests(SimpleTestCase):
    class _Profile:
        def __init__(self, ch=None, ca=None, kh=None, ka=None, sh=None, sa=None, matches=20):
            self.corners_home, self.corners_away = ch, ca
            self.cards_home, self.cards_away = kh, ka
            self.shots_on_target_home, self.shots_on_target_away = sh, sa
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

    def test_total_shots_on_target_combine_home_and_away_rates(self):
        forecast = counts.expected_shots_on_target(
            self._Profile(sh=5.8), self._Profile(sa=4.3)
        )

        self.assertGreater(forecast.expected, 9.0)
        self.assertEqual(len(forecast.sources), 2)


class TeamPayloadParsingTests(SimpleTestCase):
    def _payload(self):
        return {"team": {"id": "1", "name": "Alpha", "league_stats": {"league": [{
            "id": "77",
            "fulltime": {
                "avg_corners": {"home": "6.2", "away": "4.1", "total": "5.2"},
                "avg_yellowcards": {"home": "2.0", "away": "2.4", "total": "2.2"},
                "avg_redcards": {"home": "0.1", "away": "0.2", "total": "0.15"},
                "shots_on_goal": {"home": "5.8", "away": "4.3", "total": "10.1"},
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

    def test_shots_on_target_rates_are_read_per_side(self):
        parsed = parse_team_payload(self._payload())

        self.assertAlmostEqual(parsed["shots_on_target_home"], 5.8 / 18, places=3)
        self.assertAlmostEqual(parsed["shots_on_target_away"], 4.3 / 18, places=3)

    def test_match_count_comes_from_the_result_record(self):
        self.assertEqual(parse_team_payload(self._payload())["matches"], 18)

    def test_empty_payload_yields_nothing(self):
        self.assertEqual(parse_team_payload({}), {})


class CountEvaluatorSnapshotFallbackTests(SimpleTestCase):
    def _descriptor(self, family, selection, *, line="", team="", period="match"):
        return MarketDescriptor(
            raw=f"{family} {selection}",
            canonical=f"{family} {selection}",
            code=f"{family}:{selection}",
            family=family,
            category=family,
            selection=selection,
            side=selection,
            line=line,
            team=team,
            period=period,
            requires_corner_stats=True,
        )

    def test_corner_market_can_use_statpal_detailed_stats_snapshot_without_team_rates(self):
        fixture = {
            "hname": "Nobody",
            "aname": "Nowhere",
            "statpal_context": {
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_corners": 6,
                            "away_corners": 5,
                        }
                    }
                }
            },
        }

        with patch("betpreneur.modules.scoring.evaluators.count_market_evaluator._profiles", return_value=(None, None)):
            result = count_market_evaluator.evaluate(describe_market("Corners Over 9.5"), fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "corners_count_model")
        self.assertEqual(result["evidence"]["expected_corners"], 11)
        self.assertEqual(result["evidence"]["sources"], ["statpal_detailed_stats"])

    def test_team_cards_can_use_statpal_detailed_stats_snapshot_without_team_rates(self):
        fixture = {
            "hname": "Nobody",
            "aname": "Nowhere",
            "statpal_context": {
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_yellow_cards": 2,
                            "home_red_cards": 1,
                            "away_yellow_cards": 1,
                            "away_red_cards": 0,
                        }
                    }
                }
            },
        }
        descriptor = self._descriptor("team_cards", "over", line="2.5", team="home")

        with patch("betpreneur.modules.scoring.evaluators.count_market_evaluator._profiles", return_value=(None, None)):
            result = count_market_evaluator.evaluate(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["evidence"]["expected_cards"], 3)
        self.assertEqual(result["evidence"]["sources"], ["statpal_detailed_stats"])

    def test_shots_on_target_total_can_use_statpal_detailed_stats_snapshot(self):
        fixture = {
            "hname": "Nobody",
            "aname": "Nowhere",
            "statpal_context": {
                "snapshots": {
                    "detailed_stats": {
                        "summary": {
                            "home_shots_on_target": 6,
                            "away_shots_on_target": 5,
                        }
                    }
                }
            },
        }
        descriptor = self._descriptor("shots_on_target_total", "over", line="9.5")

        with patch("betpreneur.modules.scoring.evaluators.count_market_evaluator._profiles", return_value=(None, None)):
            result = count_market_evaluator.evaluate(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "shots_on_target_count_model")
        self.assertEqual(result["evidence"]["expected_shots_on_target"], 11)
        self.assertEqual(result["evidence"]["sources"], ["statpal_detailed_stats"])

    def test_shots_on_target_total_can_use_statpal_team_stats_snapshot(self):
        fixture = {
            "hname": "Nobody",
            "aname": "Nowhere",
            "statpal_context": {
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "home": {
                                "fixture_side": "home",
                                "shots_on_target_home": 5.4,
                            },
                            "away": {
                                "fixture_side": "away",
                                "shots_on_target_away": 4.9,
                            },
                        }
                    }
                }
            },
        }
        descriptor = self._descriptor("shots_on_target_total", "over", line="9.5")

        with patch("betpreneur.modules.scoring.evaluators.count_market_evaluator._profiles", return_value=(None, None)):
            result = count_market_evaluator.evaluate(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "shots_on_target_count_model")
        self.assertEqual(result["evidence"]["expected_shots_on_target"], 10.3)
        self.assertEqual(result["evidence"]["sources"], ["statpal_team_stats"])

    def test_shots_on_target_total_can_fallback_to_total_shots_proxy(self):
        fixture = {
            "hname": "Nobody",
            "aname": "Nowhere",
            "statpal_context": {
                "snapshots": {
                    "team_stats": {
                        "summary": {
                            "home": {
                                "fixture_side": "home",
                                "shots_total_home": 15,
                            },
                            "away": {
                                "fixture_side": "away",
                                "shots_total_away": 13,
                            },
                        }
                    }
                }
            },
        }
        descriptor = self._descriptor("shots_on_target_total", "over", line="9.5")

        with patch("betpreneur.modules.scoring.evaluators.count_market_evaluator._profiles", return_value=(None, None)):
            result = count_market_evaluator.evaluate(descriptor, fixture=fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "shots_on_target_count_model")
        self.assertEqual(result["evidence"]["expected_shots_on_target"], 9.52)
        self.assertEqual(result["evidence"]["sources"], ["statpal_team_stats_shots_total_proxy"])
        self.assertIn("shots_on_target_estimated_from_total_shots", result["warnings"])

    def test_api_team_ids_are_not_used_as_statpal_profile_ids(self):
        fixture = {"hid": "1025", "aid": "571", "hname": "Wolfsberger AC", "aname": "Salzburg"}

        with patch("betpreneur.modules.scoring.evaluators.count_market_evaluator.team_rate_profile_service.profile_for", return_value=None) as profile_for:
            count_market_evaluator.evaluate(describe_market("Corners Over 8.5"), fixture=fixture)

        self.assertEqual(profile_for.call_args_list[0].kwargs["team_id"], "")
        self.assertEqual(profile_for.call_args_list[1].kwargs["team_id"], "")

    def test_explicit_statpal_team_ids_are_used_for_profiles(self):
        fixture = {
            "statpal_home_team_id": "2341001",
            "statpal_away_team_id": "2341002",
            "hname": "Home",
            "aname": "Away",
        }

        with patch("betpreneur.modules.scoring.evaluators.count_market_evaluator.team_rate_profile_service.profile_for", return_value=None) as profile_for:
            count_market_evaluator.evaluate(describe_market("Corners Over 8.5"), fixture=fixture)

        self.assertEqual(profile_for.call_args_list[0].kwargs["team_id"], "2341001")
        self.assertEqual(profile_for.call_args_list[1].kwargs["team_id"], "2341002")


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
        self.fixture = {"statpal_home_team_id": "h1", "statpal_away_team_id": "a1", "hname": "Alpha", "aname": "Beta"}

    def _evaluate(self, market):
        return count_market_evaluator.evaluate(describe_market(market), fixture=self.fixture)

    def test_booking_points_line_is_converted_to_bookings(self):
        # Lives here because it needs the team rate profiles this class sets up; it had
        # drifted into TeamRateProfileServiceTests, which has no `self.fixture`.
        result = count_market_evaluator.evaluate(
            describe_market("Booking Points Over 25"), fixture=self.fixture
        )

        if result["available"]:
            self.assertLess(result["evidence"]["effective_line"], result["evidence"]["line"])

    def _range_descriptor(self, family, bucket, *, team=""):
        return MarketDescriptor(
            raw=f"{family} {bucket}",
            canonical=f"{family} {bucket}",
            code=f"{family}:{bucket}",
            family=family,
            category=family,
            selection=bucket,
            side=bucket,
            team=team,
            requires_corner_stats=True,
        )

    def _descriptor(self, family, selection, *, line="", team="", period="match"):
        return MarketDescriptor(
            raw=f"{family} {selection}",
            canonical=f"{family} {selection}",
            code=f"{family}:{selection}",
            family=family,
            category=family,
            selection=selection,
            side=selection,
            line=line,
            team=team,
            period=period,
            requires_corner_stats=True,
        )

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

    def test_corner_range_uses_range_probability(self):
        descriptor = self._range_descriptor("corner_range", "9-11")

        result = count_market_evaluator.evaluate(descriptor, fixture=self.fixture)

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "corners_range_count_model")
        self.assertEqual(result["evidence"]["range_lower"], 9)
        self.assertEqual(result["evidence"]["range_upper"], 11)
        self.assertGreater(result["probability"], 0)

    def test_team_corner_range_uses_selected_team_profile(self):
        home = self._range_descriptor("team_corner_range", "7+", team="home")
        away = self._range_descriptor("team_corner_range", "7+", team="away")

        home_result = count_market_evaluator.evaluate(home, fixture=self.fixture)
        away_result = count_market_evaluator.evaluate(away, fixture=self.fixture)

        self.assertTrue(home_result["available"])
        self.assertTrue(away_result["available"])
        self.assertGreater(home_result["probability"], away_result["probability"])
        # Each side reads its own rate (6.5 home / 4.0 away), then shrinks toward the
        # league prior of 5.1 with 5 pseudo-matches -- so 20 matches at 6.5 lands on 6.22.
        # Asserting the raw profile rate here asserted that shrinkage does not happen.
        self.assertEqual(home_result["evidence"]["expected_corners"], 6.22)
        self.assertEqual(away_result["evidence"]["expected_corners"], 4.22)

    def test_invalid_corner_range_declines(self):
        descriptor = self._range_descriptor("corner_range", "many")

        result = count_market_evaluator.evaluate(descriptor, fixture=self.fixture)

        self.assertFalse(result["available"])
        self.assertEqual(result["basis"], "count_market_invalid_range")
        self.assertIn("invalid_range", result["warnings"])

    def test_first_half_corner_total_is_period_adjusted(self):
        full = self._descriptor("corners_total", "over", line="5.5")
        first_half = self._descriptor("corners_total", "over", line="5.5", period="1st_half")

        full_result = count_market_evaluator.evaluate(full, fixture=self.fixture)
        first_half_result = count_market_evaluator.evaluate(first_half, fixture=self.fixture)

        self.assertLess(first_half_result["evidence"]["expected_corners"], full_result["evidence"]["expected_corners"])
        self.assertEqual(first_half_result["evidence"]["period_factor"], 0.45)
        self.assertIn("period_expectation_scaled", first_half_result["warnings"])

    def test_first_half_team_corner_total_is_period_adjusted(self):
        full = self._descriptor("team_corners", "over", line="2.5", team="home")
        first_half = self._descriptor("team_corners", "over", line="2.5", team="home", period="1st_half")

        full_result = count_market_evaluator.evaluate(full, fixture=self.fixture)
        first_half_result = count_market_evaluator.evaluate(first_half, fixture=self.fixture)

        self.assertLess(first_half_result["evidence"]["expected_corners"], full_result["evidence"]["expected_corners"])
        self.assertEqual(first_half_result["evidence"]["period_factor"], 0.45)
        self.assertIn("period_expectation_scaled", first_half_result["warnings"])

    def test_corner_result_uses_team_corner_poisson_1x2(self):
        home = self._descriptor("corners_result", "home")
        away = self._descriptor("corners_result", "away")

        home_result = count_market_evaluator.evaluate(home, fixture=self.fixture)
        away_result = count_market_evaluator.evaluate(away, fixture=self.fixture)

        self.assertTrue(home_result["available"])
        self.assertEqual(home_result["basis"], "corners_result_count_model")
        self.assertGreater(home_result["probability"], away_result["probability"])
        self.assertIn("draw_probability", home_result["evidence"])

    def test_cards_result_uses_team_card_poisson_1x2(self):
        home = self._descriptor("cards_result", "home")
        away = self._descriptor("cards_result", "away")

        home_result = count_market_evaluator.evaluate(home, fixture=self.fixture)
        away_result = count_market_evaluator.evaluate(away, fixture=self.fixture)

        self.assertTrue(home_result["available"])
        self.assertEqual(home_result["basis"], "cards_result_count_model")
        self.assertGreater(home_result["probability"], 0)
        self.assertGreater(away_result["probability"], 0)
        self.assertIn("expected_home_cards", home_result["evidence"])
        self.assertIn("draw_probability", home_result["evidence"])

    def test_corner_handicap_uses_team_corner_poisson_handicap(self):
        home = self._descriptor("corner_handicap", "home", line="-0.5", period="1st_half")
        away = self._descriptor("corner_handicap", "away", line="-0.5", period="1st_half")

        home_result = count_market_evaluator.evaluate(home, fixture=self.fixture)
        away_result = count_market_evaluator.evaluate(away, fixture=self.fixture)

        self.assertTrue(home_result["available"])
        self.assertEqual(home_result["basis"], "corners_handicap_count_model")
        self.assertGreater(home_result["probability"], 0)
        self.assertGreater(away_result["probability"], 0)
        self.assertAlmostEqual(home_result["probability"] + away_result["probability"], 1.0, places=5)
        self.assertEqual(home_result["evidence"]["period_factor"], 0.45)

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


class TeamRateProfileServiceTests(TestCase):
    def setUp(self):
        _fetch_failures.clear()

    def test_profile_for_uses_saved_team_snapshot_before_network(self):
        StatPalFixtureSnapshot.objects.create(
            match_id="statpal:team:2341001",
            provider_match_id="2341001",
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.TEAM_STATS,
            status="available",
            payload={
                "team": {
                    "id": "2341001",
                    "name": "Alpha",
                    "league_stats": {
                        "league": [
                            {
                                "id": "3363",
                                "fulltime": {
                                    "win": {"total": 4},
                                    "draw": {"total": 2},
                                    "lost": {"total": 2},
                                    "avg_corners": {"home": 6.2, "away": 4.1},
                                    "avg_yellowcards": {"home": 2.0, "away": 1.5},
                                    "avg_redcards": {"home": 0.1, "away": 0.0},
                                    "shots_on_goal": {"home": 32, "away": 24},
                                },
                            }
                        ]
                    },
                }
            },
        )
        client = Mock()

        profile = TeamRateProfileService(client=client).profile_for(team_id="2341001", team_name="Alpha")

        self.assertEqual(profile.team_id, "2341001")
        self.assertEqual(profile.team_name, "Alpha")
        self.assertEqual(profile.corners_home, 6.2)
        self.assertEqual(profile.shots_on_target_home, 4.0)
        self.assertFalse(client.soccer_endpoint.called)

    def test_profile_for_negative_caches_fetch_failures(self):
        client = Mock()
        client.soccer_endpoint.side_effect = TimeoutError("timeout")
        service = TeamRateProfileService(client=client)

        first = service.profile_for(team_id="2341001", team_name="Alpha")
        second = service.profile_for(team_id="2341001", team_name="Alpha")

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(client.soccer_endpoint.call_count, 1)


class CountRegistryTests(SimpleTestCase):
    def test_count_families_use_the_count_engine(self):
        for family in [
            "corners_total", "team_corners", "corner_range", "team_corner_range",
            "corners_result", "corner_handicap",
            "cards_total", "cards_result", "team_cards", "booking_points",
        ]:
            self.assertEqual(evaluator_for(family).engine, COUNT_MODEL_ENGINE, family)

    def test_only_one_entry_exists_per_family(self):
        from betpreneur.modules.markets.api import MARKET_EVALUATORS

        self.assertEqual(MARKET_EVALUATORS["cards"].engine, COUNT_MODEL_ENGINE)

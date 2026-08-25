"""
Fitting team strengths, looking them up, and evaluating markets from the fit.

Shrinkage is the load-bearing behaviour: slips are full of small-sample leagues, and an
unshrunk ratio from three games would assert things the data cannot support.
"""

from django.test import SimpleTestCase, TestCase

from betpreneur.modules.catalog.api import FixtureSearchService
from betpreneur.modules.markets.api import (
    QUANTITATIVE,
    SCORE_MATRIX_ENGINE,
    assessment_type_for,
    describe_market,
    evaluator_for,
)
from betpreneur.modules.scoring.domain.fitting import (
    MAX_FACTOR,
    MIN_FACTOR,
    expected_goals,
    fit_league_from_standings,
)
from betpreneur.modules.scoring.evaluators import score_matrix_evaluator
from betpreneur.modules.scoring.models import LeagueScoreModel
from betpreneur.modules.scoring.services.service import score_model_service


def _standings(teams):
    return {"standings": {"tournament": [{"team": teams}]}}


def _team(name, *, hs, ha, hg, aws, awa, ag, team_id=""):
    return {
        "id": team_id or name.lower().replace(" ", ""),
        "name": name,
        "home": {"goals_scored": str(hs), "goals_allowed": str(ha), "games_played": str(hg)},
        "away": {"goals_scored": str(aws), "goals_allowed": str(awa), "games_played": str(ag)},
    }


def _balanced_league():
    return _standings([
        _team("Strong FC", hs=20, ha=6, hg=10, aws=14, awa=10, ag=10),
        _team("Average FC", hs=13, ha=12, hg=10, aws=10, awa=14, ag=10),
        _team("Weak FC", hs=6, ha=21, hg=10, aws=4, awa=24, ag=10),
    ])


class FittingTests(SimpleTestCase):
    def test_baselines_track_observed_scoring_but_are_shrunk_toward_the_prior(self):
        # 30 home games is a real sample, so the fitted baseline sits close to the
        # observed rate — but still pulled slightly toward the global prior, which is
        # what stops a two-game league asserting 0.5 home and 3.0 away goals.
        fit = fit_league_from_standings(_balanced_league(), league_id="1")

        observed_home, observed_away = 39 / 30, 28 / 30
        self.assertAlmostEqual(fit.home_goal_baseline, observed_home, delta=0.1)
        self.assertAlmostEqual(fit.away_goal_baseline, observed_away, delta=0.15)
        self.assertGreater(fit.home_goal_baseline, observed_home)   # pulled up toward 1.35
        self.assertGreater(fit.away_goal_baseline, observed_away)   # pulled up toward 1.10

    def test_strong_team_outranks_weak_team_on_attack(self):
        fit = fit_league_from_standings(_balanced_league(), league_id="1")
        by_name = {team.team_name: team for team in fit.teams}

        self.assertGreater(by_name["Strong FC"].home_attack, by_name["Weak FC"].home_attack)
        self.assertLess(by_name["Strong FC"].home_defence, by_name["Weak FC"].home_defence)

    def test_small_samples_are_shrunk_toward_the_league_average(self):
        big = fit_league_from_standings(
            _standings([_team("A", hs=20, ha=2, hg=10, aws=10, awa=5, ag=10),
                        _team("B", hs=10, ha=10, hg=10, aws=5, awa=10, ag=10)]),
            league_id="1",
        )
        small = fit_league_from_standings(
            _standings([_team("A", hs=6, ha=0, hg=3, aws=3, awa=1, ag=3),
                        _team("B", hs=3, ha=3, hg=3, aws=1, awa=3, ag=3)]),
            league_id="1",
        )
        big_a = next(t for t in big.teams if t.team_name == "A")
        small_a = next(t for t in small.teams if t.team_name == "A")

        # Same shape of record, far fewer games: the estimate must be pulled toward 1.0.
        self.assertLess(abs(small_a.home_attack - 1.0), abs(big_a.home_attack - 1.0))

    def test_a_goalless_team_is_never_assigned_zero_attack(self):
        fit = fit_league_from_standings(
            _standings([_team("Silent FC", hs=0, ha=9, hg=3, aws=0, awa=9, ag=3),
                        _team("Loud FC", hs=9, ha=0, hg=3, aws=9, awa=0, ag=3)]),
            league_id="1",
        )
        silent = next(t for t in fit.teams if t.team_name == "Silent FC")

        self.assertGreaterEqual(silent.home_attack, MIN_FACTOR)
        self.assertGreater(silent.home_attack, 0)

    def test_factors_are_bounded(self):
        fit = fit_league_from_standings(
            _standings([_team("Machine", hs=90, ha=0, hg=10, aws=90, awa=0, ag=10),
                        _team("Sieve", hs=0, ha=90, hg=10, aws=0, awa=90, ag=10)]),
            league_id="1",
        )
        for team in fit.teams:
            for factor in (team.home_attack, team.home_defence, team.away_attack, team.away_defence):
                self.assertGreaterEqual(factor, MIN_FACTOR)
                self.assertLessEqual(factor, MAX_FACTOR)

    def test_empty_standings_are_reported_as_poor_rather_than_guessed(self):
        fit = fit_league_from_standings(_standings([]), league_id="1")

        self.assertEqual(fit.data_quality, "poor")
        self.assertEqual(fit.teams, ())

    def test_fit_records_that_no_time_decay_was_applied(self):
        fit = fit_league_from_standings(_balanced_league(), league_id="1")

        self.assertFalse(fit.diagnostics["time_decay_applied"])

    def test_expected_goals_reward_strong_attack_against_weak_defence(self):
        strong = expected_goals(
            home_attack=1.5, home_defence=0.7, away_attack=0.7, away_defence=1.4,
            home_baseline=1.4, away_baseline=1.1,
        )
        even = expected_goals(
            home_attack=1.0, home_defence=1.0, away_attack=1.0, away_defence=1.0,
            home_baseline=1.4, away_baseline=1.1,
        )

        self.assertGreater(strong[0], even[0])
        self.assertLess(strong[1], even[1])


class ServiceTests(TestCase):
    def setUp(self):
        self.model = score_model_service.fit_league(
            league_id="2914", league_name="Primera", standings_payload=_balanced_league()
        )

    def test_fit_is_persisted_with_its_teams(self):
        self.assertEqual(LeagueScoreModel.objects.count(), 1)
        self.assertEqual(self.model.teams.count(), 3)
        self.assertEqual(self.model.data_quality, "medium")

    def test_refitting_replaces_rather_than_duplicates(self):
        score_model_service.fit_league(
            league_id="2914", league_name="Primera", standings_payload=_balanced_league()
        )

        self.assertEqual(LeagueScoreModel.objects.count(), 1)
        self.assertEqual(self.model.teams.count(), 3)

    def test_rates_reflect_the_relative_strengths(self):
        strong_home = score_model_service.rates_for_fixture(
            league_id="2914", home_team_name="Strong FC", away_team_name="Weak FC"
        )
        weak_home = score_model_service.rates_for_fixture(
            league_id="2914", home_team_name="Weak FC", away_team_name="Strong FC"
        )

        self.assertTrue(strong_home.usable)
        self.assertGreater(strong_home.home_rate, weak_home.home_rate)

    def test_team_names_match_loosely(self):
        rates = score_model_service.rates_for_fixture(
            league_id="2914", home_team_name="CA Strong FC", away_team_name="Weak FC"
        )

        self.assertTrue(rates.matched_home)

    def test_unknown_league_is_not_usable(self):
        rates = score_model_service.rates_for_fixture(
            league_id="does-not-exist", home_team_name="A", away_team_name="B"
        )

        self.assertFalse(rates.usable)
        self.assertEqual(rates.data_quality, "poor")

    def test_one_unmatched_team_makes_the_fixture_unusable(self):
        rates = score_model_service.rates_for_fixture(
            league_id="2914", home_team_name="Strong FC", away_team_name="Nonexistent United"
        )

        self.assertFalse(rates.usable)


class MatrixEvaluatorTests(TestCase):
    def setUp(self):
        score_model_service.fit_league(
            league_id="2914", league_name="Primera", standings_payload=_balanced_league()
        )
        self.fixture = {"code": "2914", "hname": "Strong FC", "aname": "Weak FC"}

    def _evaluate(self, market):
        return score_matrix_evaluator.evaluate(describe_market(market), fixture=self.fixture)

    def test_result_market_is_modelled(self):
        result = self._evaluate("Home Win")

        self.assertTrue(result["available"])
        self.assertEqual(result["basis"], "score_matrix")
        self.assertGreater(result["probability"], 0.5)

    def test_double_chance_is_consistent_with_its_parts(self):
        home = self._evaluate("Home Win")["probability"]
        draw = self._evaluate("Draw")["probability"]
        dc = self._evaluate("DC: 1X")["probability"]

        # The evaluator rounds each probability to 6dp, so a sum of two rounded
        # values can differ from the rounded sum by 1e-6.
        self.assertAlmostEqual(dc, home + draw, places=5)

    def test_totals_are_modelled(self):
        over = self._evaluate("Over 2.5")
        under = self._evaluate("Under 2.5")

        self.assertAlmostEqual(over["probability"] + under["probability"], 1.0, places=6)

    def test_btts_is_modelled(self):
        self.assertTrue(self._evaluate("GG / BTTS Yes")["available"])

    def test_missing_fit_declines_rather_than_defaulting(self):
        result = score_matrix_evaluator.evaluate(
            describe_market("Home Win"), fixture={"code": "9999", "hname": "X", "aname": "Y"}
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["basis"], "score_matrix_no_fit")
        self.assertIn("no_fitted_score_model", result["warnings"])

    def test_evidence_reports_expected_goals_and_model_version(self):
        evidence = self._evaluate("Over 2.5")["evidence"]

        self.assertIn("expected_goals_home", evidence)
        self.assertIn("model_version", evidence)


class RegistrySwapTests(SimpleTestCase):
    def test_result_families_now_publish_probabilities(self):
        for family in [
            "match_result", "double_chance", "draw_no_bet", "btts",
            "clean_sheet", "total_goals", "team_total_goals", "asian_handicap",
        ]:
            self.assertEqual(assessment_type_for(family), QUANTITATIVE, family)
            self.assertEqual(evaluator_for(family).engine, SCORE_MATRIX_ENGINE, family)

    def test_specialised_families_still_use_the_statpal_engine(self):
        for family in ["corners_total", "cards_total", "player_goal"]:
            self.assertNotEqual(evaluator_for(family).engine, SCORE_MATRIX_ENGINE, family)


class EarlySeasonFittingTests(SimpleTestCase):
    """
    A league two matches into its season is not a league profile.

    Production fitted Eredivisie from 2 completed matches and produced baselines of
    0.50 home / 3.00 away goals per game, labelled `medium` quality — so every
    probability for that competition would have been published from noise. Team factors
    were always shrunk; the baselines they multiply were not.
    """

    def _league(self, teams):
        return _standings(teams)

    def test_a_two_game_league_baseline_stays_near_the_prior(self):
        fit = fit_league_from_standings(
            self._league([
                _team("A", hs=1, ha=3, hg=1, aws=0, awa=0, ag=0),
                _team("B", hs=0, ha=0, hg=0, aws=3, awa=1, ag=1),
            ]),
            league_id="3155",
        )

        self.assertGreater(fit.home_goal_baseline, 1.0)
        self.assertLess(fit.home_goal_baseline, 1.7)
        self.assertGreater(fit.away_goal_baseline, 0.8)
        self.assertLess(fit.away_goal_baseline, 1.6)

    def test_a_tiny_sample_is_not_reported_as_medium_quality(self):
        fit = fit_league_from_standings(
            self._league([
                _team("A", hs=1, ha=3, hg=1, aws=0, awa=0, ag=0),
                _team("B", hs=0, ha=0, hg=0, aws=3, awa=1, ag=1),
            ]),
            league_id="3155",
        )

        self.assertEqual(fit.data_quality, "poor")

    def test_a_full_season_still_reflects_its_own_scoring_rate(self):
        fit = fit_league_from_standings(
            self._league([_team(f"T{i}", hs=30, ha=20, hg=19, aws=22, awa=28, ag=19) for i in range(20)]),
            league_id="x",
        )

        self.assertEqual(fit.data_quality, "medium")
        self.assertGreater(fit.home_goal_baseline, fit.away_goal_baseline)

    def test_home_advantage_survives_shrinkage_on_a_real_sample(self):
        fit = fit_league_from_standings(
            self._league([_team(f"T{i}", hs=34, ha=18, hg=19, aws=20, awa=32, ag=19) for i in range(20)]),
            league_id="x",
        )

        self.assertGreater(fit.home_goal_baseline, 1.4)
        self.assertLess(fit.away_goal_baseline, 1.3)

    def test_a_goalless_early_league_never_produces_a_zero_baseline(self):
        fit = fit_league_from_standings(
            self._league([_team("A", hs=0, ha=0, hg=1, aws=0, awa=0, ag=1)]),
            league_id="x",
        )

        self.assertGreater(fit.home_goal_baseline, 0)
        self.assertGreater(fit.away_goal_baseline, 0)


class FixtureResolutionPreferenceTests(TestCase):
    """
    The same fixture is cached from several providers. Resolution must prefer the row
    that can actually be priced.

    In production an API-Football row and a StatPal row both matched at 100%; the sort
    tiebroke on date, the API-Football row won, and it carries no league id — so every
    leg resolved perfectly and then reported "no fitted goal model".
    """

    def setUp(self):
        import datetime

        from betpreneur.modules.catalog.api import FixtureCache

        self.match_date = datetime.date(2026, 8, 9)
        common = dict(
            match_date=self.match_date, fixture="Malmo FF vs Degerfors",
            home_team="Malmo FF", away_team="Degerfors",
            home_team_normalized="malmo ff", away_team_normalized="degerfors",
            fixture_normalized="malmo ff vs degerfors", league="Allsvenskan",
        )
        FixtureCache.objects.create(
            match_id="aps-1", source="aps_provider_lookup", api_payload={}, **common
        )
        FixtureCache.objects.create(
            match_id="statpal:1", source="statpal",
            api_payload={"provider_competition_id": "3240",
                         "provider_home_team_id": "2348353",
                         "provider_away_team_id": "2348259"},
            **common,
        )

    def _results(self):
        return FixtureSearchService()._search_cached(
            "Malmo FF vs Degerfors", start_date=self.match_date, days=1, limit=5
        )

    def test_the_priceable_row_is_preferred_when_both_match_equally(self):
        results = self._results()

        self.assertEqual(results[0]["source"], "statpal")
        self.assertEqual(results[0]["code"], "3240")

    def test_the_chosen_row_carries_the_team_ids_the_models_need(self):
        chosen = self._results()[0]

        self.assertEqual(chosen["hid"], "2348353")
        self.assertEqual(chosen["aid"], "2348259")

    def test_both_rows_are_still_offered_as_candidates(self):
        self.assertEqual(len(self._results()), 2)

    def test_a_better_scoring_row_still_wins_regardless_of_provider(self):
        # Preference is only a tiebreak; it must never override a better match.
        from betpreneur.modules.catalog.api import FixtureCache

        FixtureCache.objects.filter(source="statpal").update(
            fixture="Malmo FF vs Someone Else", fixture_normalized="malmo ff vs someone else",
            away_team="Someone Else", away_team_normalized="someone else",
        )

        self.assertEqual(self._results()[0]["source"], "aps_provider_lookup")

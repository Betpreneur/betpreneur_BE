from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    MarketProbability,
    PredictionDiagnostics,
    analyze_ticket_correlation,
)


def _selection(fixture_id, market, *, family=""):
    return MarketProbability(
        fixture_id=fixture_id,
        market=market,
        calibrated_probability=0.7,
        data_quality="strong",
        diagnostics=PredictionDiagnostics(metadata={"market_family": family} if family else {}),
    )


class CorrelationEngineTests(SimpleTestCase):
    def test_detects_goal_and_btts_relationship(self):
        report = analyze_ticket_correlation(
            (
                _selection("fixture-1", "Over 2.5"),
                _selection("fixture-1", "GG / BTTS Yes"),
            )
        )

        self.assertTrue(report.has_correlation)
        self.assertEqual(report.pairs[0].relationship, "goals_btts")
        self.assertEqual(report.pairs[0].direction, "reinforcing")
        self.assertIn("related_markets_detected", report.warnings)

    def test_detects_nested_goal_lines(self):
        report = analyze_ticket_correlation(
            (
                _selection("fixture-1", "Under 3.5"),
                _selection("fixture-1", "Under 4.5"),
            )
        )

        self.assertEqual(report.pairs[0].direction, "nested")
        self.assertIn("nested_same_fixture_markets", report.warnings)

    def test_detects_result_protection_relationship(self):
        report = analyze_ticket_correlation(
            (
                _selection("fixture-1", "Away Win"),
                _selection("fixture-1", "DC: X2"),
            )
        )

        self.assertEqual(report.pairs[0].relationship, "result_protection")
        self.assertEqual(report.pairs[0].direction, "nested")

    def test_detects_corner_line_ladder(self):
        report = analyze_ticket_correlation(
            (
                _selection("fixture-1", "Corners Over 7.5"),
                _selection("fixture-1", "Corners Over 8.5"),
            )
        )

        self.assertEqual(report.pairs[0].direction, "nested")
        self.assertIn("same_fixture_correlation", report.warnings)

    def test_reports_portfolio_exposure_without_same_fixture_pair(self):
        report = analyze_ticket_correlation(
            (
                _selection("fixture-1", "Over 1.5", family="total_goals"),
                _selection("fixture-2", "Over 2.5", family="total_goals"),
                _selection("fixture-3", "Under 3.5", family="total_goals"),
                _selection("fixture-4", "Home Win", family="match_result"),
            )
        )

        self.assertFalse(report.has_correlation)
        self.assertEqual(report.market_family_exposure["total_goals"], 3)
        self.assertEqual(report.concentration_score, 75.0)
        self.assertIn("market_family_concentration", report.warnings)

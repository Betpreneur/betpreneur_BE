from datetime import date

from django.test import TestCase

from betpreneur.modules.prediction.api import (
    PredictionTeamMatchFeedback,
    TeamMatchFeedbackRecord,
    record_team_match_feedback,
)


class TeamMatchFeedbackTests(TestCase):
    def test_records_team_match_feedback(self):
        feedback = record_team_match_feedback(
            TeamMatchFeedbackRecord(
                fixture_id="statpal:2026083118010",
                provider_match_id="2026083118010",
                fixture_name="Aston Villa vs Arsenal",
                match_date=date(2026, 8, 31),
                league_key="england-premier-league",
                team_name="Arsenal",
                opponent_name="Aston Villa",
                side="away",
                actual_result="win",
                goals_for=2,
                goals_against=1,
                corners_for=6,
                corners_against=4,
                cards_for=2,
                cards_against=3,
                referee_name="Michael Oliver",
                source="settlement",
                prediction_snapshot={"markets": [{"market": "Over 2.5", "confidence": 72}]},
                actual_stats={"away": {"goals": 2}},
            )
        )

        self.assertEqual(PredictionTeamMatchFeedback.objects.count(), 1)
        self.assertEqual(feedback.team_name, "Arsenal")
        self.assertEqual(feedback.side, "away")
        self.assertEqual(feedback.goals_for, 2)
        self.assertEqual(feedback.corners_against, 4)
        self.assertEqual(feedback.prediction_snapshot["markets"][0]["market"], "Over 2.5")

    def test_deduplicates_fixture_team_side(self):
        first = record_team_match_feedback(
            fixture_id="1557381",
            fixture_name="Crystal Palace vs Manchester City",
            team_name="Manchester City",
            opponent_name="Crystal Palace",
            side="away",
            actual_result="loss",
            goals_for=0,
            goals_against=1,
            prediction_snapshot={"markets": [{"market": "Away Win", "confidence": 70}]},
        )
        second = record_team_match_feedback(
            fixture_id="1557381",
            fixture_name="Crystal Palace vs Manchester City",
            team_name="Manchester City",
            opponent_name="Crystal Palace",
            side="away",
            actual_result="draw",
            goals_for=1,
            goals_against=1,
            prediction_snapshot={"markets": [{"market": "Over 2.5", "confidence": 62}]},
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(PredictionTeamMatchFeedback.objects.count(), 1)
        second.refresh_from_db()
        self.assertEqual(second.actual_result, "draw")
        self.assertEqual(second.goals_for, 1)
        self.assertEqual(second.prediction_snapshot["markets"][0]["market"], "Over 2.5")

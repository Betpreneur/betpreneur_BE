from django.core.exceptions import ValidationError
from django.test import TestCase

from betpreneur.modules.catalog.models import (
    CoachProfile,
    CoachTacticalProfile,
    TeamCoachAssignment,
)
from betpreneur.modules.catalog.services.coach_intelligence import CoachIntelligenceSyncService


class FakeStatPalCoachClient:
    coach_id = "coach-1"
    coach_name = "First Manager"

    def soccer_league_standings(self, league_id, params=None):
        return {
            "standings": {
                "country": "England",
                "tournament": {
                    "id": str(league_id),
                    "league": "English Premier League",
                    "season": (params or {}).get("season", ""),
                    "stage_id": "current",
                    "is_current": "True",
                    "team": [{"id": "team-1", "name": "Example United"}],
                },
            }
        }

    def soccer_team(self, team_id, params=None):
        return {
            "team": {
                "id": str(team_id),
                "name": "Example United",
                "country": "England",
                "coach": {"id": self.coach_id, "name": self.coach_name},
            }
        }

    def soccer_coach(self, coach_id, params=None):
        return {"coach": {"id": coach_id, "name": self.coach_name}}


class CoachIntelligenceSyncTests(TestCase):
    def setUp(self):
        self.client = FakeStatPalCoachClient()
        self.service = CoachIntelligenceSyncService(client=self.client)

    def sync(self):
        return self.service.sync(league_keys=["england-premier-league"])

    def test_initial_sync_creates_coach_and_current_assignment(self):
        result = self.sync()

        self.assertEqual(result["teams_synced"], 1)
        self.assertEqual(result["coaches_created"], 1)
        self.assertEqual(result["assignments_created"], 1)
        self.assertEqual(result["manager_changes"], 0)
        coach = CoachProfile.objects.get(provider_coach_id="coach-1")
        assignment = TeamCoachAssignment.objects.get(currently_active=True)
        self.assertEqual(coach.research_status, CoachProfile.ResearchStatus.UNRESEARCHED)
        self.assertEqual(assignment.coach, coach)
        self.assertEqual(assignment.team.canonical_name, "Example United")
        self.assertEqual(assignment.change_reason, "initial_import")

    def test_repeat_sync_updates_existing_assignment_without_duplicates(self):
        self.sync()
        result = self.sync()

        self.assertEqual(result["coaches_created"], 0)
        self.assertEqual(result["assignments_created"], 0)
        self.assertEqual(CoachProfile.objects.count(), 1)
        self.assertEqual(TeamCoachAssignment.objects.count(), 1)

    def test_changed_manager_closes_history_and_opens_new_assignment(self):
        self.sync()
        self.client.coach_id = "coach-2"
        self.client.coach_name = "Second Manager"

        result = self.sync()

        self.assertEqual(result["manager_changes"], 1)
        self.assertEqual(TeamCoachAssignment.objects.count(), 2)
        previous = TeamCoachAssignment.objects.get(provider_coach_id="coach-1")
        current = TeamCoachAssignment.objects.get(provider_coach_id="coach-2")
        self.assertFalse(previous.currently_active)
        self.assertIsNotNone(previous.ended_on)
        self.assertTrue(current.currently_active)
        self.assertEqual(current.change_reason, "provider_manager_change")
        self.assertFalse(previous.coach.active)

    def test_tactical_ratings_are_limited_to_zero_through_one_hundred(self):
        coach = CoachProfile.objects.create(
            canonical_name="Research Manager",
            canonical_normalized="research manager",
        )
        profile = CoachTacticalProfile(coach=coach, pressing_intensity=101)

        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_approved_tactical_profile_requires_evidence_and_confidence(self):
        coach = CoachProfile.objects.create(
            canonical_name="Research Manager",
            canonical_normalized="research manager",
        )
        profile = CoachTacticalProfile(
            coach=coach,
            status=CoachTacticalProfile.Status.APPROVED,
            philosophy_summary="A structured positional approach.",
        )

        with self.assertRaises(ValidationError) as raised:
            profile.full_clean()

        self.assertIn("confidence", raised.exception.message_dict)
        self.assertIn("source_urls", raised.exception.message_dict)

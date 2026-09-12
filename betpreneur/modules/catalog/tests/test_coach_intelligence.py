from datetime import date

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


class FakeCupCoachClient(FakeStatPalCoachClient):
    def soccer_league_standings(self, league_id, params=None):
        return {"standings": {"country": "England", "tournament": []}}

    def soccer_league_matches(self, league_id, params=None):
        return {
            "matches": {
                "country": "England",
                "tournament": {
                    "id": str(league_id),
                    "league": "FA Cup",
                    "season": "2026/2027",
                    "stage_id": "current",
                    "is_current": "True",
                    "week": {
                        "number": "1",
                        "match": {
                            "main_id": "cup-match-1",
                            "date": "10.09.2026",
                            "home": {"id": "cup-home", "name": "Cup Home"},
                            "away": {"id": "cup-away", "name": "Cup Away"},
                        },
                    },
                },
            }
        }

    def soccer_team(self, team_id, params=None):
        names = {"cup-home": "Cup Home", "cup-away": "Cup Away"}
        return {
            "team": {
                "id": team_id,
                "name": names[team_id],
                "country": "England",
                "coach": {"id": f"coach-{team_id}", "name": f"Manager {names[team_id]}"},
            }
        }


class CoachIntelligenceSyncTests(TestCase):
    def setUp(self):
        self.client = FakeStatPalCoachClient()
        self.service = CoachIntelligenceSyncService(client=self.client)

    def sync(self):
        return self.service.sync(league_keys=["england-premier-league"])

    def test_initial_sync_creates_coach_and_current_assignment(self):
        result = self.sync()

        self.assertEqual(result["leagues_considered"], 1)
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

    def test_cup_uses_league_matches_when_standings_are_unavailable(self):
        service = CoachIntelligenceSyncService(client=FakeCupCoachClient())

        result = service.sync(league_keys=["england-fa-cup"])

        self.assertEqual(result["teams_synced"], 2)
        self.assertEqual(result["assignments_created"], 2)
        self.assertEqual(result["results"][0]["discovery_source"], "league_matches")

    def test_tactical_ratings_are_limited_to_zero_through_one_hundred(self):
        coach = CoachProfile.objects.create(
            canonical_name="Research Manager",
            canonical_normalized="research manager",
        )
        profile = CoachTacticalProfile(coach=coach, pressing_intensity=101)

        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_approved_tactical_profile_requires_evidence(self):
        coach = CoachProfile.objects.create(
            canonical_name="Research Manager",
            canonical_normalized="research manager",
        )
        profile = CoachTacticalProfile(
            coach=coach,
            status=CoachTacticalProfile.Status.APPROVED,
            effective_from=date(2026, 7, 1),
            philosophy_summary="A structured positional approach.",
        )

        with self.assertRaises(ValidationError) as raised:
            profile.full_clean()

        self.assertIn("source_urls", raised.exception.message_dict)

    def test_tactical_confidence_is_calculated_from_profile_coverage(self):
        coach = CoachProfile.objects.create(
            canonical_name="Detailed Manager",
            canonical_normalized="detailed manager",
        )
        sparse = CoachTacticalProfile.objects.create(
            coach=coach,
            version=1,
            philosophy_summary="A developing tactical profile.",
        )
        detailed = CoachTacticalProfile(
            coach=coach,
            version=2,
            status=CoachTacticalProfile.Status.APPROVED,
            effective_from=date(2026, 7, 1),
            preferred_formation="4-3-3",
            alternative_formations=["4-2-3-1", "3-4-3", "4-4-2"],
            source_urls=["https://example.com/one", "https://example.com/two", "https://example.com/three"],
            philosophy_summary="Detailed positional philosophy.",
            attacking_style="Structured positional attacks",
            build_up_style="Patient short build-up",
            defensive_style="High counterpress",
        )
        for field in detailed.RATING_FIELDS:
            setattr(detailed, field, 50)
        detailed.full_clean()
        detailed.save()

        self.assertLess(sparse.confidence_score, detailed.confidence_score)
        self.assertEqual(detailed.confidence_score, 100)
        self.assertEqual(detailed.confidence, CoachProfile.Confidence.HIGH)

    def test_ratings_without_core_style_fields_do_not_create_high_confidence(self):
        coach = CoachProfile.objects.create(
            canonical_name="Ratings Heavy Manager",
            canonical_normalized="ratings heavy manager",
        )
        profile = CoachTacticalProfile(
            coach=coach,
            status=CoachTacticalProfile.Status.APPROVED,
            effective_from=date(2026, 7, 1),
            preferred_formation="4-3-3",
            philosophy_summary="Structured and aggressive.",
            source_urls=["https://example.com/source"],
        )
        for field in profile.RATING_FIELDS:
            setattr(profile, field, 50)

        profile.full_clean()

        self.assertLess(profile.confidence_score, 80)
        self.assertEqual(profile.confidence, CoachProfile.Confidence.MEDIUM)

    def test_sources_and_research_notes_do_not_increase_confidence(self):
        coach = CoachProfile.objects.create(
            canonical_name="Source Heavy Manager",
            canonical_normalized="source heavy manager",
        )
        with_sources = CoachTacticalProfile(
            coach=coach,
            source_urls=["https://example.com/one", "https://example.com/two"],
            research_notes="Very detailed source notes.",
        )
        without_sources = CoachTacticalProfile(coach=coach)

        with_sources.recalculate_confidence()
        without_sources.recalculate_confidence()

        self.assertEqual(with_sources.confidence_score, without_sources.confidence_score)

    def test_ai_review_score_controls_tactical_confidence_when_available(self):
        coach = CoachProfile.objects.create(
            canonical_name="AI Reviewed Manager",
            canonical_normalized="ai reviewed manager",
        )
        profile = CoachTacticalProfile(
            coach=coach,
            philosophy_summary="Conservative low-block manager.",
            attacking_style="Direct counters",
            build_up_style="Fast transitions",
            defensive_style="Compact low block",
        )

        profile.apply_ai_confidence_review(
            {
                "ai_confidence_score": 37,
                "text_rating_agreement": 30,
                "tactical_coherence": 40,
                "evidence_clarity": 35,
                "prediction_usefulness": 45,
                "warnings": ["Ratings conflict with the written low-block style."],
            },
            model="deepseek-test",
        )

        self.assertEqual(profile.confidence_score, 37)
        self.assertEqual(profile.confidence, CoachProfile.Confidence.LOW)
        self.assertEqual(profile.ai_confidence_model, "deepseek-test")

import csv
import tempfile

from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.catalog.models import (
    CoachProfile,
    CoachTacticalProfile,
    TeamCoachAssignment,
    TeamProfile,
)
from betpreneur.modules.catalog.services.coach_profile_import import CoachProfileCsvImporter


class CoachProfileCsvImporterTests(TestCase):
    def setUp(self):
        self.team = TeamProfile.objects.create(
            canonical_name="Arsenal",
            canonical_normalized="arsenal",
        )
        self.coach = CoachProfile.objects.create(
            canonical_name="Mikel Arteta",
            canonical_normalized="mikel arteta",
        )
        TeamCoachAssignment.objects.create(
            team=self.team,
            coach=self.coach,
            currently_active=True,
            first_detected_at=timezone.now(),
            last_confirmed_at=timezone.now(),
        )
        self.importer = CoachProfileCsvImporter()

    def test_import_creates_team_profile_and_calculates_confidence(self):
        result = self._import(
            {
                "Status": "Approved",
                "Version": "1",
                "Team": "Arsenal",
                "Manager": "Mikel Arteta",
                "Nationality": "Spain",
                "Effective from": "Dec 2019",
                "Preferred formation": "4-3-2003",
                "Alternative formations": "4-2-3-1, 3-4-3",
                "Philosophy summary": "Patient positional football supported by structured pressing.",
                "Pressing intensity": "72",
                "Source urls": "https://example.com/one, https://example.com/two",
                "Research notes": "Reviewed tactical research.",
            }
        )

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], 0)
        profile = CoachTacticalProfile.objects.get(coach=self.coach, team=self.team, version=1)
        self.assertEqual(profile.preferred_formation, "4-3-3")
        self.assertEqual(profile.effective_from.isoformat(), "2019-12-01")
        self.assertEqual(profile.alternative_formations, ["4-2-3-1", "3-4-3"])
        self.assertEqual(profile.source_urls, ["https://example.com/one", "https://example.com/two"])
        self.assertGreater(profile.confidence_score, 0)
        self.coach.refresh_from_db()
        self.assertEqual(self.coach.research_confidence, profile.confidence)
        self.assertEqual(self.coach.nationality, "Spain")
        self.assertEqual(len(result["warnings"]), 1)

    def test_dry_run_validates_without_saving(self):
        result = self._import(
            {
                "Status": "Draft",
                "Version": "1",
                "Team": "Arsenal",
                "Manager": "Mikel Arteta",
            },
            dry_run=True,
        )

        self.assertEqual(result["created"], 1)
        self.assertFalse(CoachTacticalProfile.objects.exists())

    def test_import_accepts_year_only_effective_date(self):
        result = self._import(
            {
                "Status": "Draft",
                "Version": "1",
                "Team": "Arsenal",
                "Manager": "Mikel Arteta",
                "Effective from": "2026",
            }
        )

        self.assertEqual(result["created"], 1)
        profile = CoachTacticalProfile.objects.get(coach=self.coach, team=self.team, version=1)
        self.assertEqual(profile.effective_from.isoformat(), "2026-01-01")

    def test_unmatched_coach_is_reported_and_skipped(self):
        result = self._import(
            {
                "Status": "Draft",
                "Version": "1",
                "Team": "Arsenal",
                "Manager": "Unknown Manager",
            }
        )

        self.assertEqual(result["skipped"], 1)
        self.assertIn("Coach not found", result["errors"][0]["error"])

    def _import(self, row, *, dry_run=False):
        fieldnames = [
            "Status",
            "Version",
            "Team",
            "Manager",
            "Nationality",
            "Effective from",
            "Effective to",
            "Preferred formation",
            "Alternative formations",
            "Philosophy summary",
            "Attacking style",
            "Build up style",
            "Defensive style",
            *[field.replace("_", " ").capitalize() for field in CoachTacticalProfile.RATING_FIELDS],
            "Source urls",
            "Research notes",
        ]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", suffix=".csv") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(row)
            handle.flush()
            return self.importer.import_file(handle.name, dry_run=dry_run)

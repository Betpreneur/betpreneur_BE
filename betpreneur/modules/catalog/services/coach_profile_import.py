import csv
import json
import re
from datetime import datetime
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from betpreneur.modules.catalog.domain.text import normalize_fixture_text
from betpreneur.modules.catalog.models import (
    CoachProfile,
    CoachTacticalProfile,
    TeamProfile,
)


class CoachProfileCsvImporter:
    TEXT_FIELDS = (
        "preferred_formation",
        "philosophy_summary",
        "attacking_style",
        "build_up_style",
        "defensive_style",
        "research_notes",
    )
    COLUMN_NAMES = {
        "preferred_formation": "Preferred formation",
        "philosophy_summary": "Philosophy summary",
        "attacking_style": "Attacking style",
        "build_up_style": "Build up style",
        "defensive_style": "Defensive style",
        "research_notes": "Research notes",
    }
    DATE_COLUMNS = {
        "effective_from": "Effective from",
        "effective_to": "Effective to",
    }
    EXCEL_FORMATION_RE = re.compile(r"(?<!\d)([1-5])-(\d)-20(0[1-5])(?!\d)")

    def import_file(self, path, *, dry_run=False):
        csv_path = Path(path).expanduser()
        if not csv_path.is_file():
            raise ValueError(f"CSV file does not exist: {csv_path}")

        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = {str(header or "").strip() for header in (reader.fieldnames or [])}
            missing = {"Manager", "Team"} - headers
            if missing:
                raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
            rows = list(reader)

        result = {
            "file": str(csv_path),
            "dry_run": dry_run,
            "rows": len(rows),
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "warnings": [],
            "errors": [],
        }
        with transaction.atomic():
            for row_number, row in enumerate(rows, start=2):
                try:
                    outcome = self._import_row(row, row_number, result["warnings"])
                    result[outcome] += 1
                except (ValueError, ValidationError) as exc:
                    result["skipped"] += 1
                    result["errors"].append({"row": row_number, "error": self._error_text(exc)})
            if dry_run:
                transaction.set_rollback(True)
        return result

    def _import_row(self, row, row_number, warnings):
        manager_name = self._required(row, "Manager")
        team_name = self._required(row, "Team")
        team = self._find_team(team_name)
        if team is None:
            raise ValueError(f'Team not found: "{team_name}"')
        coach = self._find_coach(manager_name, team)
        if coach is None:
            raise ValueError(f'Coach not found or not assigned to this team: "{manager_name}"')

        version = self._parse_version(row.get("Version"))
        status = str(row.get("Status") or CoachTacticalProfile.Status.DRAFT).strip().lower()
        valid_statuses = {value for value, _label in CoachTacticalProfile.Status.choices}
        if status not in valid_statuses:
            raise ValueError(f'Invalid status "{row.get("Status")}"')

        defaults = {field: str(row.get(column) or "").strip() for field, column in self.COLUMN_NAMES.items()}
        defaults["preferred_formation"] = self._repair_formation(
            defaults["preferred_formation"], row_number, warnings
        )
        defaults["alternative_formations"] = [
            self._repair_formation(value, row_number, warnings)
            for value in self._parse_list(row.get("Alternative formations"))
        ]
        defaults["source_urls"] = self._parse_list(row.get("Source urls"))
        defaults["status"] = status
        for field, column in self.DATE_COLUMNS.items():
            defaults[field] = self._parse_date(row.get(column), column)
        for field in CoachTacticalProfile.RATING_FIELDS:
            defaults[field] = self._parse_rating(row.get(self._rating_column(field)), self._rating_column(field))

        profile = CoachTacticalProfile.objects.filter(coach=coach, team=team, version=version).first()
        created = profile is None
        if created:
            profile = CoachTacticalProfile(coach=coach, team=team, version=version)
        for field, value in defaults.items():
            setattr(profile, field, value)
        profile.full_clean()
        profile.save()

        coach_updates = []
        nationality = str(row.get("Nationality") or "").strip()
        if nationality and coach.nationality != nationality:
            coach.nationality = nationality
            coach_updates.append("nationality")
        if status == CoachTacticalProfile.Status.APPROVED:
            coach.research_status = CoachProfile.ResearchStatus.APPROVED
            coach.research_confidence = profile.confidence
            coach.research_sources = profile.source_urls
            coach.research_notes = profile.research_notes
            coach.reviewed_at = timezone.now()
            coach_updates.extend(
                ["research_status", "research_confidence", "research_sources", "research_notes", "reviewed_at"]
            )
        if coach_updates:
            coach.save(update_fields=[*dict.fromkeys(coach_updates), "updated_at"])
        return "created" if created else "updated"

    @staticmethod
    def _required(row, column):
        value = str(row.get(column) or "").strip()
        if not value:
            raise ValueError(f'Column "{column}" is required')
        return value

    @staticmethod
    def _find_team(name):
        normalized = normalize_fixture_text(name)
        team = TeamProfile.objects.filter(canonical_normalized=normalized).first()
        if team:
            return team
        for candidate in TeamProfile.objects.filter(active=True).only("id", "aliases"):
            if normalized in {normalize_fixture_text(alias) for alias in candidate.aliases or []}:
                return candidate
        return None

    @staticmethod
    def _find_coach(name, team):
        normalized = normalize_fixture_text(name)
        assigned = list(
            CoachProfile.objects.filter(
                team_assignments__team=team,
                team_assignments__currently_active=True,
            ).distinct()
        )
        assigned_matches = [coach for coach in assigned if CoachProfileCsvImporter._coach_matches(coach, normalized)]
        if len(assigned_matches) == 1:
            return assigned_matches[0]

        candidates = list(
            CoachProfile.objects.filter(
                Q(canonical_normalized=normalized) | Q(canonical_name__iexact=name) | Q(provider_name__iexact=name)
            ).distinct()
        )
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            alias_matches = [
                coach
                for coach in CoachProfile.objects.filter(active=True).only("id", "aliases")
                if normalized in {normalize_fixture_text(alias) for alias in coach.aliases or []}
            ]
            if len(alias_matches) == 1:
                return alias_matches[0]
        return None

    @staticmethod
    def _coach_matches(coach, normalized):
        names = [coach.canonical_name, coach.canonical_normalized, coach.provider_name, *(coach.aliases or [])]
        return normalized in {normalize_fixture_text(name) for name in names if name}

    @staticmethod
    def _parse_version(value):
        text = str(value or "1").strip()
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f'Invalid version "{text}"') from exc
        if not number.is_integer() or number < 1:
            raise ValueError(f'Invalid version "{text}"')
        return int(number)

    @staticmethod
    def _parse_date(value, column):
        text = str(value or "").strip()
        if not text:
            return None
        for date_format in ("%Y-%m-%d", "%b %Y", "%B %Y", "%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(text, date_format).date()
            except ValueError:
                continue
        raise ValueError(f'Invalid {column} date "{text}"')

    @staticmethod
    def _parse_rating(value, column):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f'Invalid {column} rating "{text}"') from exc
        if not number.is_integer() or not 0 <= number <= 100:
            raise ValueError(f'{column} must be a whole number from 0 to 100; received "{text}"')
        return int(number)

    @staticmethod
    def _parse_list(value):
        text = str(value or "").strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Invalid JSON list "{text}"') from exc
            if not isinstance(parsed, list):
                raise ValueError(f'Expected a JSON list; received "{text}"')
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.split(",") if item.strip()]

    def _repair_formation(self, value, row_number, warnings):
        repaired = self.EXCEL_FORMATION_RE.sub(lambda match: f"{match.group(1)}-{match.group(2)}-{int(match.group(3))}", value)
        if repaired != value:
            warnings.append({"row": row_number, "warning": f'Repaired formation "{value}" to "{repaired}"'})
        return repaired

    @staticmethod
    def _rating_column(field):
        return field.replace("_", " ").capitalize()

    @staticmethod
    def _error_text(exc):
        if isinstance(exc, ValidationError):
            return "; ".join(exc.messages)
        return str(exc)


coach_profile_csv_importer = CoachProfileCsvImporter()

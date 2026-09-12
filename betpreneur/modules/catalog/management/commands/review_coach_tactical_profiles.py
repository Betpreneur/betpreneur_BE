import json

from django.core.management.base import BaseCommand

from betpreneur.modules.catalog.models import CoachTacticalProfile
from betpreneur.modules.catalog.services.coach_tactical_ai import (
    CoachTacticalAIReviewError,
    coach_tactical_ai_reviewer,
)


class Command(BaseCommand):
    help = "Run DeepSeek tactical confidence review for approved coach profiles."

    def add_arguments(self, parser):
        parser.add_argument("--profile-id", type=int, help="Review one CoachTacticalProfile id.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum profiles to review.")
        parser.add_argument("--force", action="store_true", help="Review profiles even if they already have AI review.")
        parser.add_argument("--dry-run", action="store_true", help="Show selected profile ids without calling DeepSeek.")

    def handle(self, *args, **options):
        queryset = CoachTacticalProfile.objects.select_related("coach", "team").filter(
            status=CoachTacticalProfile.Status.APPROVED
        )
        if options.get("profile_id"):
            queryset = queryset.filter(pk=options["profile_id"])
        if not options["force"]:
            queryset = queryset.filter(ai_confidence_review={})
        queryset = queryset.order_by("coach__canonical_name", "team__canonical_name", "-version")
        if options["limit"]:
            queryset = queryset[: options["limit"]]
        profiles = list(queryset)
        result = {
            "selected": len(profiles),
            "reviewed": 0,
            "failed": 0,
            "dry_run": options["dry_run"],
            "profiles": [],
        }
        for profile in profiles:
            item = {
                "profile_id": profile.pk,
                "coach": profile.coach.canonical_name,
                "team": profile.team.canonical_name if profile.team_id else "",
            }
            if options["dry_run"]:
                result["profiles"].append(item)
                continue
            try:
                review = coach_tactical_ai_reviewer.save_review(profile)
            except CoachTacticalAIReviewError as exc:
                result["failed"] += 1
                item["error"] = str(exc)
            else:
                result["reviewed"] += 1
                item["ai_confidence_score"] = review.get("ai_confidence_score")
                item["confidence"] = profile.confidence
            result["profiles"].append(item)
        self.stdout.write(json.dumps(result, indent=2, default=str))
        if result["failed"]:
            self.stdout.write(self.style.WARNING("Some coach tactical profiles failed AI review."))
        elif options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run only; no AI reviews were requested."))
        else:
            self.stdout.write(self.style.SUCCESS("Coach tactical AI review completed."))

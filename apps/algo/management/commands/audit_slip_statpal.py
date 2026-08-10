import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.algo.models import SlipReview, StatPalFixtureSnapshot
from apps.algo.statpal_snapshots import statpal_snapshot_service


class Command(BaseCommand):
    help = "Audit whether slip review legs are genuinely backed by usable StatPal data."

    def add_arguments(self, parser):
        parser.add_argument("review_ids", nargs="*", type=int, help="SlipReview ids to audit.")
        parser.add_argument("--recent", type=int, default=0, help="Audit the most recent N slip reviews.")
        parser.add_argument(
            "--resummarize",
            action="store_true",
            help="Rebuild cached StatPal snapshot summaries from their stored payloads before auditing.",
        )

    def handle(self, *args, **options):
        review_ids = options["review_ids"]
        recent = int(options["recent"] or 0)
        if not review_ids and recent <= 0:
            raise CommandError("Provide at least one review id or --recent N.")

        qs = SlipReview.objects.prefetch_related("selections").order_by("-created_at")
        if review_ids:
            qs = qs.filter(id__in=review_ids)
        if recent:
            qs = qs[:recent]

        reports = [self._audit_review(review, resummarize=options["resummarize"]) for review in qs]
        self.stdout.write(json.dumps({"reviews": reports}, indent=2, sort_keys=True, default=str))

    def _audit_review(self, review: SlipReview, *, resummarize: bool) -> dict:
        legs = [self._audit_selection(selection, resummarize=resummarize) for selection in review.selections.order_by("order")]
        statuses = [leg["statpal_status"] for leg in legs]
        return {
            "review_id": review.id,
            "source": review.source,
            "status": review.status,
            "summary": {
                "legs": len(legs),
                "statpal_backed": statuses.count("statpal_backed"),
                "model_or_fallback_backed": statuses.count("model_or_fallback_backed"),
                "missing_statpal_identity": statuses.count("missing_statpal_identity"),
                "statpal_without_usable_fields": statuses.count("statpal_without_usable_fields"),
                "not_assessed": statuses.count("not_assessed"),
            },
            "legs": legs,
        }

    def _audit_selection(self, selection, *, resummarize: bool) -> dict:
        payload = selection.analysis_payload or {}
        matched = payload.get("matched_fixture") or {}
        selected = payload.get("selected_market") or selection.selected_market or {}
        advisory = selected.get("statpal_advisory") or {}
        taxonomy = payload.get("market_taxonomy") or {}
        family = taxonomy.get("family") or (advisory.get("evidence") or {}).get("market_family") or ""
        internal_match_id = str(matched.get("match_id") or selection.match_id or "").strip()
        statpal_provider_match_id = str(
            matched.get("statpal_provider_match_id")
            or matched.get("statpal_provider_event_id")
            or matched.get("statpal_match_id")
            or ""
        ).replace("statpal:", "", 1).strip()
        provider_competition_id = str(
            matched.get("statpal_provider_competition_id")
            or matched.get("provider_competition_id")
            or ""
        ).strip()
        snapshots = self._snapshots(
            internal_match_id=internal_match_id,
            provider_match_id=statpal_provider_match_id,
            resummarize=resummarize,
        )
        usable = self._usable_fields(family, snapshots)
        has_probability = (advisory.get("evidence") or {}).get("estimated_probability") is not None
        basis = str(advisory.get("basis") or "")
        statpal_status = self._statpal_status(
            statpal_provider_match_id=statpal_provider_match_id,
            snapshots=snapshots,
            usable=usable,
            has_probability=has_probability,
            basis=basis,
            status=str(payload.get("status") or ""),
        )
        return {
            "order": selection.order,
            "match": selection.submitted_match,
            "market": selection.submitted_market,
            "analysis_status": payload.get("status") or selection.status,
            "market_family": family,
            "basis": basis,
            "statpal_status": statpal_status,
            "identity": {
                "internal_match_id": internal_match_id,
                "statpal_provider_match_id": statpal_provider_match_id,
                "provider_competition_id": provider_competition_id,
                "statpal_home_team_id": matched.get("statpal_home_team_id") or "",
                "statpal_away_team_id": matched.get("statpal_away_team_id") or "",
                "match_resolution_score": matched.get("match_score"),
            },
            "snapshots": {
                snapshot_type: {
                    "id": item["id"],
                    "source_endpoint": item["source_endpoint"],
                    "usable_fields": item["usable_fields"],
                    "summary_keys": item["summary_keys"],
                }
                for snapshot_type, item in snapshots.items()
            },
            "usable_fields": usable,
            "warnings": list(dict.fromkeys(
                (advisory.get("warnings") or [])
                + ((selected.get("market_capability") or {}).get("warnings") or [])
                + self._audit_warnings(statpal_status, family, snapshots, usable)
            )),
            "recommendation": self._recommendation(statpal_status, family, statpal_provider_match_id, snapshots, usable),
        }

    def _snapshots(self, *, internal_match_id: str, provider_match_id: str, resummarize: bool) -> dict:
        query = Q()
        if internal_match_id:
            query |= Q(match_id=internal_match_id) | Q(match_id=f"statpal:{provider_match_id}" if provider_match_id else internal_match_id)
        if provider_match_id:
            query |= Q(provider_match_id=provider_match_id) | Q(match_id=f"statpal:{provider_match_id}")
        if not query:
            return {}

        rows = StatPalFixtureSnapshot.objects.filter(query, status="available").order_by("snapshot_type", "-fetched_at", "-updated_at")
        by_type = {}
        for row in rows:
            if row.snapshot_type in by_type:
                continue
            if resummarize:
                summary = statpal_snapshot_service.summarize(
                    snapshot_type=row.snapshot_type,
                    payload=row.payload or {},
                    match_id=row.match_id,
                    provider_match_id=row.provider_match_id,
                )
                if summary != row.summary:
                    row.summary = summary
                    row.save(update_fields=["summary", "updated_at"])
            by_type[row.snapshot_type] = {
                "id": row.id,
                "source_endpoint": row.source_endpoint,
                "summary": row.summary or {},
                "summary_keys": sorted((row.summary or {}).keys()),
                "usable_fields": self._summary_usable_fields(row.snapshot_type, row.summary or {}),
            }
        return by_type

    @staticmethod
    def _summary_usable_fields(snapshot_type: str, summary: dict) -> list[str]:
        if snapshot_type == StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS:
            keys = (
                "expected_goals",
                "home_xg",
                "away_xg",
                "home_shots",
                "away_shots",
                "home_shots_on_target",
                "away_shots_on_target",
                "home_corners",
                "away_corners",
                "total_cards",
                "booking_points",
            )
        elif snapshot_type == StatPalFixtureSnapshot.SnapshotType.TEAM_STATS:
            keys = (
                "avg_goals_for",
                "avg_goals_against",
                "avg_total_goals",
                "avg_corners",
                "shots_on_target_home",
                "shots_on_target_away",
                "shots_on_target_total",
            )
        elif snapshot_type == StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS:
            keys = ("home_odds", "draw_odds", "away_odds", "over25_odds", "under25_odds", "market_count")
        elif snapshot_type == StatPalFixtureSnapshot.SnapshotType.LINEUPS:
            keys = ("starting_count", "bench_count", "home_confidence", "away_confidence")
        elif snapshot_type == StatPalFixtureSnapshot.SnapshotType.INJURIES_SUSPENSIONS:
            keys = ("total_to_miss_count", "total_questionable_count")
        elif snapshot_type == StatPalFixtureSnapshot.SnapshotType.PREDICTIONS:
            keys = ("expected_goals", "home_xg", "away_xg", "home_win_percent", "draw_percent", "away_win_percent", "over25_percent")
        else:
            keys = tuple(summary.keys())
        return [key for key in keys if summary.get(key) not in (None, "", [], {})]

    @staticmethod
    def _usable_fields(family: str, snapshots: dict) -> list[str]:
        detailed = (snapshots.get(StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS) or {}).get("summary") or {}
        team_stats = (snapshots.get(StatPalFixtureSnapshot.SnapshotType.TEAM_STATS) or {}).get("summary") or {}
        predictions = (snapshots.get(StatPalFixtureSnapshot.SnapshotType.PREDICTIONS) or {}).get("summary") or {}
        odds = (snapshots.get(StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS) or {}).get("summary") or {}

        checks = {
            "shots_on_target_total": [
                ("detailed_stats.home_shots_on_target", detailed.get("home_shots_on_target")),
                ("detailed_stats.away_shots_on_target", detailed.get("away_shots_on_target")),
                ("detailed_stats.home_shots", detailed.get("home_shots")),
                ("detailed_stats.away_shots", detailed.get("away_shots")),
                ("team_stats.shots_on_target_total", team_stats.get("shots_on_target_total")),
            ],
            "team_shots_on_target": [
                ("detailed_stats.home_shots_on_target", detailed.get("home_shots_on_target")),
                ("detailed_stats.away_shots_on_target", detailed.get("away_shots_on_target")),
                ("team_stats.shots_on_target_home", team_stats.get("shots_on_target_home")),
                ("team_stats.shots_on_target_away", team_stats.get("shots_on_target_away")),
            ],
            "corners_total": [
                ("detailed_stats.home_corners", detailed.get("home_corners")),
                ("detailed_stats.away_corners", detailed.get("away_corners")),
                ("team_stats.avg_corners", team_stats.get("avg_corners")),
            ],
            "team_corners": [
                ("detailed_stats.home_corners", detailed.get("home_corners")),
                ("detailed_stats.away_corners", detailed.get("away_corners")),
                ("team_stats.avg_corners", team_stats.get("avg_corners")),
            ],
            "cards_total": [
                ("detailed_stats.total_cards", detailed.get("total_cards")),
                ("detailed_stats.booking_points", detailed.get("booking_points")),
            ],
            "booking_points": [
                ("detailed_stats.booking_points", detailed.get("booking_points")),
                ("detailed_stats.total_cards", detailed.get("total_cards")),
            ],
            "both_halves_total_goals": [
                ("team_stats.firsthalf_avg_goals_for", team_stats.get("firsthalf_avg_goals_for")),
                ("team_stats.secondhalf_avg_goals_for", team_stats.get("secondhalf_avg_goals_for")),
                ("detailed_stats.expected_goals", detailed.get("expected_goals")),
                ("predictions.expected_goals", predictions.get("expected_goals")),
            ],
        }
        goal_families = {
            "match_result",
            "double_chance",
            "draw_no_bet",
            "total_goals",
            "team_total_goals",
            "btts",
            "clean_sheet",
            "result_total_goals",
            "result_or_total_goals",
            "result_or_btts",
            "total_btts",
        }
        if family in goal_families:
            checks[family] = [
                ("detailed_stats.expected_goals", detailed.get("expected_goals")),
                ("detailed_stats.home_xg", detailed.get("home_xg")),
                ("detailed_stats.away_xg", detailed.get("away_xg")),
                ("predictions.expected_goals", predictions.get("expected_goals")),
                ("team_stats.avg_total_goals", team_stats.get("avg_total_goals")),
                ("prematch_odds.market_count", odds.get("market_count")),
            ]
        fields = checks.get(family, [])
        return [name for name, value in fields if value not in (None, "", [], {})]

    @staticmethod
    def _statpal_status(*, statpal_provider_match_id: str, snapshots: dict, usable: list[str], has_probability: bool, basis: str, status: str) -> str:
        if not statpal_provider_match_id:
            return "model_or_fallback_backed" if has_probability or basis.startswith("fixture_") else "missing_statpal_identity"
        if usable:
            return "statpal_backed"
        if snapshots:
            return "statpal_without_usable_fields"
        if status in {"analysed", "assessed"} and (has_probability or basis):
            return "model_or_fallback_backed"
        return "not_assessed"

    @staticmethod
    def _audit_warnings(statpal_status: str, family: str, snapshots: dict, usable: list[str]) -> list[str]:
        warnings = []
        if statpal_status == "missing_statpal_identity":
            warnings.append("statpal_identity_missing")
        if statpal_status == "statpal_without_usable_fields":
            warnings.append("statpal_fetched_but_market_fields_missing")
        if snapshots and not usable:
            warnings.append(f"{family or 'market'}_has_no_usable_statpal_fields")
        return warnings

    @staticmethod
    def _recommendation(statpal_status: str, family: str, statpal_provider_match_id: str, snapshots: dict, usable: list[str]) -> str:
        if statpal_status == "statpal_backed":
            return "OK: StatPal identity, snapshots, and usable market fields are present."
        if statpal_status == "missing_statpal_identity":
            return "Resolve StatPal fixture mapping for this game before trusting StatPal-backed analysis."
        if statpal_status == "statpal_without_usable_fields":
            available = ", ".join(sorted(snapshots.keys())) or "none"
            return f"StatPal matched {statpal_provider_match_id}, but {family or 'this market'} has no usable fields in snapshots: {available}."
        if statpal_status == "model_or_fallback_backed":
            return "This leg is model/fallback-backed, not fully StatPal-backed. Keep confidence capped and label it that way."
        return "Not assessed: no usable model or StatPal evidence was found."

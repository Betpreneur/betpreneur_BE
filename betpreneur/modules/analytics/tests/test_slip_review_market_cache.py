import json
from datetime import date
from decimal import Decimal
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.analytics.interface.views import _maintenance_jobs
from betpreneur.modules.catalog.api import SlipReviewMarketCache, SlipReviewMarketCacheWriter
from betpreneur.modules.picks.api import algo_runner_service
from betpreneur.modules.slips.api import manual_fixture_game


class SlipReviewMarketCacheWriterTests(TestCase):
    def test_writer_upserts_scored_fixture_markets(self):
        fixture = {
            "match_date": date(2026, 8, 15),
            "fixture": "Chelsea vs Tottenham",
            "home_team": "Chelsea",
            "away_team": "Tottenham",
            "league": "Premier League",
            "country": "england",
            "kickoff": "2026-08-15T17:30:00+00:00",
            "match_id": "statpal:2026081512345",
            "provider_match_id": "2026081512345",
            "provider_competition_id": "3037",
            "home_team_id": "1",
            "away_team_id": "2",
            "provider_merge": {"primary": "statpal"},
            "markets": [
                {
                    "market": "Over 2.5",
                    "meaning": "3 or more total goals",
                    "market_family": "total_goals",
                    "raw_confidence": 68,
                    "confidence": 66,
                    "final_confidence": 64.5,
                    "odds": 1.82,
                    "ev": 0.041,
                    "odds_source": "statpal",
                    "eligible": True,
                    "risk_flags": ["priced_market"],
                    "insights": {
                        "summary": "Goals are supported.",
                        "positive_evidence": ["Expected goals sit around 3.1."],
                    },
                }
            ],
        }

        result = SlipReviewMarketCacheWriter().upsert_fixture_markets(fixture)

        self.assertEqual(result["cached"], 1)
        row = SlipReviewMarketCache.objects.get(match_id="statpal:2026081512345", market="Over 2.5")
        self.assertEqual(row.market_family, "total_goals")
        self.assertEqual(row.provider_match_id, "2026081512345")
        self.assertEqual(row.provider_competition_id, "3037")
        self.assertEqual(row.confidence, 66)
        self.assertEqual(row.final_confidence, 64.5)
        self.assertEqual(row.odds, Decimal("1.82"))
        self.assertEqual(row.ev, Decimal("0.041"))
        self.assertEqual(row.insights["summary"], "Goals are supported.")
        self.assertEqual(row.provider_merge["primary"], "statpal")

        fixture["markets"][0]["confidence"] = 72
        result = SlipReviewMarketCacheWriter().upsert_fixture_markets(fixture)

        self.assertEqual(result["cached"], 1)
        self.assertEqual(SlipReviewMarketCache.objects.count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.confidence, 72)

    def test_writer_dedupes_duplicate_canonical_markets_before_bulk_upsert(self):
        fixture = {
            "match_date": date(2026, 8, 15),
            "fixture": "Chelsea vs Tottenham",
            "home_team": "Chelsea",
            "away_team": "Tottenham",
            "match_id": "statpal:duplicate-canonical",
            "markets": [
                {
                    "market": "Over 2.5 Goals",
                    "market_family": "total_goals",
                    "confidence": 55,
                    "final_confidence": 55,
                    "odds": 1.70,
                    "odds_source": "estimated",
                },
                {
                    "market": "Over 2.5",
                    "market_family": "total_goals",
                    "confidence": 68,
                    "final_confidence": 68,
                    "odds": 1.82,
                    "odds_source": "statpal",
                    "eligible": True,
                },
            ],
        }

        result = SlipReviewMarketCacheWriter().upsert_fixture_markets(fixture)

        self.assertEqual(result["cached"], 1)
        self.assertEqual(result["deduped"], 1)
        self.assertEqual(SlipReviewMarketCache.objects.count(), 1)
        row = SlipReviewMarketCache.objects.get(match_id="statpal:duplicate-canonical", market="Over 2.5")
        self.assertEqual(row.confidence, 68)
        self.assertEqual(row.odds_source, "statpal")


class SlipReviewMarketCacheReadPathTests(TestCase):
    def test_manual_fixture_game_reads_private_cache_by_provider_match_id(self):
        expires_at = timezone.now() + timezone.timedelta(hours=6)
        SlipReviewMarketCache.objects.create(
            match_date=date(2026, 8, 15),
            fixture="Chelsea vs Tottenham",
            home_team="Chelsea",
            away_team="Tottenham",
            league="Premier League",
            country="england",
            kickoff="2026-08-15T17:30:00+00:00",
            match_id="statpal:2026081512345",
            provider_match_id="2026081512345",
            provider_competition_id="3037",
            market="Over 2.5",
            market_family="total_goals",
            meaning="3 or more total goals",
            raw_confidence=68,
            confidence=66,
            final_confidence=64.5,
            odds=Decimal("1.82"),
            ev=Decimal("0.041"),
            odds_source="statpal",
            eligible=True,
            insights={
                "summary": "Goals are supported.",
                "positive_evidence": ["Expected goals sit around 3.1."],
            },
            market_payload={"market": "Over 2.5", "confidence": 66},
            fixture_payload={
                "home_recent_form": {"games": 8, "wins": 5},
                "away_recent_form": {"games": 8, "wins": 3},
            },
            provider_merge={"primary": "statpal"},
            cache_version="test",
            expires_at=expires_at,
        )

        game = manual_fixture_game("2026081512345", date(2026, 8, 15), request=None)

        self.assertIsNotNone(game)
        self.assertEqual(game["match_id"], "statpal:2026081512345")
        self.assertEqual(game["fixture"], "Chelsea vs Tottenham")
        self.assertEqual(game["slip_review_cache"]["source"], "slip_review_market_cache")
        self.assertEqual(game["slip_review_cache"]["market_count"], 1)
        self.assertEqual(game["markets"][0]["market"], "Over 2.5")
        self.assertEqual(game["markets"][0]["confidence"], 66)
        self.assertEqual(game["markets"][0]["insights"]["summary"], "Goals are supported.")

    def test_manual_fixture_game_ignores_expired_private_cache(self):
        SlipReviewMarketCache.objects.create(
            match_date=date(2026, 8, 15),
            fixture="Chelsea vs Tottenham",
            home_team="Chelsea",
            away_team="Tottenham",
            match_id="statpal:expired",
            provider_match_id="expired",
            market="Over 2.5",
            confidence=66,
            expires_at=timezone.now() - timezone.timedelta(minutes=1),
        )

        self.assertIsNone(manual_fixture_game("expired", date(2026, 8, 15), request=None))


class SlipReviewMarketCacheOperationsTests(TestCase):
    def test_task_route_and_maintenance_job_are_registered(self):
        self.assertEqual(
            settings.CELERY_TASK_ROUTES["betpreneur.modules.picks.tasks.build_slip_review_market_cache"]["queue"],
            settings.ALGO_STATPAL_QUEUE,
        )
        self.assertEqual(
            settings.CELERY_TASK_ROUTES["betpreneur.modules.picks.tasks.cleanup_slip_review_market_cache"]["queue"],
            settings.ALGO_MAINTENANCE_QUEUE,
        )
        self.assertIn("slip_review_market_cache", _maintenance_jobs())
        self.assertIn("slip_review_market_cache_cleanup", _maintenance_jobs())

    def test_cleanup_deletes_only_expired_private_cache_rows(self):
        now = timezone.now()
        SlipReviewMarketCache.objects.create(
            match_date=date(2026, 8, 15),
            fixture="Expired vs Team",
            match_id="statpal:expired-cleanup",
            market="Over 2.5",
            expires_at=now - timezone.timedelta(minutes=5),
        )
        fresh = SlipReviewMarketCache.objects.create(
            match_date=date(2026, 8, 15),
            fixture="Fresh vs Team",
            match_id="statpal:fresh-cleanup",
            market="Over 2.5",
            expires_at=now + timezone.timedelta(hours=1),
        )

        result = algo_runner_service.cleanup_slip_review_market_cache()

        self.assertEqual(result["deleted"], 1)
        self.assertFalse(SlipReviewMarketCache.objects.filter(match_id="statpal:expired-cleanup").exists())
        self.assertTrue(SlipReviewMarketCache.objects.filter(id=fresh.id).exists())

    def test_management_command_status_reports_cache_totals(self):
        SlipReviewMarketCache.objects.create(
            match_date=date(2026, 8, 15),
            fixture="Status vs Team",
            match_id="statpal:status",
            market="Over 2.5",
            market_family="total_goals",
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )
        out = StringIO()

        call_command("slip_review_market_cache", "status", stdout=out)

        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["totals"]["rows"], 1)
        self.assertEqual(payload["totals"]["fixtures"], 1)
        self.assertEqual(payload["totals"]["fresh_rows"], 1)

    def test_management_command_inline_cleanup(self):
        SlipReviewMarketCache.objects.create(
            match_date=date(2026, 8, 15),
            fixture="Expired Command vs Team",
            match_id="statpal:expired-command",
            market="Over 2.5",
            expires_at=timezone.now() - timezone.timedelta(minutes=5),
        )
        out = StringIO()

        call_command("slip_review_market_cache", "cleanup", "--inline", stdout=out)

        payload = json.loads(out.getvalue())
        self.assertEqual(payload["deleted"], 1)
        self.assertFalse(SlipReviewMarketCache.objects.filter(match_id="statpal:expired-command").exists())

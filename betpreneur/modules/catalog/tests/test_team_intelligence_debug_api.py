from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from betpreneur.modules.catalog.models import (
    DataCoverage,
    LeagueMarketProfile,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)
from betpreneur.modules.catalog.services.coverage_tracker import DataCoverageTracker


class TeamIntelligenceDebugApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="tester",
            email="tester@example.com",
            password="pass",
        )
        self.admin = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="pass",
        )
        self.team = TeamProfile.objects.create(
            canonical_name="Arsenal",
            canonical_normalized="arsenal",
            country="England",
            primary_league_key="england-premier-league",
            primary_league_name="Premier League",
            provider_ids={"statpal": {"team_id": "42"}},
            aliases=["Arsenal FC"],
        )
        now = timezone.now()
        TeamSeasonProfile.objects.create(
            team=self.team,
            league_key="england-premier-league",
            league_name="Premier League",
            country="England",
            season="2026-2027",
            matches_played=20,
            goals_for=42,
            goals_against=18,
            data_quality=TeamSeasonProfile.DataQuality.STRONG,
            computed_at=now,
        )
        TeamRecentFormProfile.objects.create(
            team=self.team,
            league_key="england-premier-league",
            league_name="Premier League",
            season="2026-2027",
            window=5,
            scope=TeamRecentFormProfile.Scope.ALL,
            matches=5,
            wins=4,
            computed_at=now,
        )
        TeamMarketProfile.objects.create(
            team=self.team,
            league_key="england-premier-league",
            league_name="Premier League",
            season="2026-2027",
            market_family="total_goals",
            market="Over 2.5",
            scope=TeamMarketProfile.Scope.ALL,
            attempts=15,
            hit_rate=73,
            confidence=70,
            data_quality="strong",
            computed_at=now,
        )
        LeagueMarketProfile.objects.create(
            league_key="england-premier-league",
            league_name="Premier League",
            country="England",
            season="2026-2027",
            market_family="result",
            market="Home Win",
            attempts=120,
            hit_rate=45,
            confidence=63,
            fairness_score=71,
            data_quality="strong",
            computed_at=now,
        )
        DataCoverage.objects.create(
            subject_type=DataCoverage.SubjectType.TEAM,
            subject_key=f"{self.team.pk}:england-premier-league:2026-2027",
            team=self.team,
            provider=DataCoverageTracker.PROVIDER,
            coverage_key=DataCoverageTracker.TEAM_COVERAGE_KEY,
            league_key="england-premier-league",
            league_name="Premier League",
            season="2026-2027",
            status=DataCoverage.Status.FRESH,
            metadata={"confidence": 92},
            last_success_at=now,
        )

    def test_team_intelligence_debug_requires_admin(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("algo-team-intelligence-debug"), {"q": "arsenal"})

        self.assertEqual(response.status_code, 403)

    def test_admin_can_inspect_team_intelligence_by_query(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("algo-team-intelligence-debug"), {"q": "arsenal"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        team = body["teams"][0]
        self.assertEqual(team["canonical_name"], "Arsenal")
        self.assertEqual(team["coverage_summary"]["status"], "fresh")
        self.assertIsNotNone(team["last_refresh"])
        self.assertEqual(team["profile_counts"]["season"], 1)
        self.assertEqual(team["profile_counts"]["recent_form"], 1)
        self.assertEqual(team["profile_counts"]["markets"], 1)
        self.assertEqual(team["profile_counts"]["league_priors"], 1)

    def test_team_intelligence_debug_requires_identifier(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(reverse("algo-team-intelligence-debug"))

        self.assertEqual(response.status_code, 400)

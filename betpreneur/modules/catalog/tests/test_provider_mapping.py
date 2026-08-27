from datetime import UTC, datetime

from django.test import SimpleTestCase, TestCase

from betpreneur.modules.catalog.models import (
    FixtureCache,
    ProviderFixtureMap,
    ProviderPlayerMap,
    ProviderTeamMap,
    TeamAliasMap,
    TeamProfile,
)
from betpreneur.modules.catalog.services.resolution import provider_mapping_service


class ProviderMappingPureTests(SimpleTestCase):
    def test_mapping_candidate_keeps_statpal_ids_from_payload(self):
        mapping = ProviderFixtureMap(
            api_fixture_id="statpal:2026080912345",
            api_league_id=37,
            api_league_name="Eredivisie",
            api_home_team="Sparta Rotterdam",
            api_away_team="Feyenoord",
            kickoff_at=datetime(2026, 8, 9, 15, 15, tzinfo=UTC),
            confidence=100,
            payload={
                "candidate": {
                    "provider_match_id": "2026080912345",
                    "fallback_match_ids": ["6024103"],
                    "provider_competition_id": "37",
                    "home_team_id": "2339001",
                    "away_team_id": "2339002",
                    "country": "netherlands",
                }
            },
        )

        candidate = provider_mapping_service._mapping_candidate(mapping)

        self.assertEqual(candidate["provider_match_id"], "2026080912345")
        self.assertEqual(candidate["provider_competition_id"], "37")
        self.assertEqual(candidate["home_team_id"], "2339001")
        self.assertEqual(candidate["away_team_id"], "2339002")
        self.assertEqual(candidate["source"], "statpal")


class ProviderMappingServiceTests(TestCase):
    def _statpal_fixture(self, **overrides):
        defaults = {
            "match_date": datetime(2026, 8, 9, tzinfo=UTC).date(),
            "fixture": "Sparta Rotterdam vs Feyenoord",
            "home_team": "Sparta Rotterdam",
            "away_team": "Feyenoord",
            "home_team_normalized": "sparta rotterdam",
            "away_team_normalized": "feyenoord",
            "fixture_normalized": "sparta rotterdam vs feyenoord",
            "league": "Eredivisie",
            "country": "netherlands",
            "kickoff": "16:15",
            "kickoff_utc": datetime(2026, 8, 9, 15, 15, tzinfo=UTC),
            "match_id": "statpal:2026080912345",
            "source": "statpal",
            "api_payload": {
                "provider_match_id": "2026080912345",
                "fallback_match_ids": ["6024103", "6531486"],
                "provider_competition_id": "37",
                "provider_home_team_id": "2339001",
                "provider_away_team_id": "2339002",
            },
        }
        defaults.update(overrides)
        return FixtureCache.objects.create(**defaults)

    def _sportybet_event(self, **overrides):
        kickoff_ms = int(datetime(2026, 8, 9, 15, 15, tzinfo=UTC).timestamp() * 1000)
        event = {
            "eventId": "sr:match:72041042",
            "gameId": "23861",
            "estimateStartTime": kickoff_ms,
            "homeTeamName": "Sparta Rotterdam",
            "awayTeamName": "Feyenoord",
            "sport": {
                "category": {
                    "name": "Netherlands",
                    "tournament": {"id": "sr:tournament:37", "name": "Eredivisie"},
                }
            },
        }
        event.update(overrides)
        return event

    def test_match_sportybet_to_statpal_persists_high_confidence_mapping(self):
        self._statpal_fixture()

        result = provider_mapping_service.match_sportybet_to_statpal(self._sportybet_event())

        self.assertTrue(result["matched"])
        self.assertEqual(result["candidate"]["match_id"], "statpal:2026080912345")
        self.assertGreaterEqual(result["candidate"]["match_score"], 95)

        mapping = ProviderFixtureMap.objects.get(provider="sportybet", provider_event_id="sr:match:72041042")
        self.assertEqual(mapping.api_fixture_id, "statpal:2026080912345")
        self.assertEqual(mapping.api_league_id, 37)
        self.assertEqual(mapping.api_home_team, "Sparta Rotterdam")
        self.assertEqual(mapping.api_away_team, "Feyenoord")
        self.assertEqual(mapping.resolution_method, "sportybet_statpal_team_date_direct")
        self.assertEqual(mapping.payload["candidate"]["provider_match_id"], "2026080912345")

    def test_existing_sportybet_statpal_mapping_keeps_statpal_team_ids(self):
        self._statpal_fixture()
        provider_mapping_service.match_sportybet_to_statpal(self._sportybet_event())

        result = provider_mapping_service.match_sportybet_to_statpal(self._sportybet_event())

        self.assertTrue(result["matched"])
        self.assertTrue(result["existing"])
        self.assertEqual(result["candidate"]["provider_competition_id"], "37")
        self.assertEqual(result["candidate"]["home_team_id"], "2339001")
        self.assertEqual(result["candidate"]["away_team_id"], "2339002")

    def test_match_sportybet_to_statpal_rejects_weak_candidate(self):
        self._statpal_fixture(
            fixture="Ajax vs PSV",
            home_team="Ajax",
            away_team="PSV",
            home_team_normalized="ajax",
            away_team_normalized="psv",
            fixture_normalized="ajax vs psv",
        )

        result = provider_mapping_service.match_sportybet_to_statpal(self._sportybet_event())

        self.assertFalse(result["matched"])
        self.assertEqual(result["reason"], "no_candidate_above_threshold")
        self.assertFalse(ProviderFixtureMap.objects.filter(provider="sportybet").exists())

    def test_match_sportybet_to_statpal_accepts_fallback_id_match(self):
        self._statpal_fixture()

        result = provider_mapping_service.match_sportybet_to_statpal(
            self._sportybet_event(eventId="6024103", homeTeamName="Wrong Home", awayTeamName="Wrong Away")
        )

        self.assertTrue(result["matched"])
        self.assertEqual(result["candidate"]["match_score"], 100)
        self.assertEqual(result["candidate"]["resolution_method"], "sportybet_statpal_provider_id")

    def test_learn_statpal_player_payload_creates_player_team_and_alias_maps(self):
        payload = {
            "player": {
                "id": "2891848",
                "name": "Florian Wirtz",
                "nationality": "Germany",
                "position": "Midfielder",
                "team": "Liverpool",
                "team_id": "2341082",
            }
        }

        mapping = provider_mapping_service.learn_statpal_player_payload(payload)

        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.provider, "statpal")
        self.assertEqual(mapping.provider_player_id, "2891848")
        self.assertEqual(mapping.provider_player_name, "Florian Wirtz")
        self.assertEqual(mapping.provider_team_id, "2341082")
        self.assertEqual(mapping.provider_team_name, "Liverpool")
        self.assertEqual(mapping.position, "Midfielder")

        self.assertTrue(
            ProviderTeamMap.objects.filter(
                provider="statpal",
                provider_team_id="2341082",
                provider_team_name="Liverpool",
            ).exists()
        )
        self.assertTrue(
            TeamAliasMap.objects.filter(
                provider="statpal",
                alias_normalized="liverpool",
                canonical_normalized="liverpool",
            ).exists()
        )

    def test_learn_team_links_api_football_and_statpal_to_one_team_profile(self):
        api_mapping = provider_mapping_service.learn_team(
            provider="api_football",
            provider_team_id="42",
            provider_team_name="Arsenal",
            internal_team_name="Arsenal",
            country="England",
            league_key="england-premier-league",
            league_name="English Premier League",
            provider_league_id="39",
            season="2026",
            aliases=["Arsenal FC"],
            confidence=100,
            resolution_method="api_football_registry",
        )
        statpal_mapping = provider_mapping_service.learn_team(
            provider="statpal",
            provider_team_id="3001",
            provider_team_name="Arsenal FC",
            internal_team_name="Arsenal",
            country="England",
            league_key="england-premier-league",
            league_name="English Premier League",
            provider_league_id="3037",
            season="2026-2027",
            aliases=["Arsenal"],
            confidence=96,
            resolution_method="statpal_registry",
        )

        team = TeamProfile.objects.get(canonical_normalized="arsenal")
        self.assertEqual(api_mapping.internal_team_id, str(team.pk))
        self.assertEqual(statpal_mapping.internal_team_id, str(team.pk))
        self.assertEqual(team.provider_ids["api_football"]["team_id"], "42")
        self.assertEqual(team.provider_ids["api_football"]["league_id"], "39")
        self.assertEqual(team.provider_ids["statpal"]["team_id"], "3001")
        self.assertEqual(team.provider_ids["statpal"]["league_id"], "3037")
        self.assertEqual(team.primary_league_key, "england-premier-league")
        self.assertIn("Arsenal FC", team.aliases)

    def test_link_provider_team_identity_updates_legacy_provider_maps_and_aliases(self):
        mapping = provider_mapping_service.learn_team(
            provider="statpal",
            provider_team_id="2341082",
            provider_team_name="Liverpool FC",
            internal_team_name="Liverpool",
            country="England",
            league_key="england-premier-league",
            provider_league_id="3037",
            season="2026-2027",
            confidence=95,
            resolution_method="statpal_snapshot",
        )

        team = TeamProfile.objects.get(canonical_normalized="liverpool")
        self.assertEqual(mapping.internal_team_id, str(team.pk))
        self.assertEqual(mapping.internal_team_name, "Liverpool")
        self.assertEqual(mapping.internal_team_normalized, "liverpool")
        self.assertEqual(mapping.payload["team_profile_id"], team.pk)
        self.assertEqual(team.provider_ids["statpal"]["season"], "2026-2027")
        self.assertTrue(
            TeamAliasMap.objects.filter(
                provider="statpal",
                alias_normalized="liverpool fc",
                canonical_normalized="liverpool",
            ).exists()
        )

    def test_get_player_uses_provider_id_first(self):
        ProviderPlayerMap.objects.create(
            provider="statpal",
            provider_player_id="2891848",
            provider_player_name="Florian Wirtz",
            provider_player_normalized="florian wirtz",
            provider_team_id="2341082",
            provider_team_name="Liverpool",
        )

        mapping = provider_mapping_service.get_player(
            provider="statpal",
            provider_player_id="2891848",
        )

        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.provider_player_name, "Florian Wirtz")

    def test_get_team_can_fallback_to_normalized_name(self):
        ProviderTeamMap.objects.create(
            provider="statpal",
            provider_team_id="2341082",
            provider_team_name="Liverpool",
            provider_team_normalized="liverpool",
        )

        mapping = provider_mapping_service.get_team(
            provider="statpal",
            provider_team_name="Liverpool FC",
        )

        self.assertIsNone(mapping)

        mapping = provider_mapping_service.get_team(
            provider="statpal",
            provider_team_name="Liverpool",
        )
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.provider_team_id, "2341082")

    def test_get_team_can_use_learned_alias(self):
        ProviderTeamMap.objects.create(
            provider="statpal",
            provider_team_id="2341082",
            provider_team_name="Liverpool",
            provider_team_normalized="liverpool",
            internal_team_name="Liverpool",
            internal_team_normalized="liverpool",
        )
        TeamAliasMap.objects.create(
            provider="statpal",
            alias="Liverpool FC",
            alias_normalized="liverpool fc",
            canonical_name="Liverpool",
            canonical_normalized="liverpool",
            source="test",
        )

        mapping = provider_mapping_service.get_team(
            provider="statpal",
            provider_team_name="Liverpool FC",
        )

        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.provider_team_id, "2341082")

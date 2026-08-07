from django.test import TestCase

from apps.algo.models import ProviderPlayerMap, ProviderTeamMap, TeamAliasMap
from apps.algo.provider_mapping import provider_mapping_service


class ProviderMappingServiceTests(TestCase):
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

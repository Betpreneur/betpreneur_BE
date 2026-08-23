from django.test import SimpleTestCase

from apps.algo.serializers import GameAnalysisQuerySerializer, GameListQuerySerializer


class ApiQuerySerializerTests(SimpleTestCase):
    def test_games_list_defaults_to_compact_view(self):
        serializer = GameListQuerySerializer(data={})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["view"], "compact")

    def test_game_analysis_default_stays_full_for_detail_routes(self):
        serializer = GameAnalysisQuerySerializer(data={})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["view"], "full")

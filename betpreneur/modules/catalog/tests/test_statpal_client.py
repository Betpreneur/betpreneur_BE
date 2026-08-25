from django.test import SimpleTestCase, override_settings

from betpreneur.integrations.statpal.client import (
    StatPalClient,
    StatPalConfig,
    StatPalConfigurationError,
)
from betpreneur.modules.catalog.services.provider_client import statpal_client


class DummyResponse:
    status_code = 200
    text = '{"ok": true}'

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


class DummySession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return DummyResponse()


class StatPalClientTests(SimpleTestCase):
    def test_adds_access_key_and_uses_v2_base_url(self):
        session = DummySession()
        client = StatPalClient(
            config=StatPalConfig(
                access_key="demo-key",
                base_url="https://statpal.io/api/v2",
                usage_base_url="https://statpal.io/api",
                timeout=7,
                enabled=True,
            ),
            session=session,
        )

        payload = client.soccer_live_matches()

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(session.calls[0]["url"], "https://statpal.io/api/v2/soccer/matches/live")
        self.assertEqual(session.calls[0]["params"]["access_key"], "demo-key")
        self.assertEqual(session.calls[0]["timeout"], 7)

    def test_usage_count_uses_usage_base_url(self):
        session = DummySession()
        client = StatPalClient(
            config=StatPalConfig(
                access_key="demo-key",
                base_url="https://statpal.io/api/v2",
                usage_base_url="https://statpal.io/api",
                timeout=7,
                enabled=True,
            ),
            session=session,
        )

        client.usage_count()

        self.assertEqual(session.calls[0]["url"], "https://statpal.io/api/user-request-count")

    def test_disabled_client_raises_clear_error(self):
        client = StatPalClient(
            config=StatPalConfig(
                access_key="demo-key",
                base_url="https://statpal.io/api/v2",
                usage_base_url="https://statpal.io/api",
                timeout=7,
                enabled=False,
            ),
            session=DummySession(),
        )

        with self.assertRaises(StatPalConfigurationError):
            client.soccer_live_matches()

    @override_settings(
        GRIND_ALGO={
            "STATPAL_ENABLED": "True",
            "STATPAL_ACCESS_KEY": "demo-key",
            "STATPAL_BASE_URL": "https://statpal.io/api/v2",
            "STATPAL_USAGE_BASE_URL": "https://statpal.io/api",
            "STATPAL_TIMEOUT": 7,
            "STATPAL_ENDPOINT_SOCCER_PLAYER_STATS": "soccer/players/{player_id}/stats",
        }
    )
    def test_configured_endpoint_override(self):
        session = DummySession()
        # Config now comes from the catalog factory rather than the client
        # reading settings itself.
        client = statpal_client(session=session)

        client.soccer_endpoint("SOCCER_PLAYER_STATS", player_id="123")

        self.assertEqual(session.calls[0]["url"], "https://statpal.io/api/v2/soccer/players/123/stats")

    def test_fixture_endpoint_templates_accept_match_id(self):
        session = DummySession()
        client = StatPalClient(
            config=StatPalConfig(
                access_key="demo-key",
                base_url="https://statpal.io/api/v2",
                usage_base_url="https://statpal.io/api",
                timeout=7,
                enabled=True,
            ),
            session=session,
        )

        client.soccer_endpoint("SOCCER_LINEUPS", params={"match_id": "abc123"})
        client.soccer_endpoint("SOCCER_PREDICTIONS", params={"match_id": "abc123"})
        client.soccer_endpoint("SOCCER_DETAILED_STATS", params={"match_id": "abc123"}, league_id="99")

        self.assertEqual(session.calls[0]["url"], "https://statpal.io/api/v2/soccer/team-lineups")
        self.assertEqual(session.calls[0]["params"]["match_id"], "abc123")
        self.assertEqual(session.calls[1]["url"], "https://statpal.io/api/v2/soccer/predictions")
        self.assertEqual(session.calls[1]["params"]["match_id"], "abc123")
        self.assertEqual(session.calls[2]["url"], "https://statpal.io/api/v2/soccer/leagues/99/matches/stats")
        self.assertEqual(session.calls[2]["params"]["match_id"], "abc123")

    def test_documented_soccer_endpoint_helpers(self):
        session = DummySession()
        client = StatPalClient(
            config=StatPalConfig(
                access_key="demo-key",
                base_url="https://statpal.io/api/v2",
                usage_base_url="https://statpal.io/api",
                timeout=7,
                enabled=True,
            ),
            session=session,
        )

        client.soccer_leagues()
        client.soccer_league_seasons()
        client.soccer_daily_matches(params={"date": "2026-08-07"})
        client.soccer_league_matches(39, params={"season": "2026"})
        client.soccer_league_match_stats(39, params={"date": "08.12.2025"})
        client.soccer_league_standings(39, params={"season": "2025-2026"})
        client.soccer_league_stats(39, params={"season": "2025-2026"})
        client.soccer_head_to_head("2341082", "2341092")
        client.soccer_injuries_suspensions()
        client.soccer_team("2340899")
        client.soccer_player("2773317")
        client.soccer_coach("12345")
        client.soccer_team_lineups("2026061822389")
        client.soccer_weather_forecast("2026061822389")
        client.soccer_predictions(params={"match_id": "abc123"})
        client.soccer_prematch_odds(39, params={"match_id": "abc123"})
        client.soccer_images()
        client.soccer_live_storylines()
        client.soccer_live_odds()
        client.soccer_live_odds_markets()
        client.soccer_live_odds_match_states()

        self.assertEqual(session.calls[0]["url"], "https://statpal.io/api/v2/soccer/leagues")
        self.assertEqual(session.calls[1]["url"], "https://statpal.io/api/v2/soccer/leagues/seasons")
        self.assertEqual(session.calls[2]["url"], "https://statpal.io/api/v2/soccer/matches/daily")
        self.assertEqual(session.calls[2]["params"]["date"], "2026-08-07")
        self.assertEqual(session.calls[3]["url"], "https://statpal.io/api/v2/soccer/leagues/39/matches")
        self.assertEqual(session.calls[4]["url"], "https://statpal.io/api/v2/soccer/leagues/39/matches/stats")
        self.assertEqual(session.calls[4]["params"]["date"], "08.12.2025")
        self.assertEqual(session.calls[5]["url"], "https://statpal.io/api/v2/soccer/leagues/39/standings")
        self.assertEqual(session.calls[5]["params"]["season"], "2025-2026")
        self.assertEqual(session.calls[6]["url"], "https://statpal.io/api/v2/soccer/leagues/39/stats")
        self.assertEqual(session.calls[6]["params"]["season"], "2025-2026")
        self.assertEqual(session.calls[7]["url"], "https://statpal.io/api/v2/soccer/head-to-head")
        self.assertEqual(session.calls[7]["params"]["team1_id"], "2341082")
        self.assertEqual(session.calls[7]["params"]["team2_id"], "2341092")
        self.assertEqual(session.calls[8]["url"], "https://statpal.io/api/v2/soccer/injuries-suspensions")
        self.assertEqual(session.calls[9]["url"], "https://statpal.io/api/v2/soccer/teams/2340899")
        self.assertEqual(session.calls[10]["url"], "https://statpal.io/api/v2/soccer/players/2773317")
        self.assertEqual(session.calls[11]["url"], "https://statpal.io/api/v2/soccer/coaches/12345")
        self.assertEqual(session.calls[12]["url"], "https://statpal.io/api/v2/soccer/team-lineups")
        self.assertEqual(session.calls[12]["params"]["match_id"], "2026061822389")
        self.assertEqual(session.calls[13]["url"], "https://statpal.io/api/v2/soccer/weather-forecast")
        self.assertEqual(session.calls[13]["params"]["match_id"], "2026061822389")
        self.assertEqual(session.calls[14]["url"], "https://statpal.io/api/v2/soccer/predictions")
        self.assertEqual(session.calls[15]["url"], "https://statpal.io/api/v2/soccer/leagues/39/odds/prematch")
        self.assertEqual(session.calls[16]["url"], "https://statpal.io/api/v2/soccer/images")
        self.assertEqual(session.calls[17]["url"], "https://statpal.io/api/v2/soccer/live-storylines")
        self.assertEqual(session.calls[18]["url"], "https://statpal.io/api/v2/soccer/odds/live")
        self.assertEqual(session.calls[19]["url"], "https://statpal.io/api/v2/soccer/odds/live/markets")
        self.assertEqual(session.calls[20]["url"], "https://statpal.io/api/v2/soccer/odds/live/match-states")

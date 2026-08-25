from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from betpreneur.integrations.statpal.client import StatPalConfigurationError
from betpreneur.modules.catalog.models import FixtureCache
from betpreneur.modules.catalog.services.search import FixtureSearchService
from betpreneur.modules.catalog.services.statpal_normalize import (
    normalize_daily_matches,
    normalize_head_to_head,
    normalize_injuries_suspensions,
    normalize_league_seasons,
    normalize_league_standings,
    normalize_league_stats,
    normalize_leagues,
    normalize_match_stats,
    normalize_player,
    normalize_prematch_odds,
    normalize_team,
    normalize_team_lineups,
)

DAILY_PAYLOAD = {
    "matches": [
        {
            "id": "sp-100",
            "date": "2026-08-07",
            "time": "18:00",
            "home": {"id": "h1", "name": "Norway"},
            "away": {"id": "a1", "name": "England"},
            "league": {"id": "1", "name": "World Cup", "country": "World"},
            "round": "Quarter-finals",
        }
    ]
}


class StatPalLeagueNormalizeTests(SimpleTestCase):
    def test_normalize_leagues_outputs_stable_catalogue_shape(self):
        payload = {
            "leagues": {
                "sport": "soccer",
                "league": [
                    {
                        "id": "3800",
                        "country": "africa",
                        "name": "CAF African Nations Championship",
                        "season": "2025",
                        "date_start": "02.08.2025",
                        "date_end": "30.08.2025",
                    }
                ],
            }
        }

        leagues = normalize_leagues(payload)

        self.assertEqual(len(leagues), 1)
        league = leagues[0]
        self.assertEqual(league["provider"], "statpal")
        self.assertEqual(league["sport"], "soccer")
        self.assertEqual(league["provider_league_id"], "3800")
        self.assertEqual(league["name"], "CAF African Nations Championship")
        self.assertEqual(league["country"], "africa")
        self.assertEqual(league["season"], "2025")
        self.assertEqual(league["date_start"], date(2025, 8, 2))
        self.assertEqual(league["date_end"], date(2025, 8, 30))
        self.assertEqual(league["date_start_raw"], "02.08.2025")
        self.assertEqual(league["raw"]["id"], "3800")

    def test_normalize_leagues_accepts_single_league_object(self):
        payload = {
            "leagues": {
                "sport": "soccer",
                "league": {
                    "id": "3976",
                    "country": "africa",
                    "name": "CAF Women's Africa Cup of Nations",
                    "season": "2025",
                    "date_start": "05.07.2025",
                    "date_end": "26.07.2025",
                },
            }
        }

        leagues = normalize_leagues(payload)

        self.assertEqual([item["provider_league_id"] for item in leagues], ["3976"])

    def test_normalize_league_seasons_outputs_match_and_standings_history(self):
        payload = {
            "seasons": {
                "sport": "soccer",
                "league": [
                    {
                        "id": "3800",
                        "country": "africa",
                        "name": "CAF African Nations Championship",
                        "matches": {
                            "season": [
                                {"name": "2016"},
                                {"name": "2018"},
                                {"name": "2020"},
                                {"name": "2021"},
                                {"name": "2025"},
                            ]
                        },
                        "standings": {"season": {"name": "2025"}},
                    },
                    {
                        "id": "3976",
                        "country": "africa",
                        "name": "CAF Women's Africa Cup of Nations",
                        "matches": {"season": [{"name": "2022"}, {"name": "2024"}, {"name": "2025"}]},
                        "standings": None,
                    },
                ],
            }
        }

        seasons = normalize_league_seasons(payload)

        self.assertEqual(len(seasons), 2)
        first = seasons[0]
        self.assertEqual(first["provider_league_id"], "3800")
        self.assertEqual(first["match_seasons"], ["2016", "2018", "2020", "2021", "2025"])
        self.assertEqual(first["standing_seasons"], ["2025"])
        self.assertTrue(first["has_match_history"])
        self.assertTrue(first["has_standings_history"])

        second = seasons[1]
        self.assertEqual(second["provider_league_id"], "3976")
        self.assertEqual(second["match_seasons"], ["2022", "2024", "2025"])
        self.assertEqual(second["standing_seasons"], [])
        self.assertTrue(second["has_match_history"])
        self.assertFalse(second["has_standings_history"])

    def test_normalize_league_seasons_accepts_single_league_and_single_match_season(self):
        payload = {
            "seasons": {
                "sport": "soccer",
                "league": {
                    "id": "1",
                    "country": "world",
                    "name": "Example League",
                    "matches": {"season": {"name": "2025/2026"}},
                    "standings": {"season": [{"name": "2024-2025"}, {"name": "2025/2026"}]},
                },
            }
        }

        seasons = normalize_league_seasons(payload)

        self.assertEqual(seasons[0]["match_seasons"], ["2025/2026"])
        self.assertEqual(seasons[0]["standing_seasons"], ["2024-2025", "2025/2026"])

    def test_normalize_injuries_suspensions_outputs_match_availability(self):
        payload = {
            "injuries_suspensions": {
                "updated": "26.11.2025 17:01:06",
                "updated_ts": 1764176466,
                "league": [
                    {
                        "id": "2974",
                        "name": "Brazil: Serie A Betano",
                        "sub_id": "",
                        "match": [
                            {
                                "main_id": "2025112614481",
                                "fallback_id_1": "6111795",
                                "fallback_id_2": "6619208",
                                "fallback_id_3": "8769818",
                                "date": "26.11.2025",
                                "time": "22:00",
                                "home": {
                                    "id": "2339006",
                                    "name": "Bragantino",
                                    "sidelined": {
                                        "to_miss": {
                                            "player": [
                                                {"id": "2983041", "name": "Bruninho", "status": "Heel Injury"},
                                                {"id": "2901492", "name": "Eric Ramires", "status": "Hamstring Injury"},
                                            ]
                                        },
                                        "questionable": {
                                            "player": {"id": "3143001", "name": "H. Mosquera", "status": "Hamstring Injury"}
                                        },
                                    },
                                },
                                "away": {
                                    "id": "2339141",
                                    "name": "Fortaleza",
                                    "sidelined": {
                                        "to_miss": {
                                            "player": {"id": "2559503", "name": "Bruno Pacheco", "status": "Inactive"}
                                        },
                                        "questionable": {
                                            "player": {"id": "2765467", "name": "Breno Lopes", "status": "Injury"}
                                        },
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        }

        rows = normalize_injuries_suspensions(payload)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider"], "statpal")
        self.assertEqual(row["match_id"], "statpal:2025112614481")
        self.assertEqual(row["provider_match_id"], "2025112614481")
        self.assertEqual(row["fallback_match_ids"], ["6111795", "6619208", "8769818"])
        self.assertEqual(row["provider_competition_id"], "2974")
        self.assertEqual(row["league"], "Brazil: Serie A Betano")
        self.assertEqual(row["sub_id"], "")
        self.assertEqual(row["date"], date(2025, 11, 26))
        self.assertEqual(row["kickoff"], "22:00")
        self.assertEqual(row["home"]["team_id"], "2339006")
        self.assertEqual(row["home"]["team_name"], "Bragantino")
        self.assertEqual(row["home"]["to_miss_count"], 2)
        self.assertEqual(row["home"]["questionable_count"], 1)
        self.assertEqual(row["home"]["availability_risk"], "medium")
        self.assertEqual(row["home"]["to_miss"][0]["name"], "Bruninho")
        self.assertEqual(row["away"]["team_id"], "2339141")
        self.assertEqual(row["away"]["to_miss_count"], 1)
        self.assertEqual(row["away"]["questionable"][0]["name"], "Breno Lopes")
        self.assertEqual(row["total_to_miss_count"], 3)
        self.assertEqual(row["total_questionable_count"], 2)
        self.assertEqual(row["feed_updated_ts"], 1764176466)

    def test_normalize_team_outputs_profile_squad_and_stats(self):
        payload = {
            "updated": "09.12.2025 19:11:41",
            "updated_ts": 1765307501,
            "team": {
                "id": "2340899",
                "name": "Bristol City",
                "country": "England",
                "founded": "1894",
                "is_national_team": "False",
                "is_women": "False",
                "leagues": {"league_id": ["3038", "3367"]},
                "venue_name": "Ashton Gate Stadium",
                "venue_id": "2419838",
                "venue_surface": "Grass",
                "venue_capacity": "27000",
                "venue_address": "Ashton Road",
                "venue_city": "Bristol",
                "coach": {"name": "Gerhard Struber", "id": "2820329"},
                "squad": {
                    "player": [
                        {
                            "id": "3446137",
                            "name": "Josey Casa-Grande",
                            "number": "",
                            "age": "20",
                            "position": "G",
                            "is_captain": "",
                            "injured": "False",
                            "minutes_played": "90",
                            "starting_lineups": "1",
                            "on_bench": "2",
                            "appearences": "3",
                            "assists": "0",
                            "goals": "",
                            "shots_on_target": "1",
                            "shots_total": "2",
                            "pen_saved": "1",
                            "yellowcards": "0",
                            "redcards": "0",
                            "rating": "6.960000",
                        }
                    ]
                },
                "transfers": {
                    "in": {
                        "player": [
                            {
                                "id": "2664686",
                                "name": "Joe Lumley",
                                "date": "27.10.2025",
                                "age": "",
                                "position": "",
                                "from": "Sheffield Wed",
                                "team_id": "2341182",
                                "type": "Return from loan",
                                "price": "",
                            }
                        ]
                    },
                    "out": {
                        "player": [
                            {
                                "id": "2664686",
                                "name": "Joe Lumley",
                                "date": "20.10.2025",
                                "age": "",
                                "position": "",
                                "to": "Sheffield Wed",
                                "team_id": "2341182",
                                "type": "Loan",
                                "price": "",
                            }
                        ]
                    },
                },
                "trophies": {
                    "trophy": [
                        {
                            "country": "England",
                            "league": "Championship",
                            "status": "Winner",
                            "count": "1",
                            "seasons": "1905/1906,",
                        }
                    ]
                },
                "league_stats": {
                    "league": [
                        {
                            "name": "Championship",
                            "id": "3038",
                            "season": "2025/2026",
                            "fulltime": {
                                "win": {"total": "8", "home": "4", "away": "4"},
                                "lost": {"total": "6", "home": "4", "away": "2"},
                                "draw": {"total": "5", "home": "2", "away": "3"},
                                "goals_for": {"total": "26", "home": "14", "away": "12"},
                                "avg_corners": {"total": "4.8", "home": "5.1", "away": "4.5"},
                            },
                            "firsthalf": {
                                "win": {"total": "9", "home": "5", "away": "4"},
                                "lost": {"total": "4", "home": "2", "away": "2"},
                                "draw": {"total": "6", "home": "3", "away": "3"},
                            },
                            "secondhalf": {
                                "win": {"total": "4", "home": "2", "away": "2"},
                                "lost": {"total": "6", "home": "3", "away": "2"},
                                "draw": {"total": "9", "home": "5", "away": "4"},
                            },
                            "scoring_minutes": {
                                "period": [
                                    {"min": "0-15", "pct": "11%", "count": "3"},
                                    {"min": "15-30", "pct": "23%", "count": "6"},
                                ]
                            },
                            "redcard_minutes": {"period": {"min": "0-15", "pct": "%", "count": "0"}},
                        }
                    ]
                },
            },
        }

        team = normalize_team(payload)

        self.assertEqual(team["provider"], "statpal")
        self.assertEqual(team["id"], "statpal:2340899")
        self.assertEqual(team["provider_team_id"], "2340899")
        self.assertEqual(team["name"], "Bristol City")
        self.assertEqual(team["country"], "England")
        self.assertEqual(team["founded"], 1894)
        self.assertFalse(team["is_national_team"])
        self.assertFalse(team["is_women"])
        self.assertEqual(team["league_ids"], ["3038", "3367"])
        self.assertEqual(team["venue"]["capacity"], 27000)
        self.assertEqual(team["venue"]["city"], "Bristol")
        self.assertEqual(team["coach"]["id"], "2820329")
        self.assertEqual(team["squad_count"], 1)
        self.assertEqual(team["squad"][0]["player_id"], "3446137")
        self.assertEqual(team["squad"][0]["appearances"], 3)
        self.assertEqual(team["squad"][0]["shots_on"], 1)
        self.assertEqual(team["squad"][0]["stats"]["penalties_saved"], 1)
        self.assertEqual(team["transfers"]["in"][0]["date"], date(2025, 10, 27))
        self.assertEqual(team["transfers"]["out"][0]["to"], "Sheffield Wed")
        self.assertEqual(team["trophies"][0]["count"], 1)
        self.assertEqual(team["trophies"][0]["seasons"], ["1905/1906"])
        self.assertEqual(team["league_stats"][0]["league_id"], "3038")
        self.assertEqual(team["league_stats"][0]["fulltime"]["win"]["total"], 8)
        self.assertEqual(team["league_stats"][0]["fulltime"]["avg_corners"]["home"], 5.1)
        self.assertEqual(team["league_stats"][0]["firsthalf"]["win"]["home"], 5)
        self.assertEqual(team["league_stats"][0]["secondhalf"]["draw"]["away"], 4)
        self.assertEqual(team["league_stats"][0]["scoring_minutes"][1]["pct"], 23)
        self.assertIsNone(team["league_stats"][0]["redcard_minutes"][0]["pct"])
        self.assertEqual(team["feed_updated_ts"], 1765307501)

    def test_normalize_player_outputs_profile_statistics_and_history(self):
        payload = {
            "updated": "09.12.2025 21:45:23",
            "updated_ts": 1765316723,
            "player": {
                "id": "2773317",
                "name": "Erling Haaland",
                "firstname": "Erling",
                "lastname": "Haaland",
                "age": "25",
                "birthdate": "21.07.2000",
                "nationality": "Norway",
                "birthplace": "Leeds",
                "birthcountry": "Norway",
                "position": "Forward",
                "height": "195",
                "weight": "88",
                "preferred_foot": "Left",
                "team": "Manchester City",
                "team_id": "2341092",
                "national_team_id": "2345515",
                "market_value_eur": "196000000",
                "club_league_statistics": {
                    "club": [
                        {
                            "team_id": "2341092",
                            "team_name": "Manchester City",
                            "league_id": "3037",
                            "league": "Premier League",
                            "season": "2025/2026",
                            "is_captain": "0",
                            "minutes_played": "1287",
                            "appearances": "15",
                            "starting_lineups": "15",
                            "substitute_in": "0",
                            "assists": "3",
                            "goals": "15",
                            "pen_missed": "1",
                            "pen_scored": "0",
                            "rating": "7.640000",
                            "redcards": "0",
                            "shots_on_target": "32",
                            "shots_total": "52",
                            "yellowcards": "0",
                        }
                    ]
                },
                "club_domestic_cup_statistics": {"club": []},
                "club_intl_cup_statistics": {"club": []},
                "overall_club_statistics": {
                    "minutes_played": "24910",
                    "appearances": "336",
                    "starting_lineups": "294",
                    "substitute_in": "42",
                    "assists": "51",
                    "goals": "302",
                    "pen_missed": "7",
                    "pen_scored": "44",
                    "rating": "7.345846",
                    "shots_on_target": "565",
                    "shots_total": "938",
                },
                "national_team_statistics": {"leagues": []},
                "transfers": [
                    {
                        "date": "01.07.2022",
                        "from": "Borussia Dortmund",
                        "from_id": "2340935",
                        "to": "Manchester City",
                        "to_id": "2341092",
                        "type": "Transfer",
                        "price": "60000000",
                    }
                ],
                "trophies": {
                    "trophy": [
                        {
                            "country": "England",
                            "league": "Premier League",
                            "status": "Winner",
                            "count": "2",
                            "seasons": "2022/2023,2023/2024",
                        }
                    ]
                },
                "sidelined_history": [
                    {
                        "type": "Foot Injury",
                        "date_start": "10.12.2023",
                        "date_end": "31.01.2024",
                    }
                ],
            },
        }

        player = normalize_player(payload)

        self.assertEqual(player["provider"], "statpal")
        self.assertEqual(player["id"], "statpal:2773317")
        self.assertEqual(player["provider_player_id"], "2773317")
        self.assertEqual(player["name"], "Erling Haaland")
        self.assertEqual(player["birthdate"], date(2000, 7, 21))
        self.assertEqual(player["age"], 25)
        self.assertEqual(player["height_cm"], 195)
        self.assertEqual(player["weight_kg"], 88)
        self.assertEqual(player["team_id"], "2341092")
        self.assertEqual(player["market_value_eur"], 196000000)
        self.assertEqual(len(player["club_league_statistics"]), 1)
        stat = player["club_league_statistics"][0]
        self.assertEqual(stat["scope"], "club_league")
        self.assertEqual(stat["team_name"], "Manchester City")
        self.assertFalse(stat["is_captain"])
        self.assertEqual(stat["minutes_played"], 1287)
        self.assertEqual(stat["goals"], 15)
        self.assertEqual(stat["shots_on"], 32)
        self.assertEqual(stat["stats"]["penalties_missed"], 1)
        self.assertEqual(player["overall_club_statistics"]["appearances"], 336)
        self.assertEqual(player["overall_club_statistics"]["shots_on"], 565)
        self.assertEqual(player["overall_club_statistics"]["penalties_scored"], 44)
        self.assertEqual(player["transfers"][0]["date"], date(2022, 7, 1))
        self.assertEqual(player["transfers"][0]["to_id"], "2341092")
        self.assertEqual(player["trophies"][0]["count"], 2)
        self.assertEqual(player["trophies"][0]["seasons"], ["2022/2023", "2023/2024"])
        self.assertEqual(player["sidelined_history"][0]["date_start"], date(2023, 12, 10))
        self.assertEqual(player["sidelined_history"][0]["date_end"], date(2024, 1, 31))
        self.assertEqual(player["feed_updated_ts"], 1765316723)

    def test_normalize_team_lineups_outputs_projected_match_lineups(self):
        payload = {
            "main_id": "2026061822389",
            "status": "projected",
            "updated": "06.17.2026 12:31:15",
            "updated_ts": 1781699475314,
            "home": {
                "team_id": "2339730",
                "team_name": "Canada",
                "coach": {"name": "Jesse Marsch", "id": "3381958"},
                "team_formation": "4-4-2",
                "starting_xi": [
                    {"id": "2504652", "name": "Maxime Crepeau", "number": "16", "position": "goalkeeper"},
                    {"id": "2813361", "name": "Jonathan David", "number": "10", "position": "attacker"},
                ],
                "bench": [
                    {"id": "3002838", "name": "Owen Goodman", "number": "18", "position": "goalkeeper"},
                ],
                "sidelined": [
                    {
                        "id": "3224437",
                        "name": "Moise Bombito",
                        "number": "15",
                        "position": "defender",
                        "status": "doubtful",
                        "reason": "injury",
                    },
                    {
                        "id": "2929138",
                        "name": "Jacob Shaffelburg",
                        "number": "14",
                        "position": "attacker",
                        "status": "out",
                        "reason": None,
                    },
                ],
                "confidence": 45,
            },
            "away": {
                "team_id": "2346325",
                "team_name": "Qatar",
                "coach": {"name": "Julen Lopetegui", "id": "2529722"},
                "team_formation": "4-3-3",
                "starting_xi": [
                    {"id": "2923575", "name": "Mahmoud Abunada", "number": "1", "position": "goalkeeper"},
                    {"id": "2675572", "name": "Akram Afif", "number": "11", "position": "attacker"},
                ],
                "bench": [
                    {"id": "2838938", "name": "Meshaal Barsham", "number": "22", "position": "goalkeeper"},
                ],
                "sidelined": [],
                "confidence": 45,
            },
        }

        lineups = normalize_team_lineups(payload)

        self.assertEqual(lineups["provider"], "statpal")
        self.assertEqual(lineups["id"], "statpal:lineups:2026061822389")
        self.assertEqual(lineups["match_id"], "statpal:2026061822389")
        self.assertEqual(lineups["provider_match_id"], "2026061822389")
        self.assertEqual(lineups["status"], "projected")
        self.assertEqual(lineups["home"]["team_id"], "2339730")
        self.assertEqual(lineups["home"]["coach"]["name"], "Jesse Marsch")
        self.assertEqual(lineups["home"]["formation"], "4-4-2")
        self.assertEqual(lineups["home"]["starting_xi"][1]["name"], "Jonathan David")
        self.assertEqual(lineups["home"]["bench"][0]["position"], "goalkeeper")
        self.assertEqual(lineups["home"]["sidelined"][0]["status"], "doubtful")
        self.assertIsNone(lineups["home"]["sidelined"][1]["reason"])
        self.assertEqual(lineups["home"]["confidence"], 45)
        self.assertEqual(lineups["away"]["team_name"], "Qatar")
        self.assertEqual(lineups["away"]["formation"], "4-3-3")
        self.assertEqual(lineups["starting_count"], 4)
        self.assertEqual(lineups["bench_count"], 2)
        self.assertEqual(lineups["sidelined_count"], 2)
        self.assertEqual(lineups["feed_updated_ts"], 1781699475314)

    def test_normalize_prematch_odds_outputs_match_markets_and_bookmakers(self):
        payload = {
            "prematch_odds": {
                "updated": "09.12.2025 17:15:44",
                "updated_ts": 1765300544,
                "league": {
                    "id": "3037",
                    "name": "England: Premier League",
                    "country": "england",
                    "match": [
                        {
                            "main_id": "2025121318250",
                            "fallback_id_1": "6014320",
                            "fallback_id_2": "6521701",
                            "fallback_id_3": "8800931",
                            "date": "13.12.2025",
                            "time": "15:00",
                            "home": {"id": "2340925", "name": "Chelsea"},
                            "away": {"id": "2340991", "name": "Everton"},
                            "odds": [
                                {
                                    "id": "1834",
                                    "name": "1x2",
                                    "stop": "False",
                                    "bookmaker": [
                                        {
                                            "id": "1847",
                                            "name": "10Bet",
                                            "timestamp": "1765252069",
                                            "odd": [
                                                {"name": "Home", "value": "1.64"},
                                                {"name": "Draw", "value": "3.75"},
                                                {"name": "Away", "value": "5.10"},
                                            ],
                                        }
                                    ],
                                },
                                {
                                    "id": "1836",
                                    "name": "Totals",
                                    "stop": "False",
                                    "bookmaker": {
                                        "id": "1847",
                                        "name": "10Bet",
                                        "timestamp": 1765252069,
                                        "total": [
                                            {
                                                "name": "2.5",
                                                "stop": "False",
                                                "is_main": "True",
                                                "odd": [
                                                    {"name": "Over", "value": "1.85"},
                                                    {"name": "Under", "value": "1.95"},
                                                ],
                                            }
                                        ],
                                    },
                                },
                            ],
                        }
                    ],
                },
            }
        }

        rows = normalize_prematch_odds(payload)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider"], "statpal")
        self.assertEqual(row["id"], "statpal:prematch_odds:2025121318250")
        self.assertEqual(row["match_id"], "statpal:2025121318250")
        self.assertEqual(row["fallback_match_ids"], ["6014320", "6521701", "8800931"])
        self.assertEqual(row["provider_competition_id"], "3037")
        self.assertEqual(row["league"], "England: Premier League")
        self.assertEqual(row["country"], "england")
        self.assertEqual(row["date"], date(2025, 12, 13))
        self.assertEqual(row["kickoff"], "15:00")
        self.assertEqual(row["fixture"], "Chelsea vs Everton")
        self.assertEqual(row["market_count"], 2)
        market = row["markets"][0]
        self.assertEqual(market["id"], "1834")
        self.assertEqual(market["name"], "1x2")
        self.assertFalse(market["stop"])
        self.assertEqual(market["bookmaker_count"], 1)
        bookmaker = market["bookmakers"][0]
        self.assertEqual(bookmaker["id"], "1847")
        self.assertEqual(bookmaker["timestamp"], 1765252069)
        self.assertEqual(bookmaker["odds"][0]["name"], "Home")
        self.assertEqual(bookmaker["odds"][0]["value"], 1.64)
        total = row["markets"][1]["bookmakers"][0]["totals"][0]
        self.assertEqual(total["line"], 2.5)
        self.assertTrue(total["is_main"])
        self.assertEqual(total["odds"][1]["name"], "Under")
        self.assertEqual(total["odds"][1]["value"], 1.95)
        self.assertEqual(row["feed_updated_ts"], 1765300544)

    def test_normalize_league_standings_handles_single_table(self):
        payload = {
            "standings": {
                "updated": "09.12.2025 07:14:52",
                "updated_ts": 1765264492,
                "country": "england",
                "tournament": {
                    "id": "3038",
                    "league": "Championship",
                    "season": "2025/2026",
                    "stage_id": "14382914",
                    "is_current": "True",
                    "team": [
                        {
                            "position": "1",
                            "name": "Coventry",
                            "id": "2340949",
                            "status": "same",
                            "recent_form": "LWWWW",
                            "overall": {
                                "games_played": "19",
                                "wins": "13",
                                "draws": "4",
                                "losses": "2",
                                "goals_scored": "50",
                                "goals_allowed": "21",
                            },
                            "home": {
                                "games_played": "9",
                                "wins": "7",
                                "draws": "2",
                                "losses": "0",
                                "goals_scored": "25",
                                "goals_allowed": "7",
                            },
                            "away": {
                                "games_played": "10",
                                "wins": "6",
                                "draws": "2",
                                "losses": "2",
                                "goals_scored": "25",
                                "goals_allowed": "14",
                            },
                            "total": {"goal_difference": "29", "points": "43"},
                            "description": {"value": "Promotion - Premier League"},
                        }
                    ],
                },
            }
        }

        rows = normalize_league_standings(payload)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider"], "statpal")
        self.assertEqual(row["provider_competition_id"], "3038")
        self.assertEqual(row["league"], "Championship")
        self.assertEqual(row["country"], "england")
        self.assertEqual(row["season"], "2025/2026")
        self.assertTrue(row["stage_is_current"])
        self.assertEqual(row["team_id"], "2340949")
        self.assertEqual(row["team_name"], "Coventry")
        self.assertEqual(row["position"], 1)
        self.assertEqual(row["recent_form"], "LWWWW")
        self.assertEqual(row["overall"]["games_played"], 19)
        self.assertEqual(row["home"]["goals_allowed"], 7)
        self.assertEqual(row["away"]["wins"], 6)
        self.assertEqual(row["goal_difference"], 29)
        self.assertEqual(row["points"], 43)
        self.assertEqual(row["description"], "Promotion - Premier League")
        self.assertEqual(row["feed_updated_ts"], 1765264492)

    def test_normalize_league_standings_handles_grouped_tournaments(self):
        payload = {
            "standings": {
                "updated": "12.12.2022 04:40:49",
                "updated_ts": "1670820049",
                "country": "international",
                "tournament": [
                    {
                        "id": "2889",
                        "season": "2022",
                        "stage_id": "12892860",
                        "is_current": "False",
                        "name": "FIFA World Cup: Group A",
                        "date": "12.12.2022",
                        "group": "Group A",
                        "group_id": "1989",
                        "team": [
                            {
                                "position": "1",
                                "name": "Netherlands",
                                "id": "2345002",
                                "status": "same",
                                "recent_form": "WDW",
                                "overall": {
                                    "games_played": "3",
                                    "wins": "2",
                                    "draws": "1",
                                    "losses": "0",
                                    "goals_scored": "5",
                                    "goals_allowed": "1",
                                },
                                "home": {
                                    "games_played": "",
                                    "wins": "",
                                    "draws": "",
                                    "losses": "",
                                    "goals_scored": "",
                                    "goals_allowed": "",
                                },
                                "away": {
                                    "games_played": "",
                                    "wins": "",
                                    "draws": "",
                                    "losses": "",
                                    "goals_scored": "",
                                    "goals_allowed": "",
                                },
                                "total": {"goal_difference": "+4", "points": "7"},
                                "description": {"value": "Promotion - World Cup (Play Offs)"},
                            }
                        ],
                    }
                ],
            }
        }

        rows = normalize_league_standings(payload)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider_competition_id"], "2889")
        self.assertEqual(row["league"], "FIFA World Cup: Group A")
        self.assertEqual(row["stage_name"], "FIFA World Cup: Group A")
        self.assertEqual(row["stage_date"], date(2022, 12, 12))
        self.assertEqual(row["group"], "Group A")
        self.assertEqual(row["group_id"], "1989")
        self.assertFalse(row["stage_is_current"])
        self.assertEqual(row["goal_difference"], 4)
        self.assertEqual(row["goal_difference_raw"], "+4")
        self.assertIsNone(row["home"]["games_played"])
        self.assertEqual(row["feed_updated_ts"], 1670820049)

    def test_normalize_league_stats_outputs_player_rows_with_team_context(self):
        payload = {
            "league_stats": {
                "updated": "09.12.2025 17:40:59",
                "updated_ts": "1765302059",
                "league": {
                    "id": "3037",
                    "name": "Premier League",
                    "country": "england",
                    "team": [
                        {
                            "id": "2340835",
                            "name": "Arsenal",
                            "venue": {"id": "2419812", "name": "Emirates Stadium"},
                            "squad": {
                                "player": [
                                    {
                                        "id": "2662112",
                                        "name": "David Raya",
                                        "number": "1",
                                        "age": "30",
                                        "position": "G",
                                        "injured": "False",
                                        "appearences": "15",
                                        "assists": "0",
                                        "clearances": "11",
                                        "duels_total": "7",
                                        "duels_won": "7",
                                        "fouls_drawn": "4",
                                        "goals": "",
                                        "goals_conceded": "9",
                                        "inside_box_saves": "19",
                                        "key_passes": "1",
                                        "lineups": "15",
                                        "minutes_played": "1350",
                                        "pass_attempts": "458",
                                        "pass_success": "301",
                                        "penalties_missed": "0",
                                        "penalties_saved": "0",
                                        "penalties_scored": "0",
                                        "rating": "6.960000",
                                        "redcards": "0",
                                        "saves": "25",
                                        "substitute_in": "0",
                                        "yellowcards": "1",
                                    }
                                ]
                            },
                            "coach": {"id": "2334709", "name": "Mikel Arteta"},
                        }
                    ],
                },
            }
        }

        rows = normalize_league_stats(payload)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider_competition_id"], "3037")
        self.assertEqual(row["league"], "Premier League")
        self.assertEqual(row["country"], "england")
        self.assertEqual(row["team_id"], "2340835")
        self.assertEqual(row["team_name"], "Arsenal")
        self.assertEqual(row["venue"]["name"], "Emirates Stadium")
        self.assertEqual(row["coach"]["name"], "Mikel Arteta")
        self.assertEqual(row["player_id"], "2662112")
        self.assertEqual(row["player_name"], "David Raya")
        self.assertEqual(row["age"], 30)
        self.assertEqual(row["position"], "G")
        self.assertFalse(row["injured"])
        self.assertEqual(row["appearances"], 15)
        self.assertEqual(row["minutes_played"], 1350)
        self.assertEqual(row["assists"], 0)
        self.assertIsNone(row["goals"])
        self.assertEqual(row["saves"], 25)
        self.assertEqual(row["rating"], 6.96)
        self.assertEqual(row["yellowcards"], 1)
        self.assertEqual(row["stats"]["appearences"], 15)
        self.assertEqual(row["stats"]["inside_box_saves"], 19)
        self.assertEqual(row["feed_updated_ts"], 1765302059)

    def test_normalize_head_to_head_outputs_aggregate_and_match_context(self):
        payload = {
            "head-to-head": {
                "team1_id": "2341082",
                "team2_id": "2341092",
                "recent_meetings": {
                    "match": [
                        {
                            "main_id": "2025110918508",
                            "fallback_id_1": "6014271",
                            "country": "england",
                            "league": "Premier League",
                            "league_id": "3037",
                            "date": "09.11.2025",
                            "team1_name": "Manchester City",
                            "team2_name": "Liverpool",
                            "team1_id": "2341092",
                            "team2_id": "2341082",
                            "team1_score": "3",
                            "team2_score": "0",
                        }
                    ]
                },
                "overall_record": {
                    "total": {"total": [{"games": "180"}, {"team1_won": "83"}, {"team2_won": "48"}, {"draws": "48"}]},
                    "home": {
                        "team1": [{"games": "90"}, {"won": "52"}, {"lost": "14"}, {"draws": "23"}],
                        "team2": [{"games": "90"}, {"won": "34"}, {"lost": "31"}, {"draws": "25"}],
                    },
                    "away": {
                        "team1": [{"games": "90"}, {"won": "31"}, {"lost": "34"}, {"draws": "25"}],
                        "team2": [{"games": "90"}, {"won": "14"}, {"lost": "52"}, {"draws": "23"}],
                    },
                },
                "leagues": {
                    "league": [
                        {"name": "UEFA Champions League", "id": "2838", "games": "2", "team1_won": "2", "team2_won": "0", "draw": "0"}
                    ]
                },
                "goals": {
                    "total": {
                        "total": [
                            {"team1_scored": "306"},
                            {"team1_conceded": "248"},
                            {"team2_scored": "248"},
                            {"team2_conceded": "306"},
                        ]
                    },
                    "home": {"home": [{"team1_scored": "176"}, {"team1_conceded": "108"}]},
                    "away": {"away": [{"team1_scored": "130"}, {"team1_conceded": "140"}]},
                },
                "biggest_victory": {
                    "team1": {
                        "match": {
                            "main_id": "1995102718508",
                            "fallback_id_1": "2350096",
                            "country": "england",
                            "league": "Premier League",
                            "league_id": "3037",
                            "date": "27.10.1995",
                            "team1_name": "Liverpool",
                            "team2_name": "Manchester City",
                            "team1_id": "2341082",
                            "team2_id": "2341092",
                            "team1_score": "6",
                            "team2_score": "0",
                        }
                    },
                    "team2": {"match": {}},
                },
                "biggest_defeat": {"team1": {"match": {}}, "team2": {"match": {}}},
                "last5_home": {
                    "team1": {
                        "match": [
                            {
                                "main_id": "2025120318633",
                                "date": "03.12.2025",
                                "team1_name": "Liverpool",
                                "team2_name": "Sunderland",
                                "team1_score": "1",
                                "team2_score": "1",
                            }
                        ]
                    },
                    "team2": {"match": []},
                },
                "last5_away": {"team1": {"match": []}, "team2": {"match": []}},
            }
        }

        h2h = normalize_head_to_head(payload)

        self.assertEqual(h2h["team1_id"], "2341082")
        self.assertEqual(h2h["team2_id"], "2341092")
        self.assertEqual(h2h["overall_record"]["total"]["games"], 180)
        self.assertEqual(h2h["overall_record"]["home"]["team1"]["won"], 52)
        self.assertEqual(h2h["leagues"][0]["name"], "UEFA Champions League")
        self.assertEqual(h2h["leagues"][0]["draws"], 0)
        self.assertEqual(h2h["goals"]["total"]["team1_scored"], 306)
        self.assertEqual(h2h["recent_meetings"][0]["date"], date(2025, 11, 9))
        self.assertEqual(h2h["recent_meetings"][0]["team1_score"], 3)
        self.assertEqual(h2h["recent_meetings"][0]["fallback_match_ids"], ["6014271"])
        self.assertEqual(h2h["biggest_victory"]["team1"]["team1_score"], 6)
        self.assertEqual(h2h["last5_home"]["team1"][0]["provider_match_id"], "2025120318633")


class StatPalDailyNormalizeTests(SimpleTestCase):
    def test_normalize_daily_matches_handles_documented_dynamic_date_wrapper(self):
        payload = {
            "matches_15_12_2025": {
                "updated": "26.11.2025 16:54:37",
                "updated_ts": 1764176077,
                "league": [
                    {
                        "id": "2905",
                        "name": "Andorra: Primera Divisio",
                        "country": "andorra",
                        "cup": "False",
                        "match": [
                            {
                                "main_id": "2025112611667",
                                "fallback_id_1": "6099244",
                                "fallback_id_2": "6606657",
                                "fallback_id_3": "8708898",
                                "status": "FT",
                                "date": "15.12.2025",
                                "time": "13:30",
                                "venue": "Centre d'Entrenament de la FAF 1",
                                "home": {"id": "2337666", "name": "FC Santa Coloma", "goals": "1"},
                                "away": {"id": "2337667", "name": "Inter Escaldes", "goals": "3"},
                                "events": {"event": [{"id": "66102484", "type": "goal"}]},
                                "ht": {"home_goals": 1, "away_goals": 1},
                                "ft": {"home_goals": 1, "away_goals": 3},
                                "has_live_stats": "False",
                                "inplay_odds_running": "True",
                                "match_context": {
                                    "live_storylines": False,
                                    "weather_forecast": True,
                                    "team_lineups": True,
                                    "predictions": False,
                                },
                            }
                        ],
                    }
                ],
            }
        }

        fixtures = normalize_daily_matches(payload, target_date=date(2025, 12, 15))

        self.assertEqual(len(fixtures), 1)
        fixture = fixtures[0]
        self.assertEqual(fixture["match_id"], "statpal:2025112611667")
        self.assertEqual(fixture["provider_match_id"], "2025112611667")
        self.assertEqual(fixture["fallback_match_ids"], ["6099244", "6606657", "8708898"])
        self.assertEqual(fixture["provider_competition_id"], "2905")
        self.assertEqual(fixture["fixture"], "FC Santa Coloma vs Inter Escaldes")
        self.assertEqual(fixture["date"], date(2025, 12, 15))
        self.assertEqual(fixture["kickoff"], "13:30")
        self.assertEqual(fixture["status"], "FT")
        self.assertEqual(fixture["venue"], "Centre d'Entrenament de la FAF 1")
        self.assertEqual(fixture["home_goals"], 1)
        self.assertEqual(fixture["away_goals"], 3)
        self.assertEqual(fixture["ht_home_goals"], 1)
        self.assertEqual(fixture["ft_away_goals"], 3)
        self.assertFalse(fixture["has_live_stats"])
        self.assertTrue(fixture["inplay_odds_running"])
        self.assertTrue(fixture["match_context"]["weather_forecast"])
        self.assertTrue(fixture["match_context"]["team_lineups"])
        self.assertFalse(fixture["match_context"]["predictions"])
        self.assertEqual(fixture["feed_updated"], "26.11.2025 16:54:37")
        self.assertEqual(fixture["feed_updated_ts"], 1764176077)
        self.assertEqual(fixture["api_payload"]["provider_match_id"], "2025112611667")

    def test_normalize_league_matches_handles_tournament_week_payload(self):
        payload = {
            "matches": {
                "updated": "06.12.2025 15:03:51",
                "updated_ts": 1765033431,
                "country": "england",
                "tournament": {
                    "id": "3037",
                    "league": "Premier League",
                    "season": "2025/2026",
                    "stage_id": "14372914",
                    "is_current": "True",
                    "week": [
                        {
                            "number": "1",
                            "match": [
                                {
                                    "main_id": "2025081518302",
                                    "fallback_id_1": "6024103",
                                    "fallback_id_2": "6531486",
                                    "date": "15.08.2025",
                                    "time": "19:00",
                                    "status": "FT",
                                    "venue": "Anfield",
                                    "venue_id": "",
                                    "venue_city": "Liverpool",
                                    "attendance": "60 315",
                                    "home": {"id": "2341082", "name": "Liverpool", "score": "4"},
                                    "away": {"id": "2340886", "name": "Bournemouth", "score": "2"},
                                    "coaches": {
                                        "home": {"coach": {"id": "2334186", "name": "Arend Martijn Slot"}},
                                        "away": {"coach": {"id": "2887252", "name": "Andoni Iraola Sagarna"}},
                                    },
                                    "referee": {"id": "", "name": ""},
                                    "lineups": {
                                        "home": {
                                            "formation": "4-2-3-1",
                                            "player": [{"number": "1", "id": "2434476", "name": "Alisson", "booking": ""}],
                                        },
                                        "away": {
                                            "formation": "4-2-3-1",
                                            "player": [{"number": "1", "id": "2795235", "name": "D. Petrovic", "booking": ""}],
                                        },
                                    },
                                    "substitutions": {
                                        "home": {
                                            "substitution": [
                                                {
                                                    "player_in_number": "25",
                                                    "player_in_name": "G. Mamardashvili",
                                                    "player_in_booking": "",
                                                    "player_in_id": "2794974",
                                                    "player_out_name": "",
                                                    "player_out_id": "",
                                                    "minute": "",
                                                }
                                            ]
                                        },
                                        "away": {"substitution": []},
                                    },
                                    "goals": {
                                        "goal": [
                                            {
                                                "team": "home",
                                                "minute": "37",
                                                "player": "H. Ekitike",
                                                "score": "[1 - 0]",
                                                "playerid": "2956415",
                                                "assist": "A. Mac Allister",
                                                "assistid": "2799893",
                                            }
                                        ]
                                    },
                                    "ht": {"home_goals": 1, "away_goals": 0},
                                    "ft": {"home_goals": 4, "away_goals": 2},
                                }
                            ],
                        }
                    ],
                },
            }
        }

        fixtures = normalize_daily_matches(payload, target_date=date(2025, 8, 15))

        self.assertEqual(len(fixtures), 1)
        fixture = fixtures[0]
        self.assertEqual(fixture["match_id"], "statpal:2025081518302")
        self.assertEqual(fixture["provider_competition_id"], "3037")
        self.assertEqual(fixture["league"], "Premier League")
        self.assertEqual(fixture["country"], "england")
        self.assertEqual(fixture["season"], "2025/2026")
        self.assertEqual(fixture["stage_id"], "14372914")
        self.assertTrue(fixture["stage_is_current"])
        self.assertEqual(fixture["week"], "1")
        self.assertEqual(fixture["round"], "1")
        self.assertEqual(fixture["home_goals"], 4)
        self.assertEqual(fixture["away_goals"], 2)
        self.assertEqual(fixture["venue"], "Anfield")
        self.assertEqual(fixture["venue_city"], "Liverpool")
        self.assertEqual(fixture["attendance"], "60 315")
        self.assertEqual(fixture["lineups"]["home"]["formation"], "4-2-3-1")
        self.assertEqual(fixture["lineups"]["home"]["players"][0]["id"], "2434476")
        self.assertEqual(fixture["substitutions"]["home"][0]["player_in_id"], "2794974")
        self.assertEqual(fixture["goals"][0]["player_id"], "2956415")
        self.assertEqual(fixture["goals"][0]["assist_id"], "2799893")
        self.assertEqual(fixture["coaches"]["home"]["id"], "2334186")
        self.assertEqual(fixture["feed_updated"], "06.12.2025 15:03:51")


class StatPalMatchStatsNormalizeTests(SimpleTestCase):
    def test_normalize_match_stats_outputs_stats_events_and_lineup_shape(self):
        payload = {
            "match-stats": {
                "updated": "09.12.2025 04:22:31",
                "updated_ts": 1765254151,
                "tournament": {
                    "id": "3037",
                    "name": "England - Premier League",
                    "matches": {
                        "main_id": "2025120818706",
                        "fallback_id_1": "6023967",
                        "fallback_id_2": "6531349",
                        "date": "08.12.2025",
                        "time": "20:00",
                        "status": "Full-time",
                        "match_info": {
                            "stadium": {"name": "Molineux Stadium, Wolverhampton"},
                            "time": {"name": "20:00", "added_time_period_1": "2", "added_time_period_2": "9"},
                            "referee": {"name": "Michael Salisbury, England"},
                        },
                        "home": {"id": "2341279", "name": "Wolverhampton", "goals": "1"},
                        "away": {"id": "2341093", "name": "Manchester United", "goals": "4"},
                        "ht": {"home_goals": "1", "away_goals": "1"},
                        "ft": {"home_goals": "1", "away_goals": "4"},
                        "lineups": {
                            "home": {
                                "formation": "3-4-2-1",
                                "player": [
                                    {"id": "2528099", "name": "Sam Johnstone", "number": "31", "pos": "G", "formation_pos": "1"}
                                ],
                            },
                            "away": {
                                "formation": "3-4-2-1",
                                "player": [{"id": "2903698", "name": "Senne Lammens", "number": "31", "pos": "G"}],
                            },
                        },
                        "bench": {
                            "home": {"player": [{"id": "3204556", "name": "Fer Lopez", "number": "28", "pos": "F"}]},
                            "away": {"player": [{"id": "2803504", "name": "Lisandro Martinez", "number": "6", "pos": "D"}]},
                        },
                        "substitutions": {
                            "home": {
                                "substitution": [
                                    {
                                        "minute": "86",
                                        "player_on": "Jackson Tchatchoua",
                                        "player_on_id": "3018150",
                                        "player_off": "Ki-Jana Hoever",
                                        "player_off_id": "2857755",
                                        "injury": "False",
                                    }
                                ]
                            },
                            "away": {"substitution": []},
                        },
                        "team_stats": {
                            "home": {
                                "corners": {"total": "1", "total_h1": "1", "total_h2": "0"},
                                "expected_goals": {"total": "0.41", "total_h1": "0.29", "total_h2": "0.12"},
                                "fouls": {"total": "17"},
                            },
                            "away": {
                                "corners": {"total": "9", "total_h1": "6", "total_h2": "3"},
                                "expected_goals": {"total": "4.24", "total_h1": "1.01", "total_h2": "3.23"},
                                "fouls": {"total": "12"},
                            },
                        },
                        "player_stats": {
                            "home": {"player": [{"id": "2528099", "name": "Sam Johnstone", "num": "31", "pos": "G", "assists": "0"}]},
                            "away": {"player": [{"id": "2903698", "name": "Senne Lammens", "num": "31", "pos": "G"}]},
                        },
                        "event_summary": {
                            "home": {
                                "goals": {
                                    "event": {
                                        "minute": "45",
                                        "extra_min": "2",
                                        "player_id": "2752619",
                                        "player_name": "Jean-Ricner Bellegarde",
                                        "assist_player_id": "2935127",
                                        "assist_player_name": "David Moller Wolfe",
                                        "own_goal": "False",
                                        "penalty": "False",
                                        "penalty_missed": "False",
                                        "var_cancelled": "False",
                                    }
                                },
                                "yellowcards": "",
                                "redcards": "",
                                "var": "",
                            },
                            "away": {
                                "goals": "",
                                "yellowcards": {
                                    "event": [
                                        {
                                            "minute": "90",
                                            "extra_min": "6",
                                            "comment": "Argument",
                                            "player_id": "2848210",
                                            "player_name": "Joshua Zirkzee",
                                        }
                                    ]
                                },
                                "redcards": "",
                                "var": {
                                    "event": {
                                        "minute": "80",
                                        "player_id": "2964802",
                                        "player_name": "Amad Diallo",
                                        "event_type": "Penalty confirmed",
                                        "ref_decision": "Penalty cancelled",
                                        "var_decision": "False",
                                    }
                                },
                            },
                        },
                    },
                },
            }
        }

        matches = normalize_match_stats(payload)

        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match["match_id"], "statpal:2025120818706")
        self.assertEqual(match["provider_match_id"], "2025120818706")
        self.assertEqual(match["fallback_match_ids"], ["6023967", "6531349"])
        self.assertEqual(match["provider_competition_id"], "3037")
        self.assertEqual(match["league"], "England - Premier League")
        self.assertEqual(match["fixture"], "Wolverhampton vs Manchester United")
        self.assertEqual(match["date"], date(2025, 12, 8))
        self.assertEqual(match["venue"], "Molineux Stadium, Wolverhampton")
        self.assertEqual(match["referee"]["name"], "Michael Salisbury, England")
        self.assertEqual(match["home_goals"], 1)
        self.assertEqual(match["away_goals"], 4)
        self.assertEqual(match["ht_home_goals"], 1)
        self.assertEqual(match["ft_away_goals"], 4)
        self.assertEqual(match["lineups"]["home"]["players"][0]["position"], "G")
        self.assertEqual(match["lineups"]["home"]["players"][0]["formation_position"], "1")
        self.assertEqual(match["bench"]["away"]["players"][0]["id"], "2803504")
        self.assertEqual(match["substitutions"]["home"][0]["player_in_id"], "3018150")
        self.assertFalse(match["substitutions"]["home"][0]["injury"])
        self.assertEqual(match["team_stats"]["home"]["corners"]["total"], 1)
        self.assertEqual(match["team_stats"]["away"]["expected_goals"]["total"], 4.24)
        self.assertEqual(match["player_stats"]["home"][0]["stats"]["assists"], 0)
        self.assertEqual(match["goals"][0]["player_id"], "2752619")
        self.assertEqual(match["yellowcards"][0]["player_id"], "2848210")
        self.assertEqual(match["var_events"][0]["event_type"], "Penalty confirmed")
        self.assertEqual(match["feed_updated_ts"], 1765254151)


class StatPalProviderTests(TestCase):
    def test_normalize_daily_matches_outputs_fixture_cache_shape(self):
        fixtures = normalize_daily_matches(DAILY_PAYLOAD, target_date=date(2026, 8, 7))

        self.assertEqual(len(fixtures), 1)
        fixture = fixtures[0]
        self.assertEqual(fixture["match_id"], "statpal:sp-100")
        self.assertEqual(fixture["provider_match_id"], "sp-100")
        self.assertEqual(fixture["fixture"], "Norway vs England")
        self.assertEqual(fixture["league"], "World Cup")
        self.assertEqual(fixture["country"], "World")
        self.assertEqual(fixture["source"], "statpal")

    def test_sync_statpal_daily_upserts_fixture_cache(self):
        class DummyProvider:
            def fixtures_for_date(self, target_date):
                return normalize_daily_matches(DAILY_PAYLOAD, target_date=target_date)

        with patch("betpreneur.modules.catalog.services.statpal_normalize.StatPalDailyMatchProvider", return_value=DummyProvider()):
            result = FixtureSearchService().sync_statpal_daily(target_date=date(2026, 8, 7))

        self.assertEqual(result, {"synced": 1, "errors": []})
        cached = FixtureCache.objects.get(match_id="statpal:sp-100")
        self.assertEqual(cached.fixture, "Norway vs England")
        self.assertEqual(cached.source, "statpal")
        self.assertEqual(cached.api_payload["id"], "sp-100")

    def test_sync_statpal_daily_disabled_is_silent(self):
        class DisabledProvider:
            def fixtures_for_date(self, target_date):
                raise StatPalConfigurationError("disabled")

        with patch("betpreneur.modules.catalog.services.statpal_normalize.StatPalDailyMatchProvider", return_value=DisabledProvider()):
            result = FixtureSearchService().sync_statpal_daily(target_date=date(2026, 8, 7))

        self.assertEqual(result, {"synced": 0, "errors": []})

    def test_sync_statpal_horizon_uses_normalized_league_ids(self):
        class DummyClient:
            def __init__(self):
                self.match_calls = []

            def soccer_leagues(self):
                return {
                    "leagues": {
                        "sport": "soccer",
                        "league": [
                            {
                                "id": "3800",
                                "country": "africa",
                                "name": "CAF African Nations Championship",
                                "season": "2025",
                                "date_start": "02.08.2025",
                                "date_end": "30.08.2025",
                            }
                        ],
                    }
                }

            def soccer_league_matches(self, league_id):
                self.match_calls.append(str(league_id))
                return DAILY_PAYLOAD

        client = DummyClient()
        with patch("betpreneur.modules.catalog.services.provider_client.StatPalClient", return_value=client):
            result = FixtureSearchService().sync_statpal_horizon(start_date=date(2026, 8, 7), days=0)

        self.assertEqual(client.match_calls, ["3800"])
        self.assertEqual(result["leagues"], 1)

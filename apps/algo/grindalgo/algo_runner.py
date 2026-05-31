# ================================================================
# GRIND ALGO — API-Football Runner
# ================================================================
#
# FLOW:
#   1. API-Football -> fetch fixtures for target date
#   2. API-Football -> fetch predictions and pre-match odds per fixture
#   3. Score fixtures -> select picks -> persist into Django DB
#   4. Optional: write Google Sheets / PDF report when KEY_FILE is configured
# ================================================================

import os
import time
import json
import logging
import requests
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether
)
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)
WAT = timezone(timedelta(hours=1))

# ── CONFIG ────────────────────────────────────────────────────────
APS_KEY    = os.environ.get("APS_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

APS_BASE  = "https://v3.football.api-sports.io"

_KEY_DEFAULT    = os.path.join(os.getcwd(), "grind_key.json")
KEY_FILE        = os.environ.get("KEY_FILE",        _KEY_DEFAULT)
SHEET_NAME      = os.environ.get("SHEET_NAME",      "GrindAlgo Tracker")
DRIVE_FOLDER    = os.environ.get("DRIVE_FOLDER",    "GrindAlgo Reports")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT",  "")

FLAT_STAKE_PCT = 0.10  # Allocates exactly 10% of bankroll to ALL picks


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

# ── API-FOOTBALL TRACKED LEAGUES ─────────────────────────────────
APS_TRACKED_LEAGUES = {
    7:   "Asian Cup",
    848: "UEFA Europa Conference League",
    10:  "Club Friendlies",
    21:  "International Friendlies",
    71:  "Serie B (Brazil)",
    115: "Svenska Cupen (Sweden)",
    241: "Copa Colombia",
    253: "MLS",
    486: "Belarus Cup",
    587: "World Cup U17",
    658: "Latvia Cup",
    742: "Copa Paulista (Brazil)",
    822: "Morocco Cup",
    840: "Taca Revelacao U23 (Portugal)",
    853: "Supercopa de Ecuador",
    914: "Tournoi Maurice Revello",
    950: "World Cup U17 Women",
    973: "CAF Cup of Nations U17",
    975: "Serie C Relegation Play-offs (Italy)",
    989: "Oberliga Relegation Round (Germany)",
    999: "Serie D Championship Round (Italy)",
    1000: "Segunda Division RFEF Play-offs",
    1011: "Second Amateur Division Play-offs (Belgium)",
    1100: "Brasiliense U20 (Brazil)",
    1148: "Maranhense 2 (Brazil)",
    1158: "Copa Gaucha (Brazil)",
    1229: "Liga Women (Peru)",
    141: "Segunda Division",
    79:  "2. Bundesliga",
    203: "Super Lig (Turkey)",
    144: "Pro League (Belgium)",
    128: "Argentine Primera",
    119: "Danish Superliga",
    113: "Allsvenskan (Sweden)",
    262: "Liga MX",
    39:  "Premier League",
    140: "La Liga",
    78:  "Bundesliga",
    135: "Serie A",
    61:  "Ligue 1",
    3:   "UEFA Europa League",
    2:   "UEFA Champions League",
}

MARKET_MEANINGS = {
    "Home Win":"Home team to win","Away Win":"Away team to win",
    "Draw":"Match ends in a draw",
    "Over 1.5":"2 or more total goals","Under 1.5":"1 or 0 total goals",
    "Over 2.5":"3 or more total goals","Under 2.5":"2 or fewer total goals",
    "Under 3.5":"3 or fewer total goals","Over 3.5":"4 or more total goals",
    "GG / BTTS Yes":"Both teams to score","GG + Over 2.5":"Both score & 3+ goals",
    "DNB Home":"Home win (Draw = refund)","DNB Away":"Away win (Draw = refund)",
    "Home CS":"Home team keeps clean sheet","Away CS":"Away team keeps clean sheet",
    "AH Home +0.5":"Home win or draw (+0.5)","AH Away +0.5":"Away win or draw (+0.5)",
    "First to Score H":"Home team scores first","First to Score A":"Away team scores first",
}
EXCLUDED_MARKETS = {"DC: 1X", "DC: X2", "DC: 12"}

def market_meaning(market):
    if market.startswith("Corners Over "):
        line = market.rsplit(" ", 1)[-1]
        return f"Match to finish with more than {line} total corners"
    if market.startswith("Corners Under "):
        line = market.rsplit(" ", 1)[-1]
        return f"Match to finish with fewer than {line} total corners"
    return MARKET_MEANINGS.get(market, "")

# ── HELPERS ───────────────────────────────────────────────────────
def _to_wat(utc_str):
    if not utc_str: return ""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z","+00:00"))
        return dt.astimezone(WAT).strftime("%H:%M WAT")
    except Exception:
        return utc_str

def normalize(name):
    return ''.join(c for c in unicodedata.normalize('NFD', str(name or ""))
                   if unicodedata.category(c) != 'Mn').lower()

def fuzzy(a, b, n=4):
    a, b = normalize(a), normalize(b)
    return a[:n] in b or b[:n] in a

def api_sports_key():
    return os.environ.get("APS_KEY") or os.environ.get("API_SPORTS_KEY") or APS_KEY

def aps_get(path, params=None, timeout=20):
    aps_key = api_sports_key()
    if not aps_key:
        raise RuntimeError("APS_KEY is not configured")
    headers = {"x-apisports-key": aps_key}
    response = requests.get(f"{APS_BASE}{path}", headers=headers, params=params or {}, timeout=timeout)
    if response.status_code != 200:
        log.warning("API-Football %s failed: %s %s", path, response.status_code, response.text[:300])
        return []
    payload = response.json()
    errors = payload.get("errors")
    if errors:
        log.warning("API-Football %s errors: %s", path, errors)
    return payload.get("response", [])

# ── GOOGLE SERVICES ───────────────────────────────────────────────
def get_google_services():
    if not KEY_FILE or not os.path.exists(KEY_FILE):
        log.info("Google export disabled; KEY_FILE is not configured")
        return None, None, None
    import gspread
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive",
              "https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_file(KEY_FILE, scopes=scopes)
    gc     = gspread.authorize(creds)
    drive  = build("drive","v3",credentials=creds)
    sheets = gc.open(SHEET_NAME)
    log.info("Google services OK")
    return gc, drive, sheets

def get_bankroll(sheets):
    if sheets is None:
        return 10000.0
    try:
        ws   = sheets.worksheet("Bankroll")
        rows = ws.get_all_values()
        if len(rows) <= 1: return 10000.0
        last = rows[-1]
        return float(last[3]) if len(last)>3 and last[3] else 10000.0
    except Exception as e:
        log.warning(f"Bankroll read: {e}")
        return 10000.0

# ── API-FOOTBALL FIXTURE FETCHER ─────────────────────────────────
def fetch_aps_fixtures(target_date):
    fixtures = []
    seen = set()
    aps_all = aps_get("/fixtures", {"date": target_date, "timezone": "Africa/Lagos"})
    for f in aps_all:
        league_id = f.get("league",{}).get("id")
        status    = f.get("fixture",{}).get("status",{}).get("short","")
        if status in ("FT","AET","PEN","CANC","ABD"): continue
        if league_id not in APS_TRACKED_LEAGUES: continue
        hname = f["teams"]["home"]["name"]
        aname = f["teams"]["away"]["name"]
        key   = normalize(f"{hname}{aname}")
        if key in seen:
            continue
        seen.add(key)
        fixtures.append({
            "fixture":  f"{hname} vs {aname}",
            "hname":    hname, "aname": aname,
            "home_logo": f.get("teams", {}).get("home", {}).get("logo", ""),
            "away_logo": f.get("teams", {}).get("away", {}).get("logo", ""),
            "hid":      f["teams"]["home"]["id"],
            "aid":      f["teams"]["away"]["id"],
            "league":   f["league"]["name"],
            "country":  f["league"].get("country", ""),
            "round":    f["league"].get("round", ""),
            "league_type": f["league"].get("type", ""),
            "code":     str(league_id),
            "kickoff":  _to_wat(f["fixture"].get("date","")),
            "kickoff_utc": f["fixture"].get("date", ""),
            "match_id": f["fixture"]["id"],
            "source":   "aps",
            "aps_id":   f["fixture"]["id"],
            "date":     target_date,
            "season":   f["league"].get("season"),
        })
    log.info(f"API-Football tracked fixtures: {len(fixtures)}")
    return fixtures

# ── PHASE 3: PREDICTIONS FETCH ────────────────────────────────────
def fetch_prediction_data(fixture_id):
    try:
        response = aps_get("/predictions", {"fixture": fixture_id}, timeout=15)
        return response[0] if response else None
    except Exception as e:
        log.warning(f"Predictions {fixture_id}: {e}")
        return None

# ── TEAM FORM ─────────────────────────────────────────────────────
_form_cache = {}

def _default_form():
    return {"wins":3,"draws":2,"losses":3,"form":"",
            "avg_scored":1.4,"avg_conceded":1.2,
            "btts_count":4,"over25_count":3,"clean_sheets":2,
            "games":8,"scope":"overall","last_played":"","streak":0,"attack_str":0.5,"defence_str":0.5}

def _percent_to_ratio(value, default=0.5):
    try:
        return float(str(value).replace("%", "")) / 100
    except (TypeError, ValueError):
        return default

def _result_code_for_team(match, team_id):
    teams = match.get("teams", {}) or {}
    goals = match.get("goals", {}) or {}
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    home_goals = goals.get("home")
    away_goals = goals.get("away")
    if home_goals is None or away_goals is None:
        return None
    is_home = home.get("id") == team_id
    is_away = away.get("id") == team_id
    if not is_home and not is_away:
        return None
    scored = home_goals if is_home else away_goals
    conceded = away_goals if is_home else home_goals
    if scored > conceded:
        return "W"
    if scored < conceded:
        return "L"
    return "D"

def fetch_team_recent_form(team_id, lookback=8, venue=None):
    if not team_id:
        return None
    venue = venue if venue in {"home", "away"} else None
    cache_key = (team_id, lookback, venue)
    if cache_key in _form_cache:
        return _form_cache[cache_key]

    matches = []
    try:
        fetch_count = lookback if venue is None else max(lookback * 3, 20)
        matches = aps_get("/fixtures", {"team": team_id, "last": fetch_count}, timeout=15)
        time.sleep(0.15)
    except Exception as exc:
        log.warning("Team recent form %s: %s", team_id, exc)

    samples = []
    for match in matches or []:
        status = ((match.get("fixture") or {}).get("status") or {}).get("short")
        if status not in {"FT", "AET", "PEN"}:
            continue
        teams = match.get("teams", {}) or {}
        goals = match.get("goals", {}) or {}
        home = teams.get("home", {}) or {}
        away = teams.get("away", {}) or {}
        home_goals = goals.get("home")
        away_goals = goals.get("away")
        if home_goals is None or away_goals is None:
            continue
        is_home = home.get("id") == team_id
        is_away = away.get("id") == team_id
        if not is_home and not is_away:
            continue
        if venue == "home" and not is_home:
            continue
        if venue == "away" and not is_away:
            continue
        scored = home_goals if is_home else away_goals
        conceded = away_goals if is_home else home_goals
        result = _result_code_for_team(match, team_id)
        if result is None:
            continue
        samples.append({
            "scored": scored,
            "conceded": conceded,
            "result": result,
            "date": ((match.get("fixture") or {}).get("date") or ""),
        })
        if len(samples) >= lookback:
            break

    if not samples:
        return None

    games = len(samples)
    form_str = "".join(item["result"] for item in samples)
    streak = 0
    for result in reversed(form_str):
        if result == form_str[-1]:
            streak += 1
        else:
            break
    if form_str[-1] == "L":
        streak = -streak

    form = {
        "wins": sum(1 for item in samples if item["result"] == "W"),
        "draws": sum(1 for item in samples if item["result"] == "D"),
        "losses": sum(1 for item in samples if item["result"] == "L"),
        "form": form_str,
        "avg_scored": round(sum(item["scored"] for item in samples) / games, 2),
        "avg_conceded": round(sum(item["conceded"] for item in samples) / games, 2),
        "btts_count": sum(1 for item in samples if item["scored"] > 0 and item["conceded"] > 0),
        "over25_count": sum(1 for item in samples if item["scored"] + item["conceded"] > 2),
        "clean_sheets": sum(1 for item in samples if item["conceded"] == 0),
        "games": games,
        "scope": venue or "overall",
        "last_played": samples[0].get("date", ""),
        "streak": streak,
        "attack_str": 0.5,
        "defence_str": 0.5,
    }
    _form_cache[cache_key] = form
    return form

# ── MAP API-FOOTBALL PREDICTIONS -> FORM METRICS ─────────────────
def map_aps_to_form(team_pred, comp_side):
    if not team_pred: return _default_form()
    last_5   = team_pred.get("last_5",{}) or {}
    form_str = "".join(ch for ch in str(last_5.get("form") or "") if ch in {"W", "D", "L"})[:5]
    fallback = _default_form()
    games    = len(form_str) or 5
    wins     = form_str.count("W") if form_str else fallback["wins"]
    draws    = form_str.count("D") if form_str else fallback["draws"]
    losses   = form_str.count("L") if form_str else fallback["losses"]
    streak   = 0
    if form_str:
        for r_ in reversed(form_str):
            if r_==form_str[-1]: streak+=1
            else: break
        if form_str[-1]=="L": streak=-streak

    goals_data   = last_5.get("goals",{}) or {}
    avg_scored   = float((goals_data.get("for",{}) or {}).get("average") or 1.4)
    avg_conceded = float((goals_data.get("against",{}) or {}).get("average") or 1.2)

    total_avg    = avg_scored + avg_conceded
    btts_count   = round(games * min(0.9, max(0.1,
        min(avg_scored,1.0) * min(avg_conceded,1.0))))
    over25_count = round(games * min(0.9, max(0.0, (total_avg-1.5)/3.0)))
    # Clean sheets — properly derived from avg_conceded
    cs_rate      = max(0.0, min(0.75, 1.0 - avg_conceded/2.5))
    clean_sheets = round(games * cs_rate)

    # attacking_strength and defensive_strength from comparison block
    attack_str = 0.5; defence_str = 0.5
    if comp_side:
        try:
            attack_str  = float(str(comp_side.get("att","50%")).replace("%",""))/100
            defence_str = float(str(comp_side.get("def","50%")).replace("%",""))/100
        except Exception: pass

    return {"wins":wins,"draws":draws,"losses":losses,"form":form_str,
            "avg_scored":avg_scored,"avg_conceded":avg_conceded,
            "btts_count":btts_count,"over25_count":over25_count,
            "clean_sheets":clean_sheets,"games":games,"scope":"overall","last_played":"","streak":streak,
            "attack_str":attack_str,"defence_str":defence_str}

def parse_aps_h2h(h2h_list, hname):
    if not h2h_list:
        return {"games":0,"t1w":0,"t2w":0,"draws":0,"o25":0,"u25":0,"u35":0,"btts":0,"avg_goals":0.0}
    games=t1w=t2w=draws=o25=u25=u35=btts=goals_total=0
    for m in h2h_list:
        try:
            hg = m.get("goals",{}).get("home")
            ag = m.get("goals",{}).get("away")
            mh = normalize(m.get("teams",{}).get("home",{}).get("name",""))
            if hg is None or ag is None: continue
            games+=1
            total = hg + ag
            goals_total += total
            if total>2: o25+=1
            if total<3: u25+=1
            if total<4: u35+=1
            if hg>0 and ag>0: btts+=1
            if hg == ag:
                draws += 1
            elif fuzzy(hname,mh):
                if hg > ag:
                    t1w += 1
                else:
                    t2w += 1
            elif ag > hg:
                t1w += 1
            else:
                t2w += 1
        except Exception: continue
    if not games:
        return {"games":0,"t1w":0,"t2w":0,"draws":0,"o25":0,"u25":0,"u35":0,"btts":0,"avg_goals":0.0}
    return {
        "games":games,
        "t1w":t1w,
        "t2w":t2w,
        "draws":draws,
        "o25":o25,
        "u25":u25,
        "u35":u35,
        "btts":btts,
        "avg_goals":round(goals_total/games, 2),
    }

# ── FIXTURE CONTEXT / TEAM NEWS / LEAGUE STRENGTH ────────────────
LEAGUE_STRENGTH = {
    "39": 1.16,   # Premier League
    "140": 1.14,  # La Liga
    "78": 1.13,   # Bundesliga
    "135": 1.12,  # Serie A
    "61": 1.10,   # Ligue 1
    "2": 1.18,    # UEFA Champions League
    "3": 1.12,    # UEFA Europa League
    "848": 1.06,  # UEFA Europa Conference League
    "71": 0.94,   # Brazil Serie B
    "253": 0.96,  # MLS
    "10": 0.82,   # Friendlies
    "21": 0.82,   # International Friendlies
    "7": 0.98,    # Asian Cup
    "115": 0.88,  # Svenska Cupen
    "241": 0.88,  # Copa Colombia
    "486": 0.82,  # Belarus Cup
    "587": 0.78,  # World Cup U17
    "658": 0.82,  # Latvia Cup
    "742": 0.84,  # Copa Paulista
    "822": 0.82,  # Morocco Cup
    "840": 0.76,  # Portugal U23 cup
    "853": 0.84,  # Ecuador Super Cup
    "914": 0.76,  # Tournoi Maurice Revello
    "950": 0.76,  # World Cup U17 Women
    "973": 0.76,  # CAF U17
    "975": 0.80,  # Serie C relegation playoffs
    "989": 0.76,  # Oberliga relegation round
    "999": 0.78,  # Serie D championship round
    "1000": 0.80, # Segunda RFEF playoffs
    "1011": 0.76, # Belgium amateur playoffs
    "1100": 0.74, # Brasiliense U20
    "1148": 0.74, # Maranhense 2
    "1158": 0.80, # Copa Gaucha
    "1229": 0.76, # Peru Women
}

def league_strength_factor(fx):
    name = normalize(fx.get("league", ""))
    if "friendly" in name:
        return 0.82
    if "champions league" in name:
        return 1.18
    if "europa league" in name:
        return 1.12
    return LEAGUE_STRENGTH.get(str(fx.get("code") or ""), 1.0)

def apply_league_strength(form, strength):
    form = dict(form or {})
    # Stronger leagues make production more credible; weaker/friendly contexts make it more volatile.
    form["avg_scored"] = round(float(form.get("avg_scored") or 0) * strength, 2)
    form["avg_conceded"] = round(float(form.get("avg_conceded") or 0) / max(strength, 0.75), 2)
    form["league_strength"] = round(strength, 2)
    return form

def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

def _rest_days(last_played, kickoff):
    last_dt = _parse_dt(last_played)
    kick_dt = _parse_dt(kickoff)
    if not last_dt or not kick_dt:
        return None
    return max(0, (kick_dt - last_dt).days)

_standings_cache = {}
def fetch_league_standings(league_id, season):
    if not league_id or not season:
        return {}
    key = (str(league_id), str(season))
    if key in _standings_cache:
        return _standings_cache[key]
    standings = {}
    try:
        response = aps_get("/standings", {"league": league_id, "season": season}, timeout=15)
        time.sleep(0.15)
        rows = (((response[0] or {}).get("league") or {}).get("standings") or [[]])[0]
        total = len(rows)
        for row in rows:
            team = row.get("team") or {}
            team_id = team.get("id")
            if not team_id:
                continue
            standings[team_id] = {
                "rank": row.get("rank"),
                "points": row.get("points"),
                "total": total,
            }
    except Exception as exc:
        log.warning("Standings %s/%s: %s", league_id, season, exc)
    _standings_cache[key] = standings
    return standings

_injuries_cache = {}
_lineups_cache = {}

def fetch_fixture_lineups(fixture_id, home_id=None, away_id=None):
    if not fixture_id:
        return {"available": False, "home": {}, "away": {}}
    if fixture_id in _lineups_cache:
        return _lineups_cache[fixture_id]
    payload = {"available": False, "home": {}, "away": {}}
    try:
        lineups = aps_get("/fixtures/lineups", {"fixture": fixture_id}, timeout=15)
        time.sleep(0.15)
    except Exception as exc:
        log.warning("Lineups %s: %s", fixture_id, exc)
        lineups = []
    if lineups:
        payload["available"] = True
    for row in lineups or []:
        team_id = ((row.get("team") or {}).get("id"))
        bucket = "home" if team_id == home_id else "away" if team_id == away_id else None
        if not bucket:
            continue
        payload[bucket] = {
            "formation": row.get("formation", ""),
            "starter_count": len(row.get("startXI") or []),
            "substitute_count": len(row.get("substitutes") or []),
        }
    _lineups_cache[fixture_id] = payload
    return payload

def fetch_fixture_team_news(fixture_id, home_id=None, away_id=None):
    if not fixture_id:
        return {"available": False, "injuries_available": False, "lineups_available": False, "home": {"injuries": 0}, "away": {"injuries": 0}, "flags": []}
    if fixture_id in _injuries_cache:
        return _injuries_cache[fixture_id]
    news = {"available": False, "injuries_available": False, "lineups_available": False, "home": {"injuries": 0}, "away": {"injuries": 0}, "flags": []}
    try:
        injuries = aps_get("/injuries", {"fixture": fixture_id}, timeout=15)
        time.sleep(0.15)
    except Exception as exc:
        log.warning("Injuries %s: %s", fixture_id, exc)
        injuries = []
    if injuries:
        news["available"] = True
        news["injuries_available"] = True
    for item in injuries or []:
        team_id = ((item.get("team") or {}).get("id"))
        bucket = "home" if team_id == home_id else "away" if team_id == away_id else None
        if not bucket:
            continue
        news[bucket]["injuries"] += 1
    total_absences = news["home"]["injuries"] + news["away"]["injuries"]
    if total_absences >= 5:
        news["flags"].append("team_news_heavy_absences")
    elif total_absences >= 2:
        news["flags"].append("team_news_absences")
    lineups = fetch_fixture_lineups(fixture_id, home_id, away_id)
    news["lineups_available"] = bool(lineups.get("available"))
    news["available"] = news["available"] or news["lineups_available"]
    news["home"].update(lineups.get("home") or {})
    news["away"].update(lineups.get("away") or {})
    if not news["lineups_available"]:
        news["flags"].append("lineups_unavailable")
    _injuries_cache[fixture_id] = news
    return news

def _is_knockout_round(round_name):
    round_name = normalize(round_name)
    knockout_terms = (
        "knockout",
        "playoff",
        "play-off",
        "final",
        "semi",
        "quarter",
        "round of",
        "last 16",
        "1/8",
        "1/4",
        "1/2",
    )
    return any(term in round_name for term in knockout_terms)

def build_fixture_context(fx, home_form=None, away_form=None):
    league = normalize(fx.get("league", ""))
    round_name = normalize(fx.get("round", ""))
    league_type = normalize(fx.get("league_type", ""))
    flags = []
    knockout_round = _is_knockout_round(round_name)
    cup_terms = ("cup", "copa", "coppa", "taca", "supercopa")
    if "cup" in league_type or any(term in league for term in cup_terms) or knockout_round:
        flags.append("cup_or_knockout")
    if "relegation" in league or "relegation" in round_name:
        flags.append("relegation_playoff")
    if "u17" in league or "u20" in league or "u23" in league or "revelacao" in league or "revello" in league:
        flags.append("youth_competition")
    if "women" in league or "liga women" in league:
        flags.append("women_competition")
    if "amateur" in league or "oberliga" in league or "serie d" in league or "maranhense" in league:
        flags.append("lower_division")
    if "friendly" in league:
        flags.append("friendly")
    if "uefa" in league or "champions league" in league or "europa" in league:
        flags.append("continental")
    if "continental" in flags and knockout_round:
        flags.append("continental_knockout")
    home_rest = _rest_days((home_form or {}).get("last_played"), fx.get("kickoff_utc"))
    away_rest = _rest_days((away_form or {}).get("last_played"), fx.get("kickoff_utc"))
    if home_rest is not None and home_rest < 4:
        flags.append("home_short_rest")
    if away_rest is not None and away_rest < 4:
        flags.append("away_short_rest")

    standings = fetch_league_standings(fx.get("code"), fx.get("season"))
    home_pos = standings.get(fx.get("hid"), {})
    away_pos = standings.get(fx.get("aid"), {})
    for label, pos in (("home", home_pos), ("away", away_pos)):
        rank = int(pos.get("rank") or 0)
        total = int(pos.get("total") or 0)
        if rank and total:
            if rank <= 4:
                flags.append(f"{label}_top_table")
            if rank >= max(total - 3, 1):
                flags.append(f"{label}_relegation_zone")
    if home_pos and away_pos:
        total = int(home_pos.get("total") or away_pos.get("total") or 0)
        hr = int(home_pos.get("rank") or 0)
        ar = int(away_pos.get("rank") or 0)
        if total and total * 0.35 < hr < total * 0.75 and total * 0.35 < ar < total * 0.75:
            flags.append("mid_table_context")

    strength = league_strength_factor(fx)
    if strength < 0.9:
        flags.append("lower_strength_or_friendly_league")
    elif strength > 1.1:
        flags.append("elite_strength_league")
    return {
        "flags": sorted(set(flags)),
        "league_strength": round(strength, 2),
        "h2h": {},
        "home_rest_days": home_rest,
        "away_rest_days": away_rest,
        "home_standing": home_pos,
        "away_standing": away_pos,
    }

def _stat_value(statistics, stat_type):
    stat_type = normalize(stat_type)
    for item in statistics or []:
        if normalize(item.get("type", "")) == stat_type:
            value = item.get("value")
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0

_corner_profile_cache = {}
_fixture_stats_cache = {}

def fetch_fixture_statistics(fixture_id):
    if fixture_id in _fixture_stats_cache:
        return _fixture_stats_cache[fixture_id]
    try:
        stats = aps_get("/fixtures/statistics", {"fixture": fixture_id}, timeout=15)
        time.sleep(0.15)
    except Exception as exc:
        log.warning("Fixture statistics %s: %s", fixture_id, exc)
        stats = []
    _fixture_stats_cache[fixture_id] = stats
    return stats

def _team_corners_from_stats(stats, team_id):
    for row in stats or []:
        if ((row.get("team") or {}).get("id")) == team_id:
            return _stat_value(row.get("statistics", []), "Corner Kicks")
    return None

def fetch_team_corner_profile(team_id, lookback=8):
    if team_id in _corner_profile_cache:
        return _corner_profile_cache[team_id]

    samples = []
    try:
        fixtures = aps_get("/fixtures", {"team": team_id, "last": lookback}, timeout=15)
        time.sleep(0.15)
    except Exception as exc:
        log.warning("Team corner fixtures %s: %s", team_id, exc)
        fixtures = []

    for fixture in fixtures:
        status = ((fixture.get("fixture") or {}).get("status") or {}).get("short")
        if status not in {"FT", "AET", "PEN"}:
            continue
        fixture_id = (fixture.get("fixture") or {}).get("id")
        if not fixture_id:
            continue
        stats = fetch_fixture_statistics(fixture_id)
        own = _team_corners_from_stats(stats, team_id)
        if own is None:
            continue
        total = 0.0
        for row in stats or []:
            total += _stat_value(row.get("statistics", []), "Corner Kicks")
        if total <= 0:
            continue
        samples.append({"own": own, "against": max(total - own, 0), "total": total})

    if samples:
        profile = {
            "games": len(samples),
            "avg_for": round(sum(item["own"] for item in samples) / len(samples), 2),
            "avg_against": round(sum(item["against"] for item in samples) / len(samples), 2),
            "avg_total": round(sum(item["total"] for item in samples) / len(samples), 2),
        }
    else:
        profile = {"games": 0, "avg_for": 4.5, "avg_against": 4.5, "avg_total": 9.0}

    _corner_profile_cache[team_id] = profile
    return profile

def build_corner_profile(fx):
    home = fetch_team_corner_profile(fx.get("hid"))
    away = fetch_team_corner_profile(fx.get("aid"))
    games = min(home.get("games", 0), away.get("games", 0))
    expected_total = (
        home.get("avg_for", 4.5)
        + away.get("avg_for", 4.5)
        + home.get("avg_against", 4.5)
        + away.get("avg_against", 4.5)
    ) / 2
    return {
        "games": games,
        "home": home,
        "away": away,
        "expected_total": round(expected_total, 2),
    }

# ── 19-PARAMETER CONFIDENCE SCORER ───────────────────────────────
CONF_DEFLATOR = 0.84
W = {"f1":8,"f2":10,"f3":12,"f4":8,"f5":6,"f6":8,"f7":12,"f8":5,"f9":8,
     "f10":8,"f11":6,"f12":6,"f13":6,"f14":10,"f15":9,"f16":6,"f17":7,
     "f18":8,"f19":7}
MAX_W = sum(W.values())

def score_corner_markets(real_odds, corner_profile=None):
    if not algo_enable_corners():
        return {}

    corner_profile = corner_profile or {}
    expected_total = float(corner_profile.get("expected_total") or 9.0)
    games = int(corner_profile.get("games") or 0)
    if games < algo_corner_min_profile_games():
        return {}

    scores = {}

    for market in real_odds:
        if not market.startswith("Corners Over ") and not market.startswith("Corners Under "):
            continue
        try:
            line = float(market.rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            continue
        odd = real_odds.get(market)
        if not corner_market_allowed(market, odd, corner_profile):
            continue

        diff = expected_total - line
        if market.startswith("Corners Over "):
            confidence = 50 + (diff * 8)
        else:
            confidence = 50 + (-diff * 8)

        if games < 4:
            confidence = confidence * 0.82 + 50 * 0.18
        elif games < 7:
            confidence = confidence * 0.9 + 50 * 0.1

        scores[market] = round(max(25, min(82, confidence)))

    return scores

def score_fixture(hf, af, h2h, real_odds, api_preds=None, corner_profile=None, fixture_context=None):
    def sf(v,strong,mod,w):
        return w if v>=strong else round(w*0.67) if v>=mod else round(w*0.33)

    diff = hf["avg_scored"] - af["avg_scored"]
    fixture_context = fixture_context or {}
    context_flags = set(fixture_context.get("flags") or [])
    g    = max(h2h.get("games",0),0)
    h2h_games = max(g, 1)
    h2w  = h2h.get("t1w",0)/h2h_games
    h2aw = h2h.get("t2w",0)/h2h_games
    h2d  = h2h.get("draws",0)/h2h_games
    o25r = h2h.get("o25",0)/h2h_games
    h2h_available = g >= 2
    knockout_mode = bool(context_flags & {"continental_knockout", "cup_or_knockout"})

    # attack_str/defence_str: 0.0-1.0 (0.5 = average)
    h_atk = hf.get("attack_str",0.5)
    a_atk = af.get("attack_str",0.5)
    h_def = hf.get("defence_str",0.5)
    a_def = af.get("defence_str",0.5)

    # F3: map attack_str to SOT-like value (4.0 = average, range 1-7)
    h_sot = 4.0 + (h_atk-0.5)*6
    a_sot = 4.0 + (a_atk-0.5)*6

    # F10/F15: key player gap = attack vs opponent defence differential
    kap_h = h_atk - a_def   # positive = home attack > away defence
    kap_a = a_atk - h_def

    hfs = (sf(hf["wins"],6,4,W["f1"])+sf(hf["avg_scored"],2.0,1.3,W["f2"])+
           sf(h_sot,5.5,4.0,W["f3"])+sf(hf["over25_count"]/max(hf["games"],1),0.6,0.4,W["f4"])+
           sf(h_atk,0.6,0.45,W["f5"])+sf(hf["avg_scored"]/max(hf["avg_scored"],0.1),1.3,1.0,W["f6"])+
           sf(1-a_def,0.5,0.4,W["f7"])+sf(hf["wins"],3,2,W["f8"])+
           sf(hf["wins"],3,2,W["f9"])+sf(kap_h,0.1,0.0,W["f10"])+
           sf(diff,0.8,0.2,W["f11"])+sf(o25r,0.6,0.4,W["f12"])+
           sf(2.7,2.7,2.3,W["f13"])+sf(h2w,0.6,0.4,W["f14"])+
           sf(kap_h,0.1,0.0,W["f15"])+sf(0,2,0,W["f16"])+
           sf(8,3,2,W["f17"])+sf(h_atk-0.5,0.1,0.0,W["f18"])+
           sf(hf.get("streak",0),3,1,W["f19"]))
    hc = round(min(95,max(0,(hfs/MAX_W)*100*CONF_DEFLATOR)))

    afs = (sf(af["wins"],6,4,W["f1"])+sf(af["avg_scored"],2.0,1.3,W["f2"])+
           sf(a_sot,5.5,4.0,W["f3"])+sf(af["over25_count"]/max(af["games"],1),0.6,0.4,W["f4"])+
           sf(a_atk,0.6,0.45,W["f5"])+sf(af["avg_scored"]/max(af["avg_scored"],0.1),1.3,1.0,W["f6"])+
           sf(1-h_def,0.5,0.4,W["f7"])+sf(af["wins"],3,2,W["f8"])+
           sf(af["wins"],3,2,W["f9"])+sf(kap_a,0.1,0.0,W["f10"])+
           sf(-diff,0.8,0.2,W["f11"])+sf(o25r,0.6,0.4,W["f12"])+
           sf(2.7,2.7,2.3,W["f13"])+sf(h2aw,0.6,0.4,W["f14"])+
           sf(kap_a,0.1,0.0,W["f15"])+sf(0,2,0,W["f16"])+
           sf(8,3,2,W["f17"])+sf(a_atk-0.5,0.1,0.0,W["f18"])+
           sf(af.get("streak",0),3,1,W["f19"]))
    ac = round(min(95,max(0,(afs/MAX_W)*100*CONF_DEFLATOR)))

    if knockout_mode:
        # Knockout ties are usually less explained by domestic form; pull result strength toward neutral
        # and let H2H/market/API signals carry more of the load.
        hc = round(hc * 0.72 + 50 * 0.28)
        ac = round(ac * 0.72 + 50 * 0.28)
        if h2h_available:
            hc = round(hc * 0.70 + (h2w * 100) * 0.30)
            ac = round(ac * 0.70 + (h2aw * 100) * 0.30)

    # Blend API-Football ML win percent (30% weight)
    if api_preds:
        try:
            pct   = api_preds.get("predictions",{}).get("percent",{})
            api_hc = float(str(pct.get("home","0")).replace("%","") or 0)
            api_ac = float(str(pct.get("away","0")).replace("%","") or 0)
            if api_hc>0 or api_ac>0:
                hc = round(hc*0.70 + api_hc*0.30)
                ac = round(ac*0.70 + api_ac*0.30)
        except Exception: pass

    # ── GOALS MARKETS — Poisson-grounded formula ─────────────────
    # The expected total is the single most important input.
    # exp_total = home avg scored + away avg scored (the actual matchup total)
    # NOT avg_scored + avg_conceded (that double-counts defence)
    exp_total = hf["avg_scored"] + af["avg_scored"]

    # Poisson P(X>=3) and P(X>=2) given expected total goals
    import math as _math
    def _pp(lam, k):
        return (lam**k * _math.exp(-lam)) / _math.factorial(k)

    poisson_o25 = round((1 - _pp(exp_total,0) - _pp(exp_total,1) - _pp(exp_total,2)) * 100)
    poisson_o15 = round((1 - _pp(exp_total,0) - _pp(exp_total,1)) * 100)

    # Historical over25 rate from each team's recent games
    h_o25_rate = hf["over25_count"] / max(hf["games"], 1)
    a_o25_rate = af["over25_count"] / max(af["games"], 1)
    hist_o25   = (h_o25_rate + a_o25_rate) / 2 * 100

    # Blend: 60% Poisson math + 40% observed history
    o25_raw = round(poisson_o25 * 0.60 + hist_o25 * 0.40)

    # HARD CAPS based on expected total goals — prevents Over 2.5 being
    # recommended for low-scoring matchups like Rayo vs Elche (exp=2.0)
    if exp_total < 1.8:  o25_raw = min(o25_raw, 28)   # Very low scoring — almost never
    elif exp_total < 2.0: o25_raw = min(o25_raw, 36)   # Low scoring — unlikely
    elif exp_total < 2.3: o25_raw = min(o25_raw, 50)   # Below average — moderate at best
    elif exp_total < 2.5: o25_raw = min(o25_raw, 62)   # Near threshold

    o25 = min(82, max(15, o25_raw))

    # Over 1.5 — same approach
    h_o15_rate = hf["btts_count"] / max(hf["games"], 1)
    a_o15_rate = af["btts_count"] / max(af["games"], 1)
    hist_o15   = (h_o15_rate + a_o15_rate) / 2 * 100
    o15_raw    = round(poisson_o15 * 0.60 + hist_o15 * 0.40)
    if exp_total < 1.2: o15_raw = min(o15_raw, 50)
    elif exp_total < 1.5: o15_raw = min(o15_raw, 65)
    o15 = min(92, max(30, o15_raw))

    # GG/BTTS — requires BOTH teams to score, so use min of each team's scoring rate
    # A team that scores 1.0/game only scores in ~63% of games (Poisson P(X>=1))
    h_score_prob = round((1 - _math.exp(-hf["avg_scored"])) * 100)
    a_score_prob = round((1 - _math.exp(-af["avg_scored"])) * 100)
    gg_poisson   = round(h_score_prob * a_score_prob / 100)  # P(both score)
    h_btts_rate  = hf["btts_count"] / max(hf["games"], 1) * 100
    a_btts_rate  = af["btts_count"] / max(af["games"], 1) * 100
    gg_hist      = (h_btts_rate + a_btts_rate) / 2
    gg_raw       = round(gg_poisson * 0.60 + gg_hist * 0.40)
    # Hard cap: if either team averages under 0.8 goals, BTTS is very unlikely
    if hf["avg_scored"] < 0.8 or af["avg_scored"] < 0.8:
        gg_raw = min(gg_raw, 38)
    elif hf["avg_scored"] < 1.0 or af["avg_scored"] < 1.0:
        gg_raw = min(gg_raw, 48)
    gg = min(95, max(10, round(gg_raw * CONF_DEFLATOR)))
    hcs  = min(80,round(hf["clean_sheets"]/max(hf["games"],1)*100))
    acs  = min(80,round(af["clean_sheets"]/max(af["games"],1)*100))
    h_draw_rate = hf.get("draws", 0) / max(hf.get("games", 0), 1)
    a_draw_rate = af.get("draws", 0) / max(af.get("games", 0), 1)
    recent_draw_conf = round(((h_draw_rate + a_draw_rate) / 2) * 100)
    if knockout_mode and h2h_available:
        recent_draw_conf = round(recent_draw_conf * 0.45 + (h2d * 100) * 0.55)
    residual_draw_conf = max(5, 100 - hc - ac)
    draw_conf = max(5, min(45, round(residual_draw_conf * 0.65 + recent_draw_conf * 0.35)))
    dc12 = min(82, max(5, 100 - draw_conf))

    def blend_conf(m,o):
        if not o: return m
        try: return round(min(95,(m/100*0.4+1/o*0.6)*100))
        except: return m

    if real_odds:
        hc  = blend_conf(hc,  real_odds.get("hw"))
        ac  = blend_conf(ac,  real_odds.get("aw"))
        o25 = blend_conf(o25, real_odds.get("o25"))
        o15 = blend_conf(o15, real_odds.get("o15"))
        gg  = blend_conf(gg,  real_odds.get("btts_yes"))

    ta = max(hf["avg_scored"]+af["avg_scored"],0.1)
    fts_h = min(83,round(hf["avg_scored"]/ta*100*1.12+6))
    fts_a = min(50,max(10,round(af["avg_scored"]/ta*100*0.70-8)))

    scores = {
        "Home Win":hc,"Away Win":ac,"Draw":draw_conf,
        "Over 1.5":o15,"Under 1.5":100-o15,
        "Over 2.5":o25,"Under 2.5":100-o25,
        "Under 3.5":min(90,100-round(o25*0.55)),
        "GG / BTTS Yes":gg,"GG + Over 2.5":round(gg*o25/100),
        "DNB Home":hc,"DNB Away":ac,
        "Home CS":hcs,"Away CS":acs,
        "AH Home +0.5":min(95,hc+draw_conf),
        "AH Away +0.5":min(95,ac+draw_conf),
        "First to Score H":fts_h,"First to Score A":fts_a,
    }
    fixture_context["goal_model"] = {
        "expected_total": round(exp_total, 2),
        "draw_confidence": draw_conf,
        "over15_margin": round(exp_total - 1.5, 2),
        "over25_margin": round(exp_total - 2.5, 2),
        "under35_margin": round(3.5 - exp_total, 2),
    }
    if exp_total < algo_over15_min_expected_goals():
        scores["Over 1.5"] = min(scores["Over 1.5"], 59)
    if exp_total < algo_over25_min_expected_goals():
        scores["Over 2.5"] = min(scores["Over 2.5"], 59)
    if exp_total > algo_under35_max_expected_goals():
        scores["Under 3.5"] = min(scores["Under 3.5"], 59)
    if knockout_mode:
        h2_u25 = h2h.get("u25", 0) / h2h_games * 100
        h2_u35 = h2h.get("u35", 0) / h2h_games * 100
        if h2h_available:
            scores["Under 2.5"] = round(scores["Under 2.5"] * 0.55 + h2_u25 * 0.45)
            scores["Under 3.5"] = round(scores["Under 3.5"] * 0.60 + h2_u35 * 0.40)
            scores["Over 2.5"] = 100 - scores["Under 2.5"]
        scores["Over 2.5"] = min(scores["Over 2.5"], 62)
        scores["Under 3.5"] = min(92, scores["Under 3.5"] + 4)
    scores.update(score_corner_markets(real_odds, corner_profile))
    return {market: value for market, value in scores.items() if market not in EXCLUDED_MARKETS}

# ── API-FOOTBALL ODDS FETCH ───────────────────────────────────────
_odds_cache = {}

def _decimal_odd(value):
    try:
        return float(str(value).strip())
    except Exception:
        return None

def _remember_odd(odds, key, value):
    odd = _decimal_odd(value)
    if not odd:
        return
    odds.setdefault("_samples", {}).setdefault(key, []).append(odd)
    if key not in odds or odd > odds[key]:
        odds[key] = odd

def _finalize_odds_meta(odds):
    samples = odds.pop("_samples", {})
    meta = {}
    for key, values in samples.items():
        values = [value for value in values if value]
        if not values:
            continue
        avg = sum(values) / len(values)
        best = max(values)
        worst = min(values)
        meta[key] = {
            "bookmaker_count": len(values),
            "best": round(best, 3),
            "worst": round(worst, 3),
            "average": round(avg, 3),
            "spread_pct": round(((best - worst) / avg) * 100, 1) if avg else 0.0,
            "best_vs_average_pct": round(((best - avg) / avg) * 100, 1) if avg else 0.0,
        }
    if meta:
        odds["_meta"] = meta
    return odds

def _parse_line(label, prefix):
    if not label.startswith(prefix):
        return None
    try:
        return float(label.replace(prefix, "", 1).strip())
    except (TypeError, ValueError):
        return None

def get_api_football_odds(fixture_id):
    if fixture_id in _odds_cache:
        return _odds_cache[fixture_id]

    odds = {}
    try:
        response = aps_get("/odds", {"fixture": fixture_id}, timeout=15)
        time.sleep(0.25)
    except Exception as exc:
        log.warning("API-Football odds %s: %s", fixture_id, exc)
        response = []

    for item in response:
        for bookmaker in item.get("bookmakers", []) or []:
            for bet in bookmaker.get("bets", []) or []:
                bet_id = bet.get("id")
                bet_name = normalize(bet.get("name", ""))
                for value in bet.get("values", []) or []:
                    label = normalize(value.get("value", ""))
                    odd = value.get("odd")

                    if bet_id == 45 or ("corner" in bet_name and ("over under" in bet_name or "over/under" in bet_name)):
                        over_line = _parse_line(label, "over ")
                        under_line = _parse_line(label, "under ")
                        if over_line is not None:
                            _remember_odd(odds, f"Corners Over {over_line:g}", odd)
                        elif under_line is not None:
                            _remember_odd(odds, f"Corners Under {under_line:g}", odd)
                    elif bet_id == 1 or bet_name in ("match winner", "fulltime result", "1x2"):
                        if label in ("home", "1"):
                            _remember_odd(odds, "hw", odd)
                        elif label in ("away", "2"):
                            _remember_odd(odds, "aw", odd)
                        elif label in ("draw", "x"):
                            _remember_odd(odds, "d", odd)
                    elif bet_id == 5 or (
                        ("goals over/under" in bet_name or bet_name == "goal line")
                        and "first half" not in bet_name
                        and "1st half" not in bet_name
                        and "second half" not in bet_name
                        and "2nd half" not in bet_name
                    ):
                        if "over 1.5" in label:
                            _remember_odd(odds, "o15", odd)
                        elif "under 1.5" in label:
                            _remember_odd(odds, "u15", odd)
                        elif "over 2.5" in label:
                            _remember_odd(odds, "o25", odd)
                        elif "under 2.5" in label:
                            _remember_odd(odds, "u25", odd)
                        elif "over 3.5" in label:
                            _remember_odd(odds, "o35", odd)
                        elif "under 3.5" in label:
                            _remember_odd(odds, "u35", odd)
                    elif bet_id == 8 or bet_name in ("both teams score", "both teams to score"):
                        if label == "yes":
                            _remember_odd(odds, "btts_yes", odd)
                        elif label == "no":
                            _remember_odd(odds, "btts_no", odd)
    odds = _finalize_odds_meta(odds)
    _odds_cache[fixture_id] = odds
    return odds

# ── PICK SELECTOR ─────────────────────────────────────────────────
PROVEN_MARKETS = {"First to Score H","Over 1.5","AH Home +0.5","Under 3.5","GG / BTTS Yes"}
MARKET_THRESHOLDS = {
    "Home Win":64,"Away Win":85,"Draw":68,"Over 1.5":58,"Under 1.5":68,
    "Over 2.5":80,"Under 2.5":65,"Under 3.5":60,"Over 3.5":68,
    "GG / BTTS Yes":72,"GG + Over 2.5":75,"No Goal":999,
    "DNB Home":64,"DNB Away":85,
    "Home CS":65,"Away CS":72,"AH Home +0.5":58,"AH Away +0.5":78,
    "First to Score H":55,"First to Score A":85,
}
MIN_ODDS=1.25; BANKER_MIN=80; VALUE_MIN=70; WILD_MIN=60
# Scale targets: aim for 10–15 picks on a busy fixture day
MAX_BANKERS=3; MAX_VALUE_GEMS=7; MAX_WILD_CARDS=10
TARGET_MIN=10; TARGET_MAX=15
ODDS_KEYS_MAP = {
    "Home Win":"hw","Away Win":"aw","Draw":"d","Over 1.5":"o15",
    "Under 1.5":"u15","Over 2.5":"o25","Under 2.5":"u25",
    "Under 3.5":"u35","Over 3.5":"o35","GG / BTTS Yes":"btts_yes",
}

def est_odds(c): return round(1/max(c/100,0.05)*1.05,2)

def algo_min_ev():
    return _env_float("ALGO_MIN_EV", 0.0)

def algo_min_market_sample():
    return _env_int("ALGO_MIN_MARKET_SAMPLE", 15)

def algo_market_loss_streak_block():
    return _env_int("ALGO_MARKET_LOSS_STREAK_BLOCK", 3)

def algo_market_recent_loss_block():
    return _env_int("ALGO_MARKET_RECENT_5_LOSS_BLOCK", 4)

def algo_over15_min_expected_goals():
    return _env_float("ALGO_OVER15_MIN_EXPECTED_GOALS", 1.65)

def algo_over25_min_expected_goals():
    return _env_float("ALGO_OVER25_MIN_EXPECTED_GOALS", 2.65)

def algo_under35_max_expected_goals():
    return _env_float("ALGO_UNDER35_MAX_EXPECTED_GOALS", 3.05)

def algo_high_draw_risk_confidence():
    return _env_int(
        "ALGO_HIGH_DRAW_RISK_CONFIDENCE",
        _env_int("ALGO_DC12_MAX_DRAW_CONFIDENCE", 30),
    )

def algo_min_daily_picks():
    return _env_int("ALGO_MIN_DAILY_PICKS", 6)

def algo_floor_confidence():
    return _env_int("ALGO_FLOOR_CONFIDENCE", 58)

def algo_floor_ev():
    return _env_float("ALGO_FLOOR_EV", -0.03)

def algo_max_daily_picks():
    return _env_int("ALGO_MAX_DAILY_PICKS", TARGET_MAX)

def require_real_odds():
    return _env_bool("ALGO_REQUIRE_REAL_ODDS", False)

def allow_estimated_picks():
    return _env_bool("ALGO_ALLOW_ESTIMATED_PICKS", False)

def algo_enable_corners():
    return _env_bool("ALGO_ENABLE_CORNERS", True)

def algo_corner_min_line():
    return _env_float("ALGO_CORNER_MIN_LINE", 7.5)

def algo_corner_max_odds():
    return _env_float("ALGO_CORNER_MAX_ODDS", 3.5)

def algo_corner_min_profile_games():
    return _env_int("ALGO_CORNER_MIN_PROFILE_GAMES", 6)

def corner_line(market):
    if not market.startswith("Corners "):
        return None
    try:
        return float(market.rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return None

def corner_market_allowed(market, odds, corner_profile=None):
    line = corner_line(market)
    if line is None:
        return True
    if not algo_enable_corners():
        return False
    if line < algo_corner_min_line():
        return False
    if odds is None or odds < MIN_ODDS or odds > algo_corner_max_odds():
        return False
    profile_games = int((corner_profile or {}).get("games") or 0)
    if profile_games < algo_corner_min_profile_games():
        return False
    return True

def load_performance_profile():
    raw = os.environ.get("ALGO_PERFORMANCE_PROFILE", "")
    if not raw:
        return {"markets": {}, "league_markets": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ALGO_PERFORMANCE_PROFILE is not valid JSON")
        return {"markets": {}, "league_markets": {}}
    return {
        "markets": data.get("markets", {}) or {},
        "league_markets": data.get("league_markets", {}) or {},
    }

def market_profile(profile, market, league=None):
    league_key = f"{league}::{market}" if league else ""
    return (
        profile.get("league_markets", {}).get(league_key)
        or profile.get("markets", {}).get(market)
        or {}
    )

def calibrate_confidence(raw_conf, profile_data):
    count = int(profile_data.get("count") or 0)
    if count < algo_min_market_sample():
        return raw_conf

    hit_rate = float(profile_data.get("hit_rate") or 0)
    roi_flat = float(profile_data.get("roi_flat") or 0)
    avg_confidence = float(profile_data.get("avg_confidence") or 0)
    adjustment = 0

    if roi_flat < -10:
        adjustment -= 8
    elif roi_flat < 0:
        adjustment -= 4
    elif roi_flat > 8 and hit_rate >= 58:
        adjustment += 3

    if hit_rate and hit_rate < max(45, raw_conf - 18):
        adjustment -= 5
    if hit_rate and avg_confidence:
        calibration_gap = hit_rate - avg_confidence
        if calibration_gap <= -15:
            adjustment -= 6
        elif calibration_gap <= -8:
            adjustment -= 3
        elif calibration_gap >= 10 and roi_flat > 0:
            adjustment += 2

    loss_streak = int(profile_data.get("loss_streak") or 0)
    recent_5_losses = int(profile_data.get("recent_5_losses") or 0)
    recent_count = int(profile_data.get("recent_count") or 0)
    recent_10_hit_rate = float(profile_data.get("recent_10_hit_rate") or 0)
    if loss_streak >= algo_market_loss_streak_block():
        adjustment -= 10
    elif loss_streak >= 2:
        adjustment -= 5
    if recent_5_losses >= algo_market_recent_loss_block():
        adjustment -= 8
    elif recent_5_losses >= 3:
        adjustment -= 4
    if recent_count >= 5 and recent_10_hit_rate and recent_10_hit_rate < 40:
        adjustment -= 4

    return max(1, min(95, round(raw_conf + adjustment)))

def candidate_risk_flags(raw_conf, conf, market, odds, odds_is_real, ev, profile_data, odds_meta=None, fixture_context=None, team_news=None):
    flags = []
    count = int(profile_data.get("count") or 0)
    hit_rate = float(profile_data.get("hit_rate") or 0)
    roi_flat = float(profile_data.get("roi_flat") or 0)
    odds_meta = odds_meta or {}
    fixture_context = fixture_context or {}
    team_news = team_news or {}

    if not odds_is_real:
        flags.append("estimated_odds")
        if not allow_estimated_picks():
            flags.append("no_real_odds")
    if count < algo_min_market_sample():
        flags.append("limited_market_history")
    elif roi_flat < 0:
        flags.append("negative_market_roi")
    if count >= algo_min_market_sample() and hit_rate and hit_rate < 50:
        flags.append("low_market_hit_rate")
    loss_streak = int(profile_data.get("loss_streak") or 0)
    recent_5_losses = int(profile_data.get("recent_5_losses") or 0)
    recent_count = int(profile_data.get("recent_count") or 0)
    recent_10_hit_rate = float(profile_data.get("recent_10_hit_rate") or 0)
    market_state = str(profile_data.get("state") or "")
    if market_state == "suppressed":
        flags.append("market_suppressed")
    elif market_state == "cooling":
        flags.append("market_cooling")
    elif market_state == "recovered":
        flags.append("market_recovered")
    if loss_streak >= algo_market_loss_streak_block():
        flags.append("market_loss_streak")
    elif loss_streak >= 2:
        flags.append("market_cooling")
    if recent_5_losses >= algo_market_recent_loss_block():
        flags.append("market_recent_losses")
    if recent_count >= 5 and recent_10_hit_rate and recent_10_hit_rate < 40:
        flags.append("market_recent_low_hit_rate")
    if conf < raw_conf:
        flags.append("confidence_calibrated_down")
    if ev is not None and ev < algo_min_ev():
        flags.append("thin_edge")
    goal_model = fixture_context.get("goal_model") or {}
    expected_total = float(goal_model.get("expected_total") or 0)
    if market == "Over 1.5" and expected_total and expected_total < algo_over15_min_expected_goals():
        flags.append("goal_line_boundary")
    if market == "Over 2.5" and expected_total and expected_total < algo_over25_min_expected_goals():
        flags.append("goal_line_boundary")
    if market == "Under 3.5" and expected_total and expected_total > algo_under35_max_expected_goals():
        flags.append("goal_line_boundary")
    if (odds_meta.get("bookmaker_count") or 0) >= 3:
        if float(odds_meta.get("spread_pct") or 0) >= 18:
            flags.append("wide_odds_market")
        if float(odds_meta.get("best_vs_average_pct") or 0) >= 12:
            flags.append("best_price_far_above_consensus")
    for flag in fixture_context.get("flags") or []:
        flags.append(f"context:{flag}")
    for flag in team_news.get("flags") or []:
        flags.append(flag)
    if team_news and not team_news.get("available"):
        flags.append("team_news_unavailable")

    return flags

def apply_context_adjustments(scores, fixture_context=None, team_news=None):
    fixture_context = fixture_context or {}
    team_news = team_news or {}
    flags = set(fixture_context.get("flags") or []) | set(team_news.get("flags") or [])
    adjusted = dict(scores)

    def bump(markets, delta):
        for market in markets:
            if market in adjusted:
                adjusted[market] = max(1, min(95, round(adjusted[market] + delta)))

    volatility_flags = {
        "friendly",
        "cup_or_knockout",
        "relegation_playoff",
        "youth_competition",
        "women_competition",
        "lower_division",
        "team_news_heavy_absences",
        "lower_strength_or_friendly_league",
    }
    if flags & volatility_flags:
        for market, value in list(adjusted.items()):
            if value >= 70:
                adjusted[market] = max(1, value - 4)

    if "team_news_absences" in flags:
        for market, value in list(adjusted.items()):
            if value >= 70:
                adjusted[market] = max(1, value - 2)

    if "home_short_rest" in flags:
        bump(["Home Win", "DNB Home", "AH Home +0.5", "Home CS", "First to Score H"], -4)
        bump(["Away Win", "DNB Away", "AH Away +0.5"], 2)
    if "away_short_rest" in flags:
        bump(["Away Win", "DNB Away", "AH Away +0.5", "Away CS", "First to Score A"], -4)
        bump(["Home Win", "DNB Home", "AH Home +0.5"], 2)

    if "home_relegation_zone" in flags or "away_relegation_zone" in flags:
        bump(["GG / BTTS Yes", "Over 1.5", "Over 2.5"], 2)
        bump(["Under 1.5", "Under 2.5", "Under 3.5"], -2)
    if "mid_table_context" in flags:
        bump(["Under 2.5", "Under 3.5"], 2)
        bump(["Over 2.5"], -2)
    if "continental_knockout" in flags:
        bump(["Under 3.5", "AH Home +0.5", "AH Away +0.5"], 3)
        bump(["Home Win", "Away Win", "Over 2.5", "GG + Over 2.5"], -3)

    return adjusted

def market_threshold(market):
    if market.startswith("Corners "):
        return _env_int("ALGO_CORNER_MIN_CONFIDENCE", 68)
    return MARKET_THRESHOLDS.get(market, WILD_MIN)

def passes_publish_gate(candidate):
    if candidate["market"] in EXCLUDED_MARKETS:
        return False
    if candidate["market"].startswith("Corners ") and not corner_market_allowed(
        candidate["market"], candidate.get("odds"), candidate.get("corner_profile")
    ):
        return False
    if (require_real_odds() or not allow_estimated_picks()) and not candidate["odds_is_real"]:
        return False
    if candidate["conf"] < WILD_MIN:
        return False
    if candidate["odds"] < MIN_ODDS:
        return False
    if candidate["ev"] is None:
        return False
    if candidate["ev"] < algo_min_ev():
        return False

    profile_data = candidate.get("market_profile", {})
    fixture_context = candidate.get("fixture_context") or {}
    goal_model = fixture_context.get("goal_model") or {}
    expected_total = float(goal_model.get("expected_total") or 0)
    draw_confidence = float(goal_model.get("draw_confidence") or 0)
    if candidate["market"] == "Over 1.5" and expected_total and expected_total < algo_over15_min_expected_goals():
        return False
    if candidate["market"] == "Over 2.5" and expected_total and expected_total < algo_over25_min_expected_goals():
        return False
    if candidate["market"] == "Under 3.5" and expected_total and expected_total > algo_under35_max_expected_goals():
        return False
    if str(profile_data.get("state") or "") == "suppressed":
        return False
    if int(profile_data.get("loss_streak") or 0) >= algo_market_loss_streak_block():
        return False
    if int(profile_data.get("recent_5_losses") or 0) >= algo_market_recent_loss_block():
        return False
    recent_count = int(profile_data.get("recent_count") or 0)
    recent_10_hit_rate = float(profile_data.get("recent_10_hit_rate") or 0)
    if recent_count >= 5 and recent_10_hit_rate and recent_10_hit_rate < 35:
        return False
    if int(profile_data.get("count") or 0) >= algo_min_market_sample():
        if float(profile_data.get("roi_flat") or 0) < -12:
            return False
        if float(profile_data.get("hit_rate") or 0) < 45:
            return False
    return True

def _market_history_is_bad(profile_data):
    count = int(profile_data.get("count") or 0)
    if count < algo_min_market_sample():
        return False
    return (
        float(profile_data.get("roi_flat") or 0) < 0
        or float(profile_data.get("hit_rate") or 0) < 50
    )

def _has_severe_risk(candidate):
    flags = set(candidate.get("risk_flags") or [])
    return bool(flags & {
        "no_real_odds",
        "negative_market_roi",
        "low_market_hit_rate",
        "wide_odds_market",
        "best_price_far_above_consensus",
        "team_news_heavy_absences",
        "market_suppressed",
        "market_loss_streak",
        "market_recent_losses",
        "market_recent_low_hit_rate",
    })

def _form_games(candidate):
    home_games = int((candidate.get("home_recent_form") or {}).get("games") or 0)
    away_games = int((candidate.get("away_recent_form") or {}).get("games") or 0)
    return min(home_games, away_games)

def _banker_quality(candidate):
    if not candidate.get("odds_is_real"):
        return False
    if candidate.get("conf", 0) < BANKER_MIN:
        return False
    if (candidate.get("ev") or 0) < max(algo_min_ev(), 0.03):
        return False
    if _has_severe_risk(candidate):
        return False
    if _market_history_is_bad(candidate.get("market_profile") or {}):
        return False
    if _form_games(candidate) < 5:
        return False
    return True

def _value_gem_quality(candidate):
    if not candidate.get("odds_is_real"):
        return False
    conf = candidate.get("conf", 0)
    if not (VALUE_MIN <= conf < BANKER_MIN):
        return False
    odds = candidate.get("odds", 0)
    if odds < MIN_ODDS:
        return False
    if (candidate.get("ev") or 0) < max(algo_min_ev(), 0.04):
        return False
    if _has_severe_risk(candidate):
        return False
    if _market_history_is_bad(candidate.get("market_profile") or {}):
        return False
    return True

def _wild_profile(candidate):
    if not candidate.get("odds_is_real"):
        return ""
    if _has_severe_risk(candidate):
        return ""
    ev = candidate.get("ev") or 0
    conf = candidate.get("conf", 0)
    if WILD_MIN <= conf < VALUE_MIN and ev >= max(algo_min_ev(), 0.03):
        return "high_upside"
    return ""

def _best_available_quality(candidate):
    if not candidate.get("odds_is_real"):
        return False
    if _has_severe_risk(candidate):
        return False
    if candidate.get("conf", 0) < algo_floor_confidence():
        return False
    if candidate.get("odds", 0) < MIN_ODDS:
        return False
    ev = candidate.get("ev")
    if ev is None or ev < algo_floor_ev():
        return False
    if candidate["market"].startswith("Corners ") and not corner_market_allowed(
        candidate["market"], candidate.get("odds"), candidate.get("corner_profile")
    ):
        return False
    profile_data = candidate.get("market_profile") or {}
    if str(profile_data.get("state") or "") == "suppressed":
        return False
    return True

def _tag_profile(candidate, profile_name):
    flags = list(candidate.get("risk_flags") or [])
    flags = [flag for flag in flags if not str(flag).startswith("profile:")]
    flags.append(f"profile:{profile_name}")
    candidate["risk_flags"] = flags
    candidate["selection_profile"] = profile_name
    return candidate

def recent_form_summary(form):
    games = max(form.get("games", 0), 1)
    wins = form.get("wins", 0)
    draws = form.get("draws", 0)
    losses = form.get("losses")
    if losses is None:
        losses = max(0, form.get("games", 0) - wins - draws)
    return {
        "games": form.get("games", 0),
        "scope": form.get("scope", "overall"),
        "last_played": form.get("last_played", ""),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "form": form.get("form", ""),
        "avg_scored": form.get("avg_scored", 0),
        "avg_conceded": form.get("avg_conceded", 0),
        "clean_sheets": form.get("clean_sheets", 0),
        "btts_rate": round(form.get("btts_count", 0) / games * 100, 1),
        "over25_rate": round(form.get("over25_count", 0) / games * 100, 1),
        "streak": form.get("streak", 0),
    }

def _percent(value):
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "unknown"

def _format_form_line(label, form):
    form = form or {}
    games = form.get("games") or 0
    if not games:
        return f"{label}: recent form unavailable"
    return (
        f"{label}: {form.get('wins', 0)}W-{form.get('draws', 0)}D-{form.get('losses', 0)}L over {games}, "
        f"{form.get('avg_scored', 0)} scored and {form.get('avg_conceded', 0)} conceded per match, "
        f"BTTS {_percent(form.get('btts_rate'))}, over 2.5 {_percent(form.get('over25_rate'))}, "
        f"{form.get('clean_sheets', 0)} clean sheets"
    )

def _append_unique(items, value):
    if value and value not in items:
        items.append(value)

def _compact_items(items, limit=6):
    return [str(item) for item in items if item][:limit]

def build_fixture_insights(fx):
    home = fx.get("home_recent_form") or {}
    away = fx.get("away_recent_form") or {}
    context = fx.get("fixture_context") or {}
    team_news = fx.get("team_news") or {}
    flags = set(context.get("flags") or []) | set(team_news.get("flags") or [])
    goal_model = context.get("goal_model") or {}
    h2h = context.get("h2h") or {}

    key_signals = []
    risk_warnings = []
    confidence_drivers = []

    if home.get("games"):
        _append_unique(key_signals, f"Home venue form: {home.get('wins', 0)}W-{home.get('draws', 0)}D-{home.get('losses', 0)}L, {home.get('avg_scored', 0)} scored and {home.get('avg_conceded', 0)} conceded per match.")
    if away.get("games"):
        _append_unique(key_signals, f"Away venue form: {away.get('wins', 0)}W-{away.get('draws', 0)}D-{away.get('losses', 0)}L, {away.get('avg_scored', 0)} scored and {away.get('avg_conceded', 0)} conceded per match.")
    if goal_model.get("expected_total") is not None:
        _append_unique(key_signals, f"Goal model projects {goal_model.get('expected_total')} total goals with draw confidence at {goal_model.get('draw_confidence', 'unknown')}%.")
    if int(h2h.get("games") or 0) >= 2:
        _append_unique(key_signals, f"H2H sample: {h2h.get('games')} games, {h2h.get('draws', 0)} draws, average {h2h.get('avg_goals', 0)} goals.")
    if context.get("league_strength"):
        _append_unique(confidence_drivers, f"League strength factor {context.get('league_strength')}.")

    for flag in sorted(flags):
        if flag in {"cup_or_knockout", "continental_knockout", "relegation_playoff", "friendly", "youth_competition", "women_competition", "lower_division", "lower_strength_or_friendly_league"}:
            _append_unique(risk_warnings, flag)
        elif flag in {"home_short_rest", "away_short_rest", "team_news_absences", "team_news_heavy_absences", "lineups_unavailable", "team_news_unavailable"}:
            _append_unique(risk_warnings, flag)

    strategy = "Use normal pre-match selection rules."
    if risk_warnings:
        strategy = "Treat this fixture cautiously; context contains volatility flags."
    if goal_model.get("expected_total") is not None:
        expected = float(goal_model.get("expected_total") or 0)
        if expected < algo_over15_min_expected_goals():
            strategy = "Avoid aggressive goal overs; projected total is close to the lower scoring boundary."
        elif expected > algo_under35_max_expected_goals():
            strategy = "Avoid loose goal unders; projected total is close to the high-scoring boundary."
    if float(goal_model.get("draw_confidence") or 0) >= algo_high_draw_risk_confidence():
        _append_unique(risk_warnings, "high_draw_risk")

    return {
        "pre_match_strategy": strategy,
        "key_signals": _compact_items(key_signals),
        "confidence_drivers": _compact_items(confidence_drivers),
        "risk_warnings": _compact_items(risk_warnings, 8),
    }

def build_market_insights(market, confidence, odds, ev, risk_flags=None, fixture_context=None, home_form=None, away_form=None, corner_profile=None, profile_data=None, eligible=False):
    risk_flags = list(risk_flags or [])
    fixture_context = fixture_context or {}
    goal_model = fixture_context.get("goal_model") or {}
    profile_data = profile_data or {}
    key_signals = []
    confidence_drivers = []
    avoid_reasons = []

    expected_total = goal_model.get("expected_total")
    if expected_total is not None:
        _append_unique(key_signals, f"Expected goals: {expected_total}.")
    if market.startswith("Corners "):
        _append_unique(key_signals, f"Corner model projects {((corner_profile or {}).get('expected_total', 'unknown'))} total corners.")
    if ev is not None:
        _append_unique(confidence_drivers, f"EV {ev:+.3f} at {odds} odds.")
    else:
        _append_unique(avoid_reasons, "No real odds/EV available.")
    if profile_data.get("state"):
        _append_unique(confidence_drivers if profile_data.get("state") == "recovered" else avoid_reasons, f"Market state: {profile_data.get('state')}.")
    if profile_data.get("hit_rate"):
        _append_unique(confidence_drivers, f"Historical hit rate {profile_data.get('hit_rate')}%.")

    if "goal_line_boundary" in risk_flags:
        _append_unique(avoid_reasons, "Goal projection is too close to the selected line.")
    for flag in risk_flags:
        if flag in {"market_suppressed", "market_loss_streak", "market_recent_losses", "thin_edge", "negative_market_roi", "low_market_hit_rate", "wide_odds_market", "best_price_far_above_consensus"}:
            _append_unique(avoid_reasons, flag)

    strategy = "Playable candidate if it survives ranking."
    if not eligible:
        strategy = "Do not publish; keep this market in internal monitoring."
    elif avoid_reasons:
        strategy = "Eligible but caution remains; publish only if it is clearly the best fixture option."
    if confidence >= 80 and eligible and not avoid_reasons:
        strategy = "Strong pre-match candidate."

    return {
        "pre_match_strategy": strategy,
        "market_state": profile_data.get("state", "untracked"),
        "key_signals": _compact_items(key_signals),
        "confidence_drivers": _compact_items(confidence_drivers),
        "risk_warnings": _compact_items(risk_flags, 10),
        "avoid_reason": "; ".join(_compact_items(avoid_reasons, 5)),
    }

def _market_evidence(pick):
    market = pick.get("market", "")
    home = pick.get("home_recent_form") or {}
    away = pick.get("away_recent_form") or {}
    if market.startswith("Corners "):
        profile = pick.get("corner_profile") or {}
        home_corners = (profile.get("home") or {})
        away_corners = (profile.get("away") or {})
        expected = profile.get("expected_total", "unknown")
        return (
            f"The corner profile projects around {expected} total corners. "
            f"{pick.get('home_team') or pick.get('hname')}: {home_corners.get('avg_for', 'unknown')} for, "
            f"{home_corners.get('avg_against', 'unknown')} against; "
            f"{pick.get('away_team') or pick.get('aname')}: {away_corners.get('avg_for', 'unknown')} for, "
            f"{away_corners.get('avg_against', 'unknown')} against."
        )
    if market.startswith("Under"):
        return (
            f"The goal profile leans controlled: {_format_form_line('Home', home)}. "
            f"{_format_form_line('Away', away)}."
        )
    if market.startswith("Over") or "BTTS" in market or market.startswith("GG"):
        return (
            f"The attacking profile supports goals: {_format_form_line('Home', home)}. "
            f"{_format_form_line('Away', away)}."
        )
    if market.endswith("Win") or market.startswith("AH ") or market.startswith("DNB"):
        return (
            f"The result market is backed by match-state protection and recent team balance: "
            f"{_format_form_line('Home', home)}. {_format_form_line('Away', away)}."
        )
    return f"Recent team context: {_format_form_line('Home', home)}. {_format_form_line('Away', away)}."

def pick_reasoning(pick):
    edge_note = "real market odds" if pick.get("odds_is_real") else "estimated odds"
    ev_text = f"{pick.get('ev'):+.3f} expected value" if pick.get("ev") is not None else "unpriced expected value"
    return (
        f"{pick.get('market')} rates at {pick.get('conf')}% confidence with "
        f"{pick.get('odds')} odds and {ev_text}. "
        f"{_market_evidence(pick)} "
        f"Pricing is based on {edge_note}."
    )

def pick_verdict(pick):
    tier = pick.get("tier") or "pick"
    flags = pick.get("risk_flags") or []
    profile_name = pick.get("selection_profile", "")
    if "negative_market_roi" in flags or "low_market_hit_rate" in flags:
        return f"{tier.replace('_', ' ').title()} passed today, but historical market risk is flagged."
    if profile_name == "best_available":
        return "Best available pick from today's slate; published with visible risk controls."
    if tier == "wild_card" and profile_name == "lean":
        return "Wild Card marked as a lean: extra playable volume with moderate risk."
    if tier == "wild_card" and profile_name == "high_upside":
        return "Wild Card selected for controlled upside at bigger odds."
    if pick.get("proven"):
        return f"{tier.replace('_', ' ').title()} backed by a proven market profile."
    return f"{tier.replace('_', ' ').title()} selected for positive value and confidence."

def llm_reasoning_enabled():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    configured = os.environ.get("ALGO_LLM_REASONING_ENABLED")
    if configured is None or configured == "":
        return bool(api_key)
    return bool(api_key) and str(configured).strip().lower() in {"1", "true", "yes", "on"}

def llm_debug_logging_enabled():
    return _env_bool("ALGO_LLM_DEBUG_LOG_RESPONSE", False)

def _strip_llm_thinking(content):
    return re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL | re.IGNORECASE).strip()

def _compact_pick_for_llm(pick):
    return {
        "fixture": pick.get("fixture"),
        "league": pick.get("league"),
        "kickoff": pick.get("kickoff"),
        "tier": pick.get("tier"),
        "market": pick.get("market"),
        "meaning": pick.get("meaning"),
        "confidence": pick.get("conf"),
        "odds": pick.get("odds"),
        "ev": pick.get("ev"),
        "proven_market": pick.get("proven"),
        "risk_flags": pick.get("risk_flags") or [],
        "selection_profile": pick.get("selection_profile"),
        "home_recent_form": pick.get("home_recent_form") or {},
        "away_recent_form": pick.get("away_recent_form") or {},
        "corner_profile": pick.get("corner_profile") or {},
        "odds_meta": pick.get("odds_meta") or {},
        "fixture_context": pick.get("fixture_context") or {},
        "team_news": pick.get("team_news") or {},
        "insights": pick.get("insights") or {},
        "fallback_reasoning": pick.get("reasoning", ""),
        "fallback_verdict": pick.get("model_verdict", ""),
    }

def _parse_llm_json(content):
    cleaned = _strip_llm_thinking(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))

def _llm_items_from_payload(parsed):
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return []
    for key in ("picks", "items", "results", "explanations"):
        value = parsed.get(key)
        if isinstance(value, list):
            return value
    return []

def _deepseek_chat_completion(payload, *, retries=2):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    last_error = None
    for attempt in range(retries + 1):
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if response.status_code != 429:
            response.raise_for_status()
            message = ((response.json().get("choices") or [{}])[0].get("message") or {})
            content = message.get("content", "")
            if llm_debug_logging_enabled():
                log.info(
                    "DeepSeek explanation raw response: %s",
                    _strip_llm_thinking(content).replace("\n", " ")[:1500],
                )
            return content
        last_error = response
        body = (response.text or "").replace("\n", " ")[:500]
        request_id = response.headers.get("x-request-id") or response.headers.get("X-Request-Id") or ""
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 2 + attempt * 3
        except (TypeError, ValueError):
            delay = 2 + attempt * 3
        log.warning(
            "DeepSeek rate limit hit; retrying in %.1fs (attempt %s/%s, retry_after=%s, request_id=%s, body=%s)",
            delay,
            attempt + 1,
            retries + 1,
            retry_after or "",
            request_id,
            body,
        )
        time.sleep(delay)
    if last_error is not None:
        body = (last_error.text or "").replace("\n", " ")[:800]
        raise RuntimeError(f"DeepSeek 429 after retries: {body}")
    return None

def _call_deepseek_pick_batch(picks):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        log.warning("DeepSeek explanation skipped: DEEPSEEK_API_KEY is not configured")
        return {}
    if not picks:
        return {}
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    compact_picks = []
    for index, pick in enumerate(picks):
        item = _compact_pick_for_llm(pick)
        item["index"] = index
        compact_picks.append(item)
    payload = {
        "model": model,
        "temperature": 0.25,
        "top_p": 0.9,
        "max_tokens": max(1000, min(4200, 380 * len(compact_picks))),
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful football betting analyst writing for paying users. "
                    "Your job is to explain why the model likes a pick in plain, human language. "
                    "Use only the supplied data. Do not promise a win. Do not invent injuries, lineups, "
                    "standings, odds movement, venue facts, or head-to-head facts beyond the provided fields. "
                    "Be specific, balanced, and clear about the main reason and the main risk. "
                    "Return strict JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Rewrite each selected pick into a customer-facing explanation that feels like a real analyst wrote it.\n"
                    "For every input item, return the same index with:\n"
                    "- reasoning: 3-4 short sentences. Explain the football logic first, then mention confidence/odds/EV, then name the key risk if there is one.\n"
                    "- model_verdict: 1 direct sentence that tells the user how to treat the pick, without hype.\n"
                    "Avoid generic phrases like 'the model prefers this market from the available fixture markets'. "
                    "Avoid listing raw stats without interpretation. If the data is thin, say so naturally.\n"
                    "Return JSON shaped exactly as: "
                    '{"picks":[{"index":0,"reasoning":"...","model_verdict":"..."}]}.\n'
                    f"Data:\n{json.dumps(compact_picks, ensure_ascii=True)}"
                ),
            },
        ],
    }
    content = _deepseek_chat_completion(payload) or ""
    parsed = _parse_llm_json(content)
    items = _llm_items_from_payload(parsed)
    if not items:
        log.warning(
            "DeepSeek explanation response had no picks list; keys=%s body=%s",
            list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__,
            _strip_llm_thinking(content).replace("\n", " ")[:500],
        )
    generated_by_index = {}
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            index = int(item.get("index", item.get("id", item.get("pick_index"))))
        except (TypeError, ValueError):
            skipped += 1
            continue
        reasoning = str(
            item.get("reasoning")
            or item.get("analysis")
            or item.get("explanation")
            or item.get("why")
            or ""
        ).strip()
        verdict = str(
            item.get("model_verdict")
            or item.get("verdict")
            or item.get("summary")
            or ""
        ).strip()
        if len(reasoning) < 40 or len(verdict) < 10:
            skipped += 1
            continue
        generated_by_index[index] = {
            "reasoning": reasoning[:900],
            "model_verdict": verdict[:280],
        }
    if skipped or len(generated_by_index) != len(picks):
        log.info(
            "DeepSeek explanation parse accepted %s/%s items; skipped=%s",
            len(generated_by_index),
            len(picks),
            skipped,
        )
    return generated_by_index

def enhance_pick_explanations_with_llm(picks):
    if not llm_reasoning_enabled():
        log.info(
            "LLM pick explanations disabled or missing DEEPSEEK_API_KEY "
            "(enabled=%s, key_present=%s)",
            os.environ.get("ALGO_LLM_REASONING_ENABLED", ""),
            bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        )
        return
    log.info(
        "LLM pick explanations using DeepSeek model=%s debug_response=%s picks=%s",
        os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        llm_debug_logging_enabled(),
        len(picks),
    )
    try:
        generated_by_index = _call_deepseek_pick_batch(picks)
    except Exception as exc:
        log.warning("LLM pick explanation batch skipped: %s", exc)
        return
    updated = 0
    for index, pick in enumerate(picks):
        generated = generated_by_index.get(index)
        if not generated:
            continue
        pick["reasoning"] = generated["reasoning"]
        pick["model_verdict"] = generated["model_verdict"]
        updated += 1
    log.info("LLM pick explanations updated %s/%s selected picks", updated, len(picks))

def select_picks(all_confs, scored_fxs, odds_list):
    pool=[]
    profile = load_performance_profile()
    for fx,confs,real_odds in zip(scored_fxs,all_confs,odds_list):
        for market,conf in confs.items():
            key  = ODDS_KEYS_MAP.get(market)
            real_odd = (real_odds.get(key) if key else None) or real_odds.get(market)
            odds_meta = ((real_odds.get("_meta") or {}).get(key) if key else None) or (real_odds.get("_meta") or {}).get(market) or {}
            odds_is_real = bool(real_odd)
            profile_data = market_profile(profile, market, fx.get("league"))
            calibrated_conf = calibrate_confidence(conf, profile_data)
            odds = real_odd or est_odds(calibrated_conf)
            ev = round((calibrated_conf/100)*odds-1,3) if odds_is_real else None
            candidate = {
                "fixture":fx["fixture"],"league":fx["league"],
                "country":fx.get("country",""),
                "round":fx.get("round",""),
                "league_type":fx.get("league_type",""),
                "code":fx.get("code","?"),"kickoff":fx["kickoff"],
                "home_team":fx.get("hname",""),"away_team":fx.get("aname",""),
                "home_logo":fx.get("home_logo",""),"away_logo":fx.get("away_logo",""),
                "market":market,"meaning":market_meaning(market),
                "raw_conf":conf,"conf":calibrated_conf,"odds":odds,"ev":ev,
                "odds_is_real":odds_is_real,
                "proven":market in PROVEN_MARKETS,
                "hname":fx["hname"],"aname":fx["aname"],
                "match_id":fx.get("match_id"),
                "source":fx.get("source","?"),
                "home_recent_form":fx.get("home_recent_form",{}),
                "away_recent_form":fx.get("away_recent_form",{}),
                "corner_profile":fx.get("corner_profile",{}),
                "market_profile":profile_data,
                "odds_meta": odds_meta,
                "fixture_context": fx.get("fixture_context", {}),
                "team_news": fx.get("team_news", {}),
            }
            candidate["risk_flags"] = candidate_risk_flags(
                conf, calibrated_conf, market, odds, odds_is_real, ev, profile_data, odds_meta,
                fx.get("fixture_context", {}),
                fx.get("team_news", {}),
            )
            candidate["insights"] = build_market_insights(
                market,
                calibrated_conf,
                odds,
                ev,
                candidate["risk_flags"],
                fx.get("fixture_context", {}),
                fx.get("home_recent_form", {}),
                fx.get("away_recent_form", {}),
                fx.get("corner_profile", {}),
                profile_data,
                eligible=passes_publish_gate(candidate),
            )
            if passes_publish_gate(candidate):
                pool.append(candidate)

    # ── BANKERS: reliability first — proven, real-priced, low-volatility markets ──
    banker_cands = sorted(
        [_tag_profile(p, "reliability") for p in pool if _banker_quality(p)],
        key=lambda x:(x["conf"],x["ev"] or 0,-x["odds"]),
        reverse=True,
    )
    bankers=[]; used_b=set()
    for p in banker_cands:
        if p["fixture"] not in used_b:
            bankers.append(p); used_b.add(p["fixture"])
        if len(bankers)>=MAX_BANKERS: break

    # ── VALUE GEMS: mispricing first — strong EV while keeping reliability filters ──
    value_cands = sorted(
        [_tag_profile(p, "mispriced_value") for p in pool if _value_gem_quality(p) and p["fixture"] not in used_b],
        key=lambda x:(x["ev"] or 0,x["conf"],x["odds"]),
        reverse=True,
    )
    seen_v=set(); value_gems=[]
    for p in value_cands:
        if p["fixture"] not in seen_v:
            seen_v.add(p["fixture"]); value_gems.append(p)
        if len(value_gems)>=MAX_VALUE_GEMS: break

    # ── WILD CARDS: volume bucket. Internally split into lean vs high-upside profiles ──
    used_all = used_b | seen_v
    wild_pool = []
    for p in pool:
        if p["fixture"] in used_all:
            continue
        profile_name = _wild_profile(p)
        if profile_name:
            wild_pool.append(_tag_profile(p, profile_name))
    wild_cands = sorted(
        wild_pool,
        key=lambda x:(x.get("selection_profile") == "lean", x["conf"], x["ev"] or 0, -x["odds"]),
        reverse=True,
    )
    seen_w=set(); wild_cards=[]
    for p in wild_cands:
        if p["fixture"] not in seen_w:
            seen_w.add(p["fixture"]); wild_cards.append(p)
        if len(wild_cards)>=MAX_WILD_CARDS: break

    selected_fixtures = used_b | seen_v | seen_w
    min_daily = max(0, min(algo_min_daily_picks(), algo_max_daily_picks()))
    if len(bankers) + len(value_gems) + len(wild_cards) < min_daily:
        fallback_cands = sorted(
            [
                _tag_profile(p, "best_available")
                for p in pool
                if p["fixture"] not in selected_fixtures and _best_available_quality(p)
            ],
            key=lambda x: (
                x["conf"],
                x["ev"] if x.get("ev") is not None else -999,
                -x["odds"],
            ),
            reverse=True,
        )
        for p in fallback_cands:
            if p["fixture"] in selected_fixtures:
                continue
            wild_cards.append(p)
            selected_fixtures.add(p["fixture"])
            if len(bankers) + len(value_gems) + len(wild_cards) >= min_daily:
                break

    max_daily = max(1, algo_max_daily_picks())
    selected = bankers + value_gems + wild_cards
    if len(selected) > max_daily:
        keep = set(id(p) for p in sorted(selected, key=lambda x: (x["conf"], x["ev"] or 0), reverse=True)[:max_daily])
        bankers = [p for p in bankers if id(p) in keep]
        value_gems = [p for p in value_gems if id(p) in keep]
        wild_cards = [p for p in wild_cards if id(p) in keep]

    log.info(f"Picks selected — Bankers:{len(bankers)} ValueGems:{len(value_gems)} WildCards:{len(wild_cards)}")
    for tier, selected in (("banker", bankers), ("value_gem", value_gems), ("wild_card", wild_cards)):
        for pick in selected:
            pick["tier"] = tier
            pick["reasoning"] = pick_reasoning(pick)
            pick["model_verdict"] = pick_verdict(pick)
    enhance_pick_explanations_with_llm(bankers + value_gems + wild_cards)
    return bankers, value_gems, wild_cards

# ── RECORD TO SHEETS ──────────────────────────────────────────────
def record_to_sheets(sheets, bankers, value_gems, wild_cards, target_date, bankroll):
    if sheets is None:
        return sum(len(picks or []) for picks in (bankers, value_gems, wild_cards))
    ws = sheets.worksheet("Picks")
    headers = ["Date","Fixture","League","KO (WAT)","Tier","Market","Meaning",
               "Confidence %","Odds","EV","Stake (N)","Bankroll Before (N)",
               "Status","Score","Result","P&L (N)","Bankroll After (N)","Source"]
    try:
        existing = ws.row_values(1)
        if not existing or existing[0]!="Date": ws.update("A1",[headers])
    except Exception: ws.update("A1",[headers])

    picks=[]
    for b in (bankers or []):    picks.append(("Banker",b))
    for g in (value_gems or []): picks.append(("Value Gem",g))
    for w in (wild_cards or []): picks.append(("Wild Card",w))
    if not picks:
        log.info("No picks today"); return 0

    rows=[]; remaining=bankroll
    for tier,pick in picks:
        pct   = FLAT_STAKE_PCT
        stake = round(max(100,remaining*pct),2)
        tier_label = "Banker" if "Banker" in tier else "Value Gem" if "Gem" in tier else "Wild Card"
        src = "FD" if pick.get("source")=="fd" else "APS"
        rows.append([target_date,pick["fixture"],pick["league"],pick["kickoff"],
                     tier_label,pick["market"],pick["meaning"],
                     f"{pick['conf']}%",pick["odds"],f"{pick['ev']:+.3f}" if pick.get("ev") is not None else "",
                     stake,remaining,"PENDING","","","","",src])
    ws.append_rows(rows)
    log.info(f"Recorded {len(rows)} picks")
    return len(rows)


def serialize_selected_picks(bankers, value_gems, wild_cards, target_date, bankroll):
    picks = []
    for tier, selected in (
        ("banker", bankers or []),
        ("value_gem", value_gems or []),
        ("wild_card", wild_cards or []),
    ):
        for pick in selected:
            picks.append({
                "match_date": target_date,
                "fixture": pick.get("fixture", ""),
                "home_team": pick.get("home_team", ""),
                "away_team": pick.get("away_team", ""),
                "home_logo": pick.get("home_logo", ""),
                "away_logo": pick.get("away_logo", ""),
                "league": pick.get("league", ""),
                "country": pick.get("country", ""),
                "round": pick.get("round", ""),
                "league_type": pick.get("league_type", ""),
                "kickoff": pick.get("kickoff", ""),
                "match_id": str(pick.get("match_id") or ""),
                "tier": tier,
                "market": pick.get("market", ""),
                "meaning": pick.get("meaning", ""),
                "reasoning": pick.get("reasoning", ""),
                "model_verdict": pick.get("model_verdict", ""),
                "home_recent_form": pick.get("home_recent_form", {}),
                "away_recent_form": pick.get("away_recent_form", {}),
                "risk_flags": pick.get("risk_flags", []),
                "insights": pick.get("insights", {}),
                "confidence": pick.get("conf", 0),
                "odds": pick.get("odds", 0),
                "ev": pick.get("ev", 0),
                "stake": round(max(100, bankroll * FLAT_STAKE_PCT), 2),
                "source": "FD" if pick.get("source") == "fd" else "APS",
            })
    return picks

def serialize_fixture_markets(confs, real_odds=None, league=None, corner_profile=None, fixture_context=None, team_news=None, home_recent_form=None, away_recent_form=None):
    markets = []
    real_odds = real_odds or {}
    profile = load_performance_profile()
    for market, confidence in sorted(confs.items(), key=lambda item: item[1], reverse=True):
        key = ODDS_KEYS_MAP.get(market)
        real_odd = (real_odds.get(key) if key else None) or real_odds.get(market)
        odds_meta = ((real_odds.get("_meta") or {}).get(key) if key else None) or (real_odds.get("_meta") or {}).get(market) or {}
        profile_data = market_profile(profile, market, league)
        calibrated_confidence = calibrate_confidence(confidence, profile_data)
        odds = real_odd or est_odds(calibrated_confidence)
        odds_is_real = bool(real_odd)
        ev = round((calibrated_confidence / 100) * odds - 1, 3) if odds_is_real else None
        risk_flags = candidate_risk_flags(
            confidence, calibrated_confidence, market, odds, odds_is_real, ev, profile_data, odds_meta,
            fixture_context or {},
            team_news or {},
        )
        gate_candidate = {
            "market": market,
            "conf": calibrated_confidence,
            "odds": odds,
            "ev": ev,
            "odds_is_real": odds_is_real,
            "market_profile": profile_data,
            "corner_profile": corner_profile or {},
            "odds_meta": odds_meta,
            "fixture_context": fixture_context or {},
            "team_news": team_news or {},
        }
        eligible = passes_publish_gate(gate_candidate)
        markets.append({
            "market": market,
            "meaning": market_meaning(market),
            "raw_confidence": confidence,
            "confidence": calibrated_confidence,
            "odds": odds,
            "odds_meta": odds_meta,
            "ev": ev,
            "odds_source": "api_football" if odds_is_real else "estimated",
            "proven": market in PROVEN_MARKETS,
            "eligible": eligible,
            "risk_flags": risk_flags,
            "insights": build_market_insights(
                market,
                calibrated_confidence,
                odds,
                ev,
                risk_flags,
                fixture_context or {},
                home_recent_form or {},
                away_recent_form or {},
                corner_profile or {},
                profile_data,
                eligible=eligible,
            ),
        })
    return markets

def serialize_fixture_summaries(scored_fxs, all_confs, odds_list=None):
    summaries = []
    odds_list = odds_list or [{} for _ in scored_fxs]
    for fx, confs, real_odds in zip(scored_fxs, all_confs, odds_list):
        summaries.append({
            "fixture": fx.get("fixture", ""),
            "home_team": fx.get("hname", ""),
            "away_team": fx.get("aname", ""),
            "home_logo": fx.get("home_logo", ""),
            "away_logo": fx.get("away_logo", ""),
            "league": fx.get("league", ""),
            "country": fx.get("country", ""),
            "round": fx.get("round", ""),
            "league_type": fx.get("league_type", ""),
            "kickoff": fx.get("kickoff", ""),
            "match_id": str(fx.get("match_id") or ""),
            "home_recent_form": fx.get("home_recent_form", {}),
            "away_recent_form": fx.get("away_recent_form", {}),
            "fixture_context": fx.get("fixture_context", {}),
            "team_news": fx.get("team_news", {}),
            "market_count": len(confs),
            "markets_70_plus": sum(1 for value in confs.values() if value >= 70),
            "markets_65_plus": sum(1 for value in confs.values() if value >= 65),
            "corner_profile": fx.get("corner_profile", {}),
            "markets": serialize_fixture_markets(
                confs,
                real_odds,
                fx.get("league"),
                fx.get("corner_profile", {}),
                fx.get("fixture_context", {}),
                fx.get("team_news", {}),
                fx.get("home_recent_form", {}),
                fx.get("away_recent_form", {}),
            ),
            "insights": build_fixture_insights(fx),
        })
    return summaries

# ── PDF + DRIVE ───────────────────────────────────────────────────
def generate_and_upload_pdf(drive, bankers, value_gems, wild_cards,
                             target_date, bankroll, all_scored=None,
                             gemini_picks=None):
    if drive is None:
        log.info("PDF export skipped; Google Drive is not configured")
        return None
    temp_path = f"/tmp/GrindAlgo_{target_date}.pdf"
    doc = SimpleDocTemplate(temp_path, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=12*mm, bottomMargin=14*mm)

    # ── COLOUR PALETTE ────────────────────────────────────────────
    C_DARK    = colors.HexColor("#1a1a2e")
    C_GREEN   = colors.HexColor("#00b894")
    C_BLUE    = colors.HexColor("#0a3d62")
    C_RED     = colors.HexColor("#7f1d1d")
    C_AMBER   = colors.HexColor("#b45309")
    C_LIGHT   = colors.HexColor("#f5f7fa")
    C_MID     = colors.HexColor("#e8ecf0")
    C_WHITE   = colors.white
    C_GREY    = colors.HexColor("#6b7280")
    C_DKGREY  = colors.HexColor("#374151")
    C_BORDER  = colors.HexColor("#d1d5db")

    # ── STYLES ────────────────────────────────────────────────────
    def S(n, **kw): return ParagraphStyle(n, **kw)

    sTitle   = S("title",   fontName="Helvetica-Bold",  fontSize=22, alignment=TA_CENTER, textColor=C_DARK,   spaceAfter=2)
    sSubT    = S("subt",    fontName="Helvetica",       fontSize=10, alignment=TA_CENTER, textColor=C_GREY,   spaceAfter=1)
    sMeta    = S("meta",    fontName="Helvetica",       fontSize=8,  alignment=TA_CENTER, textColor=C_GREY,   spaceAfter=2)
    sSec     = S("sec",     fontName="Helvetica-Bold",  fontSize=13, textColor=C_DARK,   spaceBefore=6, spaceAfter=3)
    sSecSm   = S("secsm",   fontName="Helvetica-Bold",  fontSize=10, textColor=C_DARK,   spaceBefore=4, spaceAfter=2)
    sBody    = S("body",    fontName="Helvetica",       fontSize=9,  textColor=C_DKGREY, spaceAfter=2, leading=13)
    sBodySm  = S("bodysm",  fontName="Helvetica",       fontSize=8,  textColor=C_DKGREY, spaceAfter=1, leading=11)
    sItal    = S("ital",    fontName="Helvetica-Oblique",fontSize=8, textColor=C_GREY,   spaceAfter=2)
    sFoot    = S("foot",    fontName="Helvetica",       fontSize=7,  alignment=TA_CENTER, textColor=C_GREY)
    sCardLbl = S("clbl",    fontName="Helvetica-Bold",  fontSize=9,  textColor=C_WHITE)
    sCardFx  = S("cfx",     fontName="Helvetica-Bold",  fontSize=10, textColor=C_WHITE)
    sCardBd  = S("cbd",     fontName="Helvetica",       fontSize=8.5,textColor=C_DKGREY, leading=12)
    sCardBold= S("cbold",   fontName="Helvetica-Bold",  fontSize=8.5,textColor=C_DKGREY, leading=12)
    sMktGreen= S("mktg",    fontName="Helvetica",       fontSize=7.5,textColor=C_GREEN,  leading=11)
    sMktDark = S("mktd",    fontName="Helvetica",       fontSize=7.5,textColor=C_DKGREY, leading=11)
    sB       = S("b2",      fontName="Helvetica",       fontSize=8,  textColor=C_DKGREY, spaceAfter=1)
    sN       = S("n2",      fontName="Helvetica-Oblique",fontSize=8, textColor=C_GREY,   spaceAfter=2)

    story = []
    now_wat   = datetime.now(WAT)
    all_picks = list(bankers or []) + list(value_gems or []) + list(wild_cards or [])
    n_bankers = len(bankers or [])
    n_gems    = len(value_gems or [])
    n_wilds   = len(wild_cards or [])
    total_picks = n_bankers + n_gems + n_wilds

    # ═══════════════════════════════════════════════════════════════
    # PAGE 1 — HEADER + PICKS
    # ═══════════════════════════════════════════════════════════════

    # ── Report Header ─────────────────────────────────────────────
    story.append(Paragraph("THE GRIND ALGO", sTitle))
    story.append(Paragraph("Daily Betting Intelligence Report  |  API-Football Architecture", sSubT))
    story.append(Paragraph(
        f"Date: <b>{target_date}</b>   |   "
        f"Generated: <b>{now_wat.strftime('%d %B %Y  %H:%M WAT')}</b>   |   "
        f"Bankroll: <b>N{bankroll:,.0f}</b>   |   "
        f"Data: API-Football fixtures + predictions + odds",
        sMeta))
    story.append(HRFlowable(width="100%", thickness=2, color=C_DARK))
    story.append(Spacer(1, 3*mm))

    # ── Summary Banner ────────────────────────────────────────────
    summary_data = [[
        Paragraph(f"<b>{total_picks}</b>\nTotal Picks", S("sb1", fontName="Helvetica-Bold", fontSize=14, alignment=TA_CENTER, textColor=C_WHITE, leading=16)),
        Paragraph(f"<b>{n_bankers}</b>\nBankers", S("sb2", fontName="Helvetica-Bold", fontSize=14, alignment=TA_CENTER, textColor=C_WHITE, leading=16)),
        Paragraph(f"<b>{n_gems}</b>\nValue Gems", S("sb3", fontName="Helvetica-Bold", fontSize=14, alignment=TA_CENTER, textColor=C_WHITE, leading=16)),
        Paragraph(f"<b>{n_wilds}</b>\nWild Cards", S("sb4", fontName="Helvetica-Bold", fontSize=14, alignment=TA_CENTER, textColor=C_WHITE, leading=16)),
        Paragraph(f"<b>{len(all_scored) if all_scored else 0}</b>\nGames Scored", S("sb5", fontName="Helvetica-Bold", fontSize=14, alignment=TA_CENTER, textColor=C_WHITE, leading=16)),
    ]]
    sbanner = Table(summary_data, colWidths=[35*mm]*5)
    sbanner.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (0,0), C_DARK),
        ("BACKGROUND", (1,0), (1,0), colors.HexColor("#14532d")),
        ("BACKGROUND", (2,0), (2,0), C_BLUE),
        ("BACKGROUND", (3,0), (3,0), C_RED),
        ("BACKGROUND", (4,0), (4,0), C_AMBER),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("LINEAFTER",     (0,0), (3,0),   0.5, colors.HexColor("#ffffff44")),
    ]))
    story.append(sbanner)
    story.append(Spacer(1, 4*mm))

    # ── How to Use This Report ────────────────────────────────────
    intro_txt = (
        "This report is generated each day by the <b>GrindAlgo v8</b> engine, which analyses up to "
        "100+ fixtures across 20+ leagues using a <b>19-parameter confidence model</b>. Each pick "
        "has been ranked by confidence score and Expected Value (EV). "
        "Stake sizes are derived from your current bankroll using tiered Kelly-inspired percentages: "
        "<b>a flat percentage across all tiers.</b>. "
        "Never bet more than you can afford to lose. Past performance does not guarantee future results."
    )
    story.append(Paragraph(intro_txt, sBody))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 2*mm))

    if total_picks == 0:
        box = Table([[Paragraph("<b>No picks cleared the thresholds today.</b>", sBody)],
                     [Paragraph("All games were scored but none met the minimum confidence and EV criteria. "
                                "See the Full Market Scorecard on the next page.", sItal)]],
                    colWidths=[175*mm])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), C_DARK),
            ("TEXTCOLOR",  (0,0), (-1,0), C_WHITE),
            ("BACKGROUND", (0,1), (-1,1), C_LIGHT),
            ("TOPPADDING",    (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
            ("LEFTPADDING",   (0,0), (-1,-1), 12),
            ("BOX",           (0,0), (-1,-1), 0.5, C_BORDER),
        ]))
        story.append(box)

    # ── HELPER: build a pick card ─────────────────────────────────
    def pick_card(rank_label, pick, hdr_color, tier_tag, pct):
        stake = round(max(100, bankroll * pct / 100), 2)
        src_tag = pick.get("source","?").upper()
        ev_val  = pick.get("ev", 0)
        ev_col  = "#00b894" if (ev_val or 0) >= 0 else "#dc2626"
        ev_label = f"{ev_val:+.3f}" if ev_val is not None else "N/A"

        # Header row
        hdr_row = [[
            Paragraph(f"<b>{rank_label}</b>", sCardLbl),
            Paragraph(f"<b>{pick['fixture']}</b>", sCardFx),
            Paragraph(f"[{src_tag}]", S("src", fontName="Helvetica", fontSize=8, textColor=C_WHITE, alignment=TA_CENTER)),
        ]]
        # Detail row 1: market + league/KO
        detail1 = [[
            Paragraph(f"<b>Market:</b> {pick['market']}", sCardBold),
            Paragraph(f"<b>What this means:</b> {pick.get('meaning','')}", sCardBd),
        ]]
        # Detail row 2: conf / odds / EV / stake
        detail2 = [[
            Paragraph(
                f"Confidence: <b>{pick['conf']}%</b>  |  "
                f"Odds: <b>{pick['odds']}</b>  |  "
                f"EV: <font color='{ev_col}'><b>{ev_label}</b></font>",
                sCardBd),
            Paragraph(f"Stake: <b>N{stake:,.0f}</b>  ({pct}% bankroll)", sCardBd),
        ]]
        # Detail row 3: kickoff + league
        detail3 = [[
            Paragraph(f"Kick-off: <b>{pick['kickoff']}</b>", sCardBd),
            Paragraph(f"Competition: {pick['league']}", sCardBd),
        ]]

        hdr_t = Table(hdr_row, colWidths=[40*mm, 115*mm, 20*mm])
        hdr_t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), hdr_color),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ]))
        body_t = Table(detail1 + detail2 + detail3, colWidths=[87*mm, 88*mm])
        body_t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), C_LIGHT),
            ("ROWBACKGROUNDS",(0,0), (-1,-1), [C_LIGHT, C_MID, C_LIGHT]),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("LINEBELOW",     (0,0), (-1,-2), 0.3, C_BORDER),
        ]))
        outer = Table([[hdr_t], [body_t]], colWidths=[175*mm])
        outer.setStyle(TableStyle([
            ("BOX",           (0,0), (-1,-1), 0.8, hdr_color),
            ("TOPPADDING",    (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
            ("LEFTPADDING",   (0,0), (-1,-1), 0),
            ("RIGHTPADDING",  (0,0), (-1,-1), 0),
        ]))
        return outer

# ═══════════════════════════════════════════════════════════════
    # SECTION 0 — SHEGE ANALYSIS MODE (Dynamic Arrays)
    # ═══════════════════════════════════════════════════════════════
    C_GEMINI      = colors.HexColor("#4f46e5")
    C_GEMINI_DARK = colors.HexColor("#312e81")
    C_GEMINI_LITE = colors.HexColor("#eef2ff")

    TIER_COLORS = {
        "bankers":    (colors.HexColor("#14532d"), colors.HexColor("#f0fdf4")),
        "value_gems": (C_BLUE,                     colors.HexColor("#eff6ff")),
        "wild_cards": (C_RED,                      colors.HexColor("#fef2f2")),
    }
    TIER_LABELS = {
        "bankers":    "THE BANKER",
        "value_gems": "THE VALUE GEM",
        "wild_cards": "THE WILD CARD",
    }

    if gemini_picks:
        ai_hdr_data = [[
            Paragraph(
                "✦  SEGUN 'SHEGE' ANALYSIS MODE  ✦",
                S("gai", fontName="Helvetica-Bold", fontSize=13,
                  alignment=TA_CENTER, textColor=C_WHITE)
            )
        ]]
        ai_hdr_t = Table(ai_hdr_data, colWidths=[175*mm])
        ai_hdr_t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), C_GEMINI_DARK),
            ("TOPPADDING",    (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
            ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ]))
        story.append(ai_hdr_t)
        story.append(Spacer(1, 1*mm))

        ai_intro = (
            "The following picks were autonomously selected by Segun (aka 'Shege'), our AI analyst, from "
            "all games that passed the pre-filter (confidence &gt;70%, odds &gt;1.30, +EV). "
            "Shege evaluates the full statistical payload and extracts only the fixtures that strictly "
            "meet the strategic definitions of a Banker, Value Gem, or Wild Card. "
            "These picks appear at the top of every report as the engine's single most-distilled recommendation set."
        )
        story.append(Paragraph(ai_intro,
            S("aiintro", fontName="Helvetica", fontSize=8.5,
              textColor=C_DKGREY, spaceAfter=3, leading=12,
              backColor=C_GEMINI_LITE,
              borderPadding=(5,8,5,8))))
        story.append(Spacer(1, 3*mm))

        for tier_key in ("bankers", "value_gems", "wild_cards"):
            picks_list = gemini_picks.get(tier_key, [])
            
            for pick in picks_list:
                hdr_col, body_col = TIER_COLORS[tier_key]
                label             = TIER_LABELS[tier_key]
                ev_val            = pick.get("ev", 0)
                ev_col            = "#00b894" if ev_val >= 0 else "#dc2626"

                ai_card_hdr = Table([[
                    Paragraph(f"<b>{label}</b>",
                        S("aclbl", fontName="Helvetica-Bold", fontSize=10, textColor=C_WHITE)),
                    Paragraph(f"<b>{pick.get('fixture','')}</b>",
                        S("acfx", fontName="Helvetica-Bold", fontSize=10, textColor=C_WHITE)),
                    Paragraph("SHEGE'S PICK",
                        S("acsrc", fontName="Helvetica-Bold", fontSize=7.5,
                          textColor=C_WHITE, alignment=TA_CENTER)),
                ]], colWidths=[40*mm, 115*mm, 20*mm])
                ai_card_hdr.setStyle(TableStyle([
                    ("BACKGROUND",    (0,0), (-1,-1), hdr_col),
                    ("TOPPADDING",    (0,0), (-1,-1), 6),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 6),
                    ("LEFTPADDING",   (0,0), (-1,-1), 8),
                    ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
                ]))

                sAC = S("acbd", fontName="Helvetica",      fontSize=8.5, textColor=C_DKGREY, leading=12)
                sAB = S("acbo", fontName="Helvetica-Bold", fontSize=8.5, textColor=C_DKGREY, leading=12)
                sAR = S("acre", fontName="Helvetica-Oblique", fontSize=8.5, textColor=C_DKGREY,
                        leading=13, spaceAfter=2)

                body_rows = [
                    [Paragraph(f"<b>Market:</b> {pick.get('market','')}", sAB),
                     Paragraph(f"<b>Kick-off:</b> {pick.get('kickoff','')}  |  "
                                f"<b>Competition:</b> {pick.get('league','')}", sAC)],
                    [Paragraph(
                        f"Confidence: <b>{pick.get('confidence','')}%</b>  |  "
                        f"Live Odds: <b>{pick.get('live_odds','')}</b>  |  "
                        f"Expected Odds: <b>{pick.get('expected_odds','')}</b>  |  "
                        f"EV: <font color='{ev_col}'><b>{float(ev_val):+.4f}</b></font>",
                        sAC),
                     Paragraph("", sAC)],
                    [Paragraph(
                        f"<b>Shege's Reasoning:</b> {pick.get('reasoning','')}",
                        sAR),
                     Paragraph("", sAC)],
                ]
                ai_card_body = Table(body_rows, colWidths=[87*mm, 88*mm])
                ai_card_body.setStyle(TableStyle([
                    ("BACKGROUND",    (0,0), (-1,-1), body_col),
                    ("ROWBACKGROUNDS",(0,0), (-1,-1), [body_col, C_MID, body_col]),
                    ("TOPPADDING",    (0,0), (-1,-1), 4),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                    ("LEFTPADDING",   (0,0), (-1,-1), 8),
                    ("SPAN",          (0,2), (1,2)),
                    ("LINEBELOW",     (0,0), (-1,-2), 0.3, C_BORDER),
                ]))

                ai_outer = Table([[ai_card_hdr], [ai_card_body]], colWidths=[175*mm])
                ai_outer.setStyle(TableStyle([
                    ("BOX",           (0,0), (-1,-1), 1.0, hdr_col),
                    ("TOPPADDING",    (0,0), (-1,-1), 0),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                    ("LEFTPADDING",   (0,0), (-1,-1), 0),
                    ("RIGHTPADDING",  (0,0), (-1,-1), 0),
                ]))
                story.append(KeepTogether([ai_outer, Spacer(1, 3*mm)]))

        story.append(Paragraph(
            "<i>Shege's picks are derived from the same GrindAlgo data that powers all other picks. "
            "He selects based on strict strategic criteria, not additional external data. "
            "Not financial advice.</i>",
            S("aidiscl", fontName="Helvetica-Oblique", fontSize=7.5,
              textColor=C_GREY, spaceAfter=2)))
        story.append(HRFlowable(width="100%", thickness=1.5, color=C_GEMINI))
        story.append(Spacer(1, 4*mm))
    else:
        story.append(Paragraph(
            "<i>⚠  Shege Analysis Mode was not available for this run. "
            "Standard GrindAlgo picks are shown below.</i>",
            S("gnota", fontName="Helvetica-Oblique", fontSize=8,
              textColor=C_GREY, spaceAfter=3)))
        story.append(Spacer(1, 2*mm))

    # ═══════════════════════════════════════════════════════════════
    # SECTION 1 — BANKERS
    # ═══════════════════════════════════════════════════════════════
    if bankers:
        story.append(Paragraph("SECTION 1 — BANKERS", sSec))
        banker_expl = (
            "<b>What is a Banker?</b> A Banker is our highest-conviction pick of the day. "
            "These are selections where the algorithm has identified a confluence of strong "
            "form data, head-to-head history, market probability and positive Expected Value (EV). "
            "Bankers are drawn exclusively from our <i>Proven Markets</i> — bet types that have "
            "shown consistent statistical reliability. "
            "A Banker does NOT mean guaranteed — it means the data alignment is exceptionally strong. "
            "<b>Recommended stake: a flat percentage of bankroll per Banker.</b> "
            "On a day with multiple Bankers, spread your stakes accordingly and avoid over-exposing "
            "your bankroll on a single event."
        )
        story.append(Paragraph(banker_expl, sBody))
        story.append(Spacer(1, 2*mm))
        for i, p in enumerate(bankers):
            story.append(pick_card(f"BANKER #{i+1}", p, C_DARK, "BANKER", 15))
            story.append(Spacer(1, 3*mm))

    # ═══════════════════════════════════════════════════════════════
    # SECTION 2 — VALUE GEMS
    # ═══════════════════════════════════════════════════════════════
    if value_gems:
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("SECTION 2 — VALUE GEMS", sSec))
        gem_expl = (
            "<b>What is a Value Gem?</b> Value Gems are picks where the algorithm detects a "
            "favourable gap between our modelled probability and the implied probability in the "
            "bookmaker's odds — this gap is captured as <b>Expected Value (EV)</b>. A positive EV "
            "means the bet is mathematically profitable in the long run if repeated consistently. "
            "Value Gems may cover a broader range of markets and leagues than Bankers, but each "
            "must still clear a minimum 70% confidence threshold and positive EV before selection. "
            "They are ranked from highest to lowest EV. "
            "<b>Recommended stake: a flat percentage of bankroll per Value Gem.</b> "
            "With multiple Value Gems in play, diversification is built-in — do not combine them "
            "into an accumulator unless you understand the compounded risk."
        )
        story.append(Paragraph(gem_expl, sBody))
        story.append(Spacer(1, 2*mm))
        for i, p in enumerate(value_gems):
            story.append(pick_card(f"VALUE GEM #{i+1}", p, C_BLUE, "VALUE GEM", 8))
            story.append(Spacer(1, 2*mm))

    # ═══════════════════════════════════════════════════════════════
    # SECTION 3 — WILD CARDS
    # ═══════════════════════════════════════════════════════════════
    if wild_cards:
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("SECTION 3 — WILD CARDS", sSec))
        wild_expl = (
            "<b>What is a Wild Card?</b> Wild Cards are speculative, higher-risk picks that "
            "sit just below our standard value threshold (65–69% confidence) but offer "
            "odds of 2.00 or greater, making them attractive from a risk-reward perspective. "
            "These picks often involve less-covered leagues, unusual markets, or fixtures where "
            "data coverage is thinner — the algorithm still sees a statistical edge, but with "
            "greater uncertainty. Wild Cards should be treated as <i>optional additions</i> to "
            "your betting card, not core plays. "
            "<b>Recommended stake: a flat percentage of bankroll per Wild Card.</b> "
            "Never chase Wild Cards if your bankroll is under pressure."
        )
        story.append(Paragraph(wild_expl, sBody))
        story.append(Spacer(1, 2*mm))
        for i, p in enumerate(wild_cards):
            story.append(pick_card(f"WILD CARD #{i+1}", p, C_RED, "WILD CARD", 5))
            story.append(Spacer(1, 2*mm))

    # ── Stake Summary Table ───────────────────────────────────────
    if total_picks > 0:
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("TODAY'S STAKE PLAN", sSec))
        stake_expl = (
            "The table below summarises all recommended stakes for today based on your current "
            f"bankroll of <b>N{bankroll:,.0f}</b>. Total maximum exposure across all picks is shown. "
            "You are not required to place all picks — always exercise your own judgement."
        )
        story.append(Paragraph(stake_expl, sBody))
        story.append(Spacer(1, 2*mm))

        shdr = ["#", "Tier", "Fixture", "Market", "Conf%", "Odds", "EV", "Stake (N)"]
        srows = [shdr]
        total_stake = 0
        flat_print_pct = int(FLAT_STAKE_PCT * 100)
        for idx, (tier_name, pct, picks_list, col) in enumerate([
            ("Banker", flat_print_pct, bankers or [], C_DARK),
            ("Value Gem", flat_print_pct, value_gems or [], C_BLUE),
            ("Wild Card", flat_print_pct, wild_cards or [], C_RED),
        ]):
            for i, p in enumerate(picks_list):
                stake = round(max(100, bankroll * pct / 100), 2)
                total_stake += stake
                srows.append([
                    str(len(srows)),
                    tier_name,
                    p["fixture"][:30],
                    p["market"],
                    f"{p['conf']}%",
                    str(p["odds"]),
                    f"{p.get('ev'):+.3f}" if p.get("ev") is not None else "",
                    f"N{stake:,.0f}",
                ])
        srows.append(["", "", "", "", "", "", "TOTAL EXPOSURE", f"N{total_stake:,.0f}"])

        st = Table(srows, colWidths=[8*mm, 20*mm, 52*mm, 30*mm, 14*mm, 14*mm, 16*mm, 21*mm])
        tst = [
            ("BACKGROUND",    (0,0), (-1,0),  C_DARK),
            ("TEXTCOLOR",     (0,0), (-1,0),  C_WHITE),
            ("FONTNAME",      (0,0), (-1,0),  "Helvetica-Bold"),
            ("FONTSIZE",      (0,0), (-1,-1), 7.5),
            ("TOPPADDING",    (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
            ("LEFTPADDING",   (0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS",(0,1), (-1,-2), [C_WHITE, C_LIGHT]),
            ("BACKGROUND",    (0,-1),(-1,-1), C_MID),
            ("FONTNAME",      (0,-1),(-1,-1), "Helvetica-Bold"),
            ("GRID",          (0,0), (-1,-1), 0.3, C_BORDER),
            ("LINEABOVE",     (0,-1),(-1,-1), 1,   C_DARK),
        ]
        st.setStyle(TableStyle(tst))
        story.append(st)
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            f"<i>Maximum total exposure today: N{total_stake:,.0f} ({total_stake/bankroll*100:.1f}% of bankroll). "
            f"This is across {total_picks} independent picks on {total_picks} separate fixtures.</i>",
            sItal))

    # ═══════════════════════════════════════════════════════════════
    # PAGE 2 — FULL MARKET SCORECARD
    # ═══════════════════════════════════════════════════════════════
    story.append(PageBreak())
    n = len(all_scored) if all_scored else 0
    story.append(Paragraph(f"FULL MARKET SCORECARD — ALL {n} GAMES ANALYSED TODAY", sSec))

    scorecard_expl = (
        "Every fixture analysed today is listed below with confidence scores across all 20 markets. "
        "Markets highlighted in <font color='#00b894'><b>green</b></font> have cleared their "
        "individual quality threshold. This scorecard is provided for transparency — you can see "
        "exactly how each game was evaluated by the algorithm. "
        "Source tags: <b>FD</b> = football-data.org (Big-5 + continental leagues), "
        "<b>APS</b> = API-Football fixtures, predictions, odds, events, and results. "
        "Use this section to do your own research on fixtures that interest you."
    )
    story.append(Paragraph(scorecard_expl, sBody))
    story.append(Spacer(1, 2*mm))

    if not all_scored:
        story.append(Paragraph("No games were scored today.", sBody))
    else:
        for fx, confs, real_odds in all_scored:
            src_tag = "FD" if fx.get("source") == "fd" else "APS"
            hdr = Table([[
                Paragraph(f"<b>[{src_tag}] {fx['fixture']}</b>",
                          S("fh", fontName="Helvetica-Bold", fontSize=9, textColor=C_WHITE)),
                Paragraph(fx.get("kickoff", ""),
                          S("ko", fontName="Helvetica", fontSize=8, textColor=C_WHITE)),
                Paragraph(fx.get("league", "")[:34],
                          S("lg", fontName="Helvetica", fontSize=8, textColor=C_WHITE)),
            ]], colWidths=[82*mm, 33*mm, 60*mm])
            hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (-1,-1), C_DARK),
                ("TOPPADDING",    (0,0), (-1,-1), 4),
                ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                ("LEFTPADDING",   (0,0), (-1,-1), 6),
                ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ]))
            items = sorted(confs.items(), key=lambda x: x[1], reverse=True)
            mkt_rows = []
            for i in range(0, len(items), 2):
                row = []
                for j in range(2):
                    if i + j < len(items):
                        m, c = items[i+j]
                        thresh = market_threshold(m)
                        qual   = "✔ " if c >= thresh else ""
                        bar    = "█" * (c // 10) + "░" * (10 - c // 10)
                        sty    = sMktGreen if c >= thresh else sMktDark
                        row.append(Paragraph(f"{qual}<b>{m}</b>  {bar}  <b>{c}%</b>", sty))
                    else:
                        row.append(Paragraph("", sBodySm))
                mkt_rows.append(row)
            mt = Table(mkt_rows, colWidths=[87*mm, 88*mm])
            mt.setStyle(TableStyle([
                ("ROWBACKGROUNDS", (0,0), (-1,-1), [C_WHITE, C_LIGHT]),
                ("TOPPADDING",    (0,0), (-1,-1), 2),
                ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                ("LEFTPADDING",   (0,0), (-1,-1), 5),
                ("GRID",          (0,0), (-1,-1), 0.2, C_BORDER),
            ]))
            story.append(KeepTogether([hdr, mt, Spacer(1, 3*mm)]))

    # ═══════════════════════════════════════════════════════════════
    # PAGE 3 — METHODOLOGY + DISCLAIMER
    # ═══════════════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("METHODOLOGY & GLOSSARY", sSec))

    meth_txt = (
        "The GrindAlgo v8 engine produces each daily report through a four-phase pipeline:\n\n"
        "<b>Phase 1 — Fixture Collection:</b> All matches for the target date are fetched from "
        "football-data.org (FD) across the Premier League, Championship, La Liga, Bundesliga, "
        "Serie A, Ligue 1, Eredivisie, Primeira Liga, Champions League, Europa League, MLS and more. "
        "FD requests are rate-limited and free.\n\n"
        "<b>Phase 2 — API-Football Fixtures:</b> A single API-Football request fetches all "
        "remaining fixtures for the date worldwide. The engine then filters for VIP leagues "
        "(Conference League, Super Lig, Pro League, Argentine Primera, Allsvenskan, Liga MX etc.) "
        "and deduplicates against the FD dataset. This uses exactly 1 API request.\n\n"
        "<b>Phase 3 — Form Scoring (FD games):</b> For every FD fixture, team form is fetched "
        "from FD's match history endpoint — up to 12 recent results per team. The 19-parameter "
        "engine converts form data into confidence scores across 20 betting markets.\n\n"
        "<b>Phase 4 — Prediction Scoring (API-only games):</b> For fixtures only available on "
        "API-Football's /predictions endpoint is called (1 request per fixture). The returned "
        "attack/defence comparison data is mapped into the same 19-parameter model. API-Football "
        "win percentages are blended in at 30% weight.\n\n"
        "<b>Odds Blending:</b> Where real bookmaker odds are available from API-Football, they "
        "are blended into the final confidence score at 60% weight, reducing pure-model bias and "
        "aligning picks with market reality.\n\n"
        "<b>Expected Value (EV):</b> EV = (Confidence% × Decimal Odds) − 1. A positive EV means "
        "the bet is theoretically profitable over many repetitions. EV is used for ranking Value "
        "Gems and Wild Cards."
    )
    story.append(Paragraph(meth_txt, sBody))
    story.append(Spacer(1, 3*mm))

    story.append(Paragraph("GLOSSARY OF TERMS", sSecSm))
    glossary = [
        ["Term", "Definition"],
        ["Confidence %", "Model's estimated probability that this bet wins (0–95%). NOT a guarantee."],
        ["EV (Expected Value)", "Mathematical edge. Positive EV = profitable long-term. Negative EV = avoid."],
        ["Odds", "Decimal odds. Use bookmaker odds; model estimates are shown where live odds unavailable."],
        ["Banker", "Highest-conviction pick. Proven market, Conf ≥72%, EV positive, odds 1.25–3.50."],
        ["Value Gem", "Strong EV pick. Conf ≥70%, positive EV, odds 1.35–3.50. Any market."],
        ["Wild Card", "Speculative pick. Conf 65–69%, odds ≥2.00, positive EV. Higher risk."],
        ["Proven Market", "Markets with statistically consistent reliability: Over 1.5, AH Home +0.5, GG/BTTS, Under 3.5, First to Score H."],
        ["FD", "Data from football-data.org — Big-5 leagues and continental competitions."],
        ["APS", "Data from API-Football — fixtures, predictions, odds, events, and results."],
        ["Stake %", "Percentage of current bankroll recommended per pick. Flat bet sizing."],
        ["WAT", "West Africa Time (UTC+1). All kick-off times shown in WAT."],
    ]
    gt = Table(glossary, colWidths=[45*mm, 130*mm])
    gt.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  C_DARK),
        ("TEXTCOLOR",     (0,0), (-1,0),  C_WHITE),
        ("FONTNAME",      (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTNAME",      (0,1), (0,-1),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [C_WHITE, C_LIGHT]),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
        ("GRID",          (0,0), (-1,-1), 0.3, C_BORDER),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
    ]))
    story.append(gt)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph("DISCLAIMER", sSecSm))
    disc = (
        "This report is produced by an automated algorithm for informational and analytical purposes only. "
        "It does not constitute financial advice, and no pick herein is guaranteed to win. "
        "Sports betting involves significant financial risk. You should only bet amounts you can "
        "afford to lose. The GrindAlgo team accepts no liability for any losses incurred as a result "
        "of acting on this report. Please gamble responsibly. If you feel gambling is becoming a problem, "
        "contact GamCare (www.gamcare.org.uk) or your local responsible gambling authority."
    )
    story.append(Paragraph(disc, sItal))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"THE GRIND ALGO  |  {target_date}  |  API-Football  |  "
        f"Generated {now_wat.strftime('%d %b %Y %H:%M WAT')}  |  Not financial advice.",
        sFoot))

    doc.build(story)
    log.info(f"PDF built: {temp_path}")

    # Upload to Drive (primary)
    try:
        q     = f"name='{DRIVE_FOLDER}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        items = drive.files().list(q=q,fields="files(id)").execute().get("files",[])
        fid   = items[0]["id"] if items else drive.files().create(
            body={"name":DRIVE_FOLDER,"mimeType":"application/vnd.google-apps.folder"},fields="id").execute()["id"]
        fname = f"GrindAlgo_{target_date}.pdf"
        exist = drive.files().list(q=f"name='{fname}' and '{fid}' in parents and trashed=false",
                                   fields="files(id)").execute().get("files",[])
        media = MediaFileUpload(temp_path,mimetype="application/pdf",resumable=True)
        if exist: drive.files().update(fileId=exist[0]["id"],media_body=media).execute()
        else: drive.files().create(body={"name":fname,"parents":[fid]},media_body=media,fields="id").execute()
        log.info(f"PDF in Drive: {DRIVE_FOLDER}/{fname}")
    except Exception as e:
        log.warning(f"Drive upload failed: {e}")

    # Email (secondary)
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.application import MIMEApplication
        pw = os.environ.get("GMAIL_APP_PASSWORD")
        if EMAIL_RECIPIENT and pw:
            msg=MIMEMultipart(); msg["Subject"]=f"GrindAlgo v8 - {target_date}"
            msg["From"]=EMAIL_RECIPIENT; msg["To"]=EMAIL_RECIPIENT
            msg.attach(MIMEText(
                f"GrindAlgo Daily Report — {target_date}\n\n"
                f"Picks today: {total_picks}  "
                f"(Bankers: {n_bankers}, Value Gems: {n_gems}, Wild Cards: {n_wilds})\n"
                f"Bankroll: N{bankroll:,.0f}\n\n"
                f"Full report attached.\n\nNot financial advice. Gamble responsibly.",
                "plain"))
            with open(temp_path,"rb") as fh:
                att=MIMEApplication(fh.read(),_subtype="pdf")
                att.add_header("Content-Disposition","attachment",filename=f"GrindAlgo_{target_date}.pdf")
                msg.attach(att)
            s=smtplib.SMTP_SSL("smtp.gmail.com",465); s.login(EMAIL_RECIPIENT,pw); s.send_message(msg); s.quit()
            log.info("PDF emailed")
    except Exception as e:
        log.warning(f"Email failed: {e}")

# ── MAIN RUNNER ───────────────────────────────────────────────────
def run_daily_algo():
    log.info("=== GrindAlgo API-Football Run ===")
    gc, drive, sheets = get_google_services()
    bankroll = get_bankroll(sheets)
    log.info(f"Bankroll: N{bankroll:,.0f}")

    now_wat     = datetime.now(WAT)
    tomorrow    = (now_wat+timedelta(days=1)).strftime("%Y-%m-%d")
    target_date = os.environ.get("OVERRIDE_DATE", tomorrow)
    log.info(f"WAT: {now_wat.strftime('%Y-%m-%d %H:%M')} | Target: {target_date}")

    fixtures = fetch_aps_fixtures(target_date)

    MAX_FIXTURES = int(os.environ.get("APS_MAX_FIXTURES", "90"))
    if len(fixtures) > MAX_FIXTURES:
        log.warning(f"Capping API-Football games: {len(fixtures)} -> {MAX_FIXTURES}")
        fixtures = fixtures[:MAX_FIXTURES]

    total = len(fixtures)
    log.info(f"Total API-Football games: {total}")

    if total == 0:
        log.info("No fixtures — rest day")
        return {"status":"rest_day","date":target_date,"picks_count":0}

    all_confs=[]; scored_fxs=[]; odds_list=[]

    # Score all tracked games with API-Football predictions + odds.
    log.info(f"Scoring {len(fixtures)} API-Football games...")
    for idx,fx in enumerate(fixtures):
        try:
            log.info(f"  [APS {idx+1}/{len(fixtures)}] {fx['fixture']}")
            pred_data = fetch_prediction_data(fx["aps_id"])
            if pred_data:
                teams_data = pred_data.get("teams",{})
                comparison = pred_data.get("comparison",{})
                h_comp = {"att":comparison.get("att",{}).get("home","50%"),
                          "def":comparison.get("def",{}).get("home","50%")}
                a_comp = {"att":comparison.get("att",{}).get("away","50%"),
                          "def":comparison.get("def",{}).get("away","50%")}
                hf_overall = fetch_team_recent_form(fx.get("hid"))
                af_overall = fetch_team_recent_form(fx.get("aid"))
                hf = fetch_team_recent_form(fx.get("hid"), venue="home") or hf_overall or map_aps_to_form(teams_data.get("home"),h_comp)
                af = fetch_team_recent_form(fx.get("aid"), venue="away") or af_overall or map_aps_to_form(teams_data.get("away"),a_comp)
                strength = league_strength_factor(fx)
                hf = apply_league_strength(hf, strength)
                af = apply_league_strength(af, strength)
                hf["attack_str"] = _percent_to_ratio(h_comp.get("att"), hf.get("attack_str", 0.5))
                af["attack_str"] = _percent_to_ratio(a_comp.get("att"), af.get("attack_str", 0.5))
                hf["defence_str"] = _percent_to_ratio(h_comp.get("def"), hf.get("defence_str", 0.5))
                af["defence_str"] = _percent_to_ratio(a_comp.get("def"), af.get("defence_str", 0.5))
                h2h = parse_aps_h2h(pred_data.get("h2h",[]),fx["hname"])
            else:
                hf=fetch_team_recent_form(fx.get("hid"), venue="home") or fetch_team_recent_form(fx.get("hid")) or _default_form()
                af=fetch_team_recent_form(fx.get("aid"), venue="away") or fetch_team_recent_form(fx.get("aid")) or _default_form()
                strength = league_strength_factor(fx)
                hf = apply_league_strength(hf, strength)
                af = apply_league_strength(af, strength)
                h2h = parse_aps_h2h([], fx["hname"])
            fx["home_recent_form"] = recent_form_summary(hf)
            fx["away_recent_form"] = recent_form_summary(af)
            fixture_context = build_fixture_context(fx, hf, af)
            fixture_context["h2h"] = h2h
            context_flags = set(fixture_context.get("flags") or [])
            context_flags.add("h2h_available" if int(h2h.get("games") or 0) >= 2 else "h2h_unavailable")
            fixture_context["flags"] = sorted(context_flags)
            team_news = fetch_fixture_team_news(fx.get("aps_id"), fx.get("hid"), fx.get("aid"))
            fx["fixture_context"] = fixture_context
            fx["team_news"] = team_news
            real_odds = get_api_football_odds(fx["aps_id"])
            corner_odds_available = any(key.startswith("Corners ") for key in real_odds)
            corner_profile = build_corner_profile(fx) if corner_odds_available else {}
            fx["corner_profile"] = corner_profile
            confs = score_fixture(
                hf,
                af,
                h2h,
                real_odds,
                api_preds=pred_data,
                corner_profile=corner_profile,
                fixture_context=fixture_context,
            )
            confs = apply_context_adjustments(confs, fixture_context, team_news)
            all_confs.append(confs); scored_fxs.append(fx); odds_list.append(real_odds)
            log.info("    APS scored OK")
        except Exception as e:
            log.warning(f"APS score error {fx['fixture']}: {e}")
            confs = score_fixture(_default_form(),_default_form(),parse_aps_h2h([], fx.get("hname", "")),{})
            all_confs.append(confs); scored_fxs.append(fx); odds_list.append({})
        time.sleep(0.5)   # Paid tier limit

    if not all_confs:
        return {"status":"no_data","date":target_date,"picks_count":0}

    bankers, value_gems, wild_cards = select_picks(all_confs, scored_fxs, odds_list)
    picks_count = record_to_sheets(sheets, bankers, value_gems, wild_cards, target_date, bankroll)

    # ── SHEGE ANALYSIS MODE ──────────────────────────────────────
    shege_picks = None
    try:
        from .gemini_analyst import filter_ev_candidates, call_shege_analyst
        ev_candidates = filter_ev_candidates(all_confs, scored_fxs, odds_list)
        shege_picks  = call_shege_analyst(ev_candidates)
    except Exception as _gem_err:
        log.error(f"Shege analysis failed (non-fatal): {_gem_err}")
    # ── END SHEGE ────────────────────────────────────────────────

    generate_and_upload_pdf(drive, bankers, value_gems, wild_cards, target_date, bankroll,
                            all_scored=list(zip(scored_fxs,all_confs,odds_list)),
                            gemini_picks=shege_picks)

    result = {"status":"success","date":target_date,
              "fd_fixtures":0,"aps_fixtures":len(fixtures),
              "total_scored":len(all_confs),"picks_count":picks_count,
              "no_bet": picks_count == 0,
              "publish_policy":"best_available_with_risk_controls",
              "market_count":sum(len(confs) for confs in all_confs),
              "markets_70_plus":sum(1 for confs in all_confs for value in confs.values() if value >= 70),
              "markets_65_plus":sum(1 for confs in all_confs for value in confs.values() if value >= 65),
              "fixture_summaries":serialize_fixture_summaries(scored_fxs, all_confs, odds_list),
              "bankers":len(bankers or []),"value_gems":len(value_gems or []),
              "wild_cards":len(wild_cards or []),"bankroll":bankroll,
              "selected_picks": serialize_selected_picks(
                  bankers, value_gems, wild_cards, target_date, bankroll
              )}
    log.info(f"Run complete: {result}")
    return result

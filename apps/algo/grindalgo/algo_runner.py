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
import logging
import requests
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

# ── API-FOOTBALL TRACKED LEAGUES ─────────────────────────────────
APS_TRACKED_LEAGUES = {
    848: "UEFA Europa Conference League",
    10:  "Club Friendlies",
    21:  "International Friendlies",
    71:  "Serie B (Brazil)",
    253: "MLS",
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
    "DC: 1X":"Home win or draw","DC: X2":"Away win or draw","DC: 12":"Home or Away win",
    "DNB Home":"Home win (Draw = refund)","DNB Away":"Away win (Draw = refund)",
    "Home CS":"Home team keeps clean sheet","Away CS":"Away team keeps clean sheet",
    "AH Home +0.5":"Home win or draw (+0.5)","AH Away +0.5":"Away win or draw (+0.5)",
    "First to Score H":"Home team scores first","First to Score A":"Away team scores first",
}

# ── HELPERS ───────────────────────────────────────────────────────
def _to_wat(utc_str):
    if not utc_str: return ""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z","+00:00"))
        return dt.astimezone(WAT).strftime("%H:%M WAT")
    except Exception:
        return utc_str

def normalize(name):
    return ''.join(c for c in unicodedata.normalize('NFD', name)
                   if unicodedata.category(c) != 'Mn').lower()

def fuzzy(a, b, n=4):
    a, b = normalize(a), normalize(b)
    return a[:n] in b or b[:n] in a

def aps_get(path, params=None, timeout=20):
    if not APS_KEY:
        raise RuntimeError("APS_KEY is not configured")
    headers = {"x-apisports-key": APS_KEY}
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
            "hid":      f["teams"]["home"]["id"],
            "aid":      f["teams"]["away"]["id"],
            "league":   f["league"]["name"],
            "code":     str(league_id),
            "kickoff":  _to_wat(f["fixture"].get("date","")),
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
    return {"wins":3,"avg_scored":1.4,"avg_conceded":1.2,
            "btts_count":4,"over25_count":3,"clean_sheets":2,
            "games":8,"streak":0,"attack_str":0.5,"defence_str":0.5}

# ── MAP API-FOOTBALL PREDICTIONS -> FORM METRICS ─────────────────
def map_aps_to_form(team_pred, comp_side):
    if not team_pred: return _default_form()
    last_5   = team_pred.get("last_5",{}) or {}
    form_str = (last_5.get("form") or "WDWDW")[:5]
    games    = max(len(form_str),5)
    wins     = form_str.count("W")
    streak   = 0
    for r_ in reversed(form_str):
        if r_==form_str[-1]: streak+=1
        else: break
    if form_str and form_str[-1]=="L": streak=-streak

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

    return {"wins":wins,"avg_scored":avg_scored,"avg_conceded":avg_conceded,
            "btts_count":btts_count,"over25_count":over25_count,
            "clean_sheets":clean_sheets,"games":games,"streak":streak,
            "attack_str":attack_str,"defence_str":defence_str}

def parse_aps_h2h(h2h_list, hname):
    if not h2h_list:
        return {"games":5,"t1w":2,"o25":3,"btts":2}
    games=t1w=o25=btts=0
    for m in h2h_list:
        try:
            hg = m.get("goals",{}).get("home") or 0
            ag = m.get("goals",{}).get("away") or 0
            mh = normalize(m.get("teams",{}).get("home",{}).get("name",""))
            if hg is None or ag is None: continue
            games+=1
            if hg+ag>2: o25+=1
            if hg>0 and ag>0: btts+=1
            if fuzzy(hname,mh) and hg>ag: t1w+=1
            elif not fuzzy(hname,mh) and ag>hg: t1w+=1
        except Exception: continue
    if not games: return {"games":5,"t1w":2,"o25":3,"btts":2}
    return {"games":games,"t1w":t1w,"o25":o25,"btts":btts}

# ── 19-PARAMETER CONFIDENCE SCORER ───────────────────────────────
CONF_DEFLATOR = 0.84
W = {"f1":8,"f2":10,"f3":12,"f4":8,"f5":6,"f6":8,"f7":12,"f8":5,"f9":8,
     "f10":8,"f11":6,"f12":6,"f13":6,"f14":10,"f15":9,"f16":6,"f17":7,
     "f18":8,"f19":7}
MAX_W = sum(W.values())

def score_fixture(hf, af, h2h, real_odds, api_preds=None):
    def sf(v,strong,mod,w):
        return w if v>=strong else round(w*0.67) if v>=mod else round(w*0.33)

    diff = hf["avg_scored"] - af["avg_scored"]
    g    = max(h2h.get("games",1),1)
    h2w  = h2h.get("t1w",2)/g
    o25r = h2h.get("o25",3)/g

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
           sf(2.7,2.7,2.3,W["f13"])+sf((g-h2h.get("t1w",2))/g,0.6,0.4,W["f14"])+
           sf(kap_a,0.1,0.0,W["f15"])+sf(0,2,0,W["f16"])+
           sf(8,3,2,W["f17"])+sf(a_atk-0.5,0.1,0.0,W["f18"])+
           sf(af.get("streak",0),3,1,W["f19"]))
    ac = round(min(95,max(0,(afs/MAX_W)*100*CONF_DEFLATOR)))

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
    dc12 = min(82,hc+ac)

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

    return {
        "Home Win":hc,"Away Win":ac,"Draw":max(5,100-hc-ac),
        "Over 1.5":o15,"Under 1.5":100-o15,
        "Over 2.5":o25,"Under 2.5":100-o25,
        "Under 3.5":min(90,100-round(o25*0.55)),
        "GG / BTTS Yes":gg,"GG + Over 2.5":round(gg*o25/100),
        "DC: 1X":min(95,hc+max(5,100-hc-ac)),
        "DC: X2":min(95,ac+max(5,100-hc-ac)),
        "DC: 12":dc12,"DNB Home":hc,"DNB Away":ac,
        "Home CS":hcs,"Away CS":acs,
        "AH Home +0.5":min(95,hc+max(5,100-hc-ac)),
        "AH Away +0.5":min(95,ac+max(5,100-hc-ac)),
        "First to Score H":fts_h,"First to Score A":fts_a,
    }

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
    if key not in odds or odd > odds[key]:
        odds[key] = odd

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
                bet_name = normalize(bet.get("name", ""))
                for value in bet.get("values", []) or []:
                    label = normalize(value.get("value", ""))
                    odd = value.get("odd")

                    if bet_name in ("match winner", "fulltime result", "1x2"):
                        if label in ("home", "1"):
                            _remember_odd(odds, "hw", odd)
                        elif label in ("away", "2"):
                            _remember_odd(odds, "aw", odd)
                        elif label in ("draw", "x"):
                            _remember_odd(odds, "d", odd)
                    elif "goals over/under" in bet_name or "over/under" in bet_name:
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
                    elif "both teams score" in bet_name:
                        if label == "yes":
                            _remember_odd(odds, "btts_yes", odd)
                        elif label == "no":
                            _remember_odd(odds, "btts_no", odd)
                    elif "double chance" in bet_name:
                        if label in ("home/draw", "1x"):
                            _remember_odd(odds, "1x", odd)
                        elif label in ("draw/away", "x2"):
                            _remember_odd(odds, "x2", odd)
                        elif label in ("home/away", "12"):
                            _remember_odd(odds, "12", odd)

    _odds_cache[fixture_id] = odds
    return odds

# ── PICK SELECTOR ─────────────────────────────────────────────────
PROVEN_MARKETS = {"First to Score H","Over 1.5","DC: 1X","AH Home +0.5","Under 3.5","GG / BTTS Yes"}
MARKET_THRESHOLDS = {
    "Home Win":64,"Away Win":85,"Draw":68,"Over 1.5":58,"Under 1.5":68,
    "Over 2.5":80,"Under 2.5":65,"Under 3.5":60,"Over 3.5":68,
    "GG / BTTS Yes":72,"GG + Over 2.5":75,"No Goal":999,
    "DC: 1X":60,"DC: X2":80,"DC: 12":82,"DNB Home":64,"DNB Away":85,
    "Home CS":65,"Away CS":72,"AH Home +0.5":58,"AH Away +0.5":78,
    "First to Score H":55,"First to Score A":85,
}
MIN_ODDS=1.25; BANKER_MIN=72; VALUE_MIN=70; WILD_MIN=65
# Scale targets: aim for 10–15 picks on a busy fixture day
MAX_BANKERS=3; MAX_VALUE_GEMS=8; MAX_WILD_CARDS=5
TARGET_MIN=10; TARGET_MAX=15
ODDS_KEYS_MAP = {
    "Home Win":"hw","Away Win":"aw","Draw":"d","Over 1.5":"o15",
    "Under 1.5":"u15","Over 2.5":"o25","Under 2.5":"u25",
    "Under 3.5":"u35","Over 3.5":"o35","GG / BTTS Yes":"btts_yes",
    "DC: 1X":"1x","DC: X2":"x2","DC: 12":"12",
}

def est_odds(c): return round(1/max(c/100,0.05)*1.05,2)

def recent_form_summary(form):
    games = max(form.get("games", 0), 1)
    return {
        "games": form.get("games", 0),
        "wins": form.get("wins", 0),
        "avg_scored": form.get("avg_scored", 0),
        "avg_conceded": form.get("avg_conceded", 0),
        "clean_sheets": form.get("clean_sheets", 0),
        "btts_rate": round(form.get("btts_count", 0) / games * 100, 1),
        "over25_rate": round(form.get("over25_count", 0) / games * 100, 1),
        "streak": form.get("streak", 0),
    }

def pick_reasoning(pick):
    return (
        f"{pick.get('market')} rates at {pick.get('conf')}% confidence with "
        f"{pick.get('odds')} odds and {pick.get('ev'):+.3f} expected value. "
        f"The model prefers this market from the available fixture markets."
    )

def pick_verdict(pick):
    tier = pick.get("tier") or "pick"
    if pick.get("proven"):
        return f"{tier.replace('_', ' ').title()} backed by a proven market profile."
    return f"{tier.replace('_', ' ').title()} selected for positive value and confidence."

def select_picks(all_confs, scored_fxs, odds_list):
    pool=[]
    for fx,confs,real_odds in zip(scored_fxs,all_confs,odds_list):
        for market,conf in confs.items():
            if conf<WILD_MIN: continue
            key  = ODDS_KEYS_MAP.get(market)
            odds = (real_odds.get(key) if key else None) or est_odds(conf)
            if odds<MIN_ODDS: continue
            ev = round((conf/100)*odds-1,3)
            pool.append({"fixture":fx["fixture"],"league":fx["league"],
                         "code":fx.get("code","?"),"kickoff":fx["kickoff"],
                         "home_team":fx.get("hname",""),"away_team":fx.get("aname",""),
                         "market":market,"meaning":MARKET_MEANINGS.get(market,""),
                         "conf":conf,"odds":odds,"ev":ev,
                         "proven":market in PROVEN_MARKETS,
                         "hname":fx["hname"],"aname":fx["aname"],
                         "match_id":fx.get("match_id"),
                         "source":fx.get("source","?"),
                         "home_recent_form":fx.get("home_recent_form",{}),
                         "away_recent_form":fx.get("away_recent_form",{})})

    # ── BANKERS: up to MAX_BANKERS, one per fixture, proven markets ──
    banker_cands = sorted([p for p in pool if p["proven"] and p["conf"]>=BANKER_MIN and 1.25<=p["odds"]<=3.50],
                          key=lambda x:(x["conf"],x["ev"]),reverse=True)
    bankers=[]; used_b=set()
    for p in banker_cands:
        if p["fixture"] not in used_b:
            bankers.append(p); used_b.add(p["fixture"])
        if len(bankers)>=MAX_BANKERS: break

    # ── VALUE GEMS: up to MAX_VALUE_GEMS, one per fixture, EV-ranked ──
    value_cands = sorted([p for p in pool if p["conf"]>=VALUE_MIN and p["ev"]>0
                          and 1.35<=p["odds"]<=3.50 and p["fixture"] not in used_b],
                         key=lambda x:x["ev"],reverse=True)
    seen_v=set(); value_gems=[]
    for p in value_cands:
        if p["fixture"] not in seen_v:
            seen_v.add(p["fixture"]); value_gems.append(p)
        if len(value_gems)>=MAX_VALUE_GEMS: break

    # ── WILD CARDS: up to MAX_WILD_CARDS, higher-odds speculative picks ──
    used_all = used_b | seen_v
    wild_cands = sorted([p for p in pool if WILD_MIN<=p["conf"]<VALUE_MIN
                         and p["odds"]>=2.00 and p["ev"]>0 and p["fixture"] not in used_all],
                        key=lambda x:x["ev"],reverse=True)
    seen_w=set(); wild_cards=[]
    for p in wild_cands:
        if p["fixture"] not in seen_w:
            seen_w.add(p["fixture"]); wild_cards.append(p)
        if len(wild_cards)>=MAX_WILD_CARDS: break

    # ── FILL UP: if no wild cards, expand bankers & value gems to hit TARGET ──
    total = len(bankers)+len(value_gems)+len(wild_cards)
    if total < TARGET_MIN and not wild_cards:
        used_all2 = used_b | seen_v
        # more bankers (relax to conf>=68)
        for p in sorted([p for p in pool if p["proven"] and p["conf"]>=68 and 1.25<=p["odds"]<=4.00
                         and p["fixture"] not in used_all2],key=lambda x:(x["conf"],x["ev"]),reverse=True):
            if p["fixture"] not in used_all2:
                bankers.append(p); used_all2.add(p["fixture"])
            if len(bankers)>=MAX_BANKERS+1 or len(bankers)+len(value_gems)>=TARGET_MAX: break
        # more value gems (slightly relax EV threshold)
        for p in sorted([p for p in pool if p["conf"]>=68 and p["ev"]>-0.02
                         and 1.30<=p["odds"]<=4.00 and p["fixture"] not in used_all2],
                        key=lambda x:x["ev"],reverse=True):
            if p["fixture"] not in used_all2:
                value_gems.append(p); used_all2.add(p["fixture"])
            if len(bankers)+len(value_gems)>=TARGET_MAX: break

    log.info(f"Picks selected — Bankers:{len(bankers)} ValueGems:{len(value_gems)} WildCards:{len(wild_cards)}")
    for tier, selected in (("banker", bankers), ("value_gem", value_gems), ("wild_card", wild_cards)):
        for pick in selected:
            pick["tier"] = tier
            pick["reasoning"] = pick_reasoning(pick)
            pick["model_verdict"] = pick_verdict(pick)
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
                     f"{pick['conf']}%",pick["odds"],f"{pick['ev']:+.3f}",
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
                "league": pick.get("league", ""),
                "kickoff": pick.get("kickoff", ""),
                "match_id": str(pick.get("match_id") or ""),
                "tier": tier,
                "market": pick.get("market", ""),
                "meaning": pick.get("meaning", ""),
                "reasoning": pick.get("reasoning", ""),
                "model_verdict": pick.get("model_verdict", ""),
                "home_recent_form": pick.get("home_recent_form", {}),
                "away_recent_form": pick.get("away_recent_form", {}),
                "confidence": pick.get("conf", 0),
                "odds": pick.get("odds", 0),
                "ev": pick.get("ev", 0),
                "stake": round(max(100, bankroll * FLAT_STAKE_PCT), 2),
                "source": "FD" if pick.get("source") == "fd" else "APS",
            })
    return picks

def serialize_fixture_summaries(scored_fxs, all_confs):
    summaries = []
    for fx, confs in zip(scored_fxs, all_confs):
        summaries.append({
            "fixture": fx.get("fixture", ""),
            "home_team": fx.get("hname", ""),
            "away_team": fx.get("aname", ""),
            "league": fx.get("league", ""),
            "kickoff": fx.get("kickoff", ""),
            "match_id": str(fx.get("match_id") or ""),
            "market_count": len(confs),
            "markets_70_plus": sum(1 for value in confs.values() if value >= 70),
            "markets_65_plus": sum(1 for value in confs.values() if value >= 65),
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
        ev_col  = "#00b894" if ev_val >= 0 else "#dc2626"

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
                f"EV: <font color='{ev_col}'><b>{ev_val:+.3f}</b></font>",
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
                    f"{p.get('ev',0):+.3f}",
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
                        thresh = MARKET_THRESHOLDS.get(m, 68)
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
        ["Proven Market", "Markets with statistically consistent reliability: Over 1.5, DC:1X, AH Home +0.5, GG/BTTS, Under 3.5, First to Score H."],
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
                hf = map_aps_to_form(teams_data.get("home"),h_comp)
                af = map_aps_to_form(teams_data.get("away"),a_comp)
                h2h = parse_aps_h2h(pred_data.get("h2h",[]),fx["hname"])
            else:
                hf=_default_form(); af=_default_form()
                h2h={"games":5,"t1w":2,"o25":3,"btts":2}
            fx["home_recent_form"] = recent_form_summary(hf)
            fx["away_recent_form"] = recent_form_summary(af)
            real_odds = get_api_football_odds(fx["aps_id"])
            confs = score_fixture(hf,af,h2h,real_odds,api_preds=pred_data)
            all_confs.append(confs); scored_fxs.append(fx); odds_list.append(real_odds)
            log.info("    APS scored OK")
        except Exception as e:
            log.warning(f"APS score error {fx['fixture']}: {e}")
            confs = score_fixture(_default_form(),_default_form(),{"games":5,"t1w":2,"o25":3,"btts":2},{})
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
              "market_count":sum(len(confs) for confs in all_confs),
              "markets_70_plus":sum(1 for confs in all_confs for value in confs.values() if value >= 70),
              "markets_65_plus":sum(1 for confs in all_confs for value in confs.values() if value >= 65),
              "fixture_summaries":serialize_fixture_summaries(scored_fxs, all_confs),
              "bankers":len(bankers or []),"value_gems":len(value_gems or []),
              "wild_cards":len(wild_cards or []),"bankroll":bankroll,
              "selected_picks": serialize_selected_picks(
                  bankers, value_gems, wild_cards, target_date, bankroll
              )}
    log.info(f"Run complete: {result}")
    return result

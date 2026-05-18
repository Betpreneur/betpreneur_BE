# ================================================================
# RESULTS RUNNER — 6:30am WAT daily run (API-SPORTS UPGRADE)
# ================================================================
# DAILY SWEEP ONLY: Checks "yesterday's" games.
# Fetches real match scores, marks picks W/L, updates bankroll.
# Safely handles upfront stake deductions & "Actual Odds" integration.
# Uses Batch Updating to prevent Google Sheets 429 Quota Exhaustion.
# ================================================================

import os
import time
import logging
import requests
import unicodedata
import difflib
import gspread  # Global import for batch updating
from datetime import datetime, timedelta, timezone, date

log = logging.getLogger(__name__)
WAT = timezone(timedelta(hours=1))

# ── API-SPORTS CONFIG ──────────────────────────────────────────────
API_SPORTS_KEY = os.environ.get("API_SPORTS_KEY", "")
API_HOST = "v3.football.api-sports.io"
BASE_URL = f"https://{API_HOST}"
HEADERS = {
    "x-apisports-key": API_SPORTS_KEY,
}

# --- ACCENT STRIPPER ---
def normalize_name(name):
    """Removes accents (ã, é, ó) and converts to lowercase for perfect matching."""
    if not name: return ""
    return ''.join(c for c in unicodedata.normalize('NFD', name)
                  if unicodedata.category(c) != 'Mn').lower()

# ── 1. MARKET RESULT CHECKER ──────────────────────────────────────
def check_market(market, hg, ag, home_team=None, away_team=None, first_to_score_team=None):
    """Returns True/False/None (void) for a market given the score."""
    
    if market == "DNB Home":
        if hg == ag: return None
        return hg > ag
    if market == "DNB Away":
        if hg == ag: return None
        return ag > hg
        
    # 🚨 SMARTER First to Score Logic (Bypasses missing API timelines)
    if market == "First to Score H":
        if hg == 0 and ag == 0: return False # 0-0 is a loss
        if hg > 0 and ag == 0: return True   # Home scored, Away didn't. Timeline not needed!
        if ag > 0 and hg == 0: return False  # Away scored, Home didn't. Timeline not needed!
        
        # If BOTH scored (e.g. 1-1, 2-1), we MUST rely on the timeline
        if first_to_score_team:
            return first_to_score_team == home_team
        return None # Void ONLY if the game had goals from both sides but API lacks timeline
        
    if market == "First to Score A":
        if hg == 0 and ag == 0: return False
        if ag > 0 and hg == 0: return True   
        if hg > 0 and ag == 0: return False  
        
        if first_to_score_team:
            return first_to_score_team == away_team
        return None 
        
    total = hg + ag
    checks = {
        "Home Win":         hg > ag,
        "Away Win":         ag > hg,
        "Draw":             hg == ag,
        "Over 1.5":         total >= 2,
        "Over 2.5":         total >= 3,
        "Over 3.5":         total >= 4,
        "Under 1.5":        total <= 1,
        "Under 2.5":        total <= 2,
        "Under 3.5":        total <= 3,
        "GG / BTTS Yes":    hg > 0 and ag > 0,
        "GG + Over 2.5":    hg > 0 and ag > 0 and total >= 3,
        "DC: 1X":           hg >= ag,
        "DC: X2":           ag >= hg,
        "DC: 12":           hg != ag,
        "Home CS":          ag == 0,
        "Away CS":          hg == 0,
        "Home Win to Nil":  hg > ag and ag == 0,
        "Away Win to Nil":  ag > hg and hg == 0,
        "AH Home -0.5":     hg > ag,
        "AH Home +0.5":     hg >= ag,
        "AH Away -0.5":     ag > hg,
        "AH Away +0.5":     ag >= hg,
    }
    return checks.get(market)

# ── 2. FETCH FIRST TO SCORE FROM API ──────────────────────────────
def get_first_scorer(fixture_id):
    """Hits the events endpoint to find the first valid goal."""
    try:
        url = f"{BASE_URL}/fixtures/events?fixture={fixture_id}"
        resp = requests.get(url, headers=HEADERS, timeout=10).json()
        events = resp.get("response", [])
        
        for event in events:
            # We want a Goal, but ignore missed penalties
            if str(event.get('type')).title() == 'Goal' and 'Missed' not in str(event.get('detail', '')):
                return event['team']['name'] 
                
        return None
    except Exception as e:
        log.error(f"Error fetching events for fixture {fixture_id}: {e}")
        return None

# ── GOOGLE SERVICES ───────────────────────────────────────────────
def get_sheets():
    """Connect to Google Sheets."""
    from google.oauth2.service_account import Credentials
    _KEY_DEFAULT = os.path.join(os.getcwd(), "grind_key.json")
    KEY_FILE    = os.environ.get("KEY_FILE", _KEY_DEFAULT)
    SHEET_NAME  = os.environ.get("SHEET_NAME", "GrindAlgo Tracker")
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds  = Credentials.from_service_account_file(KEY_FILE, scopes=scopes)
    gc     = gspread.authorize(creds)
    return gc.open(SHEET_NAME)

# ── MAIN RESULTS RUNNER ───────────────────────────────────────────
def run_results_update():
    """
    Fetches results for yesterday's picks (or explicitly set date) and updates Sheets.
    Called at 6:30am WAT.
    """
    # Define Target Date (Default to Yesterday)
    yesterday_wat = (datetime.now(WAT) - timedelta(days=1)).strftime("%Y-%m-%d")
    target_date = os.environ.get("OVERRIDE_RESULTS_DATE", yesterday_wat)
    
    log.info(f"Starting API-Sports Daily Sweep for target date: {target_date}")

    sheets = get_sheets()
    picks_ws   = sheets.worksheet("Picks")
    bankroll_ws = sheets.worksheet("Bankroll")

    all_rows = picks_ws.get_all_values()
    if len(all_rows) <= 1:
        log.info("No picks in sheet")
        return {"status": "no_picks", "updated_count": 0}

    headers = all_rows[0]
    def col(name):
        return headers.index(name) if name in headers else None

    # Fetch API data ONLY for the target date
    try:
        url = f"{BASE_URL}/fixtures?date={target_date}"
        resp = requests.get(url, headers=HEADERS, timeout=15).json()
        daily_fixtures = resp.get("response", [])
    except Exception as e:
        log.error(f"Failed to fetch fixtures for {target_date}: {e}")
        daily_fixtures = []

    if not daily_fixtures:
        log.info(f"No API-Sports fixtures found for {target_date}. Aborting.")
        return {"status": "no_fixtures", "updated_count": 0}

    # Pre-build fuzzy matching dictionary
    fixture_map = {normalize_name(f["teams"]["home"]["name"]): f for f in daily_fixtures}
    home_names = list(fixture_map.keys())

    updated     = 0
    total_pnl   = 0
    bankroll    = 10000.0
    deducted_set = set()

    # Get current bankroll from last Bankroll row, and scrape for upfront deductions!
    try:
        bk_rows = bankroll_ws.get_all_values()
        if len(bk_rows) > 1:
            bankroll = float(bk_rows[-1][3] or 10000)
            
        for br in bk_rows[1:]:
            event_name = br[1]
            if "Stake Deducted —" in event_name:
                deducted_set.add(event_name)
    except Exception:
        pass

    # Batch Update Lists
    cells_to_update = []
    new_bankroll_rows = []
    settled_picks = []

    # Iterate through sheet to grade picks
    for row_idx, row in enumerate(all_rows[1:], 2):
        if len(row) < 12:
            continue

        # Extract strictly formatted cell data
        row_date   = str(row[col("Date")]).strip()      if col("Date")   is not None else ""
        row_status = str(row[col("Status")]).strip().upper() if col("Status") is not None else ""
        fixture    = str(row[col("Fixture")]).strip()   if col("Fixture") is not None else ""
        market     = str(row[col("Market")]).strip()    if col("Market") is not None else ""
        stake_str  = str(row[col("Stake (₦)")]).replace(',', '').strip() if col("Stake (₦)") is not None else "0"
        manual_score_str = str(row[col("Score")]).strip() if col("Score") is not None else ""

        # 🚨 DAILY FILTER: Ignore if it's not the target date, or if it's already win/loss
        if row_date != target_date:
            continue
        if row_status != "PENDING" and "VOID" not in row_status:
            continue
        if not fixture or not market:
            continue

        # --- 1. ODDS LOGIC ---
        actual_odds_idx = col("Actual Odds")
        est_odds_idx    = col("Est. Odds")
        odds_idx        = col("Odds")
        
        odds_str = ""
        if actual_odds_idx is not None and len(row) > actual_odds_idx and str(row[actual_odds_idx]).strip():
            odds_str = str(row[actual_odds_idx]).strip()
        elif est_odds_idx is not None and len(row) > est_odds_idx and str(row[est_odds_idx]).strip():
            odds_str = str(row[est_odds_idx]).strip()
        elif odds_idx is not None and len(row) > odds_idx and str(row[odds_idx]).strip():
            odds_str = str(row[odds_idx]).strip()
        else:
            odds_str = "1.5"

        try:
            odds  = float(odds_str)
            stake = float(stake_str)
        except Exception:
            continue

        # --- 2. BANKROLL CHECK ---
        event_str = f"Stake Deducted — {fixture} ({market})"
        was_deducted_upfront = event_str in deducted_set

        parts = fixture.split(" vs ")
        if len(parts) != 2:
            continue
        hname, aname = parts[0].strip(), parts[1].strip()

        score = None
        api_home_name = hname
        api_away_name = aname
        first_scorer = None

        # --- 3. GOD MODE: Check Sheet for Manual Score First ---
        if manual_score_str and "-" in manual_score_str:
            try:
                h_s, a_s = manual_score_str.split("-")
                score = (int(h_s.strip()), int(a_s.strip()))
                log.info(f"✅ MANUAL OVERRIDE: Using sheet score {score} for {fixture}")
            except Exception:
                pass

        # --- 4. API-SPORTS MATCHING ---
        if not score:
            match = difflib.get_close_matches(normalize_name(hname), home_names, n=1, cutoff=0.6)
            
            if match:
                f_data = fixture_map[match[0]]
                status_short = f_data["fixture"]["status"]["short"]
                
                # Verify match is completely finished
                if status_short in ["FT", "AET", "PEN"]:
                    hg = f_data["goals"]["home"]
                    ag = f_data["goals"]["away"]
                    score = (hg, ag)
                    
                    fixture_id = f_data["fixture"]["id"]
                    api_home_name = f_data["teams"]["home"]["name"]
                    api_away_name = f_data["teams"]["away"]["name"]
                    
                    # Fetch timeline events ONLY if the market requires First to Score
                    if "First to Score" in market:
                        first_scorer = get_first_scorer(fixture_id)

        if not score:
            log.info(f"No completed API result yet for: {fixture} on {row_date}")
            continue

        hg, ag   = score
        won      = check_market(market, hg, ag, api_home_name, api_away_name, first_scorer)
        score_str = f"{hg}-{ag}"

        # --- 5. CALCULATE MATH SAFELY ---
        if won is None:
            status = "VOID ⚪"
            api_status = "void"
            pnl    = 0.0
            bk_change = stake if was_deducted_upfront else 0.0
        elif won:
            status = "WIN ✅"
            api_status = "win"
            pnl    = round(stake * (odds - 1), 2)
            bk_change = round(pnl + stake, 2) if was_deducted_upfront else pnl
        else:
            status = "LOSS ❌"
            api_status = "loss"
            pnl    = -stake
            bk_change = 0.0 if was_deducted_upfront else pnl

        bankroll    = round(bankroll + bk_change, 2)
        total_pnl   = round(total_pnl + pnl, 2)

        # Build Batch Cell Updates
        if col("Status")             is not None: cells_to_update.append(gspread.Cell(row_idx, col("Status")+1, status))
        if col("Score")              is not None: cells_to_update.append(gspread.Cell(row_idx, col("Score")+1, score_str))
        if col("Result")             is not None: cells_to_update.append(gspread.Cell(row_idx, col("Result")+1, score_str))
        if col("P&L (₦)")            is not None: cells_to_update.append(gspread.Cell(row_idx, col("P&L (₦)")+1, pnl))
        if col("Bankroll After (₦)") is not None: cells_to_update.append(gspread.Cell(row_idx, col("Bankroll After (₦)")+1, bankroll))

        # Build Batch Row Update for Bankroll
        new_bankroll_rows.append([
            row_date,
            f"{status} — {fixture} ({market})",
            bk_change,
            bankroll,
            f"Score: {score_str} | P&L: ₦{pnl}"
        ])

        log.info(f"{fixture} [{market}]: {score_str} → {status} | "
                 f"Odds: {odds} | P&L: ₦{pnl:+,.0f} | Change to Bankroll: ₦{bk_change:+,.0f}")
        settled_picks.append({
            "match_date": row_date,
            "fixture": fixture,
            "market": market,
            "status": api_status,
            "score": score_str,
            "result": score_str,
            "pnl": pnl,
            "stake": stake,
        })
        updated += 1

    # --- MASSIVE BATCH UPDATE (Prevents Quota Exhaustion) ---
    if cells_to_update:
        picks_ws.update_cells(cells_to_update)
        
    if new_bankroll_rows:
        bankroll_ws.append_rows(new_bankroll_rows)

    # Write daily summary
    if updated > 0:
        try:
            summary_ws = sheets.worksheet("Summary")
            today_wat = datetime.now(WAT).strftime("%Y-%m-%d")
            summary_ws.append_row([
                today_wat, updated,
                sum(1 for r in new_bankroll_rows if "WIN" in r[1]),
                sum(1 for r in new_bankroll_rows if "LOSS" in r[1]),
                total_pnl, bankroll
            ])
        except Exception as e:
            log.warning(f"Summary write failed: {e}")

    log.info(f"Results sweep complete: {updated} picks settled | "
             f"P&L: ₦{total_pnl:+,.0f} | Balance: ₦{bankroll:,.0f}")

    return {
        "status":        "success",
        "date":          target_date,
        "updated_count": updated,
        "total_pnl":     total_pnl,
        "bankroll":      bankroll,
        "settled_picks": settled_picks,
    }

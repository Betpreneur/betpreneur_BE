# ================================================================
# GRIND ALGO — Shege Analysis Mode
# ================================================================
#
# Sits between the data-scoring phase and the PDF generation phase.
#
# FLOW:
#   Phase 1 — filter_ev_candidates()
#     → Reads all scored fixtures, applies two hard constraints:
#         • Confidence  > 70%
#         • Live odds   > 1.30  (drop heavy favourites)
#     → Calculates +EV for each surviving (fixture, market) pair.
#     → Builds a clean JSON array ready for Shege.
#
#   Phase 2 — call_shege_analyst()
#     → Posts the filtered payload to the Gemini API.
#     → Forces structured JSON output: Arrays for Bankers / Value Gems / Wild Cards,
#       each with a written tactical reasoning.
#     → Returns the parsed dict or None on any failure.
# ================================================================

import os
import json
import logging
import requests
import time

log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────
# SECURITY FIX: Rely strictly on environment variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# gemini-2.5-flash: active, fast model.
GEMINI_MODEL          = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# v1beta required: gemini-2.5-flash and responseSchema/responseMimeType are v1beta only.
GEMINI_BASE           = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MAX_CANDIDATES = 30

# Phase 1 filter thresholds (per spec)
MIN_CONFIDENCE = 70     # strictly above 70 %
MIN_ODDS       = 1.30   # strictly above 1.30 (drops heavy favourites)

ODDS_KEYS_MAP = {
    "Home Win":   "hw",
    "Away Win":   "aw",
    "Over 1.5":   "o15",
    "Under 1.5":  "u15",
    "Over 2.5":   "o25",
    "Under 2.5":  "u25",
}

# ── SYSTEM PROMPT ─────────────────────────────────────────────────
_SYSTEM_PROMPT = """
You are Segun (aka "Shege"), an elite, brutally analytical sports betting AI embedded inside the GrindAlgo engine.
You will receive a JSON array of pre-filtered football betting candidates.
Every candidate has already passed strict quantitative filters:
  • confidence  > 70 %
  • live odds   > 1.30
  • Expected Value (EV) > 0

Your task: Evaluate ALL candidates and extract the absolute best picks that strictly meet the tier definitions below. 

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIER DEFINITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

THE BANKERS
  The safest, highest-conviction picks. High confidence score combined with solid +EV.
  Must be a statistically reliable market (DC:1X, Over 1.5, AH Home +0.5, etc.).
  Odds between 1.30 and 3.50 preferred.

THE VALUE GEMS
  Picks with the LARGEST discrepancy between live_odds and expected_odds (highest EV).
  This is where the bookmaker has significantly underestimated the true probability.

THE WILD CARDS
  Contrarian, higher-risk/reward picks.
  Prioritise live_odds >= 2.00, strong positive EV, and an interesting statistical case.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Extract AS MANY candidates as you deem worthy. 
• DO NOT force a pick into a tier if it doesn't strictly fit the criteria. If there are no valid picks for a specific tier, leave that array empty [].
• A fixture can only be used ONCE across all tiers.
• reasoning must be 2–3 sentences of sharp tactical insight. Do NOT use double-quote characters inside this field.
• Return ONLY the JSON object below — no markdown fences, no preamble, no trailing text.
""".strip()


# ── PHASE 1: FILTER & EV CALCULATION ──────────────────────────────

def _est_odds(conf):
    return round(1 / max(conf / 100, 0.05) * 1.05, 2)

def filter_ev_candidates(all_confs, scored_fxs, odds_list):
    candidates = []

    # EDGE CASE FIX: Warn if upstream data arrays are misaligned to avoid silent truncation via zip()
    if not (len(all_confs) == len(scored_fxs) == len(odds_list)):
        log.warning("[Shege] Mismatch in array lengths! zip() will silently truncate data.")

    for fx, confs, real_odds in zip(scored_fxs, all_confs, odds_list):
        for market, conf in confs.items():
            if conf <= MIN_CONFIDENCE:
                continue

            key        = ODDS_KEYS_MAP.get(market)
            live_odds  = (real_odds.get(key) if key else None) or _est_odds(conf)

            if live_odds <= MIN_ODDS:
                continue

            expected_odds = _est_odds(conf)
            ev = round((conf / 100) * live_odds - 1, 4)
            if ev <= 0:
                continue

            candidates.append({
                "fixture":       fx["fixture"],
                "league":        fx.get("league", ""),
                "kickoff":       fx.get("kickoff", ""),
                "market":        market,
                "confidence":    conf,
                "live_odds":     live_odds,
                "expected_odds": expected_odds,
                "ev":            ev,
                "source":        fx.get("source", "?"),
            })

    candidates.sort(key=lambda x: x["ev"], reverse=True)
    log.info(
        f"[Shege] Phase 1 filter: {len(candidates)} +EV candidates "
        f"(conf >{MIN_CONFIDENCE}%, odds >{MIN_ODDS}, EV>0)"
    )
    return candidates


# ── RESPONSE SCHEMA ──────────────────────────────────────────────

_PICK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "fixture":       {"type": "STRING"},
        "market":        {"type": "STRING"},
        "league":        {"type": "STRING"},
        "kickoff":       {"type": "STRING"},
        "confidence":    {"type": "INTEGER"},
        "live_odds":     {"type": "NUMBER"},
        "expected_odds": {"type": "NUMBER"},
        "ev":            {"type": "NUMBER"},
        "reasoning":     {"type": "STRING"},
    },
    "required": ["fixture", "market", "league", "kickoff",
                 "confidence", "live_odds", "expected_odds", "ev", "reasoning"],
}

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "bankers":    {"type": "ARRAY", "items": _PICK_SCHEMA},
        "value_gems": {"type": "ARRAY", "items": _PICK_SCHEMA},
        "wild_cards": {"type": "ARRAY", "items": _PICK_SCHEMA},
    },
    "required": ["bankers", "value_gems", "wild_cards"],
}


# ── PHASE 2: SHEGE API CALL ───────────────────────────────────────

def call_shege_analyst(candidates):
    if not candidates:
        log.warning("[Shege] No candidates to analyse — skipping Shege phase.")
        return None
        
    if not GEMINI_API_KEY:
        log.error("[Shege] GEMINI_API_KEY is not configured.")
        return None

    model    = os.environ.get("GEMINI_MODEL", GEMINI_MODEL)
    api_url  = f"{GEMINI_BASE}/{model}:generateContent"
    payload  = candidates[:GEMINI_MAX_CANDIDATES]

    user_message = (
        f"Analyse the following {len(payload)} pre-filtered GrindAlgo betting candidates "
        f"for today and extract the Bankers, Value Gems, and Wild Cards.\n\n"
        f"CANDIDATES:\n{json.dumps(payload, indent=2)}"
    )

    request_body = {
        # CRITICAL FIX: systemInstruction MUST be camelCase for the Gemini API
        "systemInstruction": {
            "parts": [{"text": _SYSTEM_PROMPT}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_message}]
            }
        ],
        "generationConfig": {
            "temperature":      0.25,
            # CRITICAL FIX: Double token limit to prevent mid-JSON cutoff
            "maxOutputTokens":  8192,
            "responseMimeType": "application/json",   
            "responseSchema":   _RESPONSE_SCHEMA,     
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"}
        ]
    }

    max_retries = 3
    data = None

    for attempt in range(max_retries):
        try:
            log.info(f"[Shege] Calling {model} (Attempt {attempt + 1}/{max_retries}) ...")
            r = requests.post(
                f"{api_url}?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json=request_body,
                timeout=90,
            )

            # RETRY LOGIC FIX: Catch 5xx errors (like 500, 502, 504) as well
            if r.status_code in (429, 503) or r.status_code >= 500:
                wait = 10 if r.status_code == 429 else 5
                log.warning(f"[Shege] HTTP {r.status_code} — retrying in {wait}s...")
                time.sleep(wait)
                continue

            if r.status_code != 200:
                log.error(f"[Shege] API error {r.status_code}: {r.text[:1000]}")
                return None

            data = r.json()
            break

        except requests.exceptions.RequestException as e:
            log.warning(f"[Shege] Network error: {e}. Retrying in 5s...")
            time.sleep(5)
    else:
        log.error("[Shege] Max retries reached.")
        return None

    # ── Parse & Validate ─────────────────────────────────────────────
    try:
        if not data or "candidates" not in data or not data["candidates"]:
            log.error(f"[Shege] No candidates in response. Possible safety block: {data}")
            return None

        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason", "UNKNOWN")

        # INDEX ERROR FIX: Safely retrieve parts, checking if the array is empty
        parts = candidate.get("content", {}).get("parts", [])
        if not parts:
            log.error(f"[Shege] Generation stopped or parts empty. finishReason={finish_reason}")
            return None
            
        if finish_reason == "MAX_TOKENS":
            log.error("[Shege] Generation truncated due to maxOutputTokens. Cannot parse incomplete JSON.")
            return None
        elif finish_reason != "STOP":
            log.warning(f"[Shege] Unexpected finishReason={finish_reason} — proceeding anyway")

        raw_text = parts[0].get("text", "")
        log.info(f"[Shege] Response length: {len(raw_text)} chars | finishReason={finish_reason}")

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            log.error(
                f"[Shege] Schema-enforced JSON still failed to parse: {exc}\n"
                f"RAW (first 2000): {raw_text[:2000]}"
            )
            return None

        # Sanity-check the tier keys
        for tier in ("bankers", "value_gems", "wild_cards"):
            if tier not in parsed or not isinstance(parsed[tier], list):
                log.error(f"[Shege] Missing or invalid tier '{tier}' in parsed response.")
                return None

        log.info(
            f"[Shege] Analysis complete — "
            f"Bankers: {len(parsed['bankers'])} | "
            f"Value Gems: {len(parsed['value_gems'])} | "
            f"Wild Cards: {len(parsed['wild_cards'])}"
        )
        return parsed

    except Exception as exc:
        log.error(f"[Shege] Unexpected error during parsing: {exc}")
        return None
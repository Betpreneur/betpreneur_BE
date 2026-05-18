# ================================================================
# AUDITOR RUNNER — Monthly Cloud Run back-test
# ================================================================
# Runs on the 1st of each month at 8am WAT.
# Reads 30 days of picks + results from GrindAlgo Tracker sheet.
# Generates comprehensive 10-section PDF.
# Emails PDF directly to Korosky11@Gmail.com
# ================================================================

import os
import io
import time
import logging
import requests
from datetime import datetime, timedelta, timezone, date
from collections import defaultdict

log = logging.getLogger(__name__)
WAT = timezone(timedelta(hours=1))

SHEET_NAME   = os.environ.get("SHEET_NAME",   "GrindAlgo Tracker")
_KEY_DEFAULT = os.path.join(os.getcwd(), "grind_key.json")
KEY_FILE     = os.environ.get("KEY_FILE",     _KEY_DEFAULT)

MARKETS = [
    "Home Win","Away Win","Draw","Over 1.5","Over 2.5","Under 3.5",
    "GG / BTTS Yes","DC: 1X","DC: X2","DC: 12","DNB Home","DNB Away",
    "AH Home +0.5","AH Away +0.5","First to Score H","First to Score A",
]

PROVEN_MARKETS = {
    "First to Score H","Over 1.5","DC: 1X","AH Home +0.5","Under 3.5","GG / BTTS Yes"
}

# ── GOOGLE SERVICES ───────────────────────────────────────────────
def get_services():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds  = Credentials.from_service_account_file(KEY_FILE, scopes=scopes)
    gc     = gspread.authorize(creds)
    sheets = gc.open(SHEET_NAME)
    return gc, sheets

# ── READ PICKS FROM SHEET ─────────────────────────────────────────
def read_picks_from_sheet(sheets, from_date, to_date):
    """
    Read all settled picks between from_date and to_date
    from the Picks tab of GrindAlgo Tracker.
    Returns list of pick dicts.
    """
    try:
        ws   = sheets.worksheet("Picks")
        rows = ws.get_all_values()
    except Exception as e:
        log.error(f"Could not read Picks sheet: {e}")
        return []

    if len(rows) <= 1:
        return []

    headers = rows[0]

    def col(name):
        return headers.index(name) if name in headers else None

    picks = []
    for row in rows[1:]:
        if len(row) < 6:
            continue
        try:
            row_date = row[col("Date")] if col("Date") is not None else ""
            if row_date < from_date or row_date > to_date:
                continue

            status = row[col("Status")] if col("Status") is not None else ""
            if "PENDING" in status or not status:
                continue

            market = row[col("Market")] if col("Market") is not None else ""
            if not market:
                continue

            conf_str = row[col("Confidence %")] if col("Confidence %") is not None else "70"
            conf = float(conf_str.replace("%","").strip() or 70)

            odds_str = row[col("Est. Odds")] if col("Est. Odds") is not None else "1.5"
            odds = float(odds_str or 1.5)

            stake_str = row[col("Stake (₦)")] if col("Stake (₦)") is not None else "0"
            stake = float(stake_str or 0)

            pnl_str = row[col("P&L (₦)")] if col("P&L (₦)") is not None else "0"
            pnl = float(pnl_str or 0)

            result_str = row[col("Score")] if col("Score") is not None else ""
            hg, ag = None, None
            if result_str and "-" in result_str:
                parts = result_str.split("-")
                try:
                    hg, ag = int(parts[0]), int(parts[1])
                except Exception:
                    pass

            won = "WIN" in status
            void = "VOID" in status

            fixture = row[col("Fixture")] if col("Fixture") is not None else ""
            league  = row[col("League")]  if col("League")  is not None else ""
            tier    = row[col("Tier")]    if col("Tier")    is not None else ""

            picks.append({
                "date":    row_date,
                "fixture": fixture,
                "league":  league,
                "tier":    tier,
                "market":  market,
                "conf":    conf,
                "odds":    odds,
                "stake":   stake,
                "pnl":     pnl,
                "won":     won,
                "void":    void,
                "hg":      hg,
                "ag":      ag,
                "status":  status,
            })
        except Exception as e:
            log.warning(f"Row parse error: {e}")
            continue

    log.info(f"Read {len(picks)} settled picks from sheet ({from_date} → {to_date})")
    return picks

# ── ANALYSE PICKS ─────────────────────────────────────────────────
def analyse_picks(picks):
    """Run full statistical analysis on picks."""

    # Overall
    settled = [p for p in picks if not p["void"]]
    wins    = [p for p in settled if p["won"]]
    losses  = [p for p in settled if not p["won"]]
    total_pnl   = round(sum(p["pnl"] for p in picks), 2)
    total_staked = round(sum(p["stake"] for p in picks), 2)
    hit_rate    = round(len(wins)/max(len(settled),1)*100, 1)
    avg_conf    = round(sum(p["conf"] for p in picks)/max(len(picks),1), 1)
    roi         = round(total_pnl/max(total_staked,1)*100, 1)

    # Per market
    mkt_stats = defaultdict(lambda: {"picks":0,"wins":0,"conf_sum":0,"pnl":0})
    for p in settled:
        m = p["market"]
        mkt_stats[m]["picks"] += 1
        mkt_stats[m]["wins"]  += 1 if p["won"] else 0
        mkt_stats[m]["conf_sum"] += p["conf"]
        mkt_stats[m]["pnl"]   += p["pnl"]

    market_summary = {}
    for m, s in mkt_stats.items():
        n = s["picks"]
        market_summary[m] = {
            "picks":    n,
            "wins":     s["wins"],
            "hit_rate": round(s["wins"]/max(n,1)*100, 1),
            "avg_conf": round(s["conf_sum"]/max(n,1), 1),
            "gap":      round(s["wins"]/max(n,1)*100 - s["conf_sum"]/max(n,1), 1),
            "pnl":      round(s["pnl"], 2),
        }

    # Per league
    league_stats = defaultdict(lambda: {"picks":0,"wins":0,"pnl":0})
    for p in settled:
        l = p["league"] or "Unknown"
        league_stats[l]["picks"] += 1
        league_stats[l]["wins"]  += 1 if p["won"] else 0
        league_stats[l]["pnl"]   += p["pnl"]

    league_summary = {
        l: {
            "picks":    s["picks"],
            "wins":     s["wins"],
            "hit_rate": round(s["wins"]/max(s["picks"],1)*100, 1),
            "pnl":      round(s["pnl"], 2),
        }
        for l, s in league_stats.items() if s["picks"] >= 3
    }

    # Per tier
    tier_stats = defaultdict(lambda: {"picks":0,"wins":0,"pnl":0,"staked":0})
    for p in settled:
        t = "Banker" if "Banker" in p["tier"] else \
            "Value Gem" if "Gem" in p["tier"] else \
            "Wild Card" if "Wild" in p["tier"] else "Other"
        tier_stats[t]["picks"]  += 1
        tier_stats[t]["wins"]   += 1 if p["won"] else 0
        tier_stats[t]["pnl"]    += p["pnl"]
        tier_stats[t]["staked"] += p["stake"]

    # Confidence band accuracy
    bands = [(65,70,"65–70%"),(70,75,"70–75%"),(75,80,"75–80%"),
             (80,85,"80–85%"),(85,90,"85–90%"),(90,96,"90%+")]
    band_stats = {}
    for lo, hi, label in bands:
        band = [p for p in settled if lo <= p["conf"] < hi]
        if len(band) < 3:
            continue
        w   = sum(1 for p in band if p["won"])
        hr  = round(w/len(band)*100, 1)
        exp = round((lo+hi)/2, 1)
        band_stats[label] = {
            "picks":    len(band),
            "hit_rate": hr,
            "expected": exp,
            "gap":      round(hr-exp, 1),
        }

    # Day of week
    from collections import Counter
    dow_wins   = Counter()
    dow_total  = Counter()
    for p in settled:
        try:
            d   = datetime.strptime(p["date"], "%Y-%m-%d")
            dow = d.strftime("%A")
            dow_total[dow] += 1
            if p["won"]: dow_wins[dow] += 1
        except Exception:
            pass

    dow_stats = {
        dow: {"picks": dow_total[dow],
               "hit_rate": round(dow_wins[dow]/max(dow_total[dow],1)*100, 1)}
        for dow in dow_total if dow_total[dow] >= 3
    }

    # Weekly trend for key markets
    weekly = defaultdict(lambda: defaultdict(lambda: {"wins":0,"picks":0}))
    for p in settled:
        try:
            d    = datetime.strptime(p["date"], "%Y-%m-%d")
            week = d.strftime("%Y-W%U")
            if p["market"] in {"Over 1.5","DC: 1X","First to Score H","GG / BTTS Yes"}:
                weekly[week][p["market"]]["picks"] += 1
                if p["won"]: weekly[week][p["market"]]["wins"] += 1
        except Exception:
            pass

    return {
        "total_picks":   len(picks),
        "settled":       len(settled),
        "wins":          len(wins),
        "losses":        len(losses),
        "void":          len(picks) - len(settled),
        "hit_rate":      hit_rate,
        "avg_conf":      avg_conf,
        "total_pnl":     total_pnl,
        "total_staked":  total_staked,
        "roi":           roi,
        "market_summary":market_summary,
        "league_summary":league_summary,
        "tier_stats":    dict(tier_stats),
        "band_stats":    band_stats,
        "dow_stats":     dow_stats,
        "weekly":        {k: dict(v) for k,v in weekly.items()},
    }

# ── PDF GENERATOR ─────────────────────────────────────────────────
def generate_auditor_pdf(stats, from_date, to_date, picks):
    """Generate comprehensive 10-section Auditor PDF."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, PageBreak, KeepTogether
        )
    except ImportError:
        log.error("reportlab not installed")
        raise

    filepath = f"/tmp/TheAuditor_{to_date}.pdf"

    doc = SimpleDocTemplate(
        filepath, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=15*mm, bottomMargin=15*mm
    )

    # Styles
    C_DARK   = colors.HexColor("#1a1a2e")
    C_GREEN  = colors.HexColor("#00b894")
    C_AMBER  = colors.HexColor("#f39c12")
    C_RED    = colors.HexColor("#e74c3c")
    C_LIGHT  = colors.HexColor("#f0f0f0")

    def S(name, **kw):
        return ParagraphStyle(name, **kw)

    sTitle  = S("t", fontName="Helvetica-Bold", fontSize=22,
                alignment=TA_CENTER, textColor=C_DARK, spaceAfter=4)
    sSub    = S("s", fontName="Helvetica", fontSize=10,
                alignment=TA_CENTER, textColor=colors.grey, spaceAfter=2)
    sSec    = S("sec", fontName="Helvetica-Bold", fontSize=12,
                textColor=C_DARK, spaceBefore=6, spaceAfter=4)
    sBody   = S("b", fontName="Helvetica", fontSize=9,
                textColor=colors.HexColor("#333333"), spaceAfter=3)
    sNote   = S("n", fontName="Helvetica-Oblique", fontSize=8,
                textColor=colors.grey, spaceAfter=3)
    sFooter = S("f", fontName="Helvetica", fontSize=7,
                alignment=TA_CENTER, textColor=colors.grey)

    def hdr_row(cols, widths):
        t = Table([cols], colWidths=widths)
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), C_DARK),
            ("TEXTCOLOR",     (0,0),(-1,-1), colors.white),
            ("FONTNAME",      (0,0),(-1,-1), "Helvetica-Bold"),
            ("FONTSIZE",      (0,0),(-1,-1), 8),
            ("TOPPADDING",    (0,0),(-1,-1), 4),
            ("BOTTOMPADDING", (0,0),(-1,-1), 4),
            ("LEFTPADDING",   (0,0),(-1,-1), 5),
        ]))
        return t

    def data_tbl(rows, widths):
        t = Table(rows, colWidths=widths)
        t.setStyle(TableStyle([
            ("FONTSIZE",      (0,0),(-1,-1), 8),
            ("FONTNAME",      (0,0),(-1,-1), "Helvetica"),
            ("GRID",          (0,0),(-1,-1), 0.3, colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS",(0,0),(-1,-1), [colors.white, C_LIGHT]),
            ("TOPPADDING",    (0,0),(-1,-1), 3),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
            ("LEFTPADDING",   (0,0),(-1,-1), 5),
        ]))
        return t

    story = []
    grade = ("STRONG ✅" if stats["hit_rate"] >= 65 else
             "SOLID 📈"  if stats["hit_rate"] >= 58 else
             "MODERATE ⚠️" if stats["hit_rate"] >= 50 else "WEAK ❌")

    # ── TITLE ─────────────────────────────────────────────────────
    story.append(Paragraph("THE AUDITOR", sTitle))
    story.append(Paragraph(f"GrindAlgo v6 — Monthly Back-test Report", sSub))
    story.append(Paragraph(f"Period: {from_date}  →  {to_date}", sSub))
    story.append(Paragraph(
        f"{stats['settled']} picks settled  |  "
        f"Full 19-feature scorer  |  Auto-generated by Cloud Run", sSub))
    story.append(HRFlowable(width="100%", thickness=2, color=C_DARK))
    story.append(Spacer(1, 4*mm))

    # Overall headline
    story.append(Paragraph(
        f"OVERALL HIT RATE: {stats['hit_rate']}%  "
        f"vs  {stats['avg_conf']}% avg confidence  —  {grade}",
        S("hl", fontName="Helvetica-Bold", fontSize=12,
          textColor=C_GREEN if stats["hit_rate"]>=60 else C_AMBER,
          spaceAfter=8)
    ))
    story.append(Paragraph(
        f"P&L: ₦{stats['total_pnl']:+,.0f}  |  "
        f"Staked: ₦{stats['total_staked']:,.0f}  |  "
        f"ROI: {stats['roi']:+.1f}%  |  "
        f"W{stats['wins']} L{stats['losses']} V{stats['void']}",
        S("sub2", fontName="Helvetica", fontSize=9, spaceAfter=8)
    ))
    story.append(PageBreak())

    # ── 1. MARKET HIT RATES ───────────────────────────────────────
    story.append(Paragraph("1. MARKET HIT RATES", sSec))
    story.append(Paragraph(
        "Sorted by hit rate. Gap = Hit Rate minus Avg Confidence. "
        "Positive = model underestimates (lower threshold). "
        "Negative = model overestimates (raise threshold).", sNote))
    col_w = [52*mm,16*mm,14*mm,22*mm,22*mm,20*mm,14*mm]
    story.append(hdr_row(["Market","Picks","Wins","Hit Rate","Avg Conf","Gap","P&L ₦"], col_w))
    mkt_rows = sorted(
        stats["market_summary"].items(),
        key=lambda x: x[1]["hit_rate"], reverse=True
    )
    tbl_data = []
    for m, s in mkt_rows:
        proven = "⭐" if m in PROVEN_MARKETS else ""
        flag   = "✅" if abs(s["gap"])<=5 else "⚠" if abs(s["gap"])<=15 else "❌"
        tbl_data.append([
            f"{proven} {m}", str(s["picks"]), str(s["wins"]),
            f"{s['hit_rate']}%", f"{s['avg_conf']}%",
            f"{s['gap']:+.1f}%", f"₦{s['pnl']:+,.0f}"
        ])
    if tbl_data:
        story.append(data_tbl(tbl_data, col_w))
    story.append(Spacer(1, 5*mm))

    # ── 2. TIER PERFORMANCE ───────────────────────────────────────
    story.append(Paragraph("2. PERFORMANCE BY TIER", sSec))
    story.append(Paragraph("How did Bankers, Value Gems, and Wild Cards perform?", sNote))
    col_w2 = [45*mm,20*mm,20*mm,25*mm,25*mm,25*mm]
    story.append(hdr_row(["Tier","Picks","Wins","Hit Rate","Staked","P&L"], col_w2))
    tier_rows = []
    for tier, s in stats["tier_stats"].items():
        hr = round(s["wins"]/max(s["picks"],1)*100, 1)
        tier_rows.append([
            tier, str(s["picks"]), str(s["wins"]),
            f"{hr}%", f"₦{s['staked']:,.0f}", f"₦{s['pnl']:+,.0f}"
        ])
    if tier_rows:
        story.append(data_tbl(tier_rows, col_w2))
    story.append(Spacer(1, 5*mm))

    # ── 3. CONFIDENCE BAND ACCURACY ───────────────────────────────
    story.append(Paragraph("3. ACCURACY BY CONFIDENCE BAND", sSec))
    story.append(Paragraph(
        "When the model says 80%, does it land 80% of the time? "
        "Consistent negative gaps = model is overconfident.", sNote))
    col_w3 = [40*mm,20*mm,25*mm,25*mm,20*mm,20*mm]
    story.append(hdr_row(["Band","Picks","Hit Rate","Expected","Gap",""], col_w3))
    band_rows = []
    for label, s in stats["band_stats"].items():
        flag = "✅" if abs(s["gap"])<=5 else "⚠" if abs(s["gap"])<=10 else "❌"
        band_rows.append([
            label, str(s["picks"]), f"{s['hit_rate']}%",
            f"{s['expected']}%", f"{s['gap']:+.1f}%", flag
        ])
    if band_rows:
        story.append(data_tbl(band_rows, col_w3))
    story.append(Spacer(1, 5*mm))

    # ── 4. LEAGUE ACCURACY ────────────────────────────────────────
    story.append(Paragraph("4. LEAGUE-BY-LEAGUE ACCURACY", sSec))
    story.append(Paragraph(
        "Min 3 picks required. Sorted by hit rate. "
        "Low performers may need league-specific calibration.", sNote))
    col_w4 = [60*mm,18*mm,14*mm,25*mm,30*mm]
    story.append(hdr_row(["League","Picks","Wins","Hit Rate","P&L"], col_w4))
    lg_rows = sorted(
        stats["league_summary"].items(),
        key=lambda x: x[1]["hit_rate"], reverse=True
    )
    lg_data = []
    for l, s in lg_rows:
        lg_data.append([
            l[:35], str(s["picks"]), str(s["wins"]),
            f"{s['hit_rate']}%", f"₦{s['pnl']:+,.0f}"
        ])
    if lg_data:
        story.append(data_tbl(lg_data, col_w4))
    story.append(Spacer(1, 5*mm))

    # ── 5. DAY OF WEEK ────────────────────────────────────────────
    story.append(Paragraph("5. BEST AND WORST PICK DAYS", sSec))
    story.append(Paragraph(
        "Which days of the week produce the most reliable picks?", sNote))
    col_w5 = [45*mm,20*mm,25*mm,50*mm]
    story.append(hdr_row(["Day","Picks","Hit Rate","Verdict"], col_w5))
    days_order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    dow_data = []
    for d in days_order:
        if d not in stats["dow_stats"]: continue
        s  = stats["dow_stats"][d]
        hr = s["hit_rate"]
        verdict = "🏆 Best day" if hr>=70 else "✅ Good" if hr>=60 else "⚠ Average" if hr>=50 else "❌ Avoid"
        dow_data.append([d, str(s["picks"]), f"{hr}%", verdict])
    if dow_data:
        story.append(data_tbl(dow_data, col_w5))
    story.append(Spacer(1, 5*mm))

    # ── 6. WEEKLY TRENDS ─────────────────────────────────────────
    story.append(Paragraph("6. MARKET ACCURACY TRENDS (WEEK BY WEEK)", sSec))
    story.append(Paragraph(
        "How key proven markets performed each week. "
        "Falling trend = consider recalibration.", sNote))
    key_mkts = ["Over 1.5","DC: 1X","First to Score H","GG / BTTS Yes"]
    if stats["weekly"]:
        col_tw = [28*mm] + [36*mm]*len(key_mkts)
        trend_hdr = ["Week"] + key_mkts
        story.append(hdr_row(trend_hdr, col_tw))
        trend_data = []
        for week in sorted(stats["weekly"].keys()):
            row = [week]
            for m in key_mkts:
                ms = stats["weekly"][week].get(m, {"picks":0,"wins":0})
                if ms["picks"] > 0:
                    hr = round(ms["wins"]/ms["picks"]*100)
                    row.append(f"{hr}% ({ms['picks']})")
                else:
                    row.append("—")
            trend_data.append(row)
        if trend_data:
            story.append(data_tbl(trend_data, col_tw))
    story.append(Spacer(1, 5*mm))

    story.append(PageBreak())

    # ── 7. CALIBRATION RECOMMENDATIONS ───────────────────────────
    story.append(Paragraph("7. CALIBRATION RECOMMENDATIONS", sSec))
    story.append(Paragraph(
        "Sorted by severity. Apply these to MARKET_THRESHOLDS in the algo.", sNote))
    rec_rows = []
    for m, s in sorted(stats["market_summary"].items(),
                        key=lambda x: abs(x[1]["gap"]), reverse=True):
        if abs(s["gap"]) < 3: continue
        direction = "🔴 Raise threshold" if s["gap"] < 0 else "🟢 Lower threshold"
        severity  = "HIGH" if abs(s["gap"])>15 else "MEDIUM" if abs(s["gap"])>8 else "LOW"
        rec_rows.append([m, f"{s['gap']:+.1f}%", severity, direction])
    if rec_rows:
        col_w7 = [55*mm,20*mm,22*mm,63*mm]
        story.append(hdr_row(["Market","Gap","Severity","Action"], col_w7))
        story.append(data_tbl(rec_rows, col_w7))
    story.append(Spacer(1, 5*mm))

    # ── 8. PROVEN MARKETS ─────────────────────────────────────────
    story.append(Paragraph("8. PROVEN MARKETS — KEEP BETTING THESE", sSec))
    proven_data = [
        (m, s) for m, s in stats["market_summary"].items()
        if m in PROVEN_MARKETS and s["picks"] >= 3
    ]
    proven_data.sort(key=lambda x: x[1]["hit_rate"], reverse=True)
    if proven_data:
        col_wp = [52*mm,18*mm,14*mm,22*mm,22*mm,22*mm]
        story.append(hdr_row(["Market","Picks","Wins","Hit Rate","Avg Conf","P&L"], col_wp))
        proven_rows = []
        for m, s in proven_data:
            proven_rows.append([
                f"⭐ {m}", str(s["picks"]), str(s["wins"]),
                f"{s['hit_rate']}%", f"{s['avg_conf']}%", f"₦{s['pnl']:+,.0f}"
            ])
        story.append(data_tbl(proven_rows, col_wp))
    story.append(Spacer(1, 5*mm))

    # ── 9. MARKETS TO AVOID ───────────────────────────────────────
    story.append(Paragraph("9. MARKETS TO AVOID", sSec))
    avoid_data = [
        (m, s) for m, s in stats["market_summary"].items()
        if s["gap"] < -10 and s["picks"] >= 3
    ]
    avoid_data.sort(key=lambda x: x[1]["gap"])
    if avoid_data:
        col_wa = [52*mm,18*mm,14*mm,22*mm,22*mm,22*mm]
        story.append(hdr_row(["Market","Picks","Wins","Hit Rate","Gap","P&L"], col_wa))
        avoid_rows = []
        for m, s in avoid_data:
            avoid_rows.append([
                f"❌ {m}", str(s["picks"]), str(s["wins"]),
                f"{s['hit_rate']}%", f"{s['gap']:+.1f}%", f"₦{s['pnl']:+,.0f}"
            ])
        story.append(data_tbl(avoid_rows, col_wa))
    story.append(Spacer(1, 5*mm))

    # ── 10. RECENT PICKS LOG ──────────────────────────────────────
    story.append(Paragraph("10. RECENT PICKS LOG (last 30)", sSec))
    recent = sorted(picks, key=lambda x: x["date"], reverse=True)[:30]
    if recent:
        col_wl = [20*mm,45*mm,25*mm,18*mm,18*mm,20*mm,18*mm]
        story.append(hdr_row(["Date","Fixture","Market","Conf","Odds","Result","P&L"], col_wl))
        log_rows = []
        for p in recent:
            icon = "✅" if p["won"] else "❌" if not p["void"] else "⚪"
            log_rows.append([
                p["date"], p["fixture"][:28], p["market"][:20],
                f"{p['conf']:.0f}%", str(p["odds"]),
                f"{icon} {p.get('hg','-')}-{p.get('ag','-')}",
                f"₦{p['pnl']:+,.0f}"
            ])
        story.append(data_tbl(log_rows, col_wl))

    # ── FOOTER ────────────────────────────────────────────────────
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"THE AUDITOR  ·  GrindAlgo v6  ·  {from_date} → {to_date}  ·  "
        f"{stats['settled']} picks  ·  Auto-generated by Cloud Run  ·  "
        f"Past performance does not guarantee future results.",
        sFooter
    ))

    doc.build(story)
    log.info(f"PDF generated: {filepath}")
    return filepath

# ── EMAIL SENDER ──────────────────────────────────────────────────
def send_to_email(filepath, filename, recipient="Korosky11@Gmail.com"):
    """Emails the generated PDF directly to the specified address."""
    import smtplib
    from email.message import EmailMessage
    import os

    # Grabs the email credentials from Cloud Run's secure vault
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_pass  = os.environ.get("SENDER_PASSWORD")

    if not sender_email or not sender_pass:
        log.error("Email credentials missing. Cannot send PDF.")
        return "Failed: Missing Email Credentials"

    # Build the email
    msg = EmailMessage()
    msg['Subject'] = f"GrindAlgo Monthly Audit Report: {filename}"
    msg['From'] = sender_email
    msg['To'] = recipient
    msg.set_content("Attached is your automated monthly GrindAlgo performance report.\n\nKeep grinding!")

    # Attach the PDF
    with open(filepath, 'rb') as f:
        pdf_data = f.read()
    msg.add_attachment(pdf_data, maintype='application', subtype='pdf', filename=filename)

    # Send the email via Gmail's SMTP server
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender_email, sender_pass)
            smtp.send_message(msg)
        log.info(f"Report successfully emailed to {recipient}")
        return "Email Sent Successfully"
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        return f"Email Failed: {e}"
    
# ── MAIN AUDITOR RUNNER ───────────────────────────────────────────
def run_auditor():
    """
    Monthly Auditor run. Called on 1st of month at 8am WAT.
    Reads 30 days of picks from Sheets, generates PDF, emails to user.
    """
    now_wat  = datetime.now(WAT)
    to_date  = (now_wat - timedelta(days=1)).strftime("%Y-%m-%d")
    from_date = (now_wat - timedelta(days=31)).strftime("%Y-%m-%d")

    # Allow override
    from_date = os.environ.get("AUDITOR_FROM", from_date)
    to_date   = os.environ.get("AUDITOR_TO",   to_date)

    log.info(f"Running Auditor: {from_date} → {to_date}")

    # Connect to Google
    gc, sheets = get_services()

    # Read picks
    picks = read_picks_from_sheet(sheets, from_date, to_date)

    if len(picks) < 10:
        log.warning(f"Only {len(picks)} settled picks — need at least 10 for meaningful analysis")
        return {
            "status": "insufficient_data",
            "picks":  len(picks),
            "message": f"Only {len(picks)} picks found between {from_date} and {to_date}. "
                      f"Need at least 10 settled picks for meaningful back-test."
        }

    # Analyse
    stats = analyse_picks(picks)
    log.info(f"Analysis: {stats['hit_rate']}% hit rate | "
             f"P&L: ₦{stats['total_pnl']:+,.0f} | ROI: {stats['roi']:+.1f}%")

    # Generate PDF
    filename = f"TheAuditor_{to_date}.pdf"
    pdf_path = generate_auditor_pdf(stats, from_date, to_date, picks)

    # Email the PDF
    email_status = send_to_email(pdf_path, filename)

    result = {
        "status":       "success",
        "from_date":    from_date,
        "to_date":      to_date,
        "picks":        len(picks),
        "hit_rate":     stats["hit_rate"],
        "pnl":          stats["total_pnl"],
        "roi":          stats["roi"],
        "pdf":          filename,
        "delivery":     email_status,
    }
    log.info(f"Auditor complete: {result}")
    return result

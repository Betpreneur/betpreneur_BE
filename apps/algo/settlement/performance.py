from collections import defaultdict

from ..models import Pick


def latest_audited_picks(days=None):
    queryset = (
        Pick.objects.filter(status__in=[Pick.Status.WIN, Pick.Status.LOSS])
        .select_related("run")
        .order_by("-match_date", "-run__target_date", "-created_at", "-id")
    )
    if days:
        from django.utils import timezone
        from datetime import timedelta

        since = timezone.localdate() - timedelta(days=days)
        queryset = queryset.filter(match_date__gte=since)

    latest = {}
    for pick in queryset:
        key = (
            pick.match_date or pick.run.target_date,
            str(pick.match_id or "").strip(),
            pick.fixture,
            pick.market,
        )
        if key not in latest:
            latest[key] = pick
    return list(latest.values())


def confidence_band(confidence):
    if confidence >= 80:
        return "80+"
    if confidence >= 75:
        return "75-79"
    if confidence >= 70:
        return "70-74"
    if confidence >= 65:
        return "65-69"
    return "Below 65"


def odds_band(odds):
    odds = float(odds or 0)
    if odds < 1.5:
        return "1.00-1.49"
    if odds < 2:
        return "1.50-1.99"
    if odds < 3:
        return "2.00-2.99"
    return "3.00+"


def empty_stats():
    return {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "stake": 0.0,
        "pnl": 0.0,
        "avg_confidence": 0.0,
        "avg_odds": 0.0,
        "_confidence_total": 0.0,
        "_odds_total": 0.0,
    }


def add_pick(stats, pick):
    stats["count"] += 1
    if pick.status == Pick.Status.WIN:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    stats["stake"] += float(pick.stake or 0)
    stats["pnl"] += float(pick.pnl or 0)
    stats["_confidence_total"] += float(pick.confidence or 0)
    stats["_odds_total"] += float(pick.odds or 0)


def finalize_stats(stats):
    settled = stats["wins"] + stats["losses"]
    stake = stats["stake"]
    stats["hit_rate"] = round((stats["wins"] / settled) * 100, 1) if settled else 0.0
    stats["roi_flat"] = round((stats["pnl"] / stake) * 100, 1) if stake else 0.0
    stats["avg_confidence"] = round(stats["_confidence_total"] / stats["count"], 1) if stats["count"] else 0.0
    stats["avg_odds"] = round(stats["_odds_total"] / stats["count"], 2) if stats["count"] else 0.0
    stats["stake"] = round(stats["stake"], 2)
    stats["pnl"] = round(stats["pnl"], 2)
    stats.pop("_confidence_total", None)
    stats.pop("_odds_total", None)
    return stats


def sorted_rows(grouped):
    rows = []
    for label, stats in grouped.items():
        row = {"label": label}
        row.update(finalize_stats(stats))
        rows.append(row)
    return sorted(rows, key=lambda row: (row["roi_flat"], row["hit_rate"], row["count"]), reverse=True)


def performance_dashboard(days=90):
    picks = latest_audited_picks(days=days)
    overall = empty_stats()
    by_market = defaultdict(empty_stats)
    by_league = defaultdict(empty_stats)
    by_league_market = defaultdict(empty_stats)
    by_confidence = defaultdict(empty_stats)
    by_odds = defaultdict(empty_stats)

    for pick in picks:
        add_pick(overall, pick)
        add_pick(by_market[pick.market], pick)
        add_pick(by_league[pick.league or "Unknown"], pick)
        add_pick(by_league_market[f"{pick.league or 'Unknown'} / {pick.market}"], pick)
        add_pick(by_confidence[confidence_band(pick.confidence)], pick)
        add_pick(by_odds[odds_band(pick.odds)], pick)

    return {
        "days": days,
        "overall": finalize_stats(overall),
        "markets": sorted_rows(by_market),
        "leagues": sorted_rows(by_league),
        "league_markets": sorted_rows(by_league_market),
        "confidence_bands": sorted_rows(by_confidence),
        "odds_bands": sorted_rows(by_odds),
    }

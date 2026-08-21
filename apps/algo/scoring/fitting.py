"""
Fit per-league goal-scoring parameters from StatPal aggregates.

A multiplicative strength model: each team carries attack and defence factors relative
to the league average, kept separate for home and away because home advantage varies
enormously by league and StatPal reports the split directly.

    lambda_home = home_baseline * attack_home(H) * defence_away(A)
    lambda_away = away_baseline * attack_away(A) * defence_home(H)

**Shrinkage is mandatory, not a refinement.** Slips are full of second divisions where a
team may have played six games; an unshrunk ratio from `goals_scored=0, games=3` would
assert a team cannot score. Every factor is pulled toward the league average with weight
proportional to matches played.

*Known limitation:* StatPal exposes season aggregates, not dated match rows, so no
exponential time decay is applied here. A team's early-season form is weighted the same
as its most recent. Adding decay requires fitting from `leagues/{id}/matches/stats`
instead, which is a later change — the omission is recorded rather than papered over.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MODEL_VERSION = "sm-1.0.0"

# Pseudo-matches of prior weight. A team needs roughly this many games before its own
# record outweighs the league average.
SHRINKAGE_MATCHES = 6

# Guard rails: no team is ten times better than the league, however small the sample.
MIN_FACTOR = 0.35
MAX_FACTOR = 2.6

FALLBACK_HOME_BASELINE = 1.35
FALLBACK_AWAY_BASELINE = 1.10

# Pseudo-games of prior weight on the *league* baselines. Team factors were always
# shrunk, but the baselines they multiply were raw quotients: a league two matches into
# its season produced things like 0.5 home and 3.0 away goals per game, and those fed
# straight into every probability for that competition.
BASELINE_PRIOR_GAMES = 10

# Below this many completed league matches the split is noise, however many teams are
# listed, so the fit must not claim medium-quality data.
MIN_GAMES_FOR_SPLIT = 20
MIN_GAMES_FOR_LIMITED = 5

# Evidence behind ONE team's factors, counting the prior season. `matches_observed` is a
# league-wide total, so twenty teams two games in clears it comfortably while every
# individual factor is still shrunk to ~1.0 -- which is how two different Saudi fixtures
# came back with identical expected goals, presented as a fitted model. Result markets
# are only meaningful once the model can actually tell the two sides apart.
MIN_TEAM_MATCHES_FOR_RESULT = 8

# A prior season needs a real sample of its own before it is worth carrying forward.
MIN_GAMES_FOR_PRIOR = 40


def _num(value, default=0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _shrink(observed: float, matches: float, prior: float = 1.0, weight: int = SHRINKAGE_MATCHES) -> float:
    """Pull a rate toward the league average, in proportion to how little we've seen."""
    matches = max(0.0, matches)
    factor = (matches * observed + weight * prior) / (matches + weight)
    return min(MAX_FACTOR, max(MIN_FACTOR, factor))


@dataclass(frozen=True)
class TeamFit:
    team_id: str
    team_name: str
    home_attack: float
    home_defence: float
    away_attack: float
    away_defence: float
    matches: int
    shots_per_game: float | None = None
    # Matches behind the prior-season factors this fit was shrunk toward. Combined with
    # `matches` it is the total evidence standing behind the numbers above.
    prior_matches: int = 0
    prior_season: str = ""

    @property
    def effective_matches(self) -> int:
        return int(self.matches) + int(self.prior_matches)


@dataclass(frozen=True)
class LeagueFit:
    league_id: str
    league_name: str
    season: str
    home_goal_baseline: float
    away_goal_baseline: float
    data_quality: str
    teams: tuple[TeamFit, ...]
    matches_observed: int
    diagnostics: dict
    model_version: str = MODEL_VERSION
    prior_season: str = ""


def _team_rows(standings_payload) -> list[dict]:
    """Flatten StatPal standings, which nest teams under one or more group tables."""
    standings = (standings_payload or {}).get("standings") or {}
    tournaments = standings.get("tournament") or []
    if isinstance(tournaments, dict):
        tournaments = [tournaments]
    rows = []
    for tournament in tournaments:
        teams = tournament.get("team") or []
        if isinstance(teams, dict):
            teams = [teams]
        rows.extend(teams)
    return rows


def prior_index(fit: LeagueFit | None) -> dict[str, TeamFit]:
    """
    Index a previous-season fit for lookup by id and by normalised name.

    Team ids are stable across seasons in StatPal, but a promoted side simply will not
    appear -- those teams keep the neutral prior and are reported with the smaller
    sample they actually have, rather than borrowing someone else's strength.
    """
    index: dict[str, TeamFit] = {}
    for team in (fit.teams if fit else ()):
        if team.team_id:
            index[f"id:{team.team_id}"] = team
        normalized = normalize_team_name(team.team_name)
        if normalized:
            index.setdefault(f"name:{normalized}", team)
    return index


def _prior_for(index: dict[str, TeamFit], team_id: str, team_name: str) -> TeamFit | None:
    if not index:
        return None
    if team_id and f"id:{team_id}" in index:
        return index[f"id:{team_id}"]
    normalized = normalize_team_name(team_name)
    return index.get(f"name:{normalized}") if normalized else None


def fit_league_from_standings(
    standings_payload,
    *,
    league_id: str,
    league_name: str = "",
    season: str = "",
    shots_by_team: dict | None = None,
    prior_fit: LeagueFit | None = None,
) -> LeagueFit:
    """
    Fit attack/defence strengths from a StatPal standings response.

    `prior_fit` is the previous season's fit for the same league. Its factors replace the
    flat 1.0 that shrinkage otherwise pulls toward, which is what makes the model usable
    in August: with zero games played every factor used to come back as exactly 1.0, so
    every fixture in a league produced the identical league-average scoreline.
    """
    rows = _team_rows(standings_payload)
    shots_by_team = shots_by_team or {}

    home_goals = home_games = away_goals = away_games = 0.0
    parsed = []
    for row in rows:
        home = row.get("home") or {}
        away = row.get("away") or {}
        entry = {
            "team_id": str(row.get("id") or ""),
            "team_name": str(row.get("name") or ""),
            "home_scored": _num(home.get("goals_scored")),
            "home_allowed": _num(home.get("goals_allowed")),
            "home_games": _num(home.get("games_played")),
            "away_scored": _num(away.get("goals_scored")),
            "away_allowed": _num(away.get("goals_allowed")),
            "away_games": _num(away.get("games_played")),
        }
        parsed.append(entry)
        home_goals += entry["home_scored"]
        home_games += entry["home_games"]
        away_goals += entry["away_scored"]
        away_games += entry["away_games"]

    has_splits = home_games > 0 and away_games > 0

    def _baseline(goals: float, games: float, prior: float) -> float:
        """Shrink the observed league rate toward the global prior by sample size."""
        if games <= 0:
            return prior
        observed = goals / games
        if observed <= 0:
            observed = prior
        return round((games * observed + BASELINE_PRIOR_GAMES * prior) / (games + BASELINE_PRIOR_GAMES), 4)

    home_baseline = _baseline(home_goals, home_games, FALLBACK_HOME_BASELINE)
    away_baseline = _baseline(away_goals, away_games, FALLBACK_AWAY_BASELINE)

    priors = prior_index(prior_fit)
    prior_season = prior_fit.season if prior_fit else ""

    teams = []
    for entry in parsed:
        hg, ag = entry["home_games"], entry["away_games"]
        prior = _prior_for(priors, entry["team_id"], entry["team_name"])
        # Shrink toward last season's strength rather than toward "average team". With no
        # games played the factor becomes the prior; as the season builds, current form
        # takes over at the same rate it always did.
        home_attack = _shrink(
            entry["home_scored"] / hg / home_baseline if hg else 1.0, hg,
            prior=prior.home_attack if prior else 1.0,
        )
        home_defence = _shrink(
            entry["home_allowed"] / hg / away_baseline if hg else 1.0, hg,
            prior=prior.home_defence if prior else 1.0,
        )
        away_attack = _shrink(
            entry["away_scored"] / ag / away_baseline if ag else 1.0, ag,
            prior=prior.away_attack if prior else 1.0,
        )
        away_defence = _shrink(
            entry["away_allowed"] / ag / home_baseline if ag else 1.0, ag,
            prior=prior.away_defence if prior else 1.0,
        )
        teams.append(
            TeamFit(
                team_id=entry["team_id"],
                team_name=entry["team_name"],
                home_attack=round(home_attack, 4),
                home_defence=round(home_defence, 4),
                away_attack=round(away_attack, 4),
                away_defence=round(away_defence, 4),
                matches=int(hg + ag),
                shots_per_game=shots_by_team.get(entry["team_id"]),
                prior_matches=int(prior.matches) if prior else 0,
                prior_season=prior_season if prior else "",
            )
        )

    matches_observed = int(home_games)
    if not teams:
        quality = "poor"
    elif matches_observed < MIN_GAMES_FOR_LIMITED:
        # A couple of matches is not a league profile, whatever the table shows.
        quality = "poor"
    elif shots_by_team and matches_observed >= MIN_GAMES_FOR_SPLIT:
        quality = "strong"
    elif has_splits and matches_observed >= MIN_GAMES_FOR_SPLIT:
        quality = "medium"
    else:
        quality = "limited"

    return LeagueFit(
        league_id=str(league_id),
        league_name=league_name,
        season=season,
        home_goal_baseline=round(home_baseline, 4),
        away_goal_baseline=round(away_baseline, 4),
        data_quality=quality,
        teams=tuple(teams),
        matches_observed=matches_observed,
        prior_season=prior_season,
        diagnostics={
            "teams": len(teams),
            "home_games": int(home_games),
            "away_games": int(away_games),
            "shrinkage_matches": SHRINKAGE_MATCHES,
            "prior_season": prior_season,
            "teams_with_prior": sum(1 for team in teams if team.prior_matches),
            "teams_differentiated": sum(
                1 for team in teams if team.effective_matches >= MIN_TEAM_MATCHES_FOR_RESULT
            ),
            # Recorded so the limitation is visible in the fit itself, not only in docs.
            "time_decay_applied": False,
        },
    )


def expected_goals(
    *,
    home_attack: float,
    home_defence: float,
    away_attack: float,
    away_defence: float,
    home_baseline: float,
    away_baseline: float,
) -> tuple[float, float]:
    """Convert fitted strengths into the two Poisson rates for one fixture."""
    lambda_home = home_baseline * home_attack * away_defence
    lambda_away = away_baseline * away_attack * home_defence
    return (
        round(max(0.05, min(6.0, lambda_home)), 4),
        round(max(0.05, min(6.0, lambda_away)), 4),
    )


def normalize_team_name(value: str) -> str:
    text = "".join(
        char for char in str(value or "").lower()
        if char.isalnum() or char.isspace()
    )
    return " ".join(text.split())


__all__ = [
    "MIN_GAMES_FOR_PRIOR",
    "MIN_TEAM_MATCHES_FOR_RESULT",
    "MODEL_VERSION",
    "LeagueFit",
    "TeamFit",
    "expected_goals",
    "fit_league_from_standings",
    "normalize_team_name",
    "prior_index",
]

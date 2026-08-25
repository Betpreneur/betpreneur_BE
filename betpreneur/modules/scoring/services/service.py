"""
Fit, store and look up score models.

Fitting is a nightly job. At request time this only reads a cached fit and multiplies
four numbers, so a slip review never waits on model estimation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from betpreneur.modules.scoring.models import LeagueScoreModel, TeamStrength

from ..domain.dixon_coles import DEFAULT_RHO, ScoreMatrix, build_score_matrix
from ..domain.fitting import (
    FALLBACK_AWAY_BASELINE,
    FALLBACK_HOME_BASELINE,
    MIN_GAMES_FOR_PRIOR,
    MIN_TEAM_MATCHES_FOR_RESULT,
    MODEL_VERSION,
    expected_goals,
    fit_league_from_standings,
    normalize_team_name,
)

log = logging.getLogger(__name__)

# How many past seasons to try before giving up on a prior for a league.
PRIOR_SEASON_ATTEMPTS = 2


def _team_evidence(team) -> int:
    """Total matches behind a team's factors, current season plus carried-over prior."""
    if team is None:
        return 0
    return int(getattr(team, "matches", 0) or 0) + int(getattr(team, "prior_matches", 0) or 0)


@dataclass(frozen=True)
class FixtureRates:
    home_rate: float
    away_rate: float
    data_quality: str
    league_id: str = ""
    model_version: str = MODEL_VERSION
    matched_home: bool = False
    matched_away: bool = False
    home_matches: int = 0
    away_matches: int = 0
    # The league's own average scoreline, kept so the reference fixture ("this market for
    # two average sides here") can be built without a second query.
    home_baseline: float = FALLBACK_HOME_BASELINE
    away_baseline: float = FALLBACK_AWAY_BASELINE

    @property
    def usable(self) -> bool:
        """Both sides must be identified; one fitted team and one guess is not a model."""
        return self.matched_home and self.matched_away and self.data_quality != "poor"

    @property
    def differentiated(self) -> bool:
        """
        Whether the model can actually tell these two teams apart.

        With no games played every strength factor collapses to the prior, so a league
        returns the same expected goals for every one of its fixtures. Totals are still
        meaningful in that state -- a league average total is a real estimate -- but a
        result market is not: home advantage alone would rate the home side higher in
        every fixture, however weak they are.
        """
        return (
            self.usable
            and self.home_matches >= MIN_TEAM_MATCHES_FOR_RESULT
            and self.away_matches >= MIN_TEAM_MATCHES_FOR_RESULT
        )

    def matrix(self, rho: float = DEFAULT_RHO) -> ScoreMatrix:
        return build_score_matrix(self.home_rate, self.away_rate, rho=rho)

    def reference_matrix(self, rho: float = DEFAULT_RHO) -> ScoreMatrix:
        """The same league with team strengths switched off."""
        return build_score_matrix(self.home_baseline, self.away_baseline, rho=rho)


class ScoreModelService:
    def _fetch_standings(self, league_id, season=""):
        from betpreneur.modules.catalog.api import statpal_client

        return statpal_client().soccer_endpoint(
            "SOCCER_LEAGUE_STANDINGS",
            params={"season": season} if season else None,
            league_id=str(league_id),
        )

    def prior_season_fit(self, league_id, *, candidate_seasons=(), league_name=""):
        """
        Fit the most recent finished season worth carrying forward.

        StatPal's standings snapshot for a season is not always complete -- England's
        2025/2026 stands at 20 games, frozen mid-January -- so candidates are tried
        newest first and the first one with a real sample wins. Bounded to
        `PRIOR_SEASON_ATTEMPTS` calls so a league with a long, sparse history cannot
        walk the whole archive.
        """
        attempts = 0
        for candidate in candidate_seasons:
            if not candidate or attempts >= PRIOR_SEASON_ATTEMPTS:
                break
            attempts += 1
            try:
                payload = self._fetch_standings(league_id, candidate)
            except Exception:
                log.warning("Prior-season standings fetch failed league=%s season=%s", league_id, candidate)
                continue
            fit = fit_league_from_standings(
                payload,
                league_id=str(league_id),
                league_name=league_name,
                season=candidate,
            )
            if fit.matches_observed >= MIN_GAMES_FOR_PRIOR and fit.teams:
                return fit
        return None

    def fit_league(
        self,
        *,
        league_id,
        league_name="",
        season="",
        standings_payload=None,
        shots_by_team=None,
        prior_fit=None,
        prior_seasons=(),
    ):
        """Fit one league and replace its stored parameters atomically."""
        if standings_payload is None:
            standings_payload = self._fetch_standings(league_id)

        if prior_fit is None and prior_seasons:
            prior_fit = self.prior_season_fit(
                league_id, candidate_seasons=prior_seasons, league_name=league_name
            )

        fit = fit_league_from_standings(
            standings_payload,
            league_id=str(league_id),
            league_name=league_name,
            season=season,
            shots_by_team=shots_by_team,
            prior_fit=prior_fit,
        )

        with transaction.atomic():
            model, _ = LeagueScoreModel.objects.update_or_create(
                provider="statpal",
                league_id=fit.league_id,
                model_version=fit.model_version,
                defaults={
                    "league_name": fit.league_name,
                    "season": fit.season,
                    "home_goal_baseline": fit.home_goal_baseline,
                    "away_goal_baseline": fit.away_goal_baseline,
                    "rho": DEFAULT_RHO,
                    "data_quality": fit.data_quality,
                    "teams_fitted": len(fit.teams),
                    "matches_observed": fit.matches_observed,
                    "prior_season": fit.prior_season,
                    "diagnostics": fit.diagnostics,
                },
            )
            model.teams.all().delete()
            TeamStrength.objects.bulk_create(
                [
                    TeamStrength(
                        model=model,
                        team_id=team.team_id,
                        team_name=team.team_name,
                        team_name_normalized=normalize_team_name(team.team_name),
                        home_attack=team.home_attack,
                        home_defence=team.home_defence,
                        away_attack=team.away_attack,
                        away_defence=team.away_defence,
                        matches=team.matches,
                        prior_matches=team.prior_matches,
                        prior_season=team.prior_season,
                        shots_per_game=team.shots_per_game,
                    )
                    for team in fit.teams
                ],
                batch_size=200,
            )
        return model

    def _find_team(self, model, *, team_id="", team_name=""):
        teams = list(model.teams.all())
        team_id = str(team_id or "").strip()
        if team_id:
            for team in teams:
                if team.team_id and team.team_id == team_id:
                    return team
        normalized = normalize_team_name(team_name)
        if not normalized:
            return None
        for team in teams:
            if team.team_name_normalized == normalized:
                return team
        # Fall back to containment, which handles "Boca Juniors" vs "CA Boca Juniors".
        for team in teams:
            if normalized in team.team_name_normalized or team.team_name_normalized in normalized:
                return team
        return None

    def rates_for_fixture(
        self,
        *,
        league_id,
        home_team_name="",
        away_team_name="",
        home_team_id="",
        away_team_id="",
    ) -> FixtureRates:
        """
        Expected goals for one fixture.

        When the league or either team is unknown we return the league-neutral baseline
        flagged `poor`, so callers can decline to assess rather than publish a number
        built on defaults.
        """
        model = (
            LeagueScoreModel.objects.filter(
                provider="statpal", league_id=str(league_id or ""), model_version=MODEL_VERSION
            )
            .prefetch_related("teams")
            .first()
        )
        if model is None:
            return FixtureRates(
                home_rate=FALLBACK_HOME_BASELINE,
                away_rate=FALLBACK_AWAY_BASELINE,
                data_quality="poor",
                league_id=str(league_id or ""),
            )

        home = self._find_team(model, team_id=home_team_id, team_name=home_team_name)
        away = self._find_team(model, team_id=away_team_id, team_name=away_team_name)

        home_rate, away_rate = expected_goals(
            home_attack=home.home_attack if home else 1.0,
            home_defence=home.home_defence if home else 1.0,
            away_attack=away.away_attack if away else 1.0,
            away_defence=away.away_defence if away else 1.0,
            home_baseline=model.home_goal_baseline,
            away_baseline=model.away_goal_baseline,
        )
        quality = model.data_quality if (home and away) else "poor"
        home_matches = _team_evidence(home)
        away_matches = _team_evidence(away)
        # A league-average fixture must not be sold as a fitted one. Thin per-team
        # evidence caps the quality even when the league-wide sample looks healthy.
        if quality != "poor" and min(home_matches, away_matches) < MIN_TEAM_MATCHES_FOR_RESULT:
            quality = "limited"
        return FixtureRates(
            home_rate=home_rate,
            away_rate=away_rate,
            data_quality=quality,
            league_id=model.league_id,
            model_version=model.model_version,
            matched_home=home is not None,
            matched_away=away is not None,
            home_matches=home_matches,
            away_matches=away_matches,
            home_baseline=model.home_goal_baseline,
            away_baseline=model.away_goal_baseline,
        )


score_model_service = ScoreModelService()

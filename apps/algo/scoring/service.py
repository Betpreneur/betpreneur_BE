"""
Fit, store and look up score models.

Fitting is a nightly job. At request time this only reads a cached fit and multiplies
four numbers, so a slip review never waits on model estimation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from ..models import LeagueScoreModel, TeamStrength
from .dixon_coles import DEFAULT_RHO, ScoreMatrix, build_score_matrix
from .fitting import (
    FALLBACK_AWAY_BASELINE,
    FALLBACK_HOME_BASELINE,
    MODEL_VERSION,
    expected_goals,
    fit_league_from_standings,
    normalize_team_name,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FixtureRates:
    home_rate: float
    away_rate: float
    data_quality: str
    league_id: str = ""
    model_version: str = MODEL_VERSION
    matched_home: bool = False
    matched_away: bool = False

    @property
    def usable(self) -> bool:
        """Both sides must be identified; one fitted team and one guess is not a model."""
        return self.matched_home and self.matched_away and self.data_quality != "poor"

    def matrix(self, rho: float = DEFAULT_RHO) -> ScoreMatrix:
        return build_score_matrix(self.home_rate, self.away_rate, rho=rho)


class ScoreModelService:
    def fit_league(self, *, league_id, league_name="", season="", standings_payload=None, shots_by_team=None):
        """Fit one league and replace its stored parameters atomically."""
        if standings_payload is None:
            from ..statpal import StatPalClient

            standings_payload = StatPalClient().soccer_endpoint(
                "SOCCER_LEAGUE_STANDINGS", league_id=str(league_id)
            )

        fit = fit_league_from_standings(
            standings_payload,
            league_id=str(league_id),
            league_name=league_name,
            season=season,
            shots_by_team=shots_by_team,
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
        return FixtureRates(
            home_rate=home_rate,
            away_rate=away_rate,
            data_quality=quality,
            league_id=model.league_id,
            model_version=model.model_version,
            matched_home=home is not None,
            matched_away=away is not None,
        )


score_model_service = ScoreModelService()

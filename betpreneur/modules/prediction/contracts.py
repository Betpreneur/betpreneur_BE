"""Contracts for the shared prediction module.

These dataclasses are intentionally product-neutral. They describe what the
football models believe, not whether a pick should be published, replaced, or
sold.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _validate_probability(value: float | None, field_name: str) -> None:
    if value is None:
        return
    if value < 0 or value > 1:
        raise ValueError(f"{field_name} must be between 0.0 and 1.0")


def _validate_score(value: float | None, field_name: str) -> None:
    if value is None:
        return
    if value < 0 or value > 100:
        raise ValueError(f"{field_name} must be between 0 and 100")


def _fair_odds(probability: float | None) -> float | None:
    if probability is None or probability <= 0:
        return None
    return round(1 / probability, 4)


def _as_payload(value) -> dict[str, Any]:
    return asdict(value)


@dataclass(frozen=True, slots=True)
class PredictionDiagnostics:
    """Machine-readable health and provenance for a prediction result."""

    data_quality: str = "unknown"
    model_version: str = "prediction-boundary-v1"
    model_sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class TeamStrengthSnapshot:
    """Team strength inputs available to the prediction engine."""

    team_id: str
    team_name: str = ""
    league_key: str = ""
    season: str = ""
    elo: float | None = None
    attack_rating: float | None = None
    defence_rating: float | None = None
    recent_form_score: float | None = None
    data_quality: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class FixtureFeatureSet:
    """Normalized fixture features consumed by statistical models."""

    fixture_id: str
    fixture_name: str = ""
    league_key: str = ""
    season: str = ""
    home_team: TeamStrengthSnapshot | None = None
    away_team: TeamStrengthSnapshot | None = None
    features: dict[str, Any] = field(default_factory=dict)
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class GoalModelOutput:
    """Goal distribution output before calibration or product policy.

    Scoreline probabilities use decimal probability values in the 0.0-1.0
    range.
    """

    home_expected_goals: float | None = None
    away_expected_goals: float | None = None
    scoreline_matrix: dict[str, float] = field(default_factory=dict)
    over_1_5_probability: float | None = None
    over_2_5_probability: float | None = None
    under_3_5_probability: float | None = None
    btts_probability: float | None = None
    team_goal_probabilities: dict[str, dict[str, float]] = field(default_factory=dict)
    result_probabilities: dict[str, float] = field(default_factory=dict)
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        for scoreline, probability in self.scoreline_matrix.items():
            _validate_probability(probability, f"scoreline_matrix[{scoreline!r}]")
        _validate_probability(self.over_1_5_probability, "over_1_5_probability")
        _validate_probability(self.over_2_5_probability, "over_2_5_probability")
        _validate_probability(self.under_3_5_probability, "under_3_5_probability")
        _validate_probability(self.btts_probability, "btts_probability")
        for team, probabilities in self.team_goal_probabilities.items():
            for market, probability in probabilities.items():
                _validate_probability(probability, f"team_goal_probabilities[{team!r}][{market!r}]")
        for market, probability in self.result_probabilities.items():
            _validate_probability(probability, f"result_probabilities[{market!r}]")

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class CountModelOutput:
    """Count-event model output for corners, cards, and shots on target.

    These events are separate from goals: each has its own feature family,
    expected totals, and line probabilities.
    """

    expected_total_corners: float | None = None
    expected_total_cards: float | None = None
    expected_total_sot: float | None = None
    line_probabilities: dict[str, dict[str, float]] = field(default_factory=dict)
    team_line_probabilities: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    expected_team_counts: dict[str, dict[str, float]] = field(default_factory=dict)
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        for event, probabilities in self.line_probabilities.items():
            for market, probability in probabilities.items():
                _validate_probability(probability, f"line_probabilities[{event!r}][{market!r}]")
        for event, side_payload in self.team_line_probabilities.items():
            for side, probabilities in side_payload.items():
                for market, probability in probabilities.items():
                    _validate_probability(
                        probability,
                        f"team_line_probabilities[{event!r}][{side!r}][{market!r}]",
                    )

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class ResultProbabilityOutput:
    """Raw match-result probabilities before pricing decisions.

    Probabilities use decimal values in the 0.0-1.0 range.
    """

    home_win: float | None = None
    draw: float | None = None
    away_win: float | None = None
    home_elo: float | None = None
    away_elo: float | None = None
    elo_gap: float | None = None
    home_result_probability: float | None = None
    draw_probability: float | None = None
    away_result_probability: float | None = None
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        if self.home_result_probability is None and self.home_win is not None:
            object.__setattr__(self, "home_result_probability", self.home_win)
        if self.draw_probability is None and self.draw is not None:
            object.__setattr__(self, "draw_probability", self.draw)
        if self.away_result_probability is None and self.away_win is not None:
            object.__setattr__(self, "away_result_probability", self.away_win)
        _validate_probability(self.home_win, "home_win")
        _validate_probability(self.draw, "draw")
        _validate_probability(self.away_win, "away_win")
        _validate_probability(self.home_result_probability, "home_result_probability")
        _validate_probability(self.draw_probability, "draw_probability")
        _validate_probability(self.away_result_probability, "away_result_probability")

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Raw-to-calibrated probability adjustment.

    Probability values are decimal 0.0-1.0 values. This object describes model
    reliability only; product recommendation decisions belong outside
    prediction.
    """

    raw_probability: float | None
    calibrated_probability: float | None
    method: str = "none"
    calibration_penalty: float | None = None
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        _validate_probability(self.raw_probability, "raw_probability")
        _validate_probability(self.calibrated_probability, "calibrated_probability")

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class MarketProbability:
    """Probability for one market on one fixture.

    This contract intentionally excludes product decisions such as Banker,
    Value Gem, Wild Card, publishability, or replacement advice. It says what
    the model believes and how reliable that belief is.
    """

    fixture_id: str
    market: str
    raw_probability: float | None = None
    calibrated_probability: float | None = None
    confidence_score: float | None = None
    fair_odds: float | None = None
    model: str = ""
    data_quality: str = "unknown"
    model_sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    explanation_facts: tuple[str, ...] = ()
    supporting_facts: tuple[str, ...] = ()
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        _validate_probability(self.raw_probability, "raw_probability")
        _validate_probability(self.calibrated_probability, "calibrated_probability")
        _validate_score(self.confidence_score, "confidence_score")
        if not self.supporting_facts and self.explanation_facts:
            object.__setattr__(self, "supporting_facts", self.explanation_facts)
        if not self.explanation_facts and self.supporting_facts:
            object.__setattr__(self, "explanation_facts", self.supporting_facts)
        if self.fair_odds is None:
            object.__setattr__(self, "fair_odds", _fair_odds(self.effective_probability))

    @property
    def effective_probability(self) -> float | None:
        """Best probability for product layers to consume."""
        return (
            self.calibrated_probability
            if self.calibrated_probability is not None
            else self.raw_probability
        )

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class ValueAssessment:
    """Odds-aware value math for one market.

    This still stops short of product policy. A positive value_score is an input
    to picks/slips/pricing, not a decision to publish or recommend.
    """

    fixture_id: str
    market: str
    calibrated_probability: float | None = None
    available_odds: float | None = None
    fair_odds: float | None = None
    bookmaker_implied_probability: float | None = None
    edge: float | None = None
    ev: float | None = None
    edge_score: float | None = None
    value_score: float | None = None
    odds_source_penalty: float = 0.0
    sample_size_penalty: float = 0.0
    market_volatility_penalty: float = 0.0
    league_uncertainty_penalty: float = 0.0
    correlation_penalty: float = 0.0
    pricing_warning: str = ""
    pricing_warnings: tuple[str, ...] = ()
    explanation_facts: tuple[str, ...] = ()
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        _validate_probability(self.calibrated_probability, "calibrated_probability")
        _validate_probability(self.bookmaker_implied_probability, "bookmaker_implied_probability")
        if not self.pricing_warning and self.pricing_warnings:
            object.__setattr__(self, "pricing_warning", self.pricing_warnings[0])
        if not self.pricing_warnings and self.pricing_warning:
            object.__setattr__(self, "pricing_warnings", (self.pricing_warning,))

    @property
    def total_penalty(self) -> float:
        return round(
            self.odds_source_penalty
            + self.sample_size_penalty
            + self.market_volatility_penalty
            + self.league_uncertainty_penalty
            + self.correlation_penalty,
            2,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _as_payload(self)
        payload["total_penalty"] = self.total_penalty
        return payload


@dataclass(frozen=True, slots=True)
class RecommendationScore:
    """Balanced score for ranking markets without letting one signal dominate."""

    fixture_id: str
    market: str
    recommendation_score: float | None = None
    calibrated_probability_score: float | None = None
    market_fit_score: float | None = None
    value_score: float | None = None
    uncertainty_penalty: float = 0.0
    weak_market_penalty: float = 0.0
    correlation_penalty: float = 0.0
    stale_data_penalty: float = 0.0
    warnings: tuple[str, ...] = ()
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        _validate_score(self.recommendation_score, "recommendation_score")
        _validate_score(self.calibrated_probability_score, "calibrated_probability_score")
        _validate_score(self.market_fit_score, "market_fit_score")
        _validate_score(self.value_score, "value_score")

    @property
    def total_penalty(self) -> float:
        return round(
            self.uncertainty_penalty
            + self.weak_market_penalty
            + self.correlation_penalty
            + self.stale_data_penalty,
            2,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _as_payload(self)
        payload["total_penalty"] = self.total_penalty
        return payload


@dataclass(frozen=True, slots=True)
class CorrelationPair:
    """Detected relationship between two markets in the same fixture."""

    fixture_id: str
    left_market: str
    right_market: str
    relationship: str
    direction: str
    strength: float
    reason: str = ""

    def __post_init__(self) -> None:
        _validate_probability(self.strength, "strength")

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class CorrelationReport:
    """Ticket-level correlation and exposure diagnostics."""

    pairs: tuple[CorrelationPair, ...] = ()
    fixture_exposure: dict[str, int] = field(default_factory=dict)
    market_family_exposure: dict[str, int] = field(default_factory=dict)
    max_fixture_share: float = 0.0
    max_market_family_share: float = 0.0
    concentration_score: float = 0.0
    warnings: tuple[str, ...] = ()
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        _validate_probability(self.max_fixture_share, "max_fixture_share")
        _validate_probability(self.max_market_family_share, "max_market_family_share")
        _validate_score(self.concentration_score, "concentration_score")

    @property
    def has_correlation(self) -> bool:
        return bool(self.pairs)

    def to_dict(self) -> dict[str, Any]:
        payload = _as_payload(self)
        payload["has_correlation"] = self.has_correlation
        return payload


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One out-of-sample evaluation step for a prediction day."""

    prediction_date: str
    train_through: str
    train_samples: int = 0
    test_samples: int = 0
    wins: int = 0
    losses: int = 0
    voids: int = 0
    average_predicted_probability: float | None = None
    actual_hit_rate: float | None = None
    brier_score: float | None = None
    roi: float | None = None
    leakage_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_probability(self.average_predicted_probability, "average_predicted_probability")
        _validate_probability(self.actual_hit_rate, "actual_hit_rate")

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluation:
    """Out-of-sample validation report over frozen prediction samples."""

    folds: tuple[WalkForwardFold, ...] = ()
    total_train_samples: int = 0
    total_test_samples: int = 0
    average_brier_score: float | None = None
    actual_hit_rate: float | None = None
    roi: float | None = None
    leakage_warnings: tuple[str, ...] = ()
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        _validate_probability(self.actual_hit_rate, "actual_hit_rate")

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class FixturePrediction:
    """Product-neutral prediction bundle for a football fixture."""

    fixture_id: str
    fixture_name: str = ""
    features: FixtureFeatureSet | None = None
    goals: GoalModelOutput | None = None
    counts: CountModelOutput | None = None
    result: ResultProbabilityOutput | None = None
    market_probabilities: tuple[MarketProbability, ...] = ()
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def market(self, name: str) -> MarketProbability | None:
        """Return a market probability by canonical/display name."""
        normalized = name.strip().lower()
        for probability in self.market_probabilities:
            if probability.market.strip().lower() == normalized:
                return probability
        return None

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class TicketSimulation:
    """Monte Carlo ticket output with correlation and concentration diagnostics."""

    selections: tuple[MarketProbability, ...] = ()
    simulations: int = 0
    estimated_success_probability: float | None = None
    independent_success_probability: float | None = None
    correlation_adjustment: float | None = None
    risk_concentration_score: float | None = None
    fixture_exposure: dict[str, int] = field(default_factory=dict)
    portfolio_exposure: dict[str, int] = field(default_factory=dict)
    correlation_warnings: tuple[str, ...] = ()
    diagnostics: PredictionDiagnostics = field(default_factory=PredictionDiagnostics)

    def __post_init__(self) -> None:
        _validate_probability(self.estimated_success_probability, "estimated_success_probability")
        _validate_probability(
            self.independent_success_probability, "independent_success_probability"
        )

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class TrainingSampleRecord:
    """One settled product prediction ready for calibration.

    This is an ingestion contract, not a recommendation contract. Product
    modules pass plain data into prediction; prediction stores the canonical
    row and keeps reruns deduplicated.
    """

    fixture_id: str
    canonical_market: str
    settlement_result: str
    first_prediction_score: float | None = None
    last_prediction_score: float | None = None
    selected_status: str = ""
    published_status: str = ""
    odds_source: str = ""
    real_odds: Decimal | str | float | int | None = None
    estimated_odds: bool = False
    market_family: str = ""
    line: str = ""
    side: str = ""
    league_key: str = ""
    season: str = ""
    kickoff: datetime | None = None
    prediction_created_at: datetime | None = None
    source: str = ""
    source_reference: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.fixture_id or "").strip():
            raise ValueError("fixture_id is required")
        if not str(self.canonical_market or "").strip():
            raise ValueError("canonical_market is required")
        _validate_score(self.first_prediction_score, "first_prediction_score")
        _validate_score(self.last_prediction_score, "last_prediction_score")

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)


@dataclass(frozen=True, slots=True)
class TeamMatchFeedbackRecord:
    """One team's settled match actuals tied to the prediction snapshot."""

    fixture_id: str
    team_name: str
    side: str
    actual_result: str
    provider_match_id: str = ""
    fixture_name: str = ""
    match_date: date | None = None
    league_key: str = ""
    season: str = ""
    team_id: str = ""
    opponent_id: str = ""
    opponent_name: str = ""
    goals_for: int | None = None
    goals_against: int | None = None
    corners_for: float | None = None
    corners_against: float | None = None
    cards_for: float | None = None
    cards_against: float | None = None
    shots_on_target_for: float | None = None
    shots_on_target_against: float | None = None
    referee_name: str = ""
    source: str = ""
    prediction_snapshot: dict[str, Any] = field(default_factory=dict)
    actual_stats: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.fixture_id or "").strip():
            raise ValueError("fixture_id is required")
        if not str(self.team_name or "").strip():
            raise ValueError("team_name is required")
        if str(self.side or "").strip().lower() not in {"home", "away"}:
            raise ValueError("side must be home or away")
        if str(self.actual_result or "").strip().lower() not in {"win", "draw", "loss"}:
            raise ValueError("actual_result must be win, draw, or loss")

    def to_dict(self) -> dict[str, Any]:
        return _as_payload(self)

# Match Checker — System Architecture

**Status:** Draft for review
**Last updated:** 2026-08-15
**Scope:** Bet-slip import → per-leg market assessment → ticket-level risk → repair suggestions

---

## 1. Product statement

> Before you stake this ticket, show exactly which legs are likely to ruin it, why they are
> risky, and what a more defensible version of the ticket looks like.

Positioning is **"we challenge your ticket before the match does"** — not "we predict winners".

Three rules constrain every design decision in this document:

1. **Never assert more than the evidence supports.** A market we cannot see is *unknown*, not *bad*.
2. **Probability and confidence are different axes.** 72% on thin data is not 72% on strong data.
3. **A repair is an evidence-based alternative, not a promise of better returns.** Odds go down.

---

## 2. Data reality (measured 2026-08-08)

All endpoint findings below were probed live against `statpal.io/api/v2` using the production
key, sampling Argentina Primera (`league_id=2914`). **This section is the factual basis for the
rest of the document.** Re-verify before major model work.

### 2.1 Endpoint inventory

| Endpoint | Params | What it actually gives us | Verdict |
|---|---|---|---|
| `soccer/leagues` | — | league id, country, name, season, date range | Catalogue |
| `soccer/leagues/{id}/standings` | — | per team **home/away/overall**: `games_played`, `goals_scored`, `goals_allowed`, W/D/L, `recent_form` (`WDWWL`) | **λ inputs** |
| `soccer/teams/{team_id}` | — | per team per league per period (`firsthalf`/`secondhalf`/`fulltime`), each split **home/away/total** | **Primary feature source** |
| `soccer/leagues/{id}/stats` | — | full squad, **per-player season stats** (46 fields) | Player props, team aggregates |
| `soccer/leagues/{id}/matches/stats` | — | per match: goals, goal events w/ minute, card events w/ minute + player, lineups, subs (+injury flag), **referee name**, stadium | Match history, cards |
| `soccer/leagues/{id}/odds/prematch` | — | **90 market groups** per match × ~70 bookmakers, each with stable market `id` + `name` | **Market prior + dictionary** |
| `soccer/head-to-head` | `team1_id`, `team2_id` | historical meetings, biggest win/defeat | H2H feature |
| `soccer/predictions` | `match_id` (= `main_id`) | single `choice` string, one 1x2 odd, prose `reasoning` | **Low value — a tip, not probabilities** |
| `soccer/injuries-suspensions` | — | league-wide, per match | Availability |
| `soccer/team-lineups` | `match_id` | projected/confirmed lineups | Player props (gated) |
| `soccer/matches/live`, `soccer/odds/live*`, `soccer/live-storylines` | — | live data | Out of scope (pre-match product) |
| `soccer/weather-forecast`, `soccer/images`, `soccer/coaches/{id}` | — | ancillary | Low priority |

### 2.2 The critical finding: no xG anywhere

**StatPal does not expose xG or xGA on any endpoint.** The input ladder therefore tops out one
rung lower than a typical modelling stack, and `data_quality` must reflect that permanently.

What *is* available for goal modelling, per team, split home/away:

```
soccer/teams/{id} → league_stats.league[].fulltime.*
  avg_goals_per_game_scored     {home, away, total}
  avg_goals_per_game_conceded   {home, away, total}
  goals_for / goals_against     {home, away, total}
  shots_total / shots_on_goal   {home, away, total}
  possession                    {home, away, total}
  clean_sheet / failed_to_score {home, away, total}
```

### 2.3 Corners and cards ARE available

Earlier analysis concluded corners had no source. That was wrong — `match-stats` has no corners,
but `soccer/teams/{id}` does:

```
  corners / avg_corners         {home, away, total}
  yellowcards / avg_yellowcards {home, away, total}
  redcards / avg_redcards       {home, away, total}
  fouls / offsides              {home, away, total}
```

Plus **referee name** on every match in `match-stats` — the single strongest cards feature.

### 2.4 Goal-interval markets are directly supported

`soccer/teams/{id}` returns minute-bucket distributions:

```
scoring_minutes.period[]        {min: "0-15", count: "1", pct: "50%"}
goals_conceded_minutes.period[]
yellowcard_minutes.period[]
redcard_minutes.period[]
avg_first_goal_scored / avg_first_goal_conceded
```

Buckets are 15-minute, 0–90. This makes "Team to score 1–15" and "first goal before X" cheap,
which reverses the earlier recommendation to defer interval markets.

### 2.5 Prematch odds is a first-class asset

90 market groups per fixture, each with a stable numeric id, across ~70 bookmakers. Sample:

```
1834 1x2                    2055 Double Chance         24477 Corners Over Under
1838 Over/Under             1848 Both Teams To Score   24791 Cards Over/Under
1837 Asian Handicap         1846 Clean Sheet - Home    24678 Anytime Goal Scorer
1914 Correct Score          24447 Exact Goals Number   24665 Winning Margin
24669 Goal Line             4057 Team To Score First   24709 1x2 - 15 minutes
```

Two independent uses:

1. **Market-implied prior.** De-vigged consensus across bookmakers gives a probability estimate
   for most markets, independent of our model. Use for calibration, sanity bounds, and as the
   fallback when our own model has no coverage.
2. **Market dictionary.** The id/name pairs are a ready-made canonical vocabulary.

### 2.6 Per-player season stats (46 fields)

```
goals assists minutes_played appearences lineups substitute_in
shots_total shots_on shots_woodwork
yellowcards redcards yellowred fouls_committed fouls_drawn
penalties_scored penalties_missed penalties_won penalties_committed penalties_saved
key_passes pass_attempts pass_success crosses_total crosses_accurate
dribble_attempts dribble_success duels_total duels_won tackles interceptions
blocks clearances saves inside_box_saves dispossesed offsides rating position age injured
```

Sufficient for anytime-scorer, shots, shots-on-target, cards and assists props. `minutes_played`
+ `lineups` + `appearences` gives expected-minutes. Note `injured` is also on the player row.

### 2.7 Data caveats

- **Everything is a string.** `"2"`, `"0.86"`, `"33%"`. Parse defensively at the boundary.
- **Small samples early season.** The sampled team had `games_played` in single digits and
  `avg_goals_per_game_scored.away = "0"`. Shrinkage is mandatory, not optional.
- **Sparse fields.** Many `match-stats` entries have `null` for `lineups`, `event_summary`,
  `player_stats` — especially for `status: "Not Started"`.
- **Multiple id systems.** `main_id`, `fallback_id_1..3`, plus team/player/league ids. Store all;
  `main_id` is what `soccer/predictions` and `soccer/team-lineups` accept.
- **Date format is `DD.MM.YYYY`**, times local to the fixture.

### 2.8 Bookmaker side: SportyBet sends structured market identity

`_market_name` in `services.py` already reads:

```python
market_id  = item.get("marketId")     # stable market id
specifier  = item.get("specifier")    # e.g. "total=2.5"
outcome_id = item.get("outcomeId")    # which side of the market
```

…then collapses them to a display string and **discards the structure**. Every downstream
ambiguity (`Over 9.5` = goals or corners?) is a direct consequence of that discard.

---

## 3. Capability model

Two vocabularies exist today and are incorrectly compared: descriptor `data_requirements`
(`team_stats`, `league_stats`, `h2h`, `odds`) versus snapshot names (`detailed_stats`,
`predictions`, `prematch_odds`, `lineups`, `injuries_suspensions`). They can never match, so the
fallback path always computes 0% coverage.

Replace both with one canonical enum, and map providers into it:

```python
class DataCapability(StrEnum):
    TEAM_GOALS_FOR        # goals scored, home/away split
    TEAM_GOALS_AGAINST
    TEAM_SHOTS
    TEAM_POSSESSION
    TEAM_CORNERS
    TEAM_CARDS
    TEAM_FOULS
    TEAM_CLEAN_SHEET
    GOAL_MINUTE_DIST      # scoring_minutes buckets
    CARD_MINUTE_DIST
    PLAYER_SEASON_STATS
    LINEUP_PROJECTED
    LINEUP_CONFIRMED
    INJURIES
    REFEREE
    H2H
    MARKET_ODDS
```

Provider mapping (StatPal, measured):

| Endpoint | Provides |
|---|---|
| `teams/{id}` | `TEAM_GOALS_FOR/AGAINST`, `TEAM_SHOTS`, `TEAM_POSSESSION`, `TEAM_CORNERS`, `TEAM_CARDS`, `TEAM_FOULS`, `TEAM_CLEAN_SHEET`, `GOAL_MINUTE_DIST`, `CARD_MINUTE_DIST` |
| `leagues/{id}/standings` | `TEAM_GOALS_FOR/AGAINST` (corroborating) |
| `leagues/{id}/stats` | `PLAYER_SEASON_STATS` |
| `leagues/{id}/matches/stats` | `REFEREE`, match history, card events |
| `leagues/{id}/odds/prematch` | `MARKET_ODDS` |
| `head-to-head` | `H2H` |
| `team-lineups` | `LINEUP_PROJECTED` / `LINEUP_CONFIRMED` |
| `injuries-suspensions` | `INJURIES` |

Each evaluator declares `required_capabilities` and `optional_capabilities`. Coverage is then a
well-defined set operation, and adding a second provider later is a mapping change only.

---

## 4. Leg lifecycle

Every leg walks one state machine. Each stage has an explicit terminal, and the API reports
**where a leg stopped** rather than coercing it into a verdict.

```
PARSED ─► RECOGNIZED ─► FIXTURE_RESOLVED ─► DATA_PLANNED ─► DATA_AVAILABLE ─► MODEL_AVAILABLE ─► ASSESSED
   │           │                │                                  │                  │
   ▼           ▼                ▼                                  ▼                  ▼
UNPARSEABLE  UNKNOWN_MARKET  UNMATCHED                      INSUFFICIENT_DATA      NO_MODEL
                             AMBIGUOUS_FIXTURE
                             EXPIRED
```

### 4.1 The assessment contract

**Invariant, enforced in code:** `probability` may be non-null **only** when
`assessment_type == "quantitative_model"`.

```json
{
  "state": "ASSESSED",
  "assessment_type": "quantitative_model",
  "model": { "name": "score_matrix", "version": "sm-1.2.0" },
  "probability": 0.71,
  "confidence": 0.83,
  "data_quality": "strong",
  "confidence_cap": 88,
  "capabilities_used": ["TEAM_GOALS_FOR", "TEAM_GOALS_AGAINST", "MARKET_ODDS"],
  "capabilities_missing": []
}
```

A heuristic path returns `assessment_type: "heuristic"`, a `score`, and `probability: null`.
This single invariant is what prevents a constant-58 evaluator from rendering as "67%", and it
is what makes a phased migration honest — the payload always states which kind of answer it is.

---

## 5. Pipeline

```
                    SPORTYBET / BETANO SLIP
                              │
                   ┌──────────▼──────────┐
                   │ 1. IMPORT           │  raw provider payload, preserved
                   └──────────┬──────────┘
                   ┌──────────▼──────────┐
                   │ 2. NORMALIZE        │  (marketId, specifier, outcomeId) → canonical
                   └──────────┬──────────┘
                   ┌──────────▼──────────┐
                   │ 3. FIXTURE RESOLVE  │  cached mapping, learned
                   └──────────┬──────────┘
                   ┌──────────▼──────────┐
                   │ 4. CAPABILITY PLAN  │  union over ALL legs of this fixture
                   └──────────┬──────────┘
                   ┌──────────▼──────────┐
                   │ 5. DATA REPOSITORY  │  cache → StatPal, per fixture (not per leg)
                   └──────────┬──────────┘
                   ┌──────────▼──────────┐
                   │ 6. FEATURE LAYER    │  typed features, persisted with the prediction
                   └──────────┬──────────┘
              ┌───────────────┴───────────────┐
              ▼                               ▼
   ┌────────────────────┐        ┌────────────────────────┐
   │ 7a. SCORE MATRIX   │        │ 7b. SPECIALISED MODELS │
   │ 1x2 DC DNB O/U     │        │ corners cards player   │
   │ BTTS CS totals AH  │        │ intervals              │
   │ exact/correct/marg │        │                        │
   └─────────┬──────────┘        └───────────┬────────────┘
             └───────────────┬───────────────┘
                   ┌─────────▼───────────┐
                   │ 8. ASSESSMENT       │  probability + data_quality + confidence_cap
                   └─────────┬───────────┘
                   ┌─────────▼───────────┐
                   │ 9. ALTERNATIVES     │  same fixture, intent-preserving, constrained
                   └─────────┬───────────┘
                   ┌─────────▼───────────┐
                   │ 10. TICKET ENGINE   │  correlation-aware, killers, repair plan
                   └─────────┬───────────┘
                   ┌─────────▼───────────┐
                   │ 11. EXPLANATION     │  template-first, LLM optional, validated
                   └─────────────────────┘
```

Steps 4–9 run per leg; 4–6 are deduplicated **per fixture**.

---

## 6. Module layout

One Django app, clear internal seams. Not microservices.

```
apps/algo/
  ingest/          sportybet.py betano.py             → RawSelection
  normalize/       resolver.py bookmaker_map.py       → CanonicalSelection
  fixtures/        matcher.py mapping.py
  data/
    capability.py  DataCapability enum + provider map
    planner.py     required_capabilities(legs) -> plan     ← single source of truth
    statpal/       client.py endpoints.py parsers.py
    repository.py  cache, TTL, rate budget
  features/        team_form.py goals.py cards.py corners.py player.py intervals.py
  models_/
    registry.py    MARKET_EVALUATORS[family]
    score_matrix/  dixon_coles.py fitting.py derived.py
    corners.py cards.py player.py intervals.py
  risk/            ticket.py correlation.py calibration.py
  alternatives/    generator.py ranking.py
  explain/         templates.py llm.py validator.py
  api/             views split out of the current 4,736-line module
```

**Dispatch rule:** `family` selects the evaluator. Data requirements select only what gets
*fetched*. A metadata flag must never determine dispatch — that is the current bug where
`requires_player_stats` hijacks `First to Score H` into the player model.

---

## 7. ADR-001 — One score distribution, not fifteen models

**Decision.** Fit a per-fixture goal distribution and derive every goals/result market from it.

**Model.** Dixon-Coles: independent Poisson with the low-score correction τ for 0-0/1-0/0-1/1-1.
Plain Poisson under-predicts draws, and draws are where Double Chance lives — the market we most
want for repairs.

- Attack/defence strength per team, home advantage per league.
- Exponential time decay on past matches (half-life ≈ 60–90 days).
- **Hierarchical shrinkage** toward a league prior, and small leagues toward a global prior.
  Non-negotiable: slips are full of second divisions with tiny samples (§2.7).
- Truncate at 8 goals per side, renormalise.
- **Refit nightly per league.** Request-time work is a strength lookup, λ computation, matrix
  build and summation — microseconds.

**Inputs** (measured, §2.2), best available rung wins:

| Rung | Source | `data_quality` |
|---|---|---|
| 1 | `avg_goals_*` + `shots_total`/`shots_on_goal` + `possession`, home/away split | `strong` |
| 2 | `avg_goals_*` home/away split only | `medium` |
| 3 | `standings` overall goals only | `limited` |
| 4 | league baseline | `poor` → leg is **unassessed** |

There is no xG rung. `strong` here is a shots-informed goals model, and the confidence caps must
be set with that ceiling in mind.

**Derived markets** — all by summation over the matrix:

```
Home/Draw/Away · 1X/X2/12 · DNB · O/U any line · BTTS · Clean sheet H/A
Team totals · Asian handicap · Exact goals · Correct score · Winning margin
Odd/Even · Result+BTTS combos · Goal line
```

**Consequences.**
- ~15 of 29 families come from one model, replacing `_evaluate_fixture_context_market`.
- Markets become **arithmetically consistent**: `P(1X) = P(Home) + P(Draw)` by construction.
- Same-fixture correlation becomes computable (ADR-002).
- First-half variants come free using `firsthalf` period stats.

**Rejected:** independent per-market heuristics (status quo — inconsistent, unmaintainable);
pure market-implied probabilities from odds (no edge, and unavailable for some markets).

---

## 8. ADR-002 — Correlation is in scope for same-fixture legs only

**Problem.** `P(ticket) = Π pᵢ` assumes independence. Slips routinely contain two legs on the
same fixture (`Home Win` + `Over 2.5`), where independence is badly wrong. The current
`ticket_risk.py` multiplies unconditionally.

**Decision.**
- Group legs by fixture. Within a group, compute the **joint probability from the shared score
  matrix** — `P(Home Win ∧ Over 2.5) = Σ cells where h>a ∧ h+a≥3`.
- Multiply *across* fixture groups.
- Legs in the same fixture that are not both matrix-derived (e.g. a corners leg + a goals leg)
  fall back to independence, flagged in the payload.
- **Cross-fixture correlation is explicitly out of scope.** Documented assumption, not an
  oversight.

**Consequence.** Ticket probabilities rise for positively-correlated same-match legs, which is
correct and currently under-reported.

---

## 9. ADR-003 — Hierarchical calibration

**Problem.** Per-family × per-band calibration needs ~200 settled legs per cell. At 15 families ×
5 bands that is ~15,000 settled legs, or roughly 2,000 reviewed slips, before any per-family
number means anything.

**Decision.** Three-level shrinkage, each level detaching from its parent as evidence arrives:

```
global prior ──► family level ──► family × probability band
        shrink toward parent, weight ∝ n (Beta, prior weight ≈ 25)
```

The API always reports `basis` (`prior` | `blended` | `empirical`), `level`, and `sample_size`.
This is the existing Phase 1 mechanism extended one level.

**Measure calibration, not accuracy.** When the model says 70%, does it happen ~70% of the time?
Track a reliability curve per family over time. That is the number worth showing users.

**Feedstock** is the settlement engine already built: `SlipSelection.outcome` + `advisory_score`,
settled nightly against finished fixtures.

---

## 10. ADR-004 — Alternative ranking objective

**Problem.** Ranking alternatives by probability alone always recommends `Under 8.5 Goals`.

**Decision.** Rank by **Δ(ticket probability) at the user's stake**, subject to hard constraints:

| Constraint | Rule |
|---|---|
| Intent preservation | Same fixture, same directional thesis. Backed Chelsea → never offer Over 1.5 or a Brentford market. |
| Correlation guard | Reject alternatives near-duplicating another leg already in the ticket. |
| Odds floor | Reject below a configured minimum (default 1.10). |
| Assessment floor | Alternative must be `quantitative_model`. Never suggest a swap from a heuristic. |
| Disclosure | Always surface that odds decrease. Never present a repair as more profitable. |

**Labelling.** Until ADR-001 ships, alternatives are labelled **"possible lower-risk market"**,
not "recommended replacement". A constant-58 evaluator does not justify a recommendation.

---

## 11. Async execution and cost

Current: serial per-leg loop, one `AlgoRun` per leg, worker at `--concurrency=1`. A 20-leg slip
does not ship on this.

```
POST /slip-reviews ─► 202 {review_id, status: queued}

  import+normalize ─► fixture resolve (batched) ─► hydrate (per DISTINCT FIXTURE)
                                                        │
                                              chord: leg analysis (parallel, pure CPU)
                                                        │
                                              ticket analysis ─► persist ─► done
```

- **Hydration is per fixture, not per leg.** A 20-leg slip over 14 fixtures fetches 14 fixture
  bundles, not 20.
- **Per-review StatPal call budget** with a hard ceiling. On exhaustion, degrade legs to
  `INSUFFICIENT_DATA` rather than breaching rate limits. Emit `api_usage` per review.
- Because fitting is nightly, leg analysis is pure CPU. **Target: p95 < 15s for 20 legs.**
- Progress is Redis-backed and available through WebSockets, with HTTP polling kept as a
  fallback (`GET /slip-reviews/{id}` and `GET /slip-reviews/{id}/events`).

### Cache TTLs

| Data | TTL | Rationale |
|---|---|---|
| League catalogue | 24h | Static |
| Standings | 6h | Changes after matchdays |
| Team season stats | 6h | Same |
| Player season stats | 12h | Slow-moving |
| Match history / results | 24h (immutable once FT) | Never changes |
| Prematch odds | 30–60m | Moves, but not critical pre-match |
| Injuries/suspensions | 3h | |
| Projected lineup | 60m | |
| Confirmed lineup | until FT | Immutable once confirmed |
| Fitted league model | 24h | Nightly refit |
| Private slip-review market cache | 72h default | Covers today/tomorrow/next tomorrow |

### Private slip-review market cache

The private cache exists for one product reason: a slip review should usually be a lookup, not a
fresh provider crawl while the bettor is watching a loading screen.

It is intentionally separate from the public all-games/top-picks storage:

| Store | Purpose | League scope | User-visible feed |
|---|---|---|---|
| `MarketPrediction` | Public daily game details and top picks | Restricted tracked leagues | Yes |
| `SlipReviewMarketCache` | Private pre-scored markets for Match Checker | Broad StatPal fixture universe | No, only used to answer slips |

This lets Match Checker prepare broad coverage without accidentally publishing every StatPal
fixture in the public feed.

**Write path.**

1. `build_slip_review_market_cache` syncs the broad StatPal fixture horizon.
2. Each StatPal `FixtureCache` row is enriched with API-Football context when available.
3. The scoring engine produces the normal market payloads.
4. `SlipReviewMarketCacheWriter` bulk-upserts every market into `SlipReviewMarketCache`.

Daily/on-demand fixture scoring also writes to this private cache as a side effect. That write is
fail-open: a cache write failure is logged, but it must not fail fixture scoring.

**Read path.**

`_manual_fixture_game(...)` now checks sources in this order:

1. `MarketPrediction` for an already-scored public/daily/on-demand fixture.
2. `SlipReviewMarketCache` for non-expired private rows matching `match_id`, `provider_match_id`,
   or `statpal:{provider_match_id}`.
3. Existing heavy fallback scoring.

Cache hits emit:

```
Slip review private market cache hit match_id=... provider_match_id=... markets=... cache_version=...
```

**Expiry and cleanup.**

Rows expire by `expires_at`; the default TTL is 72 hours. Expired rows are ignored by the read
path and deleted by `cleanup_slip_review_market_cache`, scheduled hourly by default.

**Production env.**

```env
SLIP_REVIEW_MARKET_CACHE_WRITE_ENABLED=True
SLIP_REVIEW_MARKET_CACHE_TTL_HOURS=72
SLIP_REVIEW_MARKET_CACHE_VERSION=v1

SLIP_REVIEW_MARKET_CACHE_BUILD_ENABLED=True
SLIP_REVIEW_MARKET_CACHE_BUILD_HOURS=0,12
SLIP_REVIEW_MARKET_CACHE_BUILD_MINUTE=40
SLIP_REVIEW_MARKET_CACHE_BUILD_DAYS=3
SLIP_REVIEW_MARKET_CACHE_BUILD_SYNC_FIXTURES=True
SLIP_REVIEW_MARKET_CACHE_BUILD_FORCE=False
SLIP_REVIEW_MARKET_CACHE_BUILD_MAX_FIXTURES=0

SLIP_REVIEW_MARKET_CACHE_CLEANUP_ENABLED=True
SLIP_REVIEW_MARKET_CACHE_CLEANUP_MINUTES=55
SLIP_REVIEW_MARKET_CACHE_CLEANUP_GRACE_SECONDS=0
SLIP_REVIEW_MARKET_CACHE_CLEANUP_LIMIT=0
```

**Manual commands.**

Warm the private cache:

```bash
python manage.py slip_review_market_cache build --days 3
```

Force one date after a bad run or deploy:

```bash
python manage.py slip_review_market_cache build --start-date 2026-08-15 --days 0 --no-sync-fixtures --force
```

Clean expired rows:

```bash
python manage.py slip_review_market_cache cleanup
```

Run cleanup inline during maintenance:

```bash
python manage.py slip_review_market_cache cleanup --inline
```

Inspect cache status:

```bash
python manage.py slip_review_market_cache status
```

Check task state:

```bash
python manage.py shell -c "from celery.result import AsyncResult; r=AsyncResult('TASK_ID'); print(r.state, r.info)"
```

**Verification queries.**

Cache volume by date:

```bash
python manage.py shell -c "from apps.algo.models import SlipReviewMarketCache; from django.db.models import Count; [print(r) for r in SlipReviewMarketCache.objects.values('match_date').annotate(rows=Count('id'), fixtures=Count('match_id', distinct=True)).order_by('-match_date')[:5]]"
```

Inspect a fixture:

```bash
python manage.py shell -c "from apps.algo.models import SlipReviewMarketCache; [print(r.match_id, r.provider_match_id, r.fixture, r.market, r.confidence, r.odds, r.expires_at) for r in SlipReviewMarketCache.objects.filter(match_id='statpal:2026081512345').order_by('-confidence')[:20]]"
```

Expired row count:

```bash
python manage.py shell -c "from apps.algo.models import SlipReviewMarketCache; from django.utils import timezone; print(SlipReviewMarketCache.objects.filter(expires_at__lte=timezone.now()).count())"
```

**Workers to monitor.**

| Worker | Why |
|---|---|
| `celery-statpal-worker` / `ALGO_STATPAL_QUEUE` | private cache build, fixture horizon, StatPal daily cache |
| `celery-maintenance-worker` / `ALGO_MAINTENANCE_QUEUE` | private cache cleanup, stale slip recovery, lineups/availability |
| `celery-slip-review-leg-worker` / `SLIP_REVIEW_LEG_QUEUE` | should show fewer heavy fallback/on-demand scoring calls as cache warms |

Healthy logs look like:

```
Slip review private market cache build progress fixtures=25/...
Slip review private market cache build done {...}
Slip review private market cache cleanup done {...}
Slip review private market cache hit match_id=...
```

Warning logs to investigate:

```
Slip review private fixture scoring failed ...
Slip review market cache write failed ...
```

---

## 12. Data model

```
bookmakers
bookmaker_market_mappings   (bookmaker, market_id, specifier_pattern, outcome_id) → canonical
fixtures / teams / players
fixture_mappings            (bookmaker_event_id ↔ statpal main_id + fallback ids)

slip_reviews / slip_legs
analysis_runs               (model_version set, created_at) — immutable
leg_analyses                (run, leg, state, assessment_type, probability, confidence,
                             data_quality, capabilities_used[], features JSONB)
market_alternatives
ticket_analyses             (success_probability, correlation_applied, killers JSONB)

calibration_rollups         (family, band, level, n, wins, fitted_at)
model_versions
```

**Rules.**
- Every prediction persists its `model_version` **and feature vector**. Without this you cannot
  diagnose a wrong prediction or recompute calibration honestly.
- Re-analysis creates a **new** `analysis_run`; never mutate a prior run. Settled history must
  not shift under the calibration.
- Features in JSONB with 90-day retention; `calibration_rollups` kept indefinitely.

---

## 13. Explanation layer

LLM runs **last**, off the critical path, and never determines a verdict.

- Template-first. An LLM call is an enhancement, not a dependency.
- Input is the structured evidence payload only.
- **Output validator rejects any number not present in the input evidence**, and any
  guarantee/certainty language. Fail closed to the template.
- Cache per (leg, model_version). Async; the review is complete without it.

---

## 14. Phasing

```
P0  Foundations (no user-visible change)
    ├─ normalize on (marketId, specifier, outcomeId); seed bookmaker_market_mappings
    ├─ DataCapability vocabulary; single required_capabilities(legs) planner
    ├─ evaluator registry; family-drives-dispatch
    ├─ taxonomy fixes: DC 1X/X2, Home/Away CS, First to Score, Over 9.5 ambiguity
    ├─ leg state machine + assessment_type invariant
    └─ per-fixture hydration + Celery chord fan-out
                    │
P1  Score matrix ◄── the product unlock
    ├─ Dixon-Coles fitting, nightly per league, hierarchical shrinkage
    ├─ derive 1x2 / DC / DNB / O-U / BTTS / CS / team totals / AH / exact / correct score
    ├─ same-fixture joint probability (ADR-002)
    ├─ prematch-odds consensus as prior + sanity bound
    └─ delete _evaluate_fixture_context_market
                    │
P2  Specialised models — corners, cards (referee-aware), goal intervals, then player props
                    │
P3  Alternatives with the ADR-004 objective; repair flow
                    │
P4  Explanation layer
```

**Already built and carried forward:** settlement + recap (calibration feedstock), ticket risk
share/lift decomposition, snapshot planner and coverage, capability→confidence-cap layer,
honest-reporting fixes for unassessed legs.

**Deferred:** live/in-play markets, cross-fixture correlation, WebSocket progress,
microservice split.

---

## 15. Open questions

1. **Do SportyBet `marketId` values follow the Sportradar/Betradar UOF scheme?** If so, a large
   part of `bookmaker_market_mappings` can be seeded from a published table rather than by hand.
   Needs a dump of ~200 real slips to confirm.
2. **Odds coverage outside major leagues.** 90 markets was measured on Argentina Primera. Verify
   on the long tail before depending on odds-implied priors.
3. **Referee identification.** `match-stats` gives referee as a display string
   (`"Pablo Echavarria, Argentina"`), not an id. Needs normalisation to build referee card rates.
4. **StatPal rate limits and quota** on the current plan — sets the per-review call budget.
5. **Historical depth.** How far back does `leagues/{id}/matches/stats` go? Determines the
   training window for Dixon-Coles.

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

## 10b. ADR-005 — Probability and data confidence are separate numbers

**Problem.** ~80% of live slip reviews reported roughly the same confidence (~64%). The models
were not the cause: the score matrix produced a healthy spread (the most common single output
accounted for 5% of 135 values, with all buckets populated). The flattening happened in the
presentation layer, where data quality was applied as `min(cap, probability)`.

Because a handful of capability tiers cover most fixtures, truncating at the tier ceiling
collapsed genuinely different estimates onto a few numbers:

| Capability | Cap | Legs landing exactly on the cap |
|---|---|---|
| medium data | 75 | 12 / 20 (60%) |
| limited data | 62 | 19 / 20 (95%) |

Across a simulated 400-leg production mix, four values covered **39%** of all legs.

**Root cause.** `min(cap, probability)` conflates two questions that have different answers:

- *How likely is this outcome?* — a property of the fixture and the model.
- *How much evidence stands behind that estimate?* — a property of our data coverage.

Overwriting the first with the second destroys the first and misreports the second.

**Decision.** Report both, and never let one rewrite the other.

| Field | Meaning |
|---|---|
| `advisory_score` / `estimated_success_percent` | The modelled probability, as modelled. |
| `data_confidence` / `data_confidence_percent` | Evidence strength behind that estimate, same 0-100 scale. |
| `advisory_status`, `risk_tier` | The **claim**, graded at `min(probability, confidence)`. |
| `claim_limited_by_data_quality` | Set when confidence, not the model, held the claim back. |

Thin evidence therefore constrains what we are willing to *say* — an 88% estimate backed by
58 points of evidence is published as 88% with `caution`, not as 58%. This keeps the number
honest without letting the verdict become reckless.

**Result.** Across the same 400-leg mix, the top four values now cover **4%** of legs (from 39%),
and no capability tier produces any pile-up on its cap.

**Enforcement.** Both the submitted-market and direct-analysis paths go through a single
`_scored_claim` helper — they had already drifted apart once, with one path applying the
hold-back and the other not. `apps/algo/tests/test_confidence_distribution.py` guards the
distribution directly and fails against the old truncating behaviour.

**Consumers must rank on the claim, not the estimate.** Anything that *selects between*
legs has to use `min(probability, confidence)` — the same rule the status uses — or it will
silently prefer thin evidence. Smart randomize did exactly that: it ranked on the raw
probability, so a leg the review labelled `caution` could top a ticket sold as "the
strongest analysed picks". Reporting the probability unchanged is right; *choosing* on it
is not. It also filtered candidates with a denylist that let `risky` through, offering
picks the review had told the user to avoid; that filter is now an allowlist and its floor
is tied to the same 55 boundary `_match_checker_status` uses for `avoid`.
See `apps/algo/tests/test_smart_randomize.py`.

## 10c. ADR-006 — Team strength survives the season boundary, or the market declines

**Problem.** Live reviews rated a Galatasaray away trip at 1.51 and an Al-Nassr away trip
at 1.27 as *back the home side*, and no Home/Away Win ever cleared the playable threshold.
Two different Saudi fixtures came back with byte-identical expected goals — 1.4748 home,
1.2999 away — each labelled "derived from a fitted goal model".

**Root cause.** Confirmed at source: current-season standings return `games_played: 0` for
every team in August. `_shrink(x, 0)` returns exactly the prior, and the prior was a flat
`1.0`, so every attack and defence factor collapsed to 1.0 and `expected_goals` reduced to
the bare league baseline. Because the baseline carries home advantage, the home side was
rated higher in **every fixture in the league**, however weak.

`data_quality` could not catch it: `matches_observed` is a *league-wide* total, so twenty
teams two games in clears the threshold while every individual factor is still 1.0. The
fixtures were reported at `data_confidence: 75`.

**Decision.**

| | |
|---|---|
| Prior | Shrink toward **last season's fitted factors** instead of toward 1.0. `_shrink` already took a `prior` argument; nothing had ever passed one. |
| Season choice | Walk candidate seasons newest-first and take the first with a real sample. The preceding season is not automatically right — England's 2025/2026 snapshot is frozen at 20 games while 2024/2025 has the full 38. |
| Evidence | `TeamStrength.prior_matches` records what the prior was worth; `effective_matches = matches + prior_matches` is the evidence behind a team's numbers. |
| Gate | Result-dependent families **decline** when either side has fewer than `MIN_TEAM_MATCHES_FOR_RESULT` effective matches. Symmetric families (totals, BTTS, odd/even) still run — a league-average total is a real estimate — but carry `league_average_team_strength` and reduced quality. |
| Promoted teams | Absent from last season's table, so they keep the neutral prior and report zero evidence. They decline rather than borrow another club's strength. |

**Result.** Everton vs Liverpool moved from a home-favoured league average to
`home 27.8% / draw 29.3% / away 42.9%`. Arsenal vs Sunderland declines, because Sunderland
were promoted and have no prior.

**Cost.** One `SOCCER_LEAGUE_SEASONS` call covers every league, plus at most
`PRIOR_SEASON_ATTEMPTS` standings calls for leagues needing a prior — well under 1% of the
daily budget, and only on the nightly fit.

**Rejected: pulling the score toward the bookmaker's price.** A short price genuinely is
information, and a large gap is worth surfacing — it is, as `result_model_market_disagreement`.
But using it to *raise* the score publishes the bookmaker's number as ours and disables
disagreement on exactly the favourites a review exists to question. It also only reached
short prices: of the five fixtures that prompted it, two cleared the threshold, one landed
0.1 short, and one had no score to adjust. The gap was a symptom of undifferentiated team
factors, and is fixed where it occurs.

**Related.** For result markets the snapshot fallback now prefers StatPal's published 1X2
percentages over a matrix built from unadjusted goals-for averages, which cannot account
for the opposition faced.

---


## 10d. ADR-007 — Alternatives are ranked by edge, not by probability

**Problem.** ADR-004 set out to stop the repair engine always recommending `Under 8.5
Goals`, and it kept doing it. A live thirteen-leg slip came back with eight legs replaced,
every replacement a double chance, under, or team-under, and the combined odds cut from
**20.05 to 3.24**. That is not finding value, it is walking down the odds ladder.

**Root cause.** `_rank_replacement_candidates` sorted on `advisory_score` — the raw
probability — with EV only as a distant tiebreak, and `_replacement_is_meaningfully_better`
required a raw-score lift. Raw probabilities are not comparable across families:

| Market | Typical PL fixture |
|---|---|
| Under 4.5 | ~88% |
| Over 1.5 | ~72% |
| Double chance | ~57–72% |
| Match result | ~29–41% |

On one absolute scale the highest base rate always wins, whatever the fixture. A 1X2 leg
could never beat a double chance on arithmetic alone — which is also why the thresholds
(55 / 66 / 78) structurally condemn result markets.

**Decision.** Rank by **edge over a league-average fixture**: the same market evaluated
with the team strengths switched off. The reference comes from the fitted league itself,
not a hand-written table of base rates, and costs no extra query — `FixtureRates` now
carries `home_baseline`/`away_baseline`, so `reference_matrix()` is built from data
already loaded.

Everton vs Liverpool, real fitted numbers:

| Market | Raw | Rank | Edge | Rank |
|---|---|---|---|---|
| Under 4.5 | 87.9% | **1** | −1.9 | 5 |
| Over 1.5 | 74.7% | 2 | +2.8 | 3 |
| DC: X2 | 72.2% | 3 | +13.0 | 2 |
| DC: 12 | 70.7% | 4 | +1.1 | 4 |
| DC: 1X | 57.1% | 5 | −14.1 | **7** |
| Away Win | 42.9% | 6 | **+14.1** | **1** |

Under 4.5 looks safest and is *below* what a typical fixture in that league returns.
Away Win moves from next-to-last to first. Backing the weak home side not to lose — the
recommendation that prompted the complaint — ranks last.

**Constraints kept.** The absolute floor still applies: a market we would not stand behind
on its own is never a replacement, however positive its edge. A `Correct Score 2-1` at 12%
with +40 edge is still refused.

**Markets with no reference** (counts, player props) fall to the bottom of the edge key
rather than being treated as zero edge — otherwise an unmeasured market would outrank a
measured negative one — and comparisons between two such markets fall back to raw scores.

**Not addressed.** Ranking still ignores price, because generated alternatives frequently
carry `odds: null`. Until an alternative can be priced, "better" means better than a
typical fixture, not better value. Recommending an unpriced swap remains unfalsifiable
advice and is the next thing to fix.

---


## 10e. ADR-008 — Alternatives carry a price, and value decides

**Supersedes the open question in ADR-007.** That decision ranked on edge over a
league-average fixture *because generated alternatives had no price*. They do now.

**What was missing.** Nothing in the feed. `SOCCER_PREMATCH_ODDS` returns ~90 market types
per fixture across 14 bookmakers — 1x2, double chance, draw no bet, totals, team totals,
BTTS, correct score, corners, cards, goalscorers — and `normalize_prematch_odds` already
parsed all three container shapes (`odd[]`, `total[]`, `handicap[]`). 87% of line entries
are open, not suspended. What was missing was **consumption**: `_generated_match_checker_markets`
hardcoded `"odds": None`, so every alternative was published unpriced.

**Decision.**

| | |
|---|---|
| Price | Alternatives are priced from the median across books via `reference_price`. |
| Rank | By **expected value**, `p x odds - 1` — the objective ADR-004 specified. Edge (ADR-007) remains the fallback where no price exists. |
| Replace | A swap must clear `MINIMUM_EV_LIFT` (0.03 per unit staked), not merely raise the probability. |
| Consensus | De-vigged bookmaker probability is **reported**, never blended into the score. |

EV settles the cross-family comparison on its own: an Under 4.5 at 88% into 1.10 returns
**-0.032**; a 43% away win at 2.60 returns **+0.118**. Raw probability always preferred the
first, which is how a 20.05 ticket became a 3.24 one.

**Why consensus is reported and not used.** Bookmaker prices already contain team strength.
Ranking on `model_p - consensus_p` would make this a de-vigging service that can never
disagree with the market — the same circularity as the rejected score floor in ADR-006. Our
probability stays ours; their price is used for what it is, the payout.

**Two corrections found while wiring it up.**

- *Double chance de-vig.* Its three outcomes each cover two of three results, so a fair book
  sums to **2.0**, not 1.0. Dividing by the raw sum reported a 1.04 shot as 46%. `_outcome_units`
  now scales it. Verified live: 1x2 de-vigs to 99.6 total, DC to 199.2, and `DC: 1X` (91.9)
  matches Home (78.8) + Draw (14.0).
- *Handicaps are refused, not priced.* `describe_market("Handicap -1 Home")` parses as line
  `1` with the sign dropped, so a lookup can match the `+1` bucket and return **the opposite
  bet's price**. That would feed a confident EV number for a wager the user never made. They
  return no price until the descriptor carries a signed line.

**Also fixed.** `_market_matches_descriptor` had no branch for `double_chance` or
`draw_no_bet` at all — both fell through to `False` — and double-chance sides never mapped to
StatPal's `Home/Draw` / `Draw/Away` / `Home/Away` naming.

**Cost.** One call per league, and Match Checker only needs leagues with fixtures inside the
3-day horizon, which the existing sync already knows.

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

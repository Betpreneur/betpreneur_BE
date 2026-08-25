# Cutover runbook — modular monolith

Verified end to end on 2026-08-25 against a restored copy of
`betpreneur_prod_20260610_061547.dump` on PostgreSQL 17, migrated forward to
`main` first so the copy matched production.

## What the migrations do

Every `algo_*` table keeps its name. The modules adopt those tables through
`SeparateDatabaseAndState` migrations with `database_operations=[]`, so the
move emits **no DDL**. `apps/algo/migrations/0001-0039` stays registered — it is
what creates those tables on a fresh database.

Two exceptions:

| Migration | Why it differs | How to apply |
|---|---|---|
| `analytics.0001_initial` | Real `CREATE TABLE` for `bankroll_bankrollsnapshot` and `reports_report`. Those apps were deleted outright, so their history went with them — but the tables already exist in production. | **`--fake`** |
| `settlement.0001_settlement_run` | Genuinely new table. | apply normally |

## Procedure

```bash
# 1. Fake the one migration whose tables already exist.
python manage.py migrate analytics 0001 --fake

# 2. Everything else applies normally — no DDL except settlement_run.
python manage.py migrate
```

## Verification (all confirmed on the restored copy)

```bash
python manage.py makemigrations --check --dry-run   # -> No changes detected
python scripts/schema_fingerprint.py                # diff vs pre-migration
```

Expected result: the only schema difference is the new `settlement_run` table.
Every pre-existing table, column and index is unchanged — including the
auto-generated index names, which survive because `db_table` is pinned
(Django derives index names from `db_table`, not `app_label`).

Confirmed after migrating the production copy:

- `makemigrations --check` — no model drift
- schema diff — exactly one added table
- greenfield rebuild from zero — byte-identical to the migrated database
- row counts unchanged (372 picks, 69 runs, 12 users)
- 34 content types redistributed; `algo` label gone; 0 orphaned permissions;
  0 broken `django_admin_log` rows
- public and authenticated endpoints served real data

## Celery — the one thing that needs coordination

**Every task was renamed.** Queue names did not change, so the compose worker
definitions need no edits — but a worker running the new code does not
register `apps.algo.tasks.*`, and a message already queued under an old name
will fail with `NotRegistered`.

### Before deploying

```bash
# 1. See what is still queued under the old names.
celery -A config inspect scheduled
celery -A config inspect reserved
redis-cli -u "$CELERY_BROKER_URL" llen algo_maintenance   # per queue

# 2. Let them drain, or purge if you accept losing in-flight maintenance work.
#    Everything on these queues is beat-scheduled (5-minutely or nightly), so a
#    short gap is harmless — but slip_review_* carries user-facing imports.
celery -A config purge -Q algo_maintenance,algo_daily,algo_scoring,algo_llm,algo_statpal,algo_settlement

# 3. Stop beat FIRST so nothing new is queued under old names.
#    Then restart every worker and beat together — a mixed fleet will drop work
#    in both directions.
```

Do not roll workers one at a time. Old workers cannot run new messages and new
workers cannot run old ones, so a partial roll loses tasks whichever way it
goes.

### The full rename

| `apps.algo.tasks.analyse_slip_review_leg` | `betpreneur.modules.slips.tasks.analyse_slip_review_leg` |
| `apps.algo.tasks.build_slip_review_market_cache` | `betpreneur.modules.picks.tasks.build_slip_review_market_cache` |
| `apps.algo.tasks.build_statpal_daily_cache` | `betpreneur.modules.catalog.tasks.build_statpal_daily_cache` |
| `apps.algo.tasks.cleanup_slip_review_market_cache` | `betpreneur.modules.picks.tasks.cleanup_slip_review_market_cache` |
| `apps.algo.tasks.expire_token_reservations` | `betpreneur.modules.billing.tasks.expire_token_reservations` |
| `apps.algo.tasks.explain_picks_for_run` | `betpreneur.modules.picks.tasks.explain_picks_for_run` |
| `apps.algo.tasks.finalize_slip_review_import` | `betpreneur.modules.slips.tasks.finalize_slip_review_import` |
| `apps.algo.tasks.fit_score_models` | `betpreneur.modules.scoring.tasks.fit_score_models` |
| `apps.algo.tasks.generate_daily_picks` | `betpreneur.modules.picks.tasks.generate_daily_picks` |
| `apps.algo.tasks.import_slip_review` | `betpreneur.modules.slips.tasks.import_slip_review` |
| `apps.algo.tasks.publish_daily_run` | `betpreneur.modules.picks.tasks.publish_daily_run` |
| `apps.algo.tasks.recover_daily_run` | `betpreneur.modules.picks.tasks.recover_daily_run` |
| `apps.algo.tasks.recover_stale_slip_reviews` | `betpreneur.modules.slips.tasks.recover_stale_slip_reviews` |
| `apps.algo.tasks.refill_daily_free_tokens` | `betpreneur.modules.billing.tasks.refill_daily_free_tokens` |
| `apps.algo.tasks.refresh_imminent_lineups` | `betpreneur.modules.scoring.tasks.refresh_imminent_lineups` |
| `apps.algo.tasks.refresh_player_availability` | `betpreneur.modules.scoring.tasks.refresh_player_availability` |
| `apps.algo.tasks.run_monthly_auditor` | `betpreneur.modules.analytics.tasks.run_monthly_auditor` |
| `apps.algo.tasks.score_fixture_for_daily_run` | `betpreneur.modules.picks.tasks.score_fixture_for_daily_run` |
| `apps.algo.tasks.settle_daily_results` | `betpreneur.modules.settlement.tasks.settle_daily_results` |
| `apps.algo.tasks.settle_slip_selections` | `betpreneur.modules.settlement.tasks.settle_slip_selections` |
| `apps.algo.tasks.sync_fixture_horizon` | `betpreneur.modules.catalog.tasks.sync_fixture_horizon` |

## OpenAPI operation ids

Six list/detail pairs previously shared an auto-generated `operationId`, which
Spectacular resolved with `_2` suffixes assigned by URL traversal order — so
reordering routes could silently swap an SDK method between the list and the
detail endpoint. They are now pinned explicitly:

| Endpoint | operationId |
|---|---|
| `GET /api/algo/games/` | `algo_games_list` (was `algo_games_retrieve`) |
| `GET /api/algo/games/{match_id}/` | `algo_games_retrieve` (was `..._retrieve_2`) |
| `GET /api/algo/picks/` | `algo_picks_list` (was `algo_picks_retrieve`) |
| `GET /api/algo/picks/{pick_id}/` | `algo_picks_retrieve` (was `..._retrieve_2`) |
| `GET /api/algo/slip-reviews/` | `algo_slip_reviews_list` (was `..._retrieve`) |
| `GET /api/algo/slip-reviews/{review_id}/` | `algo_slip_reviews_retrieve` (was `..._retrieve_2`) |

**Paths, parameters and payloads are unchanged** — nothing at runtime moves.
Only a client that *generates an SDK from the schema* sees a difference, as
renamed methods. Regenerate before deploying if you have one.

## Rollback

Every migration is reversible. `settlement_run` is the only table created, so
`migrate settlement zero` drops it and the state-only migrations reverse
without touching data.

## Websockets

Verified. `betpreneur/modules/slips/tests/test_websocket_stream.py` drives the
whole chain under `ENABLE_WEBSOCKETS=True`: the endpoint that mints a ticket,
the ASGI middleware that resolves it, and the consumer that accepts the socket
and pushes an opening progress frame. Sockets with no ticket or a forged one
are rejected.

`config/asgi.py` imports the consumer and middleware directly from
`slips.interface` rather than through `slips.api`. That is deliberate: config
is the composition root, and routing them through the facade would make every
consumer of `slips.api` require `channels`.

Note the test supplies `CHANNEL_LAYERS` explicitly — settings only define it
when `ENABLE_WEBSOCKETS` is true at load time, so flipping the flag alone
leaves the consumer without a channel layer.

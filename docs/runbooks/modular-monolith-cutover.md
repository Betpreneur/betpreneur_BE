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

Task names changed from `apps.algo.tasks.*` to `betpreneur.modules.<module>.tasks.*`.
Queue names are unchanged, so the `docker-compose` workers need no edits.

**Messages already queued under the old names will fail on the worker.** Drain
the queues before cutover, or accept losing in-flight maintenance jobs — they
are beat-scheduled (5-minutely and nightly), so a short gap is harmless.

Renamed tasks:

| Old | New |
|---|---|
| `apps.algo.tasks.generate_daily_picks` | `betpreneur.modules.picks.tasks.generate_daily_picks` |
| `apps.algo.tasks.settle_daily_results` | `betpreneur.modules.settlement.tasks.settle_daily_results` |
| `apps.algo.tasks.settle_slip_selections` | `betpreneur.modules.settlement.tasks.settle_slip_selections` |
| `apps.algo.tasks.refill_daily_free_tokens` | `betpreneur.modules.billing.tasks.refill_daily_free_tokens` |
| …and the rest, per `config/celery/routes.py` | |

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

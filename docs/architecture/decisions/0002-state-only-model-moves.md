# 2. Move models between apps with state-only migrations

**Status:** accepted · 2026-08-25

## Context

31 models had to move from `apps.algo` into eight new Django apps, against a
live production database. A naive move renames tables and rebuilds indexes.

## Decision

Pin `db_table` on every model to the name Django already generated, then move
models with `SeparateDatabaseAndState(database_operations=[], state_operations=[...])`.

`apps/algo/migrations/0001-0039` stays registered forever: it is what creates
those tables on a fresh database. The new modules only adopt them in state.

## Consequences

- The move emits **zero DDL**. Verified against a restored production dump:
  the only schema difference after migrating is the one deliberately added
  table.
- Auto-generated index names survive, because Django derives them from
  `db_table`, not `app_label` (`django/db/models/indexes.py:set_name_with_model`).
- Table names stay `algo_*` even in modules named otherwise. Renaming them
  would need an online rename with its own downtime plan, and buys nothing.
- `apps/algo` survives as a migrations-only shell with no code in it.

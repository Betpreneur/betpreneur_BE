# 3. Invert upward dependencies with registries, not events

**Status:** accepted · 2026-08-25

## Context

Several lower modules needed something only a higher module knows:

- `billing` must know whether the work a reservation paid for was delivered —
  only `slips` knows.
- `pricing` calibrates ticket risk against settled legs — only `slips` has them.
- `scoring` refreshes lineups for fixtures users have money on — only `slips`
  knows which.
- `identity`'s verify-email endpoint returns a `tokens` field — a `billing`
  concern in an `identity` response.

An event bus was the obvious answer and is wrong for all four: the caller needs
the result synchronously, and a fire-and-forget event cannot supply a return
value or a response field.

## Decision

The lower module declares the question; the higher module registers an answer
at app-ready. Four registries:

| Lower module states | Higher module registers |
|---|---|
| `billing.register_delivery_resolver` | `slips` — was this review delivered? |
| `pricing.register_calibration_source` | `slips` — settled legs |
| `scoring.register_priority_fixture_source` | `slips` — fixtures with money on them |
| `identity.register_verification_contributor` | `billing` — the signup grant |

With nothing registered, each falls back to a safe default (undeliverable,
no evidence, no priority fixtures, no extra fields).

Cross-layer Celery dispatch uses `send_task`/`signature` by name, so no import
crosses a boundary either.

## Consequences

- The layer graph stays acyclic and machine-checked.
- `platform/events/` exists and is used for genuinely fire-and-forget fan-out
  only. It currently has one publisher and no subscribers — kept because the
  next genuinely asynchronous reaction should not reinvent it.

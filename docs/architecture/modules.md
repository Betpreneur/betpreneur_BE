# Modules

Eleven domain modules over a platform and integration layer. A module may call
**down** through the callee's `api.py`; nothing may reach up. `import-linter`
enforces this — 15 contracts, run by `make verify-imports`.

```
L8  settlement · analytics       grading and reporting
L7  slips                        the paid product
L6  picks                        the daily product
L5  pricing · explanations       judgement (no tables)
L4  scoring                      statistics
L3  catalog · billing            reference data and money
L2  markets · identity           vocabulary and accounts
L1  integrations                 one adapter per external system
L0  platform                     technical, knows no domain
```

`slips` sits above `picks` rather than beside it: the Match Checker shows our
analysis of each leg's fixture, which is picks' output. The dependency is
one-directional — picks references nothing in slips.

| Module | Django app | Owns | Mandate |
|---|---|---|---|
| `markets` | — | none | Canonical market names, families, lines, what each can be evaluated from. Pure Python. |
| `identity` | `accounts` | `accounts_user` | Accounts, JWT, verification, password reset. |
| `catalog` | `catalog` | fixtures, provider maps, snapshots, market cache | Truth about fixtures and how each provider names them. |
| `billing` | `billing` | wallet, ledger, reservations, purchases | Every token in the system. Knows nothing about what they buy. |
| `scoring` | `scoring` | league models, strengths, rate profiles, lineups, availability | Statistics. Produces distributions, holds no opinion. |
| `pricing` | — | none | Distributions in, verdicts out: edge, EV, tier, risk. All the policy. |
| `explanations` | — | none | LLM council, templates, and the validator that stops unsupported claims. |
| `picks` | `picks` | runs, fixtures, picks, backs, predictions, strategy reviews | The daily free product. |
| `slips` | `slips` | reviews, selections, events, repairs, stream tokens | The paid product. |
| `settlement` | `settlement` | `settlement_run` | Grades both products. The only module allowed to touch each. |
| `analytics` | `analytics` | reports, bankroll snapshots | Read-only aggregation and audits. |

`markets`, `pricing` and `explanations` own no tables, so they are plain Python
packages — no `apps.py`, no migrations, not in `INSTALLED_APPS`. Their tests
need no database.

`apps/algo` still exists and holds **only migrations 0001-0039**. Those are what
create every `algo_*` table on a fresh database; the modules adopted them
through state-only migrations. See ADR 0002.

## Module anatomy

```
modules/<name>/
├── api.py          the ONLY thing another module may import
├── contracts.py    frozen dataclasses that cross the boundary
├── events.py       events published (fire-and-forget only)
├── models.py       tables owned here
├── services/       write use-cases; may read settings
├── domain/         pure python — NO django import (enforced, R5)
├── handlers.py     what this module registers with modules below it
├── tasks.py        celery entrypoints, thin
├── interface/      urls, views, serializers, admin — delivery only
└── tests/
```

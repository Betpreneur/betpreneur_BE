"""Emit a normalised, backend-agnostic fingerprint of the live database schema.

Used by scripts/verify_schema.sh to prove that a refactor changed no DDL.
Output is deterministic and diffable: tables, columns, indexes and constraints
all sorted, with backend-specific noise stripped.

    python manage.py shell < scripts/schema_fingerprint.py     # or
    DJANGO_SETTINGS_MODULE=config.settings python scripts/schema_fingerprint.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

# Django's own bookkeeping tables carry row-level state, not schema meaning.
SKIP = {"django_migrations"}


def main() -> None:
    out = []
    with connection.cursor() as cursor:
        tables = sorted(
            t.name for t in connection.introspection.get_table_list(cursor)
            if t.name not in SKIP
        )
        for table in tables:
            out.append(f"TABLE {table}")

            for col in sorted(
                connection.introspection.get_table_description(cursor, table),
                key=lambda c: c.name,
            ):
                bits = [
                    f"type={col.type_code}",
                    f"null={bool(col.null_ok)}",
                ]
                if col.internal_size not in (None, -1):
                    bits.append(f"size={col.internal_size}")
                out.append(f"  COLUMN {col.name} " + " ".join(bits))

            constraints = connection.introspection.get_constraints(cursor, table)
            for name in sorted(constraints):
                c = constraints[name]
                kind = (
                    "PK" if c.get("primary_key") else
                    "UNIQUE" if c.get("unique") and not c.get("index") else
                    "FK" if c.get("foreign_key") else
                    "INDEX" if c.get("index") else
                    "CHECK" if c.get("check") else "OTHER"
                )
                cols = ",".join(c.get("columns") or [])
                extra = ""
                if c.get("foreign_key"):
                    extra = f" -> {'.'.join(c['foreign_key'])}"
                if c.get("unique") and c.get("index"):
                    kind = "UNIQUE_INDEX"
                out.append(f"  {kind} {name} ({cols}){extra}")

    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Prove that this branch's migrations produce the same schema as a base ref.
#
# This is the WP8 gate, runnable from day one. It builds each tree's schema
# from zero on a throwaway database, fingerprints both, and diffs.
#
#   ./scripts/verify_schema.sh                 # compare HEAD against main
#   ./scripts/verify_schema.sh --base dev
#
# SAFETY: this NEVER reads .env's database. It forces a throwaway sqlite file,
# because .env points DB_HOST at a remote host.
set -euo pipefail

BASE="main"
[[ "${1:-}" == "--base" ]] && BASE="$2"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; git -C "$ROOT" worktree prune' EXIT

# Build the schema inside $tree, but always fingerprint with THIS branch's
# script — it only introspects the database, so both sides get identical
# treatment even when the base ref predates the script.
fingerprint() {   # $1 = tree dir, $2 = label
  local tree="$1" label="$2"
  local guard=(DB_ENGINE=django.db.backends.sqlite3
               DB_NAME="$WORK/$label.sqlite3"
               DB_HOST= DB_USER= DB_PASSWORD= DB_PORT=)
  ( cd "$tree" && env "${guard[@]}" "$PY" manage.py migrate --run-syncdb ) \
      > "$WORK/$label.migrate.log" 2>&1 \
    || { echo "  migrate failed in $label:"; tail -15 "$WORK/$label.migrate.log"; exit 1; }
  ( cd "$ROOT" && env "${guard[@]}" "$PY" scripts/schema_fingerprint.py ) > "$WORK/$label.txt"
  echo "  $label: $(wc -l < "$WORK/$label.txt") schema lines"
}

echo "Building schema from migrations…"
git -C "$ROOT" worktree add --detach "$WORK/base" "$BASE" >/dev/null 2>&1
fingerprint "$WORK/base" "base"
fingerprint "$ROOT"      "head"

echo
diff -u "$WORK/base.txt" "$WORK/head.txt" > "$WORK/schema.diff" && {
  echo "PASS  schema identical to '$BASE' — this branch emits no DDL"
  exit 0
}

# Filter out tables listed as deliberate additions. Anything left is unexpected.
EXPECTED="$ROOT/scripts/expected_schema_changes.txt"
ALLOWED=""
[[ -f "$EXPECTED" ]] && ALLOWED=$(grep -vE '^\s*(#|$)' "$EXPECTED" || true)

"$PY" - "$WORK/schema.diff" > "$WORK/unexpected.diff" <<PYEOF
import re, sys
allowed = set("""$ALLOWED""".split())
keep, table = [], None
for line in open(sys.argv[1]):
    if line.startswith(("+++", "---", "@@")):
        continue
    m = re.match(r"^[+-]TABLE (\S+)", line)
    if m:
        table = m.group(1)
    elif line.startswith((" ", "\n")):
        table = None
    if line.startswith(("+", "-")) and table not in allowed:
        keep.append(line)
print("".join(keep), end="")
PYEOF

if [[ ! -s "$WORK/unexpected.diff" ]]; then
  echo "PASS  only the declared additions ($(echo $ALLOWED | tr '\n' ' ')) differ from '$BASE'"
  exit 0
fi
echo "FAIL  UNEXPECTED schema change vs '$BASE':"
echo
sed 's/^/  /' "$WORK/unexpected.diff" | head -60
echo
echo "  If deliberate, add the table to scripts/expected_schema_changes.txt."
exit 1

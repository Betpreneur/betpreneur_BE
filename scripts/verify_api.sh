#!/usr/bin/env bash
# Prove the public HTTP API is byte-identical to a base ref.
#
# The refactor is internal: no path, no payload field and no operation id may
# move. Regenerating the OpenAPI schema on both trees and diffing is a stronger
# check than any hand review.
#
#   ./scripts/verify_api.sh [--base main]
set -euo pipefail

BASE="main"
[[ "${1:-}" == "--base" ]] && BASE="$2"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; git -C "$ROOT" worktree prune' EXIT

guard=(DB_ENGINE=django.db.backends.sqlite3 DB_NAME="$WORK/api.sqlite3"
       DB_HOST= DB_USER= DB_PASSWORD= DB_PORT=)

schema() {   # $1 = tree, $2 = label
  ( cd "$1" && env "${guard[@]}" "$PY" manage.py spectacular --format openapi-json ) \
      > "$WORK/$2.raw.json" 2>"$WORK/$2.err" \
    || { echo "  spectacular failed in $2:"; tail -12 "$WORK/$2.err"; exit 1; }
  # Sort keys so ordering differences never masquerade as contract changes.
  "$PY" -c "
import json,sys
d=json.load(open('$WORK/$2.raw.json'))
json.dump(d,open('$WORK/$2.json','w'),indent=2,sort_keys=True)
print('  $2:', len(d.get('paths',{})), 'paths')
"
}

echo "Generating OpenAPI schema…"
git -C "$ROOT" worktree add --detach "$WORK/base" "$BASE" >/dev/null 2>&1
schema "$WORK/base" base
schema "$ROOT"      head

echo
diff -u "$WORK/base.json" "$WORK/head.json" > "$WORK/api.diff" && {
  echo "PASS  public API identical to '$BASE'"
  exit 0
}

# Drop lines matching a declared change. Anything left is unexpected.
EXPECTED="$ROOT/scripts/expected_api_changes.txt"
if [[ -f "$EXPECTED" ]]; then
  PATTERNS=$(grep -vE '^\s*(#|$)' "$EXPECTED" || true)
else
  PATTERNS=""
fi
grep '^[+-]' "$WORK/api.diff" | grep -v '^[+-][+-]' > "$WORK/changed.txt" || true
if [[ -n "$PATTERNS" ]]; then
  grep -vFf <(echo "$PATTERNS") "$WORK/changed.txt" > "$WORK/unexpected.txt" || true
else
  cp "$WORK/changed.txt" "$WORK/unexpected.txt"
fi

if [[ ! -s "$WORK/unexpected.txt" ]]; then
  echo "PASS  only declared changes ($(echo $PATTERNS | tr '\n' ' ')) differ from '$BASE'"
  exit 0
fi
echo "FAIL  UNDECLARED public API change vs '$BASE':"
echo
sed 's/^/  /' "$WORK/unexpected.txt" | head -40
echo
echo "  If deliberate, add a matching line to scripts/expected_api_changes.txt."
exit 1

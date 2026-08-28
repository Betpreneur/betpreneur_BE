"""Compare two OpenAPI schemas structurally.

A line-based diff cannot express "this endpoint is new". Adding a path emits
dozens of generic property lines (``"unit"``, ``"value"``) that can only be
declared with patterns broad enough to mask a real change somewhere else — so
the declaration file would quietly blunt the very gate it feeds.

So additions and modifications are judged differently:

* **A removed or modified existing operation** is what the freeze exists to
  catch. Its subtree is diffed line by line and filtered through the declared
  patterns, exactly as before.
* **A new path** is additive and cannot break a client. It must still be
  declared by path, but once declared its whole subtree is accepted rather
  than line-matched.
* **A new component schema** is accepted only when every operation referencing
  it is itself newly added. A new schema pulled into an *existing* operation is
  a change to that operation, and is judged as one.

Usage: compare_api.py BASE.json HEAD.json EXPECTED.txt
"""
from __future__ import annotations

import difflib
import json
import re
import sys


def declared(path: str, patterns: list[str]) -> bool:
    return any(p in path for p in patterns)


def refs(node) -> set[str]:
    """Every component schema name reachable from a subtree."""
    out: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            out.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            out |= refs(value)
    elif isinstance(node, list):
        for value in node:
            out |= refs(value)
    return out


def subtree_diff(name: str, before, after) -> list[str]:
    lines = difflib.unified_diff(
        json.dumps(before, indent=2, sort_keys=True).splitlines(),
        json.dumps(after, indent=2, sort_keys=True).splitlines(),
        fromfile=f"base{name}", tofile=f"head{name}", lineterm="",
    )
    return [ln for ln in lines if re.match(r"^[+-][^+-]", ln)]


def main() -> int:
    base = json.load(open(sys.argv[1]))
    head = json.load(open(sys.argv[2]))
    try:
        raw = open(sys.argv[3]).read().splitlines()
    except OSError:
        raw = []
    patterns = [ln.strip() for ln in raw if ln.strip() and not ln.startswith("#")]

    problems: list[str] = []

    bp, hp = base.get("paths", {}), head.get("paths", {})
    added_paths = sorted(set(hp) - set(bp))
    removed_paths = sorted(set(bp) - set(hp))
    common_paths = sorted(set(bp) & set(hp))

    for path in removed_paths:
        if not declared(path, patterns):
            problems.append(f"REMOVED path {path}")

    undeclared_added = [p for p in added_paths if not declared(p, patterns)]
    for path in undeclared_added:
        problems.append(f"ADDED path {path} (additive, but must be declared)")

    # Existing operations keep the strict line-level treatment.
    changed_lines: list[str] = []
    for path in common_paths:
        if bp[path] != hp[path]:
            changed_lines += subtree_diff(f" {path}", bp[path], hp[path])

    bs = base.get("components", {}).get("schemas", {})
    hs = head.get("components", {}).get("schemas", {})

    # Schemas reachable only from newly added, declared paths ride along.
    accepted_new_paths = [p for p in added_paths if declared(p, patterns)]
    allowed = set()
    for path in accepted_new_paths:
        allowed |= refs(hp[path])
    # Follow nested references between new schemas.
    frontier = set(allowed)
    while frontier:
        nxt = set()
        for name in frontier:
            if name in hs:
                nxt |= refs(hs[name]) - allowed
        allowed |= nxt
        frontier = nxt

    for name in sorted(set(bs) - set(hs)):
        if not declared(name, patterns):
            problems.append(f"REMOVED schema {name}")
    for name in sorted(set(hs) - set(bs)):
        if name not in allowed and not declared(name, patterns):
            problems.append(f"ADDED schema {name} (not reachable from a declared new path)")
    for name in sorted(set(bs) & set(hs)):
        if bs[name] != hs[name]:
            changed_lines += subtree_diff(f" schema {name}", bs[name], hs[name])

    if patterns and changed_lines:
        changed_lines = [ln for ln in changed_lines if not any(p in ln for p in patterns)]

    if not problems and not changed_lines:
        if added_paths:
            print(f"PASS  only declared additions ({', '.join(added_paths)})")
        else:
            print("PASS  public API identical")
        return 0

    print("FAIL  UNDECLARED public API change:")
    print()
    for line in problems:
        print(f"  {line}")
    for line in changed_lines[:40]:
        print(f"  {line}")
    print()
    print("  If deliberate, add a matching line to scripts/expected_api_changes.txt.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

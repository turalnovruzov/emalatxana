#!/bin/bash
# Part of emalatxana's system, set up 2026-10-07; maintained in place. Custom glue, no scaffold.
#
# SessionStart check — the detection half of the technology gate.
# The prevention half of the technology gate is the in-session comparison: a new
# dependency reaches Tural as a one-screen comparison and his word lands as an ADR
# before it is installed. No permission `ask` rules back it. The comparison cannot
# see a dependency that arrived by a manifest edit, or transitively as somebody
# else's dependency. This reports both: any dependency in the manifest or lockfile with no
# decision record in docs/adr/ or docs/stack.md.
#
# It parses Python manifests and lockfiles only (pyproject.toml, requirements*.txt,
# uv.lock, poetry.lock, requirements.lock). Nothing about the stack is decided, so
# extending it to whatever manifest the first stack decision brings is part of that
# decision's change.
#
# It is a REPORT, not a gate: it never blocks a session, always exits 0, and
# says nothing at all when there is no manifest to check — which is the state
# of this repo today. A hook that nags about a project with no code gets
# deleted within a week; silence is correct until there is something to check.
#
# Output goes to stdout (the only SessionStart channel the session actually
# sees — stdout on exit 0 is added to context) and is mirrored to stderr.
# Plain prose only: JSON-shaped stdout would be parsed as hook control output.
#
# Known gaps, stated rather than implied: Pipfile.lock is JSON and is not
# parsed; a dependency name that happens to appear as a whole word anywhere in
# the docs corpus counts as recorded, even in passing, so a common English word
# used as a package name can be shadowed by prose. Both mean this reports fewer
# than the truth, never more — it can miss, it cannot cry wolf.
#
# bash 3.2 (macOS system bash): no associative arrays, no mapfile, no ${var,,}.

set -u

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
[ -d "$ROOT" ] || exit 0

# --- manifests: nothing to check means nothing to say -----------------------
MANIFESTS=""
[ -f "$ROOT/pyproject.toml" ] && MANIFESTS="$MANIFESTS $ROOT/pyproject.toml"
for f in "$ROOT"/requirements*.txt; do
  [ -f "$f" ] && MANIFESTS="$MANIFESTS $f"
done
[ -z "$MANIFESTS" ] && exit 0

# --- lockfiles (optional) ---------------------------------------------------
LOCKS=""
for f in "$ROOT/uv.lock" "$ROOT/poetry.lock" "$ROOT/requirements.lock"; do
  [ -f "$f" ] && LOCKS="$LOCKS $f"
done

# --- the corpus a dependency must appear in to count as recorded ------------
CORPUS=$(cat "$ROOT/docs/stack.md" "$ROOT"/docs/adr/*.md 2>/dev/null)

# Reduce a PEP 508 requirement to its bare distribution name, lowercased.
bare_names() {
  tr 'A-Z' 'a-z' \
    | sed -E 's/[[:space:]]//g; s/[][<>=!~;,()@].*$//' \
    | grep -E '^[a-z0-9][a-z0-9._-]*$' \
    | grep -vx 'python' \
    | sort -u
}

# Direct dependencies declared by a human.
direct_names() {
  for m in $MANIFESTS; do
    case "$m" in
      *pyproject.toml)
        # q is a literal apostrophe: TOML literal strings are as valid as basic
        # ones, so single-quoted requirements are normalised before splitting.
        awk -v q="$(printf '\047')" '
          /^[[:space:]]*\[/ { tbl=$0; gsub(/[[:space:]]/, "", tbl); inarr=0; next }
          {
            # poetry style: one dependency per key = constraint line
            if (tbl ~ /^\[tool\.poetry(\.group\.[^]]+)?\.dependencies\]$/ &&
                match($0, /^[[:space:]]*"?[A-Za-z0-9_.-]+"?[[:space:]]*=/)) {
              k = substr($0, RSTART, RLENGTH); gsub(/[="[:space:]]/, "", k); print k; next
            }
            # PEP 621 style: dependencies = [ "name>=x", ... ], possibly multi-line
            if ($0 ~ /dependencies[[:space:]]*=[[:space:]]*\[/) inarr = 1
            if (tbl ~ /^\[(project\.optional-dependencies|dependency-groups)\]$/ &&
                $0 ~ /=[[:space:]]*\[/) inarr = 1
            if (inarr) {
              line = $0; gsub(q, "\"", line)
              n = split(line, part, "\"")
              for (i = 2; i <= n; i += 2) if (part[i] != "") print part[i]
              if ($0 ~ /\]/) inarr = 0
            }
          }
        ' "$m" 2>/dev/null
        ;;
      *)
        # requirements.txt: skip comments and flag lines (-r, -e, --hash, ...)
        grep -v '^[[:space:]]*#' "$m" 2>/dev/null | grep -v '^[[:space:]]*-'
        ;;
    esac
  done | bare_names
}

# Everything the lockfile pins, direct and transitive alike.
locked_names() {
  [ -z "$LOCKS" ] && return 0
  for l in $LOCKS; do
    awk '
      /^\[\[package\]\]/ { inpkg = 1; next }
      inpkg && /^[[:space:]]*name[[:space:]]*=/ {
        gsub(/.*=[[:space:]]*"/, ""); gsub(/".*/, ""); print; inpkg = 0
      }
    ' "$l" 2>/dev/null
  done | bare_names
}

# A name counts as recorded if it appears as a whole word in the docs corpus.
# -w rather than a bare substring match, so "ruff" is not satisfied by "gruff".
undocumented() { # reads names on stdin
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    alt=$(printf '%s' "$name" | tr '_' '-')
    alt2=$(printf '%s' "$name" | tr '-' '_')
    printf '%s' "$CORPUS" | grep -i -w -q -F -e "$name" -e "$alt" -e "$alt2" || printf '%s\n' "$name"
  done
}

DIRECT_MISSING=$(direct_names | undocumented)
LOCK_MISSING=$(locked_names | undocumented)
# a direct dependency is reported once, in the manifest section
[ -n "$DIRECT_MISSING" ] && LOCK_MISSING=$(printf '%s\n' "$LOCK_MISSING" | grep -v -x -F "$DIRECT_MISSING")

[ -z "$DIRECT_MISSING" ] && [ -z "$LOCK_MISSING" ] && exit 0

report() {
  if [ -n "$DIRECT_MISSING" ]; then
    n=$(printf '%s\n' "$DIRECT_MISSING" | grep -c .)
    echo "Dependency-record check: $n manifest dependency(ies) with no decision record —"
    printf '%s\n' "$DIRECT_MISSING" | sed 's/^/  - /'
  fi
  if [ -n "$LOCK_MISSING" ]; then
    n=$(printf '%s\n' "$LOCK_MISSING" | grep -c .)
    echo "Dependency-record check: $n lockfile-only package(s) with no decision record, first 10 —"
    printf '%s\n' "$LOCK_MISSING" | head -10 | sed 's/^/  - /'
  fi
  echo "Each needs an ADR under docs/adr/ or an entry in docs/stack.md (CLAUDE.md,"
  echo "Knowledge currency and technology decisions). This is a report, not a gate."
}

report
report >&2
exit 0

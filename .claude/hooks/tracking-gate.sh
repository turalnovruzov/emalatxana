#!/bin/bash
# template: product-owner @ 0.2.0 — shared; change it through Codery
# Gate: tracking-gate (deny-once)
#
# PreToolUse hook on Bash. Reads the tool call from stdin, acts only on
# commands matching the gate's pattern, and enforces the repo's rule:
# every PR references its GitHub issue (CLAUDE.md, Conventions).
# Every decision is appended to .codery/telemetry/gates.jsonl (gitignored).
#
# Accepted gaps (per the enforcement-gate manifest): a body passed via
# -F <file> is uninspectable from the command string; any "#<digits>"
# occurrence passes, even a spurious one; a command string merely
# mentioning "gh pr create" (e.g. inside an echo) trips the deny-once.
# Scope: the check compares git common dirs, so a session started in the main
# checkout is gated in any worktree of this repo, and a session in another repo
# is not. The hook that runs is the copy in the session's project dir, so a
# change here is live only once that checkout has it.
# The deny-once authorization is keyed per session AND per exact command
# string, so a bypass frees only the command that was denied; commands
# differing by so much as whitespace are distinct. Where no digest tool is
# reachable the key degrades to a bounded builtin fingerprint (length plus the
# first and last 20 alphanumerics), which can in principle collide and so let
# one other command through on a bypass — never a whole session's worth.
#
# Fail-open by construction: every error path exits 0 and stdout stays empty
# unless the gate has actually decided to deny.

set -u

GATE="tracking-gate"
MODE="deny-once"
PATTERN='gh[[:space:]]+([^;&|]*[[:space:]])?pr[[:space:]]+create'

# --- stdin: tool_input.command + session_id (missing -> allow silently) ---
INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
[ -z "$COMMAND" ] && exit 0
[ -z "$SESSION_ID" ] && exit 0

# --- defensive re-check: act only on commands this gate is about ---
printf '%s' "$COMMAND" | grep -Eq "$PATTERN" || exit 0

# --- the session's working dir: hook input cwd, else the process cwd ---
WORK_DIR=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
[ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ] || WORK_DIR=$PWD

common_dir() { # absolute git common dir of a directory (same for every worktree)
  git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null
}

REPO_ROOT=$(git -C "$WORK_DIR" rev-parse --show-toplevel 2>/dev/null) || exit 0
REPO_COMMON=$(common_dir "$WORK_DIR") || exit 0
[ -n "$REPO_COMMON" ] || exit 0

# --- scope: this gate enforces its own project's spec, nothing else ---
# same repository (any worktree of it) is in scope; another repository is not
if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  PROJECT_COMMON=$(common_dir "$CLAUDE_PROJECT_DIR") || exit 0
  [ "$REPO_COMMON" != "$PROJECT_COMMON" ] && exit 0
fi
# best-effort: a command that first cd's into a different repository is out of scope
FIRST_CD=$(printf '%s' "$COMMAND" | sed -nE 's/^[[:space:]]*cd[[:space:]]+([^[:space:];&|]+).*/\1/p')
if [ -n "$FIRST_CD" ]; then
  CD_TARGET=$(cd "$WORK_DIR" 2>/dev/null && cd "$FIRST_CD" 2>/dev/null && pwd)
  if [ -n "$CD_TARGET" ]; then
    CD_COMMON=$(common_dir "$CD_TARGET")
    [ "$CD_COMMON" != "$REPO_COMMON" ] && exit 0
  fi
fi

log_decision() { # decision
  local dir="$REPO_ROOT/.codery/telemetry"
  mkdir -p "$dir"
  printf '{"ts":"%s","gate":"%s","decision":"%s","session":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$GATE" "$1" "$SESSION_ID" >> "$dir/gates.jsonl"
}

deny() {
  log_decision "deny"
  cat <<EOF
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Tracking gate: this PR-create command references no GitHub issue, and every PR must reference its issue — add the issue number, e.g. Refs #N, or Closes #N when the PR completes the item (CLAUDE.md, Conventions). Add the reference and try again — or, if this PR genuinely tracks no issue, run the same command again and it will proceed."}}
EOF
  exit 0
}

gate_check() {
  # rule: the command must reference a GitHub issue (#N)
  printf '%s' "$COMMAND" | grep -Eq '#[0-9]+' && return 0
  return 1
}

command_digest() { # stable, filename-safe, bounded key for the exact command
  local d=""
  local san=""
  local start=0
  d=$(printf '%s' "$COMMAND" | shasum 2>/dev/null | awk '{print $1}' 2>/dev/null)
  [ -z "$d" ] && d=$(printf '%s' "$COMMAND" | md5 2>/dev/null)
  # hex only, truncated: this ends up in a /tmp filename
  d=$(printf '%s' "$d" | tr -cd '0-9a-f' 2>/dev/null | cut -c1-32 2>/dev/null)
  if [ -z "$d" ]; then
    # no digest tool reachable: builtin fingerprint, still per-command
    san=${COMMAND//[^a-zA-Z0-9]/}
    [ ${#san} -gt 20 ] && start=$(( ${#san} - 20 ))
    d="len${#COMMAND}-${san:0:20}-${san:$start:20}"
  fi
  printf '%s' "$d"
}

if gate_check; then
  log_decision "pass"
  exit 0
fi

if [ "$MODE" = "deny-once" ]; then
  # keyed on session AND command: a bypass authorizes only the command that was
  # denied, which is what the deny message promises
  MARKER="/tmp/codery-gate-${GATE}-${SESSION_ID}-$(command_digest)"
  if [ -f "$MARKER" ]; then
    log_decision "allow-after-deny"
    exit 0
  fi
  touch "$MARKER"
fi

deny

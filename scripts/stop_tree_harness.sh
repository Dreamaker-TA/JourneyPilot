#!/usr/bin/env bash
#
# Verification harness for run.sh::stop_tree (defect 25).
#
# stop_tree holds every kill this repo issues, across four call sites, two of
# them in recovery. There was no test of it, which is the whole reason the fix
# was deferred once: a change there is unverifiable by inspection and a mis-kill
# is not undoable. This harness is that missing verification.
#
# It sources run.sh (which returns early when sourced) and drives the real
# function, never a copy — a copy would drift and verify the wrong code.
#
# Run: ./scripts/stop_tree_harness.sh
# Costs nothing, calls no provider, touches no database, starts no service.
# Every process it creates is a `sleep` it spawned itself.
#
# What case 1 models, and what it does not: a parent that is winding down and
# spawns a child while doing so — npm handing off to a cleanup child is the
# shape in this repo. A parent that forks and exits in the same instant is NOT
# recoverable by any amount of resampling: the child is reparented to init
# before anyone can observe it under the root, so no walk from the root will
# ever reach it. That residue is real and is left honest rather than papered
# over; the runbook's `lsof -ti:PORT | xargs kill -9` remains the answer for it.

set -uo pipefail
# No job control: otherwise the shell prints "Killed  sleep 25" for the harness's
# own fixtures and the output stops being only PASS/FAIL lines.
set +m

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../run.sh
source "$ROOT_DIR/run.sh"
set +e

HARNESS_SHELL=$$
PASSED=0
FAILED=0
SPAWNED=()

# Track a fixture by PID and drop it from the job table: cleanup kills by PID,
# and a tracked job makes the shell print "Killed  sleep 25" between PASS lines.
spawn_note() {
  SPAWNED+=("$1")
  disown "$1" 2>/dev/null
}

pass() {
  printf '  PASS  %s\n' "$1"
  PASSED=$(( PASSED + 1 ))
}

fail() {
  printf '  FAIL  %s\n' "$1"
  FAILED=$(( FAILED + 1 ))
}

check() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass "$label"
  else
    fail "$label (expected '$expected', got '$actual')"
  fi
}

alive() {
  if kill -0 "$1" 2>/dev/null; then echo "alive"; else echo "gone"; fi
}

cleanup() {
  # Only the top-level shell cleans up: an EXIT trap also fires in every
  # subshell, and a subshell reaping the harness's own fixtures mid-run would
  # make the results meaningless.
  [[ "$BASHPID" == "$HARNESS_SHELL" ]] || return 0
  local pid
  for pid in "${SPAWNED[@]:-}"; do
    if [[ -n "$pid" ]]; then
      kill -KILL "$pid" 2>/dev/null
    fi
  done
}
trap cleanup EXIT

printf '\nstop_tree harness (defect 25)\n\n'

# ─────────────────────────────────────────────────────────────────────────────
# 1. A descendant forked *after* its parent was signalled
# ─────────────────────────────────────────────────────────────────────────────
# The defect: _collect_descendants was called once, before TERM went out, and
# both the survival recheck and the follow-up KILL iterated that frozen list.
# A child born after the signal is in no snapshot, survives, and keeps the
# listening socket's fd — so the next `run.sh start` refuses to bind the port.
#
# Spawned inline, not through $( ): a command substitution is a subshell, and
# its exit disturbs both the trap and the job it just started.

GRANDCHILD_FILE="$(mktemp)"
bash -c "trap 'sleep 25 & echo \$! > $GRANDCHILD_FILE; sleep 1; exit 0' TERM
         while :; do sleep 0.2; done" >/dev/null 2>&1 &
late_fork_parent=$!
spawn_note "$late_fork_parent"
sleep 0.4

stop_tree "$late_fork_parent"

grandchild=$(cat "$GRANDCHILD_FILE" 2>/dev/null)
rm -f "$GRANDCHILD_FILE"
if [[ -z "$grandchild" ]]; then
  fail "late fork: the parent never spawned its post-TERM child (harness bug)"
else
  spawn_note "$grandchild"
  check "a child forked after TERM is reaped" "gone" "$(alive "$grandchild")"
fi
check "the late-forking parent is gone" "gone" "$(alive "$late_fork_parent")"

# ─────────────────────────────────────────────────────────────────────────────
# 2. A process outside the tree is untouched
# ─────────────────────────────────────────────────────────────────────────────
# The reason this fix was deferred: the union has to keep PIDs that are no
# longer descendants (a child is reparented the moment its parent exits), and
# anything held in a set can in principle be signalled after the PID is
# recycled. Nothing outside the tree may be touched, ever.

sleep 25 &
bystander=$!
spawn_note "$bystander"

sleep 25 &
lone_root=$!
spawn_note "$lone_root"

sleep 0.2
stop_tree "$lone_root"

check "the tree root is gone" "gone" "$(alive "$lone_root")"
check "an unrelated process is untouched" "alive" "$(alive "$bystander")"
kill -KILL "$bystander" 2>/dev/null

# ─────────────────────────────────────────────────────────────────────────────
# 3. An ordinary tree: parent plus two children, no traps
# ─────────────────────────────────────────────────────────────────────────────
# The path every real stop takes. It must keep working exactly as before.

bash -c 'sleep 25 & sleep 25 & wait' >/dev/null 2>&1 &
plain_root=$!
spawn_note "$plain_root"
sleep 0.4

plain_children=$(pgrep -P "$plain_root" 2>/dev/null | tr '\n' ' ')
for child in $plain_children; do
  spawn_note "$child"
done

stop_tree "$plain_root"
plain_rc=$?

check "an ordinary tree stops cleanly" "0" "$plain_rc"
check "its root is gone" "gone" "$(alive "$plain_root")"
if [[ -z "$plain_children" ]]; then
  fail "ordinary tree: the root spawned no children (harness bug)"
fi
for child in $plain_children; do
  check "its child $child is gone" "gone" "$(alive "$child")"
done

# ─────────────────────────────────────────────────────────────────────────────
# 4. A root that is already dead
# ─────────────────────────────────────────────────────────────────────────────
# Return 1 means "it was not running", which two of the four call sites read.

sleep 0.1 &
dead_pid=$!
wait "$dead_pid" 2>/dev/null
stop_tree "$dead_pid"
dead_rc=$?

check "a dead root reports not-running" "1" "$dead_rc"

# ─────────────────────────────────────────────────────────────────────────────

printf '\n  %d passed, %d failed\n\n' "$PASSED" "$FAILED"
if (( FAILED > 0 )); then
  exit 1
fi
exit 0

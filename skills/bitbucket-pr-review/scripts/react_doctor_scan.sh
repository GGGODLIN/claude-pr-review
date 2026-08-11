#!/bin/bash
# react-doctor mechanical scan for a Bitbucket PR (skill step 3.1)
# usage: react_doctor_scan.sh <local-repo-path> <source_commit> <dest_commit>
# stdout: react-doctor JSON (or {"skipped": "<reason>"} — never blocks the review flow)
set -uo pipefail

REPO="$1"; SRC="$2"; DST="$3"

skip() { echo "{\"skipped\": \"$1\"}"; exit 0; }

[ -d "$REPO/.git" ] || skip "no local clone at $REPO"

git -C "$REPO" fetch origin --quiet 2>/dev/null

git -C "$REPO" cat-file -e "$SRC^{commit}" 2>/dev/null || skip "source commit $SRC not found after fetch"
git -C "$REPO" cat-file -e "$DST^{commit}" 2>/dev/null || skip "dest commit $DST not found after fetch"

WT=$(mktemp -d "${TMPDIR:-/tmp}/rd-scan-XXXXXX") || skip "mktemp failed"
cleanup() {
  [ -n "$WT" ] && git -C "$REPO" worktree remove --force "$WT" 2>/dev/null
  [ -n "$WT" ] && rm -rf "$WT" 2>/dev/null
}
trap cleanup EXIT

git -C "$REPO" worktree add --detach --quiet "$WT" "$SRC" 2>/dev/null || skip "worktree add failed for $SRC"

cd "$WT" || skip "cannot cd into worktree"
OUT=$(npx -y react-doctor@latest . --offline --no-score --scope changed --base "$DST" --json 2>/dev/null)
[ -n "$OUT" ] || skip "react-doctor produced no output (framework detect failed?)"
echo "$OUT"

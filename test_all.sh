#!/usr/bin/env bash
# Full local evals, with (bedrock) and without (bedrock-no-jev) the JEV router.
# Usage: ./test_all.sh [TASKS_REF]   (default: HEAD of the tasks repo, must contain memory-carryover)
set -euo pipefail
cd "$(dirname "$0")"

TASKS_REPO=https://github.com/Jacarte/pepi-eval-tasks.git
REF=${1:-$(git ls-remote "$TASKS_REPO" HEAD | cut -f1)}
RUNS=${RUNS:-3}
CACHE=./evals_cache
PROFILES=(bedrock bedrock-no-jev)

[[ -f auth.json && -f .env ]] || { echo "need auth.json and .env here" >&2; exit 1; }
[[ -d profiles/bedrock-no-jev ]] || { echo "missing profiles/bedrock-no-jev" >&2; exit 1; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git clone -q --depth 1 "$TASKS_REPO" "$tmp" && git -C "$tmp" checkout -q "$REF" 2>/dev/null || true
if ! grep -rqs "memory-carryover" "$tmp" 2>/dev/null; then
  echo "warning: tasks ref $REF may not contain memory-carryover" >&2
fi
echo "tasks ref: $REF"

evals() { # $1=profile $2=fixed-id, rest = extra args
  local p=$1 id=$2; shift 2
  dagger call daggervals evals \
    --auth=file://./auth.json --agent-env=file://./.env \
    --cache-dir="$CACHE" --agent-profile="$p" --runs="$RUNS" --fixed="$id" "$@" \
    cache export --path="$CACHE"
}

for p in "${PROFILES[@]}"; do
  echo "=== $p: rust + go ==="
  evals "$p" "$p" --suite=all
  echo "=== $p: tasks ==="
  evals "$p" "$p-t" --suite=tasks --tasks-ref="$REF"
done

# Compare: every run file per arm, JEV arm first.
args=()
for p in "${PROFILES[@]}"; do
  for f in "$CACHE/$p"-[0-9]*@* "$CACHE/$p-t"-[0-9]*@*; do
    [[ -e $f && $f != *.md ]] && args+=(--files="$f")
  done
done
dagger call daggervals compare-runs \
  --auth=file://./auth.json --agent-env=file://./.env \
  --agent-profile=bedrock "${args[@]}" > "$CACHE/compare-jev.md"
echo "report: $CACHE/compare-jev.md"

#!/usr/bin/env bash
# Full local evals, two arms that differ only by the JEV router:
#   root     = profile "bedrock"            (jev_route tool, model-driven)
#   nojev    = profile "bedrock-no-jev"     (no router)
#
# Cache: the engine caches a run under its run id = <fixed>-<i>@<suite suffix> in ./evals_cache and
# serves it without rerunning. Keep the fixed names and the tasks ref stable and finished runs are
# reused (only the judge report is regenerated). Existing runs: root-*, bedrock-no-jev-* (tasks ref
#
# Usage: ./test_all.sh [TASKS_REF]      env: RUNS=3  ARMS="root nojev"
set -euo pipefail
cd "$(dirname "$0")"

REF=${1:-$(tr -d '[:space:]' < eval-tasks.ref)}   # pinned ref = cache hits for the existing arms
SHA12=${REF:0:12}
RUNS=${RUNS:-3}
ARMS=${ARMS:-"root nojev"}
CACHE=./evals_cache
# bash 3.2 (macOS) has no associative arrays: map arms with case.
profile_of() { case $1 in root) echo bedrock;; nojev) echo bedrock-no-jev;;
  *) echo "unknown arm: $1" >&2; exit 1;; esac; }
fixed_of() { case $1 in root) echo root;; nojev) echo bedrock-no-jev;;
  *) echo "unknown arm: $1" >&2; exit 1;; esac; }

[[ -f auth.json && -f .env ]] || { echo "need auth.json and .env here" >&2; exit 1; }
for a in $ARMS; do [[ -d profiles/$(profile_of "$a") ]] || { echo "missing profiles/$(profile_of "$a")" >&2; exit 1; }; done
echo "tasks ref: $REF (cache suffix @tasks@$SHA12), runs: $RUNS, arms: $ARMS"

evals() { # $1=profile $2=fixed id, rest = extra args
  local p=$1 id=$2; shift 2
  dagger call daggervals evals \
    --auth=file://./auth.json --agent-env=file://./.env \
    --cache-dir="$CACHE" --agent-profile="$p" --runs="$RUNS" --fixed="$id" "$@" \
    cache export --path="$CACHE"
}

for a in $ARMS; do
  p=$(profile_of "$a"); f=$(fixed_of "$a")
  echo "=== $a ($p): rust + go ===";  evals "$p" "$f" --suite=all
  echo "=== $a ($p): tasks ===";      evals "$p" "$f-t" --suite=tasks --tasks-ref="$REF"
done

# Compare per suite. compare-runs needs --labels: same label = repeats of one arm, first label = baseline
# (nojev is the control). Files are named exactly as the engine cached them.
compare() { # $1=suite name  $2=file-name template with %s=<fixed> and %d=<run>
  local files=() labels=() a f i
  for a in nojev root; do
    [[ " $ARMS " == *" $a "* ]] || continue
    f=$(fixed_of "$a")
    for i in $(seq 1 "$RUNS"); do
      files+=("$CACHE/$(printf "$2" "$f" "$i")"); labels+=("$a")
    done
  done
  local IFS=,; local joined_labels="${labels[*]}"; unset IFS
  local args=(); for x in "${files[@]}"; do args+=(--files="$x"); done
  dagger call daggervals compare-runs \
    --auth=file://./auth.json --agent-env=file://./.env --agent-profile=bedrock \
    "${args[@]}" --labels="$joined_labels" > "$CACHE/compare-$1.md"
  echo "report: $CACHE/compare-$1.md"
}
compare all   '%s-%d@all'
compare tasks "%s-t-%d@tasks@$SHA12"

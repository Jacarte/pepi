#!/usr/bin/env bash
# Full local evals. Two arms, both on profile bedrock:
#   base    = profile bedrock
#   forced  = profile bedrock + --force-delegation (mandatory worker/reviewer rule; run id suffix @forced)
#
# Cache: a run is cached under its run id = <fixed>-<i><suite suffix><ablation suffix> in
# ./evals_cache and is served without rerunning (only the judge report is regenerated). Keep the
# fixed names and the tasks ref stable to reuse finished runs.
#
# Usage: ./test_all.sh [TASKS_REF]      env: RUNS=3  ARMS="base forced"  RUN_ARMS=forced  FIXED_PREFIX=v2
#   FIXED_PREFIX=<p> prefixes every cache id (<p>-base, ...): a guaranteed fresh run, no cache reuse.
set -euo pipefail
cd "$(dirname "$0")"

REF=${1:-$(tr -d '[:space:]' < eval-tasks.ref)}   # pinned ref = cache hits for existing arms
SHA12=${REF:0:12}
RUNS=${RUNS:-3}
ARMS=${ARMS:-"base forced"}   # arms in the comparison
RUN_ARMS=${RUN_ARMS:-$ARMS}   # arms to execute (cached ones are skipped by the engine anyway)
CACHE=./evals_cache
COMPARE_ORDER="base forced"   # first present arm = baseline

# bash 3.2 (macOS) has no associative arrays: map arms with case.
arm_check() { case $1 in base|forced) ;; *) echo "unknown arm: $1" >&2; exit 1;; esac; }
profile_of() { echo bedrock; }
fixed_of()   { printf %s "${FIXED_PREFIX:+$FIXED_PREFIX-}$1"; }
flags_of()   { case $1 in forced) echo "--force-delegation";; *) echo "";; esac; }
# Run id suffix the module appends for the flags.
suffix_of()  { case $1 in forced) echo "@forced";; *) echo "";; esac; }

[[ -f auth.json && -f .env ]] || { echo "need auth.json and .env here" >&2; exit 1; }
for a in $ARMS $RUN_ARMS; do arm_check "$a"; [[ -d profiles/$(profile_of "$a") ]] || { echo "missing profiles/$(profile_of "$a")" >&2; exit 1; }; done
echo "tasks ref: $REF (cache suffix @tasks@$SHA12), runs: $RUNS, arms: $ARMS"
# Skill lint (./skills and ~/.agents/skills): non-fatal, so lint errors never block the evals.
bin/skill-lint || echo "skill-lint reported errors (non-fatal; see above)"

evals() { # $1=profile $2=fixed id $3=flags (word-split on purpose), rest = extra args
  local p=$1 id=$2 flags=$3; shift 3
  # shellcheck disable=SC2086
  dagger call daggervals evals \
    --auth=file://./auth.json --agent-env=file://./.env \
    --cache-dir="$CACHE" --agent-profile="$p" --runs="$RUNS" --fixed="$id" $flags "$@" \
    cache export --path="$CACHE"
}

for a in $RUN_ARMS; do
  p=$(profile_of "$a"); f=$(fixed_of "$a"); fl=$(flags_of "$a")
  echo "=== $a ($p $fl): rust + go ===";  evals "$p" "$f" "$fl" --suite=all
  echo "=== $a ($p $fl): tasks ===";      evals "$p" "$f-t" "$fl" --suite=tasks --tasks-ref="$REF"
done

# Compare per suite. compare-runs needs --labels: same label = repeats of one arm, the first label is
# the baseline. File names are exactly the cached run ids. Reports are timestamped, never overwritten.
compare() { # $1=suite name  $2=run id template: %s=fixed id, %d=run, then the arm's suffix is appended
  local files=() labels=() a f i
  for a in $COMPARE_ORDER; do
    [[ " $ARMS " == *" $a "* ]] || continue
    f=$(fixed_of "$a")
    for i in $(seq 1 "$RUNS"); do
      files+=("$CACHE/$(printf "$2" "$f" "$i")$(suffix_of "$a")"); labels+=("$a")
    done
  done
  local IFS=,; local joined_labels="${labels[*]}"; unset IFS
  local args=(); for x in "${files[@]}"; do args+=(--files="$x"); done
  local out="$CACHE/compare-$1-$(date +%Y%m%d-%H%M%S).md"
  dagger call daggervals compare-runs \
    --auth=file://./auth.json --agent-env=file://./.env --agent-profile=bedrock \
    "${args[@]}" --labels="$joined_labels" > "$out"
  echo "report: $out"
}
compare all   '%s-%d@all'
compare tasks "%s-t-%d@tasks@$SHA12"

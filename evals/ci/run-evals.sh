#!/bin/bash
# CI wrapper: one non-interactive `evals/pepi-eval.sh all` for the baseline or the candidate.
#
# Usage: evals/ci/run-evals.sh baseline|candidate CONFIG_DIR CONFIG_LABEL RUN_NAME
#
# Required env: PEPI_CI_SECRETS_DIR (chmod-700 dir with auth.json, see `ci secrets-setup`),
#               PEPI_CI_LAUNCH_DIR (directory holding the .env written by `ci secrets-setup`).
# Optional env: PEPI_CI_PRESET (verified20-v1), PEPI_CI_PI_VERSION (1.1.0), PEPI_CI_SUBAGENTS_VERSION (0.78.0),
#               PEPI_CI_LITELLM_VERSION (4.2.0), PEPI_CI_NODE_IMAGE (node:22-bookworm-slim), PEPI_CI_TASK_REPO,
#               PEPI_CI_RUNS_DIR, PEPI_CI_IDS, PEPI_CI_TIMEOUT_SECONDS, PEPI_CI_CPUS, PEPI_CI_MEMORY,
#               PEPI_CI_VENV, PEPI_CI_SWEBENCH_REPO, PEPI_CI_NETWORK (pepi-eval).
# The pins above repeat the last local runs (manifest versions of runs/cmp-jev-1); every one of them
# must also be passed to `ci cache-key`. Exits with pepi-eval.sh's code (0, 2, 3, 130).
set -u -o pipefail

die() { printf 'pepi-ci: error: %s\n' "$1" >&2; exit 2; }

[[ $# -eq 4 ]] || die "usage: run-evals.sh baseline|candidate CONFIG_DIR CONFIG_LABEL RUN_NAME"
role=$1 config_dir=$2 label=$3 run_name=$4
[[ "$role" == baseline || "$role" == candidate ]] || die "role must be baseline or candidate"
[[ -d "$config_dir" && ! -L "$config_dir" ]] || die "config dir not found: $config_dir"
[[ "$label" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "unsafe config label"
[[ "$run_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "unsafe run name"
[[ -n "${PEPI_CI_SECRETS_DIR:-}" && -f "$PEPI_CI_SECRETS_DIR/auth.json" ]] ||
    die "PEPI_CI_SECRETS_DIR must point at a directory with auth.json"
[[ -n "${PEPI_CI_LAUNCH_DIR:-}" && -d "$PEPI_CI_LAUNCH_DIR" ]] || die "PEPI_CI_LAUNCH_DIR not found"

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd) || exit 2
config_dir=$(cd "$config_dir" && pwd) || exit 2

# Start from a clean PEPI_EVAL_* environment so runner-host leftovers cannot change a run.
for variable in $(compgen -e); do
    case "$variable" in PEPI_EVAL_*) unset -v "$variable" ;; esac
done
# The raw secrets stay out of the harness (and of the agent's container launcher): the eval reads
# auth.json from PEPI_CI_SECRETS_DIR and the .env from PEPI_CI_LAUNCH_DIR.
unset -v PI_AUTH EVAL_ENV

export PEPI_EVAL_RUN_NAME="$run_name"
export PEPI_EVAL_CONFIG_DIR="$config_dir"
export PEPI_EVAL_CONFIG_LABEL="$label"
export PEPI_EVAL_PRESET="${PEPI_CI_PRESET:-verified20-v1}"
export PEPI_EVAL_PI_VERSION="${PEPI_CI_PI_VERSION:-1.1.0}"
export PEPI_EVAL_SUBAGENTS_VERSION="${PEPI_CI_SUBAGENTS_VERSION:-0.78.0}"
export PEPI_EVAL_LITELLM_VERSION="${PEPI_CI_LITELLM_VERSION:-4.2.0}"
export PEPI_EVAL_NODE_IMAGE="${PEPI_CI_NODE_IMAGE:-node:22-bookworm-slim}"
export PEPI_EVAL_NETWORK="${PEPI_CI_NETWORK:-pepi-eval}"
export PEPI_EVAL_SECRETS_DIR="$PEPI_CI_SECRETS_DIR"
export PEPI_EVAL_DROP_MCP=0
# Consent answers (pepi-eval.sh never prompts without a terminal; these make every choice explicit).
export PEPI_EVAL_YES=1 PEPI_EVAL_USE_DOTENV=1 PEPI_EVAL_USE_HOST_AUTH=0 PEPI_EVAL_USE_HOST_MCP=0
export PEPI_EVAL_USE_HOST_SKILLS=0 PEPI_EVAL_STRIP_TEST_EDITS=1 PEPI_EVAL_RESTORE_TEST_FILES=1 PEPI_EVAL_AUTO_FIX=0
[[ -z "${PEPI_CI_TASK_REPO:-}" ]] || export PEPI_EVAL_TASK_REPO="$PEPI_CI_TASK_REPO"
[[ -z "${PEPI_CI_RUNS_DIR:-}" ]] || export PEPI_EVAL_RUNS_DIR="$PEPI_CI_RUNS_DIR"
[[ -z "${PEPI_CI_IDS:-}" ]] || export PEPI_EVAL_IDS="$PEPI_CI_IDS"
[[ -z "${PEPI_CI_TIMEOUT_SECONDS:-}" ]] || export PEPI_EVAL_TIMEOUT_SECONDS="$PEPI_CI_TIMEOUT_SECONDS"
[[ -z "${PEPI_CI_CPUS:-}" ]] || export PEPI_EVAL_CPUS="$PEPI_CI_CPUS"
[[ -z "${PEPI_CI_MEMORY:-}" ]] || export PEPI_EVAL_MEMORY="$PEPI_CI_MEMORY"
[[ -z "${PEPI_CI_VENV:-}" ]] || export PEPI_EVAL_VENV="$PEPI_CI_VENV"
[[ -z "${PEPI_CI_SWEBENCH_REPO:-}" ]] || export PEPI_EVAL_SWEBENCH_REPO="$PEPI_CI_SWEBENCH_REPO"
# PEPI_EVAL_PROFILE stays unset: the profile is already applied inside CONFIG_DIR, and a named
# profile from the PR checkout would otherwise fill gaps in the baseline's (main's) config.

# Operator-managed network; on hosted runners a plain bridge (unrestricted egress, accepted risk).
if command -v docker >/dev/null 2>&1; then
    docker network inspect "$PEPI_EVAL_NETWORK" >/dev/null 2>&1 ||
        docker network create "$PEPI_EVAL_NETWORK" >/dev/null 2>&1 ||
        docker network inspect "$PEPI_EVAL_NETWORK" >/dev/null 2>&1 ||
        die "could not create docker network $PEPI_EVAL_NETWORK"
fi

runs_dir="${PEPI_EVAL_RUNS_DIR:-$repo/evals/swebench/runs}"
printf 'pepi-ci: %s run %s (config label %s, preset %s)\n' "$role" "$run_name" "$label" "$PEPI_EVAL_PRESET" >&2
status=0
# ./.env is read from the launch directory, so run from there.
(cd "$PEPI_CI_LAUNCH_DIR" && exec "$repo/evals/pepi-eval.sh" all </dev/null) || status=$?
printf 'pepi-ci: %s run dir: %s (exit %d)\n' "$role" "$runs_dir/$run_name" "$status" >&2
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    { printf 'run_dir=%s\n' "$runs_dir/$run_name"; printf 'exit_code=%d\n' "$status"; } >> "$GITHUB_OUTPUT"
fi
exit "$status"

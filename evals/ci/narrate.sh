#!/bin/bash
# CI narrate: never fails the job. Writes OUTPUT on success; otherwise OUTPUT is absent and the
# comment says "narrative unavailable".
#
# Usage: evals/ci/narrate.sh BASELINE_RUN CANDIDATE_RUN CANDIDATE_CONFIG_DIR OUTPUT
# Env: PEPI_CI_SECRETS_DIR (auth.json), PEPI_CI_LAUNCH_DIR (.env with LITELLM_BASE_URL/LITELLM_API_KEY),
#      optional PEPI_CI_NARRATE_MODEL, PEPI_CI_LITELLM_VERSION (4.2.0), PEPI_CI_PI_COMMAND (pi).
set -u -o pipefail

if [[ $# -ne 4 ]]; then
    echo "pepi-ci: usage: narrate.sh BASELINE_RUN CANDIDATE_RUN CANDIDATE_CONFIG_DIR OUTPUT" >&2
    exit 0
fi
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd) || exit 0
args=(--baseline "$1" --candidate "$2" --config-dir "$3" --output "$4"
      --secrets-dir "${PEPI_CI_SECRETS_DIR:-/nonexistent}"
      --litellm-version "${PEPI_CI_LITELLM_VERSION:-4.2.0}" --pi-command "${PEPI_CI_PI_COMMAND:-pi}")
[[ -f "${PEPI_CI_LAUNCH_DIR:-/nonexistent}/.env" ]] && args+=(--env-file "$PEPI_CI_LAUNCH_DIR/.env")
[[ -n "${PEPI_CI_NARRATE_MODEL:-}" ]] && args+=(--model "$PEPI_CI_NARRATE_MODEL")
PYTHONPATH="$repo" PYTHONDONTWRITEBYTECODE=1 python3 -m evals.swebench.ci narrate "${args[@]}" ||
    echo "pepi-ci: narrate helper failed; narrative unavailable" >&2
exit 0

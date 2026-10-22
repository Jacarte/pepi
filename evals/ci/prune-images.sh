#!/bin/bash
# Remove pepi-eval agent images (pepi-eval:*) after a CI eval job; idempotent.
#
# Refuses (exit 2, nothing executed) unless GITHUB_ACTIONS=true: on a workstation it would delete your
# local agent images. The workflow runs it only on self-hosted runners, where it removes every
# pepi-eval:* image on that Docker host (dedicate the runner's Docker to these evals).
# Task images (swebench/sweb.eval.*) and the build cache are left alone: the baseline and the
# candidate share them, and pruning the build cache would wipe it for every user of the host.
set -u -o pipefail

if [[ "${GITHUB_ACTIONS:-}" != true ]]; then
    echo "pepi-ci: prune-images: refusing: only for GitHub Actions jobs (needs GITHUB_ACTIONS=true);" \
         "it deletes every pepi-eval:* image on this Docker host" >&2
    exit 2
fi
[[ $# -eq 0 ]] || { echo "pepi-ci: prune-images: takes no arguments" >&2; exit 2; }

command -v docker >/dev/null 2>&1 || { echo "pepi-ci: prune-images: docker not found" >&2; exit 0; }
images=$(docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | awk -F: '$1 == "pepi-eval" && $2 != "<none>"')
if [[ -n "$images" ]]; then
    # One image per line; names are pepi-eval:<config>-<suffix>, no spaces.
    printf '%s\n' "$images" | xargs docker image rm -f >/dev/null 2>&1 || true
    echo "pepi-ci: prune-images: removed $(printf '%s\n' "$images" | wc -l | tr -d ' ') pepi-eval image(s)" >&2
fi
docker system df 2>/dev/null >&2 || true
exit 0

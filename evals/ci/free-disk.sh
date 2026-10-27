#!/bin/bash
# Make room for SWE-bench task images on a GitHub-hosted Ubuntu runner (Linux only; idempotent).
#
# DESTRUCTIVE, disposable hosted runners only: refuses (exit 2, nothing executed) unless
# GITHUB_ACTIONS=true and RUNNER_ENVIRONMENT=github-hosted. Never run it on a workstation or a
# self-hosted runner.
#
# 1. `sudo rm -rf` of preinstalled toolchains the evals never use (fixed list below; nothing else).
# 2. Moves Docker's data-root to /mnt/docker when /mnt exists with >= 50 GB free, so image layers use
#    the runner's second disk: rewrites /etc/docker/daemon.json and restarts dockerd, which stops
#    running containers and hides every image stored under the old data-root (a fresh hosted runner
#    has none we need). Images are not migrated.
# Prints `df -h` before and after. Never touches the workspace or home directories.
set -u -o pipefail

if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted ]]; then
    echo "pepi-ci: free-disk: refusing: only for GitHub-hosted runners" \
         "(needs GITHUB_ACTIONS=true and RUNNER_ENVIRONMENT=github-hosted); it deletes toolchains" \
         "and restarts docker" >&2
    exit 2
fi
[[ $# -eq 0 ]] || { echo "pepi-ci: free-disk: takes no arguments" >&2; exit 2; }

if [[ "$(uname -s)" != Linux ]]; then
    echo "pepi-ci: free-disk: not Linux; nothing to do" >&2
    exit 0
fi
sudo_cmd=()
[[ $(id -u) -eq 0 ]] || sudo_cmd=(sudo)

df -h / /mnt 2>/dev/null >&2 || true

for directory in /usr/share/dotnet /usr/local/lib/android /opt/ghc /usr/local/.ghcup /usr/share/swift \
                 /usr/local/share/powershell /usr/local/share/chromium /usr/local/share/boost \
                 /opt/hostedtoolcache/CodeQL /opt/hostedtoolcache/go /opt/hostedtoolcache/PyPy \
                 /opt/hostedtoolcache/Ruby /usr/lib/jvm /opt/microsoft /opt/google /opt/az; do
    if [[ -d "$directory" && ! -L "$directory" ]]; then
        "${sudo_cmd[@]}" rm -rf -- "$directory" && echo "pepi-ci: free-disk: removed $directory" >&2
    fi
done

target=/mnt/docker
daemon=/etc/docker/daemon.json
if [[ -d /mnt && ! -L /mnt ]] && command -v docker >/dev/null 2>&1; then
    current=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
    free_kb=$(df -Pk /mnt | awk 'NR == 2 {print $4}')
    if [[ "$current" == "$target" ]]; then
        echo "pepi-ci: free-disk: docker data-root already $target" >&2
    elif [[ "${free_kb:-0}" -ge $((50 * 1024 * 1024)) ]]; then
        "${sudo_cmd[@]}" mkdir -p "$target"
        # Merge data-root into any existing daemon.json (hosted runners ship one).
        merged=$(python3 - "$daemon" "$target" <<'PY'
import json, sys
from pathlib import Path
path, target = Path(sys.argv[1]), sys.argv[2]
data = json.loads(path.read_text()) if path.is_file() and path.read_text().strip() else {}
if not isinstance(data, dict):
    raise SystemExit("daemon.json is not an object")
data["data-root"] = target
print(json.dumps(data, indent=2))
PY
) || { echo "pepi-ci: free-disk: could not merge $daemon; leaving docker as is" >&2; merged=""; }
        if [[ -n "$merged" ]]; then
            "${sudo_cmd[@]}" mkdir -p /etc/docker
            printf '%s\n' "$merged" | "${sudo_cmd[@]}" tee "$daemon" >/dev/null
            "${sudo_cmd[@]}" systemctl restart docker
            echo "pepi-ci: free-disk: docker data-root is now $(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" >&2
        fi
    else
        echo "pepi-ci: free-disk: /mnt has less than 50 GB free; docker data-root unchanged" >&2
    fi
fi

df -h / /mnt 2>/dev/null >&2 || true
exit 0

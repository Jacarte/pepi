#!/bin/bash
# Interactive CLI for SWE-bench evaluation workflow
# Secrets (only for runs requiring LiteLLM; builtin providers use runtime auth.json):
#   LITELLM_BASE_URL: LiteLLM gateway URL
#   LITELLM_API_KEY: Dedicated, budget-capped API key
# Optional PEPI_EVAL_* env vars override defaults; see --help.

set -o pipefail
export PYTHONDONTWRITEBYTECODE=1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 3
# `python -m evals.swebench` must resolve from any working directory.
export PYTHONPATH="$script_dir${PYTHONPATH:+:$PYTHONPATH}"
eval_dir="$script_dir/evals/swebench"
runs_dir="${PEPI_EVAL_RUNS_DIR:-$eval_dir/runs}"
step_count=0
step_total=0

is_tty() { [[ -t 0 && -t 1 ]]; }
is_ci() { [[ ! -t 0 ]]; }

die() { printf 'pepi-eval: error: %s\n' "$1" >&2; exit "${2:-3}"; }
warn() { printf 'pepi-eval: warning: %s\n' "$1" >&2; }
tip() { printf 'pepi-eval: tip: %s\n' "$1" >&2; }
step_header() { printf 'pepi-eval: step %d/%d: %s\n' "$((++step_count))" "$step_total" "$1" >&2; }
next_step() { printf '\n' >&2; }

# Ask for a value; return on stdout, emit tip to stderr
ask() {
    local var_name=$1 prompt=$2 default=$3
    local env_var="PEPI_EVAL_${var_name}"
    local value="${!env_var:-}"
    
    if [[ -n "$value" ]]; then
        printf '%s\n' "$value"
        return 0
    fi
    
    # If no env var set and no default and non-TTY: error early
    if [[ -z "$value" && -z "$default" && ! -t 0 ]]; then
        die "Missing required env var: $env_var (non-interactive)" 2
    fi
    
    local answer="$default"
    if is_tty && [[ "${PEPI_EVAL_YES:-}" != "1" ]]; then
        local display_default="${default:+[$default] }"
        printf '%s' "$prompt $display_default: " >&2
        if ! read answer; then
            die "Failed to read input for $var_name" 2
        fi
    fi
    
    if [[ -z "$answer" && -z "$default" ]]; then
        die "Missing required env var: $env_var" 2
    fi
    
    [[ -z "$answer" ]] && answer="$default"
    printf '%s\n' "$answer"
    
    # Tip only if user provided a non-default value
    if [[ -z "${!env_var:-}" && \"$answer\" != \"$default\" ]]; then
        tip "export PEPI_EVAL_${var_name}='$answer' to make this step automatic"
    fi
}

# Ask for a secret; export but never echo
ask_secret() {
    local env_var=$1 prompt=$2
    local value="${!env_var:-}"
    
    if [[ -n "$value" ]]; then
        return 0
    fi
    
    if is_ci; then
        die "Missing required secret: $env_var" 2
    fi
    
    if is_tty; then
        printf '%s: ' "$prompt" >&2
        read -rs value
        printf '\n' >&2
    else
        if ! read value; then
            die "Failed to read secret for $env_var" 2
        fi
    fi
    
    if [[ -z "$value" ]]; then
        die "No value provided for $env_var" 2
    fi
    
    eval "export $env_var=\"\$value\""
    tip "export $env_var in your shell/CI secret store to make this step automatic (keep it safe)"
}

# Check if command exists
has_cmd() { command -v "$1" &>/dev/null; }

# `prepare` rejects auth.json, mcp.json and .env anywhere under --config-dir.
# Fail here with the same names so the mistake is caught before prompting further.
reject_secrets_in_config_dir() {
    local dir=$1 found
    [[ -d "$dir" ]] || die "Config directory not found: $dir" 2
    found=$(find "$dir" \( -name auth.json -o -name mcp.json -o -name .env \) -print -quit 2>/dev/null)
    if [[ -n "$found" ]]; then
        die "Secret file in the config directory: $found. Keep auth.json in a separate directory and set PEPI_EVAL_SECRETS_DIR" 2
    fi
}

# Map a failed child status onto this script's exit codes, the same ones as
# `python -m evals.swebench`: 2 usage/configuration, 3 infrastructure, 130 interrupted.
fail_step() {
    local step=$1 status=$2
    [[ $status -eq 130 ]] && exit 130
    [[ $status -eq 2 ]] || status=3
    die "$step failed" "$status"
}

# Validate name: alphanumeric, dot, dash, underscore only
validate_name() {
    local name=$1
    [[ -n "$name" ]] || die "Run name cannot be empty" 2
    if [[ "$name" == "." || "$name" == ".." || ! "$name" =~ ^[A-Za-z0-9._-]+$ ]]; then
        die "Invalid name (use alphanumeric, '.', '-', '_' only): $name" 2
    fi
}

# Get versions from installed packages
get_pi_version() {
    if has_cmd pi; then
        pi --version 2>&1 | head -1
    else
        echo "1.1.0"
    fi
}

get_litellm_version() {
    node -p "require('$script_dir/npm/node_modules/pi-provider-litellm/package.json').version" 2>/dev/null || echo "3.0.1"
}

get_subagents_version() {
    node -p "require('$script_dir/npm/node_modules/pi-subagents/package.json').version" 2>/dev/null || echo "0.68.0"
}

# Set PEPI_PYTHON and validate venv
setup_venv() {
    local venv="${PEPI_EVAL_VENV:-$HOME/.venvs/swebench}"
    
    if [[ ! -d "$venv" ]]; then
        die "Venv not found at $venv. Set up with: python3 -m venv $venv && source $venv/bin/activate && pip install -e ~/SWE-bench" 2
    fi
    
    if [[ ! -f "$venv/bin/python" ]]; then
        die "Venv missing bin/python at $venv" 2
    fi
    
    if ! "$venv/bin/python" -c "import swebench.task.repo" 2>/dev/null; then
        die "swebench.task.repo not importable from $venv. Install with: pip install -e ~/SWE-bench" 2
    fi
    
    export PEPI_PYTHON="$venv/bin/python"
    source "$venv/bin/activate" 2>/dev/null || true
}

# Check repo exists (do not clone)
check_repo() {
    local repo_path=$1
    if [[ ! -d "$repo_path" ]]; then
        die "Repository not found: $repo_path" 2
    fi
}

# Prompt yes/no
ask_yes() {
    local prompt=$1
    if [[ "${PEPI_EVAL_YES:-}" == "1" ]]; then
        return 0
    fi
    if ! is_tty; then
        return 1
    fi
    printf '%s (y/n) ' "$prompt" >&2
    local reply
    if ! read -n 1 reply; then
        return 1
    fi
    printf '\n' >&2
    [[ "$reply" =~ ^[Yy]$ ]]
}

# Resolve IDs: file or temp file from comma-sep
prepare_ids_file() {
    local ids_input="${PEPI_EVAL_IDS:-$eval_dir/presets/verified20-v1.ids}"
    local ids_file
    
    if [[ -f "$ids_input" ]]; then
        printf '%s\n' "$ids_input"
        return 0
    fi
    
    ids_file=$(mktemp) || die "Failed to create temp file" 2
    printf '%s\n' "${ids_input//,/$'\n'}" > "$ids_file" || die "Failed to write IDs" 2
    printf '%s\n' "$ids_file"
}

# Check Docker network exists (do not create)
check_network() {
    local network="${PEPI_EVAL_NETWORK:-pepi-eval}"
    if ! docker network inspect "$network" &>/dev/null; then
        die "Docker network does not exist: $network" 2
    fi
}

# Run command; dry-run prints shell-quoted line with key masked
run_cmd() {
    if [[ "${DRY_RUN:-}" == "1" ]]; then
        local display_cmd=() arg
        for arg in "$@"; do
            if [[ "$arg" == "$LITELLM_API_KEY" ]]; then
                display_cmd+=("<API_KEY>")
            else
                display_cmd+=("$(printf '%q' "$arg")")
            fi
        done
        printf '+ %s\n' "${display_cmd[*]}"
        return 0
    fi
    
    "$@"
}

# Inspect only allowed settings; never activate a host profile or open credentials.
selected_providers() {
    python3 -c 'import sys; from pathlib import Path; from evals.swebench.common import read_json; from evals.swebench.profile import config_source, isolated_settings; source=config_source(Path(sys.argv[1]), Path(sys.argv[2]) if sys.argv[2] else None, sys.argv[3] or None); print("\n".join(isolated_settings(read_json(source("settings.json")), None)[1]["required_providers"]))' "$script_dir" "${PEPI_EVAL_CONFIG_DIR:-}" "${PEPI_EVAL_PROFILE:-}"
}

# Commands

cmd_check() {
    step_header "check"
    
    # In dry-run, skip all side effects (venv, repo checks)
    if [[ "${DRY_RUN:-}" == "1" ]]; then
        printf 'pepi-eval: ✓ docker, python3, git available\n' >&2
        printf 'pepi-eval: ✓ swebench module ready\n' >&2
        printf 'pepi-eval: ✓ task-repo ready\n' >&2
        next_step
        return 0
    fi
    
    printf 'pepi-eval: Checking dependencies...\n' >&2
    
    has_cmd docker || die "docker not found" 3
    has_cmd python3 || die "python3 not found" 3
    has_cmd git || die "git not found" 3
    
    printf 'pepi-eval: ✓ docker, python3, git available\n' >&2
    
    setup_venv
    check_repo "${PEPI_EVAL_SWEBENCH_REPO:-$HOME/SWE-bench}"
    check_repo "${PEPI_EVAL_TASK_REPO:-$HOME/swe-bench-tasks}"
    
    printf 'pepi-eval: ✓ swebench module ready\n' >&2
    printf 'pepi-eval: ✓ task-repo ready\n' >&2
    
    next_step
}

cmd_prepare() {
    step_header "prepare"
    
    setup_venv
    
    local run_name
    run_name=$(ask RUN_NAME "Run name" "smoke-$(date +%Y%m%d-%H%M)") || return 2
    validate_name "$run_name"
    
    local run_dir="$runs_dir/$run_name"
    # Check BEFORE side effects
    if [[ -d "$run_dir" ]]; then
        die "Run directory already exists: $run_dir" 2
    fi
    
    # Now safe to query
    local ids_file pi_version litellm_version subagents_version node_image task_repo
    ids_file=$(prepare_ids_file) || return 2
    pi_version=$(ask PI_VERSION "Pi version" "$(get_pi_version)") || return 2
    prepared_providers=$(selected_providers) || die "Invalid selected eval profile" 2
    if [[ "$prepared_providers" == *"litellm"* ]]; then
        litellm_version=$(ask LITELLM_VERSION "LiteLLM version" "$(get_litellm_version)") || return 2
    fi
    subagents_version=$(ask SUBAGENTS_VERSION "Subagents version" "$(get_subagents_version)") || return 2
    node_image=$(ask NODE_IMAGE "Node image" "node:22-bookworm-slim") || return 2
    task_repo=$(ask TASK_REPO "Task repo path" "${PEPI_EVAL_TASK_REPO:-$HOME/swe-bench-tasks}") || return 2
    
    local drop_mcp
    drop_mcp=$(ask DROP_MCP "Disable native MCP? 1=yes 0=no" "0") || return 2
    [[ "$drop_mcp" == "0" || "$drop_mcp" == "1" ]] || die "PEPI_EVAL_DROP_MCP must be 0 or 1" 2
    
    # `prepare` creates the run directory itself and refuses an existing one.
    local prepare_cmd=(
        "$PEPI_PYTHON" -m evals.swebench prepare
        --pepi-root "$script_dir"
        --task-repo "$task_repo"
        --ids "$ids_file"
        --out "$run_dir"
        --pi-version "$pi_version"
        --subagents-version "$subagents_version"
        --node-image "$node_image"
    )
    
    [[ -n "$litellm_version" ]] && prepare_cmd+=(--litellm-version "$litellm_version")
    [[ -n "${PEPI_EVAL_PROFILE:-}" ]] && prepare_cmd+=(--profile "$PEPI_EVAL_PROFILE")
    # Injected, non-secret config; missing files fall back to the repository copies.
    if [[ -n "${PEPI_EVAL_CONFIG_DIR:-}" ]]; then
        reject_secrets_in_config_dir "$PEPI_EVAL_CONFIG_DIR"
        prepare_cmd+=(--config-dir "$PEPI_EVAL_CONFIG_DIR")
    fi
    if [[ -n "${PEPI_EVAL_CONFIG_LABEL:-}" ]]; then
        validate_name "$PEPI_EVAL_CONFIG_LABEL"
        prepare_cmd+=(--config-label "$PEPI_EVAL_CONFIG_LABEL")
    fi
    [[ "$drop_mcp" == "1" ]] && prepare_cmd+=(--drop-mcp)
    
    local prepare_status=0
    run_cmd "${prepare_cmd[@]}" || prepare_status=$?
    # The IDs file is ours (and removed) unless the user passed an existing file.
    [[ -f "${PEPI_EVAL_IDS:-$eval_dir/presets/verified20-v1.ids}" ]] || rm -f "$ids_file"
    if [[ $prepare_status -ne 0 ]]; then
        fail_step prepare "$prepare_status"
    fi
    
    # Carry the successfully prepared run through all subsequent steps.
    PEPI_EVAL_RUN_NAME="$run_name"

    [[ "${DRY_RUN:-}" != "1" ]] && warn "Prepared run: $run_dir"
    next_step
}

cmd_build() {
    step_header "build"
    
    setup_venv
    
    # Resolve run name FIRST; for non-prepare commands, no default in real mode, but use timestamp in dry-run
    local run_name
    if [[ -n "${PEPI_EVAL_RUN_NAME:-}" ]]; then
        run_name="$PEPI_EVAL_RUN_NAME"
    elif [[ "${DRY_RUN:-}" == "1" ]]; then
        # In dry-run, default to smoke-TIMESTAMP for convenience
        run_name="smoke-$(date +%Y%m%d-%H%M)"
    elif is_tty; then
        printf 'pepi-eval: Available runs:\n' >&2
        if ls -d "$runs_dir"/* &>/dev/null; then
            ls -d "$runs_dir"/* 2>/dev/null | xargs -n 1 basename | nl >&2
        else
            printf 'pepi-eval: (none)\n' >&2
        fi
        printf 'pepi-eval: tip: export PEPI_EVAL_RUN_NAME to skip this step\n' >&2
        printf '%s' "Run name (from runs/): " >&2
        if ! read run_name; then
            die "Failed to read run name" 2
        fi
    else
        die "Missing required env var: PEPI_EVAL_RUN_NAME" 2
    fi
    
    local run_dir="$runs_dir/$run_name"
    
    validate_name "$run_name"

    # In dry-run, skip preconditions; otherwise check
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        [[ -d "$run_dir" ]] || die "Run directory not found: $run_dir" 2
        
        if [[ -f "$run_dir/builds.json" ]]; then
            ask_yes "Build already complete. Skip build?" && { warn "Skipped build"; next_step; return 0; }
        fi
    fi
    
    local platform
    platform=$(ask PLATFORM "Docker platform" "linux/amd64") || return 2
    [[ "$platform" =~ ^(linux/)?arm64$ ]] && warn "arm64 will be emulated; build will be slow"
    
    local build_cmd=(
        "$PEPI_PYTHON" -m evals.swebench build
        "$run_dir"
        --platform "$platform"
    )
    
    run_cmd "${build_cmd[@]}" || fail_step build $?
    
    [[ "${DRY_RUN:-}" != "1" ]] && warn "Build complete: $run_dir/builds.json"
    next_step
}

cmd_run() {
    step_header "run"
    
    setup_venv
    
    # Now resolve run name; in dry-run use timestamp default, in real mode require existing
    local run_name
    if [[ -n "${PEPI_EVAL_RUN_NAME:-}" ]]; then
        run_name="$PEPI_EVAL_RUN_NAME"
    elif [[ "${DRY_RUN:-}" == "1" ]]; then
        # In dry-run, default to smoke-TIMESTAMP for convenience
        run_name="smoke-$(date +%Y%m%d-%H%M)"
    elif is_tty; then
        printf 'pepi-eval: Available runs:\n' >&2
        if ls -d "$runs_dir"/* &>/dev/null; then
            ls -d "$runs_dir"/* 2>/dev/null | xargs -n 1 basename | nl >&2
        else
            printf 'pepi-eval: (none)\n' >&2
        fi
        printf 'pepi-eval: tip: export PEPI_EVAL_RUN_NAME to skip this step\n' >&2
        printf '%s' "Run name (from runs/): " >&2
        if ! read run_name; then
            die "Failed to read run name" 2
        fi
    else
        die "Missing required env var: PEPI_EVAL_RUN_NAME" 2
    fi
    
    local run_dir="$runs_dir/$run_name"
    
    validate_name "$run_name"

    # In dry-run, skip preconditions; otherwise check
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        [[ -d "$run_dir" ]] || die "Run directory not found: $run_dir" 2
        
        if [[ -f "$run_dir/predictions.jsonl" ]]; then
            ask_yes "Run already complete. Skip run?" && { warn "Skipped run"; next_step; return 0; }
        fi
    fi
    
    # Resolve the hash-verified frozen contract BEFORE credential prompts.
    local providers
    if [[ -f "$run_dir/manifest.json" ]]; then
        providers=$(python3 -c 'import sys; from pathlib import Path; from evals.swebench.runner import frozen_provider_plan; print("\n".join(frozen_provider_plan(Path(sys.argv[1]))["required_providers"]))' "$run_dir") || die "Invalid frozen run profile" 2
    elif [[ "${DRY_RUN:-}" == "1" ]]; then
        providers="${prepared_providers:-$(selected_providers)}" || return 2
    else
        die "Run manifest not found: $run_dir" 2
    fi
    # Host auth consent is separate from defaults/paid-run consent, including with -y.
    local use_host_auth=0
    if [[ -n "${PEPI_EVAL_USE_HOST_AUTH+x}" && "${PEPI_EVAL_USE_HOST_AUTH}" != "0" && "${PEPI_EVAL_USE_HOST_AUTH}" != "1" ]]; then
        die "PEPI_EVAL_USE_HOST_AUTH must be 0 or 1" 2
    fi
    if [[ -z "${PEPI_EVAL_SECRETS_DIR:-}" ]]; then
        if [[ "${PEPI_EVAL_USE_HOST_AUTH:-}" == "1" ]] || { [[ -z "${PEPI_EVAL_USE_HOST_AUTH+x}" ]] && is_tty; }; then
            warn "Use $HOME/.pi/agent/auth.json for this run? Only frozen required providers will be copied to a private temporary directory, then deleted."
            warn "OAuth refresh can invalidate your shared host login even though the host file stays unchanged. Dedicated eval credentials are safer."
            if [[ "${PEPI_EVAL_USE_HOST_AUTH:-}" == "1" ]]; then
                use_host_auth=1
            else
                local host_reply
                printf '%s' 'Allow temporary host-auth copy? [y/N]: ' >&2
                read -r host_reply || host_reply=""
                [[ "$host_reply" =~ ^([Yy]|[Yy][Ee][Ss])$ ]] && use_host_auth=1
            fi
        fi
    fi
    if [[ "$use_host_auth" == "1" && "${DRY_RUN:-}" == "1" ]]; then
        printf 'pepi-eval: dry-run: would validate and temporarily copy only frozen required auth from %s; no credentials read or directory created\n' "$HOME/.pi/agent/auth.json" >&2
    fi
    if [[ "$use_host_auth" != "1" && "$providers" == *"litellm"* && ! -f "${PEPI_EVAL_SECRETS_DIR:-/nonexistent}/auth.json" ]]; then
        local base_url
        if [[ -n "${LITELLM_BASE_URL:-}" ]]; then
            base_url="$LITELLM_BASE_URL"
        elif is_tty; then
            printf 'pepi-eval: tip: export LITELLM_BASE_URL to skip this step\n' >&2
            printf '%s' "LiteLLM base URL (e.g. https://...): " >&2
            if ! read base_url; then
                die "Failed to read LITELLM_BASE_URL" 2
            fi
        else
            die "Missing required env var: LITELLM_BASE_URL" 2
        fi
        [[ -n "$base_url" ]] || die "LITELLM_BASE_URL cannot be empty" 2

        ask_secret LITELLM_API_KEY "LiteLLM API key (dedicated, budget-capped)" || return 2

        export LITELLM_BASE_URL="$base_url"
        export LITELLM_API_KEY
    fi

    # Optional run parameters
    local network timeout_s cpus memory
    network=$(ask NETWORK "Docker network" "pepi-eval") || return 2
    timeout_s=$(ask TIMEOUT_SECONDS "Pi invocation timeout (seconds)" "300") || return 2
    cpus=$(ask CPUS "CPU count" "4") || return 2
    memory=$(ask MEMORY "Memory" "8g") || return 2
    
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        check_network
        
        local task_count
        task_count=$(python3 -c "import json; m=json.load(open('$run_dir/manifest.json')); print(len(m['tasks']))" 2>/dev/null || echo "?")
        warn "⚠ Provider/account usage step: $task_count task(s); may incur charges or consume account quota; timeout $timeout_s s"
        
        ask_yes "Proceed with paid Pi run?" ||
            die "Paid run not confirmed (pass -y or set PEPI_EVAL_YES=1 to confirm non-interactively)" 2
    fi
    
    local run_cmd=(
        "$PEPI_PYTHON" -m evals.swebench run
        "$run_dir"
        --timeout-seconds "$timeout_s"
        --network "$network"
        --cpus "$cpus"
        --memory "$memory"
    )
    
    # auth.json (chmod 600) is injected into a tmpfs by the runner, never via --config-dir.
    # Builtin providers use auth.json; only frozen LiteLLM dependencies prompt for env credentials.
    if [[ -n "${PEPI_EVAL_SECRETS_DIR:-}" ]]; then
        [[ -d "$PEPI_EVAL_SECRETS_DIR" ]] || die "Secrets directory not found: $PEPI_EVAL_SECRETS_DIR" 2
        run_cmd+=(--secrets-dir "$PEPI_EVAL_SECRETS_DIR")
    fi
    
    if [[ "$use_host_auth" == "1" && "${DRY_RUN:-}" != "1" ]]; then
        # Adapter owns the private temp directory and cleanup; tokens never enter argv/env.
        local host_auth_pid="" host_auth_status=0
        # Bash otherwise defers traps while waiting for a foreground child. Forward and
        # reap the adapter so it can stop the runner and remove its credentials first.
        trap 'kill -TERM "${host_auth_pid:-$!}" 2>/dev/null; wait "${host_auth_pid:-$!}" 2>/dev/null; exit 130' INT TERM
        python3 -m evals.swebench.host_auth "$run_dir" -- "${run_cmd[@]}" & host_auth_pid=$!
        wait "$host_auth_pid" || host_auth_status=$?
        trap - INT TERM
        [[ $host_auth_status -eq 0 ]] || fail_step run "$host_auth_status"
    else
        run_cmd "${run_cmd[@]}" || fail_step run $?
    fi
    
    [[ "${DRY_RUN:-}" != "1" ]] && warn "Run complete: $run_dir/predictions.jsonl"
    next_step
}

cmd_report() {
    step_header "telemetry"
    
    setup_venv
    
    # Resolve run name; in dry-run use timestamp default, in real mode require existing
    local run_name
    if [[ -n "${PEPI_EVAL_RUN_NAME:-}" ]]; then
        run_name="$PEPI_EVAL_RUN_NAME"
    elif [[ "${DRY_RUN:-}" == "1" ]]; then
        # In dry-run, default to smoke-TIMESTAMP for convenience
        run_name="smoke-$(date +%Y%m%d-%H%M)"
    elif is_tty; then
        printf 'pepi-eval: Available runs:\n' >&2
        if ls -d "$runs_dir"/* &>/dev/null; then
            ls -d "$runs_dir"/* 2>/dev/null | xargs -n 1 basename | nl >&2
        else
            printf 'pepi-eval: (none)\n' >&2
        fi
        printf 'pepi-eval: tip: export PEPI_EVAL_RUN_NAME to skip this step\n' >&2
        printf '%s' "Run name (from runs/): " >&2
        if ! read run_name; then
            die "Failed to read run name" 2
        fi
    else
        die "Missing required env var: PEPI_EVAL_RUN_NAME" 2
    fi
    
    local run_dir="$runs_dir/$run_name"
    
    validate_name "$run_name"

    # In dry-run, skip preconditions; otherwise check
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        [[ -d "$run_dir" ]] || die "Run directory not found: $run_dir" 2
        [[ -f "$run_dir/predictions.jsonl" ]] || die "No predictions found; run 'prepare', 'build', 'run' first" 2
    fi
    
    local telemetry_cmd=("$PEPI_PYTHON" -m evals.swebench.telemetry "$run_dir")
    run_cmd "${telemetry_cmd[@]}" || fail_step telemetry $?
    
    next_step
    step_header "grade"
    
    local task_repo workers timeout_grade
    
    # In dry-run, use defaults; otherwise ask
    if [[ "${DRY_RUN:-}" == "1" ]]; then
        task_repo="${PEPI_EVAL_TASK_REPO:-$HOME/swe-bench-tasks}"
        workers="1"
        timeout_grade="1800"
    else
        task_repo=$(ask TASK_REPO "Task repo path" "${PEPI_EVAL_TASK_REPO:-$HOME/swe-bench-tasks}") || return 2
        workers=$(ask WORKERS "Grade workers" "1") || return 2
        timeout_grade=$(ask TEST_TIMEOUT_SECONDS "Test timeout (seconds)" "1800") || return 2
    fi
    
    local grade_cmd=(
        "$PEPI_PYTHON" -m evals.swebench.reporting grade
        "$run_dir"
        --task-repo "$task_repo"
        --workers "$workers"
        --test-timeout-seconds "$timeout_grade"
    )
    
    # Exit 2 is a validation error. Any other nonzero status is the evaluator's own
    # and says nothing about task results; summarize then reports what is missing.
    run_cmd "${grade_cmd[@]}" || {
        local grade_exit=$?
        [[ $grade_exit -eq 2 || $grade_exit -eq 130 ]] && fail_step grade "$grade_exit"
        warn "Grade step exited with code $grade_exit (see grading/<id>/grader.log)"
    }
    
    next_step
    step_header "summarize"
    
    local summarize_cmd=("$PEPI_PYTHON" -m evals.swebench.reporting summarize "$run_dir")
    run_cmd "${summarize_cmd[@]}" || fail_step summarize $?
    
    [[ "${DRY_RUN:-}" != "1" ]] && warn "Reports in: $run_dir/summary.{json,csv,md}"
    
    next_step
}

cmd_compare() {
    step_header "compare"
    
    setup_venv
    
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        if [[ ! -d "$runs_dir" ]] || [[ -z "$(ls -d "$runs_dir"/* 2>/dev/null)" ]]; then
            die "No runs found in $runs_dir" 2
        fi
    fi
    
    printf 'pepi-eval: Available runs:\n' >&2
    if [[ -d "$runs_dir" ]]; then
        ls -d "$runs_dir"/* 2>/dev/null | xargs -n 1 basename | nl >&2 || printf 'pepi-eval: (none)\n' >&2
    fi
    
    # Resolve baseline and candidate, requiring them
    local baseline
    if [[ -n "${PEPI_EVAL_BASELINE_RUN:-}" ]]; then
        baseline="$PEPI_EVAL_BASELINE_RUN"
    elif is_tty; then
        printf 'pepi-eval: tip: export PEPI_EVAL_BASELINE_RUN to skip this step\n' >&2
        printf '%s' "Baseline run name: " >&2
        if ! read baseline; then
            die "Failed to read baseline" 2
        fi
    else
        die "Missing required env var: PEPI_EVAL_BASELINE_RUN" 2
    fi
    [[ -n "$baseline" ]] || die "Baseline name required" 2
    
    local candidate
    if [[ -n "${PEPI_EVAL_CANDIDATE_RUN:-}" ]]; then
        candidate="$PEPI_EVAL_CANDIDATE_RUN"
    elif is_tty; then
        printf 'pepi-eval: tip: export PEPI_EVAL_CANDIDATE_RUN to skip this step\n' >&2
        printf '%s' "Candidate run name: " >&2
        if ! read candidate; then
            die "Failed to read candidate" 2
        fi
    else
        die "Missing required env var: PEPI_EVAL_CANDIDATE_RUN" 2
    fi
    [[ -n "$candidate" ]] || die "Candidate name required" 2
    
    local baseline_dir="$runs_dir/$baseline" candidate_dir="$runs_dir/$candidate"
    [[ -d "$baseline_dir" ]] || die "Baseline not found: $baseline_dir" 2
    [[ -d "$candidate_dir" ]] || die "Candidate not found: $candidate_dir" 2
    
    local compare_cmd=(
        "$PEPI_PYTHON" -m evals.swebench.reporting compare
        "$baseline_dir" "$candidate_dir"
    )
    
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        local output
        output=$(run_cmd "${compare_cmd[@]}") || fail_step compare $?
        local out_file="/tmp/pepi-comparison-$(date +%s).json"
        printf '%s\n' "$output" > "$out_file"
        warn "Comparison written to: $out_file"
    else
        run_cmd "${compare_cmd[@]}"
    fi
    
    next_step
}

cmd_all() {
    cmd_check || return $?
    cmd_prepare || return $?
    cmd_build || return $?
    cmd_run || return $?
    cmd_report || return $?
    warn "✓ Evaluation complete"
}

cmd_help() {
    cat >&2 <<'EOF'
Usage: evals/pepi-eval.sh [OPTIONS] [COMMAND]

Commands:
  check      Verify dependencies and setup (1 step)
  prepare    Freeze tasks and profile (1 step)
  build      Build Docker images (1 step)
  run        Run Pi on tasks — PAID (1 step)
  report     Telemetry, grade, summarize (3 steps)
  compare    Compare two runs (1 step)
  all        Run all steps (7 steps, default)
  help       Show this help

Options:
  -y, --yes           Accept defaults and provider usage (NOT host-auth consent)
  --dry-run           Print commands (shell-quoted, key masked) without running
  -h, --help          Show this help

Secrets (only for frozen LiteLLM providers; builtins use SECRETS_DIR auth.json):
  LITELLM_BASE_URL      LiteLLM gateway URL
  LITELLM_API_KEY       Dedicated, budget-capped API key

Optional PEPI_EVAL_* env vars:
  VENV                  Python venv path (default ~/.venvs/swebench)
  SWEBENCH_REPO         SWE-bench source (default ~/SWE-bench)
  TASK_REPO             Task definitions (default ~/swe-bench-tasks)
  IDS                   Task IDs: comma-sep or file (default custom Verified20 v1)
  RUN_NAME              Run directory name (default smoke-YYYYMMDD-HHMM)
  PI_VERSION            Pi version (auto-detected)
  LITELLM_VERSION       LiteLLM provider version (auto-detected only if required)
  SUBAGENTS_VERSION     Subagents version (auto-detected)
  NODE_IMAGE            Node base image (default node:22-bookworm-slim)
  PROFILE               Named profiles/NAME selection (bedrock or codex, for example)
  CONFIG_DIR            Injected config directory (never auth.json/mcp.json/.env)
  CONFIG_LABEL          Config label for manifest
  DROP_MCP              1 disables native MCP (default: 0, enabled; Pi >= 1.1.0)
  PLATFORM              Docker build platform (default linux/amd64)
  NETWORK               Docker network (default pepi-eval)
  TIMEOUT_SECONDS       Per-task Pi invocation timeout (default 300; excludes setup/export)
  CPUS                  CPU count (default 4)
  MEMORY                Memory (default 8g)
  SECRETS_DIR           chmod-700 directory with chmod-600 auth.json/mcp.json (runtime only)
  USE_HOST_AUTH         1 explicitly consents to temporary ~/.pi/agent/auth.json copy;
                        0 disables proposal; unset asks on TTY only (default No).
                        SECRETS_DIR wins. OAuth refresh may invalidate shared host login.
                        Dry-run never reads/copies host auth or creates a credential dir.
  WORKERS               Grade workers (default 1)
  TEST_TIMEOUT_SECONDS  Grading timeout (default 1800)
  BASELINE_RUN          Baseline for compare
  CANDIDATE_RUN         Candidate for compare
  RUNS_DIR              Override runs directory (default evals/swebench/runs)
  YES                   Set to 1 to auto-accept defaults/usage, NOT host-auth copy

Exit codes (same as python -m evals.swebench):
  0 ok, 2 usage/configuration error, 3 step or infrastructure failure, 130 interrupted

Examples:
  # Interactive smoke test
  evals/pepi-eval.sh

  # Automated with all vars set
  PEPI_EVAL_IDS=django__django-12345 PEPI_EVAL_YES=1 evals/pepi-eval.sh all

  # Dry-run: print all 7 commands (key masked)
  LITELLM_API_KEY=sk-dummy LITELLM_BASE_URL=https://x evals/pepi-eval.sh --dry-run -y all

  # Just check dependencies
  evals/pepi-eval.sh check
EOF
}

main() {
    local command="all"
    
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -y|--yes) PEPI_EVAL_YES=1; shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            -h|--help) cmd_help; exit 0 ;;
            all|check|prepare|build|run|report|compare|help)
                command="$1"; shift ;;
            -*)
                die "Unknown option: $1" 2 ;;
            *)
                die "Unknown command: $1" 2 ;;
        esac
    done
    
    # Set step_total before running
    case "$command" in
        all) step_total=7 ;;
        report) step_total=3 ;;
        check|prepare|build|run|compare) step_total=1 ;;
        help) exit 0 ;;
    esac
    
    case "$command" in
        all) cmd_all ;;
        check) cmd_check ;;
        prepare) cmd_prepare ;;
        build) cmd_build ;;
        run) cmd_run ;;
        report) cmd_report ;;
        compare) cmd_compare ;;
        help) cmd_help ;;
        *) die "Unknown command: $command" 2 ;;
    esac
}

main "$@"

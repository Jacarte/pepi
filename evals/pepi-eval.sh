#!/bin/bash
# Interactive CLI for SWE-bench evaluation workflow
# Secrets (required for 'run' step):
#   LITELLM_BASE_URL: LiteLLM gateway URL
#   LITELLM_API_KEY: Dedicated, budget-capped API key
# Optional PEPI_EVAL_* env vars override defaults; see --help.

set -o pipefail
export PYTHONDONTWRITEBYTECODE=1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 3
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
    if is_tty && [[ -z "$value" ]]; then
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

# Check if a module --help has a flag
has_flag() {
    local module=$1 flag=$2
    $PEPI_PYTHON -m "$module" --help 2>&1 | grep -q "\-\-$flag" && return 0
    return 1
}

# Validate name: alphanumeric, dot, dash, underscore only
validate_name() {
    local name=$1
    if [[ ! "$name" =~ ^[A-Za-z0-9._-]+$ ]]; then
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
    local ids_input="${PEPI_EVAL_IDS:-sympy__sympy-20590}"
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
    local cmd=("$@")
    
    if [[ "${DRY_RUN:-}" == "1" ]]; then
        local display_cmd=()
        for arg in "${cmd[@]}"; do
            if [[ "$arg" == "$LITELLM_API_KEY" ]]; then
                display_cmd+=("<API_KEY>")
            else
                display_cmd+=("$(printf '%q' "$arg")")
            fi
        done
        printf '+ %s\n' "${display_cmd[*]}"
        return 0
    fi
    
    local output
    output=$("${cmd[@]}" 2>&1)
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        printf '%s\n' "$output" >&2
        return $exit_code
    fi
    printf '%s\n' "$output"
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
    litellm_version=$(ask LITELLM_VERSION "LiteLLM version" "$(get_litellm_version)") || return 2
    subagents_version=$(ask SUBAGENTS_VERSION "Subagents version" "$(get_subagents_version)") || return 2
    node_image=$(ask NODE_IMAGE "Node image" "node:22-bookworm-slim") || return 2
    task_repo=$(ask TASK_REPO "Task repo path" "${PEPI_EVAL_TASK_REPO:-$HOME/swe-bench-tasks}") || return 2
    
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        mkdir -p "$run_dir" || die "Failed to create run directory" 3
    fi
    
    local prepare_cmd=(
        "$PEPI_PYTHON" -m evals.swebench prepare
        --pepi-root "$script_dir"
        --task-repo "$task_repo"
        --ids "$ids_file"
        --out "$run_dir"
        --pi-version "$pi_version"
        --litellm-version "$litellm_version"
        --subagents-version "$subagents_version"
        --node-image "$node_image"
    )
    
    # Add optional config flags if supported
    if has_flag evals.swebench.prepare config-dir; then
        [[ -n "${PEPI_EVAL_CONFIG_DIR:-}" ]] && prepare_cmd+=(--config-dir "${PEPI_EVAL_CONFIG_DIR}")
        [[ -n "${PEPI_EVAL_CONFIG_LABEL:-}" ]] && prepare_cmd+=(--config-label "${PEPI_EVAL_CONFIG_LABEL}")
    fi
    if has_flag evals.swebench.prepare drop-mcp && [[ "${PEPI_EVAL_DROP_MCP:-}" == "1" ]]; then
        prepare_cmd+=(--drop-mcp)
    fi
    
    run_cmd "${prepare_cmd[@]}" || die "prepare" 3
    
    [[ -f "$ids_file" ]] && [[ "$ids_file" =~ ^/tmp/ ]] && rm -f "$ids_file"
    
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
    
    run_cmd "${build_cmd[@]}" || die "build" 3
    
    [[ "${DRY_RUN:-}" != "1" ]] && warn "Build complete: $run_dir/builds.json"
    next_step
}

cmd_run() {
    step_header "run"
    
    setup_venv
    
    # Resolve secrets FIRST (before asking for run name)
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
    
    # In dry-run, skip preconditions; otherwise check
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        [[ -d "$run_dir" ]] || die "Run directory not found: $run_dir" 2
        
        if [[ -f "$run_dir/predictions.jsonl" ]]; then
            ask_yes "Run already complete. Skip run?" && { warn "Skipped run"; next_step; return 0; }
        fi
    fi
    
    # Optional run parameters
    local network timeout_s cpus memory
    network=$(ask NETWORK "Docker network" "pepi-eval") || return 2
    timeout_s=$(ask TIMEOUT_SECONDS "Timeout (seconds)" "1200") || return 2
    cpus=$(ask CPUS "CPU count" "4") || return 2
    memory=$(ask MEMORY "Memory" "8g") || return 2
    
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        check_network
        
        local task_count
        task_count=$(python3 -c "import json; m=json.load(open('$run_dir/manifest.json')); print(len(m['tasks']))" 2>/dev/null || echo "?")
        warn "⚠ Paid step: $task_count task(s) × ~\$20–50 each; timeout $timeout_s s"
        
        ask_yes "Proceed with paid Pi run?" || return 1
    fi
    
    export LITELLM_BASE_URL="$base_url"
    export LITELLM_API_KEY
    
    local run_cmd=(
        "$PEPI_PYTHON" -m evals.swebench run
        "$run_dir"
        --timeout-seconds "$timeout_s"
        --network "$network"
        --cpus "$cpus"
        --memory "$memory"
    )
    
    if has_flag evals.swebench.run secrets-dir && [[ -n "${PEPI_EVAL_SECRETS_DIR:-}" ]]; then
        run_cmd+=(--secrets-dir "${PEPI_EVAL_SECRETS_DIR}")
    fi
    
    run_cmd "${run_cmd[@]}" || die "run" 3
    
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
    
    # In dry-run, skip preconditions; otherwise check
    if [[ "${DRY_RUN:-}" != "1" ]]; then
        [[ -d "$run_dir" ]] || die "Run directory not found: $run_dir" 2
        [[ -f "$run_dir/predictions.jsonl" ]] || die "No predictions found; run 'prepare', 'build', 'run' first" 2
    fi
    
    local telemetry_cmd=("$PEPI_PYTHON" -m evals.swebench.telemetry "$run_dir")
    run_cmd "${telemetry_cmd[@]}" || warn "Telemetry step had warnings"
    
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
    
    run_cmd "${grade_cmd[@]}" || {
        local grade_exit=$?
        [[ $grade_exit -eq 2 ]] && die "grade" 2
        warn "Grade step exited with code $grade_exit (see grading/<id>/grader.log)"
    }
    
    next_step
    step_header "summarize"
    
    local summarize_cmd=("$PEPI_PYTHON" -m evals.swebench.reporting summarize "$run_dir")
    run_cmd "${summarize_cmd[@]}" || die "summarize" 3
    
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
        output=$(run_cmd "${compare_cmd[@]}") || die "compare" 3
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
  -y, --yes           Accept all defaults without prompting
  --dry-run           Print commands (shell-quoted, key masked) without running
  -h, --help          Show this help

Secrets (required for 'run', never echoed):
  LITELLM_BASE_URL      LiteLLM gateway URL
  LITELLM_API_KEY       Dedicated, budget-capped API key

Optional PEPI_EVAL_* env vars:
  VENV                  Python venv path (default ~/.venvs/swebench)
  SWEBENCH_REPO         SWE-bench source (default ~/SWE-bench)
  TASK_REPO             Task definitions (default ~/swe-bench-tasks)
  IDS                   Task IDs: comma-sep or file (default sympy__sympy-20590)
  RUN_NAME              Run directory name (default smoke-YYYYMMDD-HHMM)
  PI_VERSION            Pi version (auto-detected)
  LITELLM_VERSION       LiteLLM provider version (auto-detected)
  SUBAGENTS_VERSION     Subagents version (auto-detected)
  NODE_IMAGE            Node base image (default node:22-bookworm-slim)
  CONFIG_DIR            Injected config directory
  CONFIG_LABEL          Config label for manifest
  DROP_MCP              Set to 1 to allow MCP and drop it
  PLATFORM              Docker build platform (default linux/amd64)
  NETWORK               Docker network (default pepi-eval)
  TIMEOUT_SECONDS       Pi timeout (default 1200)
  CPUS                  CPU count (default 4)
  MEMORY                Memory (default 8g)
  SECRETS_DIR           auth.json directory (for --secrets-dir)
  WORKERS               Grade workers (default 1)
  TEST_TIMEOUT_SECONDS  Grading timeout (default 1800)
  BASELINE_RUN          Baseline for compare
  CANDIDATE_RUN         Candidate for compare
  RUNS_DIR              Override runs directory (default evals/swebench/runs)
  YES                   Set to 1 to auto-accept (same as --yes)

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

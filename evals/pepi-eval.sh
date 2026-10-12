#!/bin/bash
# Interactive CLI for SWE-bench evaluation workflow
# Env vars (prefix PEPI_EVAL_):
#   PEPI_EVAL_VENV: Python venv (default ~/.venvs/swebench)
#   PEPI_EVAL_SWEBENCH_REPO: SWE-bench source (default ~/SWE-bench)
#   PEPI_EVAL_TASK_REPO: Task definitions (default ~/swe-bench-tasks)
#   PEPI_EVAL_IDS: Comma-separated task IDs or file path (default sympy__sympy-20590)
#   PEPI_EVAL_RUN_NAME: Run directory name (default smoke-DATE-TIME)
#   PEPI_EVAL_PI_VERSION, PEPI_EVAL_LITELLM_VERSION, PEPI_EVAL_SUBAGENTS_VERSION: package versions
#   PEPI_EVAL_NODE_IMAGE: Node base image (default node:22-bookworm-slim)
#   PEPI_EVAL_CONFIG_DIR, PEPI_EVAL_CONFIG_LABEL, PEPI_EVAL_DROP_MCP: injected config
#   PEPI_EVAL_PLATFORM: Docker build platform (default linux/amd64)
#   PEPI_EVAL_NETWORK: Docker network (default pepi-eval)
#   PEPI_EVAL_TIMEOUT_SECONDS, PEPI_EVAL_CPUS, PEPI_EVAL_MEMORY: run parameters
#   LITELLM_BASE_URL, LITELLM_API_KEY: gateway credentials (required for run)
#   PEPI_EVAL_SECRETS_DIR: auth.json directory (if --secrets-dir supported)
#   PEPI_EVAL_WORKERS, PEPI_EVAL_TEST_TIMEOUT_SECONDS: grading parameters
#   PEPI_EVAL_BASELINE_RUN, PEPI_EVAL_CANDIDATE_RUN: compare inputs

set -o pipefail
export PYTHONDONTWRITEBYTECODE=1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 3
eval_dir="$script_dir/evals/swebench"
runs_dir="$eval_dir/runs"
step_count=0
step_total=0

is_tty() { [[ -t 0 && -t 1 ]]; }
is_ci() { [[ ! -t 0 ]]; }

die() { printf 'Error: %s\n' "$1" >&2; exit "${2:-3}"; }
warn() { printf 'Warning: %s\n' "$1" >&2; }
info() { printf '%s\n' "$1"; }
step_header() { printf '==> step %d/%d: %s\n' "$((++step_count))" "$step_total" "$1"; }
next_step() { printf '\n'; }

# Prompt with env var fallback
ask() {
    local var_name=$1 prompt=$2 default=$3
    local env_var="PEPI_EVAL_${var_name}"
    local value="${!env_var:-}"
    
    if [[ -n "$value" ]]; then
        info "using $env_var"
        echo "$value"
        return
    fi
    
    if is_ci; then
        if [[ -z "$default" ]]; then
            die "Missing required env var: $env_var" 2
        fi
        echo "$default"
        return
    fi
    
    local tip_var="${var_name#_}"
    local display_default="${default:+[$default]}"
    local answer
    if is_tty; then
        read -p "$prompt $display_default: " answer
    else
        read answer
    fi
    
    if [[ -z "$answer" ]]; then
        if [[ -n "$default" ]]; then
            answer="$default"
        else
            die "No value provided for $var_name" 2
        fi
    fi
    
    echo "$answer"
    [[ -z "$default" ]] || info "tip: export PEPI_EVAL_${tip_var}='$answer' to make this step automatic"
}

ask_secret() {
    local var_name=$1 prompt=$2
    local env_var="PEPI_EVAL_${var_name}"
    local value="${!env_var:-}"
    
    if [[ -n "$value" ]]; then
        info "using $env_var"
        return 0
    fi
    
    if is_ci; then
        die "Missing required env var for secret: $env_var" 2
    fi
    
    if is_tty; then
        read -sp "$prompt: " value
        echo ""
    else
        read value
    fi
    
    if [[ -z "$value" ]]; then
        die "No value provided for $var_name" 2
    fi
    
    export "$env_var"="$value"
    info "tip: export $env_var=\<your-key\> to make this step automatic (do not paste value in code)"
}

# Ensure directory exists or create it
ensure_dir() {
    if [[ ! -d "$1" ]]; then
        ask_yes "Directory $1 does not exist. Create it?" && mkdir -p "$1" || die "Aborted" 1
    fi
}

# Check if command exists in $PATH
has_cmd() { command -v "$1" &>/dev/null; }

# Feature detection: grep subcommand help for a flag
has_flag() {
    local subcommand=$1 flag=$2
    python3 -m "evals.swebench${subcommand:+.$subcommand}" --help 2>&1 | grep -q "\-\-$flag" && return 0
    return 1
}

# Get versions from installed packages
get_pi_version() {
    pi --version 2>&1 | head -1 || echo "1.1.0"
}

get_litellm_version() {
    node -p "require('$script_dir/npm/node_modules/pi-provider-litellm/package.json').version" 2>/dev/null || echo "3.0.1"
}

get_subagents_version() {
    node -p "require('$script_dir/npm/node_modules/pi-subagents/package.json').version" 2>/dev/null || echo "0.68.0"
}

# Check if Python module is importable
can_import() {
    python3 -c "import $1" 2>/dev/null && return 0
    return 1
}

# Initialize venv and install packages if needed
setup_venv() {
    local venv="${PEPI_EVAL_VENV:-$HOME/.venvs/swebench}"
    
    if [[ ! -d "$venv" ]]; then
        if is_ci; then
            die "Venv $venv does not exist and PEPI_EVAL_VENV is not set" 2
        fi
        info "Virtual environment not found at $venv"
        if ask_yes "Create and set up venv?"; then
            python3 -m venv "$venv" || die "Failed to create venv"
            source "$venv/bin/activate" || die "Failed to activate venv"
            pip install -q -e "${PEPI_EVAL_SWEBENCH_REPO:-$HOME/SWE-bench}" || die "Failed to install SWE-bench"
            info "Venv ready at $venv"
        else
            die "Venv setup cancelled" 1
        fi
    fi
    
    if [[ -f "$venv/bin/python" ]]; then
        export PEPI_PYTHON="$venv/bin/python"
    else
        export PEPI_PYTHON="python3"
    fi
    
    if ! $PEPI_PYTHON -c "import swebench.task.repo" 2>/dev/null; then
        warn "swebench module not available in venv"
        if ask_yes "Install SWE-bench into $venv?"; then
            source "$venv/bin/activate" || die "Failed to activate venv"
            pip install -q -e "${PEPI_EVAL_SWEBENCH_REPO:-$HOME/SWE-bench}" || die "Failed to install"
            info "SWE-bench installed"
        else
            die "Cannot proceed without swebench module" 1
        fi
    fi
    
    source "$venv/bin/activate" || die "Failed to activate venv"
}

# Ensure repos are cloned
ensure_repo() {
    local repo_var=$1 url=$2 default_path=$3
    local repo_path="${!repo_var:-$default_path}"
    
    if [[ ! -d "$repo_path" ]]; then
        if is_ci; then
            die "Repository not found: $repo_path (set $repo_var)" 2
        fi
        info "Repository not found: $repo_path"
        if ask_yes "Clone $url?"; then
            git clone -q --depth 1 "$url" "$repo_path" || die "Failed to clone $url"
            info "Cloned to $repo_path"
        else
            die "Cannot proceed without repository" 1
        fi
    fi
}

ask_yes() {
    local prompt=$1
    if [[ "${PEPI_EVAL_YES:-}" == "1" ]] || [[ "${YES_FLAG:-}" == "1" ]]; then
        return 0
    fi
    if is_ci; then
        return 1
    fi
    if is_tty; then
        read -p "$prompt (y/n) " -n 1 -r
        echo
        [[ $REPLY =~ ^[Yy]$ ]]
    else
        return 1
    fi
}

# Resolve IDs and write to temp file
prepare_ids_file() {
    local ids_input="${PEPI_EVAL_IDS:-sympy__sympy-20590}"
    local ids_file
    
    if [[ -f "$ids_input" ]]; then
        echo "$ids_input"
        return
    fi
    
    ids_file=$(mktemp) || die "Failed to create temp file"
    printf '%s\n' "${ids_input//,/$'\n'}" > "$ids_file"
    echo "$ids_file"
}

# Check Docker network exists; create if needed
ensure_network() {
    local network="${PEPI_EVAL_NETWORK:-pepi-eval}"
    
    if ! docker network inspect "$network" &>/dev/null; then
        if is_ci; then
            die "Docker network $network does not exist" 2
        fi
        warn "Docker network $network does not exist"
        if ask_yes "Create Docker network $network?"; then
            docker network create "$network" || die "Failed to create network"
            warn "NOTE: This is a plain bridge network. Gateway-only egress must be enforced externally."
        else
            die "Cannot proceed without network" 1
        fi
    fi
}

# Get from auth.json if available
get_litellm_url() {
    if [[ -z "${LITELLM_BASE_URL:-}" ]]; then
        local url
        url=$(python3 -c "import json; d=json.load(open(expanduser('~/.pi/agent/auth.json'))); print(d.get('litellm',{}).get('env',{}).get('LITELLM_BASE_URL',''))" 2>/dev/null)
        [[ -n "$url" ]] && echo "$url"
    fi
}

# Main command execution
run_step() {
    local cmd=("$@")
    local output
    
    if [[ "${DRY_RUN:-}" == "1" ]]; then
        # Mask API key in dry-run output
        local display_cmd=("${cmd[@]}")
        if [[ -n "${LITELLM_API_KEY:-}" ]]; then
            display_cmd=("${display_cmd[@]//LITELLM_API_KEY=*/LITELLM_API_KEY=<redacted>}")
        fi
        printf '%s\n' "${display_cmd[@]}"
        return 0
    fi
    
    output=$("${cmd[@]}" 2>&1) || {
        local exit_code=$?
        echo "$output" >&2
        return $exit_code
    }
    echo "$output"
}

# Idempotency: check if step already done
step_done() {
    local marker=$1
    [[ -f "$marker" ]] && return 0
    return 1
}

skip_if_done() {
    local marker=$1 step_name=$2
    if step_done "$marker"; then
        if is_ci || [[ "${PEPI_EVAL_YES:-}" == "1" ]]; then
            info "Output already exists; skipping $step_name"
            return 0
        fi
        if ask_yes "Output already exists. Skip $step_name?"; then
            return 0
        fi
    fi
    return 1
}

# Commands

cmd_check() {
    step_header "check"
    
    info "Checking dependencies..."
    
    has_cmd docker || die "docker not found" 3
    has_cmd python3 || die "python3 not found" 3
    has_cmd git || die "git not found" 3
    
    info "✓ docker, python3, git available"
    
    setup_venv
    ensure_repo PEPI_EVAL_SWEBENCH_REPO "https://github.com/SWE-bench/SWE-bench" "$HOME/SWE-bench"
    ensure_repo PEPI_EVAL_TASK_REPO "https://github.com/SWE-bench/swe-bench-tasks" "$HOME/swe-bench-tasks"
    
    info "✓ swebench module ready"
    info "✓ task-repo ready"
    
    next_step
}

cmd_prepare() {
    step_header "prepare"
    
    setup_venv
    
    local run_name
    run_name=$(ask RUN_NAME "Run name" "smoke-$(date +%Y%m%d-%H%M)") || die "Failed to get run name" 2
    
    local run_dir="$runs_dir/$run_name"
    if [[ -d "$run_dir" ]]; then
        if is_ci; then
            die "Run directory already exists: $run_dir" 2
        fi
        if ! ask_yes "Run directory exists: $run_dir. Overwrite?"; then
            die "Aborted" 1
        fi
        rm -rf "$run_dir"
    fi
    mkdir -p "$run_dir"
    
    local ids_file pi_version litellm_version subagents_version node_image task_repo
    ids_file=$(prepare_ids_file)
    pi_version=$(ask PI_VERSION "Pi version" "$(get_pi_version)")
    litellm_version=$(ask LITELLM_VERSION "LiteLLM version" "$(get_litellm_version)")
    subagents_version=$(ask SUBAGENTS_VERSION "Subagents version" "$(get_subagents_version)")
    node_image=$(ask NODE_IMAGE "Node image" "node:22-bookworm-slim")
    task_repo=$(ask TASK_REPO "Task repo path" "$HOME/swe-bench-tasks")
    
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
    
    run_step "${prepare_cmd[@]}" || die "Prepare failed" 3
    
    [[ -f "$ids_file" ]] && [[ "$ids_file" =~ ^/tmp/ ]] && rm -f "$ids_file"
    
    info "Prepared run: $run_dir"
    next_step
}

cmd_build() {
    step_header "build"
    
    setup_venv
    
    local run_dir
    run_dir=$(ask RUN_NAME "Run directory (basename from runs/)" "smoke-$(date +%Y%m%d-%H%M)")
    run_dir="$runs_dir/$run_dir"
    
    if [[ ! -d "$run_dir" ]]; then
        die "Run directory not found: $run_dir" 2
    fi
    
    if skip_if_done "$run_dir/builds.json" "build"; then
        return
    fi
    
    local platform
    platform=$(ask PLATFORM "Docker platform" "linux/amd64")
    [[ "$platform" == "linux/arm64" || "$platform" == "arm64" ]] && warn "arm64 will be emulated; build will be slow"
    
    local build_cmd=(
        "$PEPI_PYTHON" -m evals.swebench build
        "$run_dir"
        --platform "$platform"
    )
    
    run_step "${build_cmd[@]}" || die "Build failed" 3
    
    info "Build complete: $run_dir/builds.json"
    next_step
}

cmd_run() {
    step_header "run"
    
    setup_venv
    ensure_network
    
    local run_dir
    run_dir=$(ask RUN_NAME "Run directory (basename from runs/)" "smoke-$(date +%Y%m%d-%H%M)")
    run_dir="$runs_dir/$run_dir"
    
    if [[ ! -d "$run_dir" ]]; then
        die "Run directory not found: $run_dir" 2
    fi
    
    if skip_if_done "$run_dir/predictions.jsonl" "run"; then
        return
    fi
    
    # Require gateway credentials for paid step
    local base_url network timeout_s cpus memory
    base_url=$(ask LITELLM_BASE_URL "LiteLLM base URL" "$(get_litellm_url)") || die "LiteLLM base URL required" 2
    
    ask_secret LITELLM_API_KEY "LiteLLM API key (dedicated, budget-capped)"
    warn "*** REMINDER: Use a dedicated, budget-capped API key, not your personal key ***"
    
    network=$(ask NETWORK "Docker network" "pepi-eval")
    timeout_s=$(ask TIMEOUT_SECONDS "Timeout (seconds)" "1200")
    cpus=$(ask CPUS "CPU count" "4")
    memory=$(ask MEMORY "Memory" "8g")
    
    # Show cost warning
    local task_count
    task_count=$(python3 -c "import json; m=json.load(open('$run_dir/manifest.json')); print(len(m['tasks']))" 2>/dev/null || echo "?")
    warn "*** Paid step: $task_count task(s) × ~\$20–50 each; timeout $timeout_s s; gateway: $base_url ***"
    
    if ! ask_yes "Proceed with Pi run?"; then
        die "Cancelled" 1
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
    
    run_step "${run_cmd[@]}" || die "Run failed" 3
    
    info "Run complete: $run_dir/predictions.jsonl"
    next_step
}

cmd_report() {
    step_header "telemetry"
    
    setup_venv
    
    local run_dir
    run_dir=$(ask RUN_NAME "Run directory (basename from runs/)" "smoke-$(date +%Y%m%d-%H%M)")
    run_dir="$runs_dir/$run_dir"
    
    if [[ ! -d "$run_dir" ]]; then
        die "Run directory not found: $run_dir" 2
    fi
    
    if [[ ! -f "$run_dir/predictions.jsonl" ]]; then
        die "No predictions found; run cmd_run first" 3
    fi
    
    local telemetry_cmd=("$PEPI_PYTHON" -m evals.swebench.telemetry "$run_dir")
    run_step "${telemetry_cmd[@]}" || warn "Telemetry step had warnings"
    
    next_step
    step_header "grade"
    
    local task_repo workers timeout_grade
    task_repo=$(ask TASK_REPO "Task repo path" "$HOME/swe-bench-tasks")
    workers=$(ask WORKERS "Workers" "1")
    timeout_grade=$(ask TEST_TIMEOUT_SECONDS "Test timeout (seconds)" "1800")
    
    local grade_cmd=(
        "$PEPI_PYTHON" -m evals.swebench.reporting grade
        "$run_dir"
        --task-repo "$task_repo"
        --workers "$workers"
        --test-timeout-seconds "$timeout_grade"
    )
    
    run_step "${grade_cmd[@]}" || {
        local grade_exit=$?
        [[ $grade_exit -eq 2 ]] && die "Grade validation error" 2
        warn "Grade step exited with code $grade_exit (see grading/<id>/grader.log)"
    }
    
    next_step
    step_header "summarize"
    
    local summarize_cmd=("$PEPI_PYTHON" -m evals.swebench.reporting summarize "$run_dir")
    run_step "${summarize_cmd[@]}" || die "Summarize failed" 3
    
    info "Reports in: $run_dir/summary.{json,csv,md}"
    python3 -c "import json; s=json.load(open('$run_dir/summary.json')); print('Resolved:', s.get('resolved_rate', 'N/A'))" 2>/dev/null || true
    
    next_step
}

cmd_compare() {
    step_header "compare"
    
    setup_venv
    
    # List available runs
    if [[ ! -d "$runs_dir" ]] || [[ -z "$(ls -d "$runs_dir"/* 2>/dev/null)" ]]; then
        die "No runs found in $runs_dir" 2
    fi
    
    info "Available runs:"
    ls -d "$runs_dir"/* 2>/dev/null | xargs -n 1 basename | nl
    
    local baseline candidate
    baseline=$(ask BASELINE_RUN "Baseline run name" "") || die "No baseline specified" 2
    candidate=$(ask CANDIDATE_RUN "Candidate run name" "") || die "No candidate specified" 2
    
    local baseline_dir="$runs_dir/$baseline" candidate_dir="$runs_dir/$candidate"
    [[ -d "$baseline_dir" ]] || die "Baseline not found: $baseline_dir" 2
    [[ -d "$candidate_dir" ]] || die "Candidate not found: $candidate_dir" 2
    
    local compare_cmd=(
        "$PEPI_PYTHON" -m evals.swebench.reporting compare
        "$baseline_dir" "$candidate_dir"
    )
    
    local output
    output=$(run_step "${compare_cmd[@]}") || die "Compare failed" 3
    
    local out_file="/tmp/pepi-comparison-$(date +%s).json"
    echo "$output" > "$out_file"
    info "Comparison written to: $out_file"
    
    next_step
}

cmd_all() {
    step_total=7
    cmd_check
    cmd_prepare
    cmd_build
    cmd_run
    cmd_report
    info "✓ Evaluation complete"
}

cmd_help() {
    cat <<'EOF'
Usage: evals/pepi-eval.sh [OPTIONS] [COMMAND]

Commands:
  check      Verify dependencies and setup
  prepare    Freeze tasks and profile
  build      Build Docker images
  run        Run Pi on tasks (PAID)
  report     Telemetry, grade, summarize
  compare    Compare two runs
  all        Run all steps (default)
  help       Show this help

Options:
  -y, --yes           Accept all defaults without prompting
  --dry-run           Print commands without running them
  -h, --help          Show this help

Environment Variables (PEPI_EVAL_* prefix):
  VENV                  Python venv path (default ~/.venvs/swebench)
  SWEBENCH_REPO         SWE-bench source (default ~/SWE-bench)
  TASK_REPO             Task definitions (default ~/swe-bench-tasks)
  IDS                   Task IDs: comma-sep or file (default sympy__sympy-20590)
  RUN_NAME              Run directory name (default smoke-YYYYMMDD-HHMM)
  PI_VERSION            Pi version (auto-detected)
  LITELLM_VERSION       LiteLLM provider version (auto-detected)
  SUBAGENTS_VERSION     Subagents version (auto-detected)
  NODE_IMAGE            Node base image (default node:22-bookworm-slim)
  PLATFORM              Docker build platform (default linux/amd64)
  NETWORK               Docker network (default pepi-eval)
  TIMEOUT_SECONDS       Pi timeout (default 1200)
  CPUS                  CPU count (default 4)
  MEMORY                Memory (default 8g)
  WORKERS               Grade workers (default 1)
  TEST_TIMEOUT_SECONDS  Grading timeout (default 1800)
  BASELINE_RUN          Baseline for compare
  CANDIDATE_RUN         Candidate for compare

Secrets (not read from auth.json; required for run):
  LITELLM_BASE_URL      Gateway URL (suggested from auth.json if available)
  LITELLM_API_KEY       Dedicated, budget-capped API key

Examples:
  # Interactive smoke test
  evals/pepi-eval.sh

  # Automated with all env vars set
  PEPI_EVAL_IDS=django__django-12345 PEPI_EVAL_YES=1 evals/pepi-eval.sh

  # Dry-run to see commands (key masked)
  LITELLM_API_KEY=dummy evals/pepi-eval.sh --dry-run

  # Just check dependencies
  evals/pepi-eval.sh check
EOF
}

# Main entry

main() {
    local command="all"
    
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -y|--yes) YES_FLAG=1 PEPI_EVAL_YES=1; shift ;;
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

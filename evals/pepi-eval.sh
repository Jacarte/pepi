#!/bin/bash
# Interactive CLI for SWE-bench evaluation workflow
# Secrets (only for runs requiring LiteLLM; builtin providers use runtime auth.json):
#   LITELLM_BASE_URL: LiteLLM gateway URL
#   LITELLM_API_KEY: Dedicated, budget-capped API key
# Optional PEPI_EVAL_* env vars override defaults; see --help.

set -o pipefail
export PYTHONDONTWRITEBYTECODE=1

# ./.env is read from the directory the script is launched from, never from the checkout.
launch_dir=$(pwd 2>/dev/null) || launch_dir=""
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 3
# `python -m evals.swebench` must resolve from any working directory.
export PYTHONPATH="$script_dir${PYTHONPATH:+:$PYTHONPATH}"
eval_dir="$script_dir/evals/swebench"
runs_dir=""  # set in main(), after ./.env may have provided PEPI_EVAL_RUNS_DIR
step_count=0
step_total=0
prepared_providers=""
# ./.env state (see load_dotenv). Declared here so .env cannot overwrite them.
dotenv_file=""
dotenv_names=()           # every name loaded from .env
dotenv_settings=()        # PEPI_EVAL_*/LITELLM_BASE_URL/LITELLM_API_KEY, exported into this script
dotenv_forward=()         # container candidates: never exported here (see dotenv_export_forwarded)
dotenv_forward_values=()  # values of dotenv_forward, same order
dotenv_forward_approved=0
dotenv_value=""
shell_litellm_base_url=""

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

# ./.env names never loaded: they steer this shell, loaders, interpreters or the Docker client.
# Mirrors runner.forwardable_env_name for early feedback; the Python runner is authoritative.
dotenv_reserved() {
    case "$1" in
        PATH|HOME|USER|SHELL|PWD|OLDPWD|TMPDIR|IFS|ENV|BASH_ENV|SHELLOPTS|BASHOPTS|PS4|PROMPT_COMMAND) return 0 ;;
        CDPATH|GLOBIGNORE|POSIXLY_CORRECT|NODE_OPTIONS|NODE_PATH) return 0 ;;
        LD_*|DYLD_*|BASH_FUNC_*|DOCKER_*|PYTHON*) return 0 ;;
    esac
    return 1
}

# Settings that count only from the real shell environment or flags, never from ./.env: the
# consent knobs, and options that choose host code (venv Python, `pip install -e` source),
# credentials (a secrets dir skips the host-auth prompt) or the egress network.
dotenv_shell_only() {
    case "$1" in
        PEPI_EVAL_YES|PEPI_EVAL_USE_HOST_AUTH|PEPI_EVAL_AUTO_FIX|PEPI_EVAL_USE_DOTENV) return 0 ;;
        PEPI_EVAL_VENV|PEPI_EVAL_SWEBENCH_REPO|PEPI_EVAL_SECRETS_DIR|PEPI_EVAL_NETWORK) return 0 ;;
    esac
    return 1
}

# Record a container candidate (last value wins) without setting it in this shell.
dotenv_forward_set() {
    local _de_i=0
    while (( _de_i < ${#dotenv_forward[@]} )); do
        if [[ "${dotenv_forward[$_de_i]}" == "$1" ]]; then
            dotenv_forward_values[$_de_i]=$2
            return 0
        fi
        _de_i=$((_de_i + 1))
    done
    dotenv_forward+=("$1")
    dotenv_forward_values+=("$2")
}

# Export the approved container candidates. Call only in the subshell that then execs the
# runner, with the command already fixed in "$@": a .env name may shadow one of the caller's
# locals (e.g. the run_cmd array, which bash would not export), so that local is dropped first.
dotenv_export_forwarded() {
    [[ $dotenv_forward_approved == 1 ]] || return 0
    local _de_index=0
    while (( _de_index < ${#dotenv_forward[@]} )); do
        unset -v "${dotenv_forward[$_de_index]}" 2>/dev/null
        export "${dotenv_forward[$_de_index]}=${dotenv_forward_values[$_de_index]}" || return 1
        _de_index=$((_de_index + 1))
    done
}

dotenv_has() {
    local candidate
    for candidate in "${dotenv_names[@]}"; do
        [[ "$candidate" == "$1" ]] && return 0
    done
    return 1
}

# Parse the text after `NAME=` into dotenv_value; return 1 on a syntax error. No expansion:
# $VAR, $(...) and backticks stay literal. 'single' quotes are literal; "double" quotes
# understand only \" \\ and \n; unquoted values are trimmed and lose a whitespace-preceded #comment.
dotenv_parse_value() {
    local _de_raw=$1 _de_rest _de_char _de_out="" _de_closed=0
    _de_rest="${_de_raw#"${_de_raw%%[![:space:]]*}"}"
    case "$_de_rest" in
        \'*)
            _de_rest=${_de_rest#\'}
            [[ "$_de_rest" == *\'* ]] || return 1
            _de_out=${_de_rest%%\'*}
            _de_rest=${_de_rest#*\'}
            ;;
        \"*)
            _de_rest=${_de_rest#\"}
            while [[ -n "$_de_rest" ]]; do
                _de_char=${_de_rest:0:1}
                _de_rest=${_de_rest:1}
                if [[ "$_de_char" == '\' ]]; then
                    case "${_de_rest:0:1}" in
                        '"') _de_out+='"'; _de_rest=${_de_rest:1} ;;
                        '\') _de_out+='\'; _de_rest=${_de_rest:1} ;;
                        n) _de_out+=$'\n'; _de_rest=${_de_rest:1} ;;
                        *) _de_out+='\' ;;
                    esac
                elif [[ "$_de_char" == '"' ]]; then
                    _de_closed=1
                    break
                else
                    _de_out+=$_de_char
                fi
            done
            [[ $_de_closed == 1 ]] || return 1
            ;;
        *)
            _de_out=${_de_raw%%[[:space:]]#*}
            _de_out="${_de_out#"${_de_out%%[![:space:]]*}"}"
            dotenv_value="${_de_out%"${_de_out##*[![:space:]]}"}"
            return 0
            ;;
    esac
    # Only whitespace or a comment may follow the closing quote.
    [[ "$_de_rest" =~ ^[[:space:]]*(#.*)?$ ]] || return 1
    dotenv_value=$_de_out
}

# Load ./.env from the launch directory. Plain NAME=VALUE text, never sourced or evaluated;
# errors name the line number, never its content. Script settings (PEPI_EVAL_* except
# dotenv_shell_only, LITELLM_BASE_URL, LITELLM_API_KEY) override the shell here. Every other
# name is only a container candidate: kept out of this script's environment (and so out of
# git, pip, docker build, ... on the host) and exported for the run step after consent.
load_dotenv() {
    local _de_file _de_line _de_line_no=0 _de_name _de_decl _de_attrs _de_size _de_kind
    local -a _de_skipped=() _de_shell_only=()
    shell_litellm_base_url=${LITELLM_BASE_URL-}
    [[ -n "$launch_dir" ]] || return 0
    _de_file="$launch_dir/.env"
    [[ -e "$_de_file" || -L "$_de_file" ]] || return 0
    [[ ! -L "$_de_file" ]] || die "Refusing $_de_file: it is a symlink; use a regular file" 2
    [[ -f "$_de_file" ]] || die "Refusing $_de_file: not a regular file" 2
    [[ -O "$_de_file" ]] || die "Refusing $_de_file: not owned by the current user" 2
    if [[ -n "$(find "$_de_file" -maxdepth 0 \( -perm -020 -o -perm -002 \) -print 2>/dev/null)" ]]; then
        die "Refusing $_de_file: writable by group/others; run: chmod 600 '$_de_file'" 2
    fi
    if [[ -n "$(find "$_de_file" -maxdepth 0 \( -perm -040 -o -perm -004 \) -print 2>/dev/null)" ]]; then
        warn "$_de_file is readable by group/others; consider: chmod 600 '$_de_file'"
    fi
    _de_size=$(wc -c < "$_de_file" 2>/dev/null) || die "Cannot read $_de_file" 2
    (( _de_size <= 1048576 )) || die "Refusing $_de_file: larger than 1 MiB" 2
    dotenv_file=$_de_file
    # NUL bytes become CR so both are rejected with their line number (byte-wise, any encoding).
    while IFS= read -r _de_line || [[ -n "$_de_line" ]]; do
        _de_line_no=$((_de_line_no + 1))
        _de_line="${_de_line#"${_de_line%%[![:space:]]*}"}"
        [[ -z "$_de_line" || "$_de_line" == \#* ]] && continue
        [[ "$_de_line" != *$'\r'* ]] ||
            die "$_de_file line $_de_line_no: CR or NUL character (use Unix line endings)" 2
        if [[ "$_de_line" =~ ^export[[:space:]]+ ]]; then
            _de_line=${_de_line#export}
            _de_line="${_de_line#"${_de_line%%[![:space:]]*}"}"
        fi
        [[ "$_de_line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] ||
            die "$_de_file line $_de_line_no: expected NAME=VALUE" 2
        _de_name=${_de_line%%=*}
        dotenv_parse_value "${_de_line#*=}" ||
            die "$_de_file line $_de_line_no: unterminated quote or text after the closing quote" 2
        if dotenv_reserved "$_de_name"; then
            warn "Ignoring reserved $_de_name from $_de_file (not loaded, not forwarded)"
            continue
        fi
        if dotenv_shell_only "$_de_name"; then
            _de_shell_only+=("$_de_name")
            continue
        fi
        case "$_de_name" in
            PEPI_EVAL_*|LITELLM_BASE_URL|LITELLM_API_KEY) _de_kind=setting ;;
            LITELLM_*)
                warn "Ignoring $_de_name from $_de_file (only LITELLM_BASE_URL and LITELLM_API_KEY are used; LITELLM_* is never forwarded)"
                continue ;;
            *) _de_kind=forward ;;
        esac
        # Existing non-exported variables are this shell's or the script's own state; _de_*
        # names are reserved for the helpers that export the container candidates.
        _de_decl=$(declare -p "$_de_name" 2>/dev/null)
        _de_attrs=${_de_decl#declare -}
        _de_attrs=${_de_attrs%% *}
        if [[ "$_de_name" == _de_* || ( -n "$_de_decl" && "$_de_attrs" != *x* ) ]]; then
            _de_skipped+=("$_de_name")
            continue
        fi
        if [[ $_de_kind == setting ]]; then
            printf -v "$_de_name" '%s' "$dotenv_value" || die "$_de_file line $_de_line_no: cannot set $_de_name" 2
            export "$_de_name"
            dotenv_has "$_de_name" || dotenv_settings+=("$_de_name")
        else
            dotenv_forward_set "$_de_name" "$dotenv_value"
        fi
        dotenv_has "$_de_name" || dotenv_names+=("$_de_name")
    done < <(LC_ALL=C tr '\000' '\r' < "$_de_file")
    (( ${#_de_shell_only[@]} == 0 )) ||
        warn "Ignoring shell-only setting(s) from $_de_file: ${_de_shell_only[*]} (consent, venv, SWE-bench source, secrets dir and network count only from the shell or flags)"
    (( ${#_de_skipped[@]} == 0 )) ||
        warn "Ignoring shell/script variable name(s) from $_de_file: ${_de_skipped[*]}"
    printf 'pepi-eval: loaded %d variable(s) from %s: script settings: %s; container candidates (forwarded by run after consent, never set on the host): %s\n' \
        "${#dotenv_names[@]}" "$_de_file" "${dotenv_settings[*]:-(none)}" "${dotenv_forward[*]:-(none)}" >&2
}

host_of_url() {
    local host=${1#*://}
    host=${host%%/*}
    printf '%s\n' "${host##*@}"
}

# A .env gateway URL that differs from the shell's would receive your LiteLLM key, whether it comes
# from the shell, the prompt or a LiteLLM api_key in auth.json (secrets dir or host-auth copy:
# the runner then takes the URL from LITELLM_BASE_URL). The shell's own URL is not a redirect.
dotenv_gateway_guard() {
    dotenv_has LITELLM_BASE_URL || return 0
    [[ "$LITELLM_BASE_URL" != "$shell_litellm_base_url" ]] || return 0
    # Even with a .env key, an auth.json LiteLLM key (secrets dir or host-auth copy) wins and
    # would still go to this gateway, so the .env key does not make the redirect safe.
    warn "⚠ $dotenv_file sets LITELLM_BASE_URL (host $(host_of_url "$LITELLM_BASE_URL")), different from your shell's: your LiteLLM key (shell, .env or auth.json) would be sent to that gateway"
    if [[ "${DRY_RUN:-}" == "1" ]]; then
        printf 'pepi-eval: dry-run: a real run asks before sending your LiteLLM key to the .env gateway\n' >&2
        return 0
    fi
    if [[ -n "${PEPI_EVAL_USE_DOTENV+x}" ]]; then
        case "$PEPI_EVAL_USE_DOTENV" in
            1) return 0 ;;
            0) die "Not sending your LiteLLM key to the .env gateway (PEPI_EVAL_USE_DOTENV=0); put LITELLM_API_KEY in .env too or remove LITELLM_BASE_URL" 2 ;;
            *) die "PEPI_EVAL_USE_DOTENV must be 0 or 1" 2 ;;
        esac
    fi
    [[ -t 0 && -t 2 ]] ||
        die "Refusing to send your LiteLLM key (shell or auth.json) to the .env gateway non-interactively; set PEPI_EVAL_USE_DOTENV=1 in the shell to allow it" 2
    local reply
    printf '%s' 'Send your LiteLLM key (shell or auth.json) to this gateway? [y/N]: ' >&2
    read -r reply || reply=""
    [[ "$reply" =~ ^([Yy]|[Yy][Ee][Ss])$ ]] || die "LiteLLM key not sent to the .env gateway" 2
}

# Decide whether the run step forwards the .env names (sets dotenv_forward_approved).
# -y/PEPI_EVAL_YES never consents; PEPI_EVAL_USE_DOTENV=1 from the shell does.
confirm_dotenv_forwarding() {
    dotenv_forward_approved=0
    (( ${#dotenv_forward[@]} )) || return 0
    local names="${dotenv_forward[*]}" reply
    if [[ -n "${PEPI_EVAL_USE_DOTENV+x}" ]]; then
        case "$PEPI_EVAL_USE_DOTENV" in
            1) dotenv_forward_approved=1 ;;
            0) warn ".env variables not forwarded (PEPI_EVAL_USE_DOTENV=0): $names" ;;
            *) die "PEPI_EVAL_USE_DOTENV must be 0 or 1" 2 ;;
        esac
        return 0
    fi
    if [[ "${DRY_RUN:-}" == "1" ]]; then
        printf 'pepi-eval: dry-run: a real run asks before forwarding .env variables: %s\n' "$names" >&2
        dotenv_forward_approved=1
        return 0
    fi
    if [[ -t 0 && -t 2 ]]; then
        printf 'pepi-eval: .env variables for the eval container (names only): %s\n' "$names" >&2
        warn "The agent and its MCP servers can read every forwarded value."
        printf '%s' 'Forward these .env variables into the eval container? [y/N]: ' >&2
        read -r reply || reply=""
        if [[ "$reply" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
            dotenv_forward_approved=1
        else
            warn "Not forwarding .env variables"
        fi
    else
        warn ".env variables not forwarded without consent: $names (set PEPI_EVAL_USE_DOTENV=1 in the shell to forward them)"
    fi
}

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

# Runtime sanity probe: os.pipe() and a subprocess must work. A pyenv Python built with a newer
# macOS SDK than the running OS detects pipe2, which is NULL at runtime, and segfaults here.
# Sets runtime_error to the exit status and last output line (bash's own "Segmentation fault"
# report is captured too); returns 1 on failure.
python_runtime_ok() {
    local output status
    runtime_error=""
    output=$( { "$1" -c "import os, subprocess; os.pipe(); subprocess.run(['true'], check=True)"
                printf 'pepi-eval-status=%s\n' "$?"; } 2>&1 )
    status=${output##*pepi-eval-status=}
    status=${status%%[!0-9]*}
    [[ "$status" == "0" ]] && return 0
    output=${output%pepi-eval-status=*}
    output=$(printf '%s\n' "$output" | sed '/^[[:space:]]*$/d' | tail -n 1)
    runtime_error="exit status ${status:-unknown}${output:+: $output}"
    return 1
}

# First failing venv check. Sets venv_issue (missing, no_python, no_swebench, no_lzma,
# broken_runtime or no_harness) and venv_issue_message (the error when nothing is fixed);
# returns 1 on failure.
# Preflight checks what grading imports: fail before a paid run, not at grading time.
check_venv() {
    local venv=$1 preflight=$2 import_error py_version
    venv_issue="" venv_issue_message=""
    if [[ ! -d "$venv" ]]; then
        venv_issue=missing
        venv_issue_message="Venv not found at $venv. Set up with: python3 -m venv $venv && source $venv/bin/activate && pip install -e ~/SWE-bench"
    elif [[ ! -f "$venv/bin/python" ]]; then
        venv_issue=no_python
        venv_issue_message="Venv missing bin/python at $venv"
    elif ! "$venv/bin/python" -c "import swebench.task.repo" 2>/dev/null; then
        venv_issue=no_swebench
        venv_issue_message="swebench.task.repo not importable from $venv. Install with: pip install -e ~/SWE-bench"
    elif [[ "$preflight" == "1" ]] && ! "$venv/bin/python" -c "import lzma" 2>/dev/null; then
        py_version=$("$venv/bin/python" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null) || py_version=unknown
        venv_issue=no_lzma
        venv_issue_message="The Python in $venv (version $py_version) lacks lzma (built without xz headers); the grader needs it via 'datasets'. Fix: brew install xz, then CFLAGS=\"-I\$(brew --prefix xz)/include\" LDFLAGS=\"-L\$(brew --prefix xz)/lib\" ac_cv_func_pipe2=no pyenv install --force $py_version (ac_cv_func_pipe2=no is for macOS), then recreate the venv and reinstall SWE-bench (pip install -e ~/SWE-bench)"
    elif [[ "$preflight" == "1" ]] && ! python_runtime_ok "$venv/bin/python"; then
        py_version=$("$venv/bin/python" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null) || py_version=unknown
        venv_issue=broken_runtime
        venv_issue_message="The Python in $venv (version $py_version) crashes on os.pipe()/subprocess ($runtime_error); often a pyenv build made with a newer macOS SDK than the running OS, where pipe2 is missing at runtime. Fix: rebuild it with ac_cv_func_pipe2=no pyenv install --force $py_version, then recreate the venv and reinstall SWE-bench (pip install -e ~/SWE-bench)"
    elif [[ "$preflight" == "1" ]] && ! import_error=$("$venv/bin/python" -c "import swebench.harness.run_evaluation" 2>&1); then
        venv_issue=no_harness
        venv_issue_message="swebench.harness.run_evaluation not importable from $venv: $(printf '%s\n' "$import_error" | tail -n 1). Reinstall with: pip install -e ~/SWE-bench"
    fi
    [[ -z "$venv_issue" ]]
}

# Physical path of an existing directory, or empty.
canonical_dir() { (cd -P -- "$1" 2>/dev/null && pwd -P); }

# Plan rebuilding a pyenv-managed Python with Homebrew's xz headers (and, on macOS,
# ac_cv_func_pipe2=no so a build with a newer SDK than the running OS does not use a missing
# pipe2). Appends brew_xz and pyenv_python steps and sets fix_interpreter to the rebuilt
# Python; returns 1 with fix_blocker.
plan_python_rebuild() {
    local interpreter=$1 installed base_prefix pyenv_prefix xz_prefix
    fix_py_version=$("$interpreter" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null) || fix_py_version=""
    if [[ ! "$fix_py_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        fix_blocker="cannot read a release version from $interpreter (got '$fix_py_version')"
        return 1
    fi
    if ! has_cmd brew; then
        fix_blocker="Homebrew not found: install xz (liblzma headers) with your OS package manager, rebuild Python $fix_py_version, then recreate the venv"
        return 1
    fi
    if ! has_cmd pyenv; then
        fix_blocker="pyenv not found: rebuild Python $fix_py_version with xz headers, then recreate the venv"
        return 1
    fi
    installed=$(pyenv versions --bare 2>/dev/null) || installed=""
    if ! grep -Fxq -- "$fix_py_version" <<<"$installed"; then
        fix_blocker="Python $fix_py_version is not a pyenv-installed version: rebuild it with xz headers, then recreate the venv"
        return 1
    fi
    pyenv_prefix=$(pyenv prefix "$fix_py_version" 2>/dev/null) || pyenv_prefix=""
    base_prefix=$("$interpreter" -c 'import sys;print(sys.base_prefix)' 2>/dev/null) || base_prefix=""
    pyenv_prefix=$(canonical_dir "$pyenv_prefix")
    if [[ -z "$pyenv_prefix" || "$(canonical_dir "$base_prefix")" != "$pyenv_prefix" ]]; then
        fix_blocker="$interpreter is based on '${base_prefix}', not on pyenv's $fix_py_version: rebuild that Python with xz headers, then recreate the venv"
        return 1
    fi
    xz_prefix=$(brew --prefix xz 2>/dev/null) || xz_prefix=""
    if [[ -z "$xz_prefix" ]]; then
        fix_blocker="cannot resolve the Homebrew xz prefix (brew --prefix xz)"
        return 1
    fi
    fix_cflags="-I$xz_prefix/include${CFLAGS:+ $CFLAGS}"
    fix_ldflags="-L$xz_prefix/lib${LDFLAGS:+ $LDFLAGS}"
    [[ "$(uname -s)" == "Darwin" ]] && fix_pipe2=no
    brew list xz >/dev/null 2>&1 || fix_steps+=(brew_xz)
    fix_steps+=(pyenv_python)
    fix_interpreter="$pyenv_prefix/bin/python3"
}

# Plan moving an existing venv aside (never deleted). Only a directory with pyvenv.cfg that is
# not / or $HOME, and is under $HOME or is exactly PEPI_EVAL_VENV, may move.
plan_venv_backup() {
    local venv=$1 canonical home_canonical env_canonical=""
    canonical=$(canonical_dir "$venv")
    home_canonical=$(canonical_dir "$HOME")
    [[ -n "${PEPI_EVAL_VENV:-}" ]] && env_canonical=$(canonical_dir "$PEPI_EVAL_VENV")
    if [[ -z "$canonical" || "$canonical" == "/" || "$canonical" == "$home_canonical" ]]; then
        fix_blocker="refusing to move $venv aside: it is not a directory, or it is / or \$HOME"
        return 1
    fi
    if [[ ( -z "$home_canonical" || "$canonical" != "$home_canonical"/* ) && "$canonical" != "$env_canonical" ]]; then
        fix_blocker="refusing to move $venv aside: it is outside \$HOME and not PEPI_EVAL_VENV"
        return 1
    fi
    if [[ ! -f "$venv/pyvenv.cfg" ]]; then
        fix_blocker="refusing to move $venv aside: no pyvenv.cfg, so it may not be a venv"
        return 1
    fi
    fix_backup="$venv.bak-$(date +%Y%m%d-%H%M%S)"
    if [[ -e "$fix_backup" || -L "$fix_backup" ]]; then
        fix_blocker="backup path already exists: $fix_backup"
        return 1
    fi
    fix_steps+=(backup_venv)
}

# Plan the ordered repair for venv_issue into fix_steps (run by run_fix_step) and its inputs;
# returns 1 with fix_blocker when no safe automatic fix exists.
plan_venv_fix() {
    local venv=$1 preflight=$2 interpreter="" rebuild_python=0
    fix_steps=() fix_blocker="" fix_venv=$venv fix_backup="" fix_interpreter=""
    fix_py_version="" fix_cflags="" fix_ldflags="" fix_pipe2=""
    fix_swebench_repo="${PEPI_EVAL_SWEBENCH_REPO:-$HOME/SWE-bench}"
    if [[ ! -d "$fix_swebench_repo" ]]; then
        fix_blocker="SWE-bench source not found at $fix_swebench_repo (clone it or set PEPI_EVAL_SWEBENCH_REPO)"
        return 1
    fi
    case "$venv_issue" in
        no_swebench|no_harness)
            fix_steps=(pip_swebench)
            return 0 ;;
        no_lzma|broken_runtime)
            interpreter="$venv/bin/python"
            rebuild_python=1 ;;
        missing|no_python)
            if ! interpreter=$(command -v python3); then
                fix_blocker="python3 not found to create the venv"
                return 1
            fi
            if [[ "$preflight" == "1" ]] && { ! "$interpreter" -c "import lzma" 2>/dev/null \
                || ! python_runtime_ok "$interpreter"; }; then
                rebuild_python=1
            fi ;;
        *)
            fix_blocker="unknown venv problem: $venv_issue"
            return 1 ;;
    esac
    if [[ "$rebuild_python" == "1" ]]; then
        plan_python_rebuild "$interpreter" || return 1
    else
        fix_interpreter=$interpreter
    fi
    if [[ -e "$venv" || -L "$venv" ]]; then
        plan_venv_backup "$venv" || return 1
    fi
    fix_steps+=(create_venv pip_swebench)
}

fix_step_display() {
    case "$1" in
        brew_xz) printf 'brew install xz' ;;
        pyenv_python) printf 'CFLAGS=%q LDFLAGS=%q %spyenv install --force %q' "$fix_cflags" "$fix_ldflags" \
            "${fix_pipe2:+ac_cv_func_pipe2=$fix_pipe2 }" "$fix_py_version" ;;
        backup_venv) printf 'mv -- %q %q' "$fix_venv" "$fix_backup" ;;
        create_venv) printf '%q -m venv %q' "$fix_interpreter" "$fix_venv" ;;
        pip_swebench) printf '%q -m pip install -e %q' "$fix_venv/bin/python" "$fix_swebench_repo" ;;
    esac
}

run_fix_step() {
    case "$1" in
        brew_xz) brew install xz ;;
        pyenv_python) env CFLAGS="$fix_cflags" LDFLAGS="$fix_ldflags" ${fix_pipe2:+ac_cv_func_pipe2=$fix_pipe2} \
            pyenv install --force "$fix_py_version" ;;
        backup_venv) [[ ! -e "$fix_backup" && ! -L "$fix_backup" ]] && mv -- "$fix_venv" "$fix_backup" ;;
        create_venv) "$fix_interpreter" -m venv "$fix_venv" ;;
        pip_swebench) "$fix_venv/bin/python" -m pip install -e "$fix_swebench_repo" ;;
        *) return 1 ;;
    esac
}

# Installing software is separate consent: -y/PEPI_EVAL_YES never implies it.
confirm_venv_fix() {
    case "${PEPI_EVAL_AUTO_FIX:-}" in
        1) return 0 ;;
        0) return 1 ;;
    esac
    [[ -t 0 && -t 2 ]] || return 1
    local reply
    printf '%s' 'Run these fix commands now? [y/N]: ' >&2
    read -r reply || reply=""
    [[ "$reply" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]
}

# Show the fix for venv_issue, run it once after consent, then re-verify once.
repair_venv() {
    local venv=$1 preflight=$2 message=$venv_issue_message step index=0
    if ! plan_venv_fix "$venv" "$preflight"; then
        warn "No automatic fix offered: $fix_blocker"
        die "$message" 2
    fi
    printf 'pepi-eval: problem: %s\n' "$message" >&2
    printf 'pepi-eval: proposed fix:\n' >&2
    for step in "${fix_steps[@]}"; do
        index=$((index + 1))
        printf '  %d. %s\n' "$index" "$(fix_step_display "$step")" >&2
    done
    [[ -n "$fix_backup" ]] && printf '  (moves the current venv to %s; the venv itself is never deleted)\n' "$fix_backup" >&2
    if [[ " ${fix_steps[*]} " == *" pyenv_python "* ]]; then
        printf '  WARNING: pyenv --force rebuilds Python %s in place, so every venv using it is affected,\n' "$fix_py_version" >&2
        printf '           and a failed or interrupted build can leave that Python broken.\n' >&2
    fi
    if ! confirm_venv_fix; then
        die "Venv not fixed: run the commands above, or confirm on a terminal (PEPI_EVAL_AUTO_FIX=1 runs them non-interactively)" 2
    fi
    index=0
    for step in "${fix_steps[@]}"; do
        index=$((index + 1))
        printf 'pepi-eval: + %s\n' "$(fix_step_display "$step")" >&2
        if ! run_fix_step "$step"; then
            if [[ -n "$fix_backup" && "$step" != "backup_venv" && -e "$fix_backup" ]]; then
                warn "The previous venv is kept at $fix_backup"
            fi
            die "Fix step $index failed: $(fix_step_display "$step")" 3
        fi
    done
    check_venv "$venv" "$preflight" || die "Fix did not resolve the venv: $venv_issue_message" 2
    printf 'pepi-eval: ✓ venv fixed\n' >&2
}

# Set PEPI_PYTHON and validate venv, proposing a fix when a check fails.
# `setup_venv --no-preflight` (compare) and dry-run skip the grader-import preflight.
setup_venv() {
    local venv="${PEPI_EVAL_VENV:-$HOME/.venvs/swebench}" preflight=1
    while [[ "$venv" == */ && "$venv" != "/" ]]; do venv=${venv%/}; done
    [[ "${DRY_RUN:-}" == "1" || "${1:-}" == "--no-preflight" ]] && preflight=0
    if [[ -n "${PEPI_EVAL_AUTO_FIX+x}" && "${PEPI_EVAL_AUTO_FIX}" != "0" && "${PEPI_EVAL_AUTO_FIX}" != "1" ]]; then
        die "PEPI_EVAL_AUTO_FIX must be 0 or 1" 2
    fi
    if ! check_venv "$venv" "$preflight"; then
        # Dry-run never plans, prompts or installs.
        [[ "${DRY_RUN:-}" == "1" ]] && die "$venv_issue_message" 2
        repair_venv "$venv" "$preflight"
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

    confirm_dotenv_forwarding
    
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
    # Names only: Docker reads each value from the runner's environment, never from argv.
    if [[ $dotenv_forward_approved == 1 ]]; then
        local forward_name
        for forward_name in "${dotenv_forward[@]}"; do
            run_cmd+=(--pass-env "$forward_name")
        done
    fi
    
    # The approved .env candidates are exported only inside the subshell that execs the
    # runner (directly or via the host-auth adapter, whose child inherits them), so no other
    # step or host tool sees them. The command is fixed in "$@" before any export.
    if [[ "$use_host_auth" == "1" && "${DRY_RUN:-}" != "1" ]]; then
        # Adapter owns the private temp directory and cleanup; tokens never enter argv/env.
        local host_auth_pid="" host_auth_status=0 host_python
        host_python=$(command -v python3) || die "python3 not found" 3
        # Bash otherwise defers traps while waiting for a foreground child. Forward and
        # reap the adapter so it can stop the runner and remove its credentials first.
        trap 'kill -TERM "${host_auth_pid:-$!}" 2>/dev/null; wait "${host_auth_pid:-$!}" 2>/dev/null; exit 130' INT TERM
        # `( ... exec ) &` keeps $! the adapter's own PID for the trap above.
        ( set -- "$host_python" -m evals.swebench.host_auth "$run_dir" -- "${run_cmd[@]}"
          dotenv_export_forwarded || exit 2
          exec "$@" ) & host_auth_pid=$!
        wait "$host_auth_pid" || host_auth_status=$?
        trap - INT TERM
        [[ $host_auth_status -eq 0 ]] || fail_step run "$host_auth_status"
    elif [[ "${DRY_RUN:-}" == "1" ]]; then
        run_cmd "${run_cmd[@]}" || fail_step run $?
    else
        ( set -- "${run_cmd[@]}"
          dotenv_export_forwarded || exit 2
          exec "$@" ) || fail_step run $?
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
    
    setup_venv --no-preflight
    
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
  AUTO_FIX              1 runs proposed venv fix commands without asking; 0 never runs them;
                        unset asks on a terminal (default No). -y/YES never consents.
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
  USE_DOTENV            1 forwards the ./.env container candidates without asking and allows a
                        .env LiteLLM gateway; 0 refuses both; unset asks on a terminal
                        (default No). -y never consents. Shell only, never from .env.

./.env (read from the launch directory, never the checkout; trust it like your shell profile):
  Plain NAME=VALUE lines, never sourced: `export ` prefix, # comments, 'literal',
  "double" (only \" \\ \n), no $VAR/$(...) expansion. Must be your own non-symlink file,
  not group/world-writable. Prints its path and the loaded NAMES, never values.
  Script settings: PEPI_EVAL_* (except the shell-only ones below), LITELLM_BASE_URL and
  LITELLM_API_KEY override the shell for this script (flags > .env > shell).
  Shell-only (ignored from .env with a warning): YES USE_HOST_AUTH AUTO_FIX USE_DOTENV
  (consent) and VENV SWEBENCH_REPO SECRETS_DIR NETWORK (host code, credentials, egress).
  Ignored: PATH HOME USER SHELL PWD OLDPWD TMPDIR IFS ENV BASH_ENV SHELLOPTS BASHOPTS PS4
  PROMPT_COMMAND CDPATH GLOBIGNORE POSIXLY_CORRECT NODE_OPTIONS NODE_PATH LD_* DYLD_*
  BASH_FUNC_* DOCKER_* PYTHON*, other LITELLM_*, and the script's own variables.
  Every other name is a container candidate: never set for this script or other host steps,
  only exported for `run` after consent and forwarded by name (run --pass-env NAME; values
  via the environment, never argv). The agent and its MCP servers can read them; values
  under 8 bytes are not redacted from artifacts.
  A .env LITELLM_BASE_URL that differs from your shell's would receive your LiteLLM key (shell, .env or
  auth.json): it needs a terminal y or USE_DOTENV=1, which also approves forwarding;
  USE_DOTENV=0 refuses both (the run then exits 2).

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
    local command="all" flag_yes=0 flag_dry_run=0
    
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -y|--yes) flag_yes=1; shift ;;
            --dry-run) flag_dry_run=1; shift ;;
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

    # Precedence: flags > ./.env > shell environment.
    load_dotenv
    [[ $flag_yes == 1 ]] && PEPI_EVAL_YES=1
    [[ $flag_dry_run == 1 ]] && DRY_RUN=1
    runs_dir="${PEPI_EVAL_RUNS_DIR:-$eval_dir/runs}"
    case "$command" in
        all|run) dotenv_gateway_guard ;;
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

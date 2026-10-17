# Pepi evaluations

The SWE-bench integration uses a separate frozen isolated profile. It never
changes your live Pi settings. Run commands from the Pepi repository root.

## Quick start

Use the interactive CLI for the full workflow:

```sh
evals/pepi-eval.sh
```

This prompts for required inputs or reads them from env vars. Defaults are the
[frozen custom Verified20 v1 subset](swebench/presets/README.md) and 300 seconds per
Pi invocation (not end-to-end wall time). This is **not an official Verified score**.
`PEPI_EVAL_IDS=django__django-11095` gives a one-task smoke; explicit IDs and timeout
overrides remain supported. Keep the frozen task-source revision for the preset.

**Secrets** (only when the frozen plan needs LiteLLM; never echoed):
- `LITELLM_BASE_URL`: LiteLLM gateway URL
- `LITELLM_API_KEY`: Dedicated, budget-capped API key (keep this safe)

**Optional `PEPI_EVAL_*` env vars** override defaults (see `evals/pepi-eval.sh --help` for full list).
Each prompt prints the variable to export to skip it next time. Without a terminal, a missing value exits 2.

**Named profiles:** select `PEPI_EVAL_PROFILE=bedrock` (LiteLLM) or `codex`
(builtin OAuth), or `python -m evals.swebench prepare --profile NAME ...`.
Preparation never switches your host profile or reads its secrets. Codex needs pinned Pi
1.1.0 + subagents, **no LiteLLM pin/install/credentials/prompt**. Supply a dedicated
Pi-schema OAuth `auth.json` at runtime. Rotation stays in tmpfs/in-memory and is
redacted; host auth is untouched, but its original refresh token can be invalidated.
See [profiles/auth policy](swebench/README.md#named-profiles-and-provider-authentication).

**Benchmarking another configuration** (details in [swebench/README.md](swebench/README.md#ci-injected-configuration-and-secrets)):
- `PEPI_EVAL_CONFIG_DIR`: directory with `settings.json`, optional safe `models.json`, `AGENTS.md`, `workflows/`,
  `extensions/subagent/config.json`, optionally `mcp-packages.json` + `mcp-package-lock.json`;
  files replace whole documents: injected > named profile > checkout. Named profiles
  without models.json never inherit stale checkout models.json. It must not contain
  `auth.json`, `mcp.json` or `.env` (the script and `prepare` both refuse them).
- `PEPI_EVAL_CONFIG_LABEL`: name recorded in the manifest and predictions.
- Native Pi MCP is enabled by default with Pi >=1.1.0. `PEPI_EVAL_DROP_MCP=1` explicitly disables it.
- `PEPI_EVAL_SECRETS_DIR`: chmod-700 directory outside the checkout with `chmod 600` `auth.json` and/or
  native `mcp.json`, injected at run time. Local servers must be lockfile-provisioned at build time;
  remote endpoints need explicit external egress policy. See [MCP setup](swebench/README.md#native-mcp).
  Builtins use validated auth.json; LiteLLM can use dedicated env credentials or saved Pi auth.
  Explicit child providers also require auth; unsupported custom dependencies fail before launch.

Without `PEPI_EVAL_SECRETS_DIR`, `run` offers on a terminal (default **No**) to
use `~/.pi/agent/auth.json`. This needs **separate explicit consent**, even with
`-y`/`PEPI_EVAL_YES=1`. For noninteractive opt-in use `PEPI_EVAL_USE_HOST_AUTH=1`;
`0` disables the proposal; other values are rejected. An explicit secrets directory
always wins, with no host-auth proposal or copy. Declining still permits LiteLLM env credentials.

Only credentials required by the hash-verified frozen provider plan are validated
and copied into a private temporary directory outside the checkout (700 directory,
600 auth file); unrelated providers and `mcp.json` are never copied. The source must
be a private readable regular non-symlink file owned by you. Runtime injection remains
stdin/tmpfs, with cleanup after success, failure or interruption. No changes are
written back to host auth. **OAuth refresh may nevertheless invalidate your shared
host login**; dedicated eval credentials are safer. Dry-run only describes the
opted-in action: it never reads/copies host auth or creates a credential directory.

The provider/account usage `run` step separately asks for confirmation; non-interactively pass `-y` (or `PEPI_EVAL_YES=1`).
Exit codes match `python -m evals.swebench`: 0 ok, 2 usage/configuration, 3 step failure, 130 interrupted.

**Venv repair:** if the SWE-bench venv is missing, cannot import `swebench`, or its Python lacks
`lzma` (built without xz headers) or crashes on `os.pipe()`/subprocess, the script prints the numbered fix commands and asks
`Run these fix commands now? [y/N]` on a terminal. For a pyenv Python this is `brew install xz`
(if missing), `pyenv install --force <version>` with xz `CFLAGS`/`LDFLAGS`, moving the old venv to
`<venv>.bak-<time>` (never deleted), recreating it and `pip install -e` SWE-bench, then one re-check.
On macOS the rebuild also sets `ac_cv_func_pipe2=no`, which repairs a pyenv Python that segfaults on
`os.pipe()`/subprocess because it was built with a newer SDK than the running OS (`pipe2` missing at runtime).
`-y` does not consent; `PEPI_EVAL_AUTO_FIX=1` runs them non-interactively and `0` never does.
Without pyenv/Homebrew, or for a directory without `pyvenv.cfg`, only manual guidance is printed.
Dry-run never plans or runs a fix.

**`./.env`:** the script reads `.env` from the directory you launch it in (never the checkout).
Trust it as much as your shell profile: launching from a directory with someone else's `.env`
(for example a cloned repo) lets it choose tasks, profile and config. It is plain `NAME=VALUE`
text, never sourced: optional `export `, `#` comments, `'literal'`, `"double"` (only `\"`, `\\`,
`\n`), no `$VAR`/`$(...)` expansion. A bad line exits 2 naming only its line number. The file must
be a regular file you own, not a symlink and not group/world-writable (`chmod 600 .env`). The path
and the loaded names (never values) are printed. Names fall into four groups:

- **Script settings** override your shell (flags override both): `PEPI_EVAL_*` and
  `LITELLM_BASE_URL`/`LITELLM_API_KEY`. They are never forwarded as extras.
- **Shell-only** settings are ignored from `.env` with a warning and count only from the shell or
  flags: the consent settings `PEPI_EVAL_YES`, `PEPI_EVAL_USE_HOST_AUTH`, `PEPI_EVAL_AUTO_FIX`,
  `PEPI_EVAL_USE_DOTENV`, and `PEPI_EVAL_VENV`, `PEPI_EVAL_SWEBENCH_REPO` (host code: the venv Python,
  `pip install -e`), `PEPI_EVAL_SECRETS_DIR` (would skip the host-auth prompt) and
  `PEPI_EVAL_NETWORK` (egress containment).
- **Ignored:** `PATH`, `HOME`, `USER`, `SHELL`, `PWD`, `OLDPWD`, `TMPDIR`, `IFS`, `ENV`, `BASH_ENV`,
  `SHELLOPTS`, `BASHOPTS`, `PS4`, `PROMPT_COMMAND`, `CDPATH`, `GLOBIGNORE`, `POSIXLY_CORRECT`,
  `NODE_OPTIONS`, `NODE_PATH`, `LD_*`, `DYLD_*`, `BASH_FUNC_*`, `DOCKER_*`, `PYTHON*`, other
  `LITELLM_*`, and the script's own variables.
- **Container candidates:** every other name. They are never set in the script's environment,
  so host steps (`git`, `pip`, `docker build`, grading) never see them, not even `GIT_*`, `PIP_*`
  or proxy settings. Only the `run` step exports them, for the runner alone, after consent.

The container gets **only** the frozen LiteLLM credentials (as before) plus the container
candidates, and only after consent: `run` lists the names and asks
`Forward these .env variables into the eval container? [y/N]` on a terminal. `-y` does not
consent; `PEPI_EVAL_USE_DOTENV=1` in the shell does, `0` never forwards. Without a terminal
nothing is forwarded (one warning says how to opt in). The agent and its MCP servers can read
every forwarded value. Values travel by environment (`--pass-env NAME`, `docker exec --env NAME`),
never argv; values of 8+ bytes (and their JSON-escaped forms and lines) are redacted from
attempts and patches, while shorter values are forwarded with a warning and **not** redacted.
`execution.json` records `forwarded_env_names`. The Python runner validates the names again and
is authoritative.

If `.env` sets `LITELLM_BASE_URL` different from your shell's (even with its own `LITELLM_API_KEY`,
since an `auth.json` key wins), your LiteLLM key, whether from the shell, `.env`, the prompt or a
LiteLLM `api_key` in `auth.json`, would go to that gateway: the script warns and needs a terminal `y` or `PEPI_EVAL_USE_DOTENV=1`, otherwise it
exits 2 before any step. `PEPI_EVAL_USE_DOTENV=1` approves **both** this gateway and the
forwarding; `0` refuses both, so a run with such a `.env` then exits 2. See
[.env forwarding](swebench/README.md#env-forwarding).

**Dry-run mode** (no side effects, prints shell-quoted commands):
```sh
PEPI_EVAL_PROFILE=codex evals/pepi-eval.sh --dry-run -y all
```

## Documentation

- [Prepare, build and run](swebench/README.md): task containers and predictions.
- [Telemetry](swebench/TELEMETRY.md): time, turns, parent/child tokens and cost.
- [Grade and compare](swebench/REPORTING.md): official results plus JSON/CSV/Markdown reports.

Each configuration change needs fresh generation, not just regrading old patches.
Use a dedicated gateway key, externally restricted networking and fixed budgets.
Do not commit run directories, credentials, benchmark answer patches or transcripts.

Offline checks:

```sh
python -m unittest discover -s evals/swebench/tests -v
node --test evals/swebench/tests/telemetry-extension.test.mjs
```

The stack is intentionally ready for review, not a claim of a measured Pepi score:
a one-task Docker/gateway smoke run is still required on your target setup.

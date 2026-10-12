# Pepi evaluations

The SWE-bench integration uses a separate frozen isolated profile. It never
changes your live Pi settings. Run commands from the Pepi repository root.

## Quick start

Use the interactive CLI for the full workflow:

```sh
evals/pepi-eval.sh
```

This prompts for required inputs or reads them from env vars.

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

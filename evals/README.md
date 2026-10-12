# Pepi evaluations

The SWE-bench integration uses a separate frozen local-only profile. It never
changes your live Pi settings. Run commands from the Pepi repository root.

## Quick start

Use the interactive CLI for the full workflow:

```sh
evals/pepi-eval.sh
```

This prompts for required inputs or reads them from env vars.

**Secrets** (required for `run` step; never echoed):
- `LITELLM_BASE_URL`: LiteLLM gateway URL
- `LITELLM_API_KEY`: Dedicated, budget-capped API key (keep this safe)

**Optional `PEPI_EVAL_*` env vars** override defaults (see `evals/pepi-eval.sh --help` for full list).
Each prompt prints the variable to export to skip it next time. Without a terminal, a missing value exits 2.

**Benchmarking another configuration** (details in [swebench/README.md](swebench/README.md#ci-injected-configuration-and-secrets)):
- `PEPI_EVAL_CONFIG_DIR`: directory with `settings.json`, `AGENTS.md`, `workflows/`,
  `extensions/subagent/config.json`; missing files fall back to this checkout. It must not contain
  `auth.json`, `mcp.json` or `.env` (the script and `prepare` both refuse them).
- `PEPI_EVAL_CONFIG_LABEL`: name recorded in the manifest and predictions.
- `PEPI_EVAL_DROP_MCP=1`: required when the config declares MCP (for example `profiles/bedrock/settings.json`).
- `PEPI_EVAL_SECRETS_DIR`: directory outside the checkout with a `chmod 600` `auth.json`, injected at run time.
  `LITELLM_BASE_URL` and `LITELLM_API_KEY` are still required for `run`.

The paid `run` step asks for confirmation; non-interactively pass `-y` (or `PEPI_EVAL_YES=1`).
Exit codes match `python -m evals.swebench`: 0 ok, 2 usage/configuration, 3 step failure, 130 interrupted.

**Dry-run mode** (no side effects, prints shell-quoted commands):
```sh
LITELLM_API_KEY=sk-dummy LITELLM_BASE_URL=https://x evals/pepi-eval.sh --dry-run -y all
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

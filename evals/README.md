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

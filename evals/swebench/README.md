# Pepi SWE-bench evals

A separate local-only evaluation profile; your live Pi config is never edited.
Python 3.10+, Docker (Linux containers), an installed SWE-bench source checkout
with `swebench.task.repo`, and a clone of `SWE-bench/swe-bench-tasks` are required.
The initial image recipe targets Verified's Debian/Ubuntu, `/testbed`, Conda
`testbed` environments. Other benchmark/image layouts require another recipe.

## Prepare, build, run

Run from the Pepi repository root. Choose exact installed/approved package
versions; these commands intentionally do not select `latest` for you.

```sh
printf '%s\n' sympy__sympy-20590 > /tmp/pepi-eval-ids.txt
export PI_VERSION=0.87.1  # example: check pi --version on your machine
export LITELLM_VERSION='<exact version>'
export SUBAGENTS_VERSION='<exact version>'
export NODE_IMAGE='node:22-bookworm-slim'  # pin its digest for repeatable builds

python -m evals.swebench prepare \
  --task-repo /path/to/swe-bench-tasks --ids /tmp/pepi-eval-ids.txt \
  --out evals/swebench/runs/smoke-01 \
  --pi-version "$PI_VERSION" --litellm-version "$LITELLM_VERSION" \
  --subagents-version "$SUBAGENTS_VERSION" --node-image "$NODE_IMAGE"
python -m evals.swebench build evals/swebench/runs/smoke-01

export LITELLM_BASE_URL='https://your-gateway.example.com'
export LITELLM_API_KEY='your-dedicated-evaluation-key'
python -m evals.swebench run evals/swebench/runs/smoke-01 \
  --timeout-seconds 1200 --network pepi-eval --cpus 4 --memory 8g
```

`pepi-eval` must already exist and reach your gateway. The runner does NOT build
an egress firewall. Configure gateway-only access externally; an arbitrary named
Docker network is not an allowlist (`host`, `bridge`, `default` and `container:*`
are rejected). No host filesystem, Docker socket, MCP credentials or grader files are
mounted in the task container. With `--secrets-dir`, `auth.json` is streamed into a
container tmpfs (see CI below): the agent can read it.
Never use `sudo pi` or put credentials into build arguments. Build before giving
the runtime its dedicated model key. Tool execution can read that key inside its
sandbox, so scope/revoke it appropriately; this is not a secrets-isolation system.

`prepare` freezes selected issue texts, model choices, AGENTS.md and workflows.
It omits personal skills (including role skill overrides), memory, web, MCP,
router and budget polling; LiteLLM's gateway skills/MCP are disabled too.
This is a named benchmark variant, not every feature of your desktop setup.
The source commit and effective profile hash are recorded. Only issue text goes
into the task runtime; reference fixes, test patches and expected tests do not.

`build` records immutable resulting image IDs. Transitive npm dependencies are
not fully locked by direct version pins: keep the resulting image IDs/digests
for comparisons and preserve/export images when moving between machines.

Network names are resolved with `docker network inspect`, so an ID of the default
bridge is rejected too.

`run` is sequential, with an explicit Pi invocation timeout. It stops the
entire dedicated agent UID before exporting the diff against the initial
commit, including new files and agent commits. A timeout submits its partial
diff; a setup failure submits an empty diff. Every selected ID stays in the
predictions file. Existing attempts cannot be overwritten or silently resumed.
The timeout covers the Pi invocation; setup and export are outside it.
Enforce monetary limits at the gateway. Cleanup/export overhead remains in agent
wall-time measurements; Docker control calls have separate 120-second timeouts.

Outputs include `manifest.json`, `builds.json`, `execution.json`,
`predictions.jsonl`, and `attempts/<id>/{attempt.json,patch.diff,parent.events.jsonl,
parent.stderr.log,artifacts/}`. Agent wall time includes Pi startup, delegation,
tools, tests, stopping children and patch extraction; setup/build/grading are
separate. A process exit is not proof of correctness; use the official grader.
Logs and sessions may contain sensitive source or prompts. Keep runs out of Git.

## CI: injected configuration and secrets

Benchmark different configurations by injecting files after cloning (for example
from GitHub secrets). Order matters: the frozen profile is hashed at `prepare`.

1. **Inject config** into a directory outside the checkout, e.g. `$RUNNER_TEMP/config`:
   `settings.json`, `AGENTS.md`, `workflows/`, `extensions/subagent/config.json`.
   Missing files fall back to the repository copies. Do NOT put `auth.json`,
   `mcp.json` or `.env` there: `prepare` and `build` fail if they find one.
2. `prepare --config-dir "$RUNNER_TEMP/config" --config-label variant-b ...`
   The manifest records the label, whether the config was `injected`/`repo`, the
   sha256 of each non-secret source file and `dropped_features`. Predictions use
   `model_name_or_path = <label>-<config_id>`, so runs stay comparable.
   `pepi_revision`/`pepi_dirty` describe the runner code, not the injected config.
3. `build` as before.
4. **Secrets**: write `auth.json` (mode 600, otherwise the run is refused; `mcp.json`
   is not accepted because MCP is always dropped) to a directory outside the checkout and pass `run --secrets-dir DIR`.
   They are streamed over stdin into a 1 MB tmpfs at `/run/pepi-secrets` (owned by the
   agent UID, `noexec`) and symlinked into the Pi agent dir. Never built into an image,
   argv, env, the manifest or `execution.json` (which records names and presence only).
   `LITELLM_BASE_URL`/`LITELLM_API_KEY` are still passed as environment variables.
5. Every known secret value (the API key and every string of 8+ characters in
   `auth.json`) is redacted from `attempts/<id>/**` and the patch; the count is
   `secret_redactions` in `attempt.json`. Redaction fails closed: symlinks, special
   files and files that cannot be read or redacted are deleted and listed under
   `removed_unredactable`. It is still best-effort: it does not see secrets inside
   binary git hunks, encoded values or partially overlapping values. The agent can
   read its key, so use a dedicated, revocable key, and upload only `predictions.jsonl`
   and `attempts/<id>/` directories that contain `attempt.json` (written after redaction).

**MCP**: not supported in this runtime. If the config declares MCP (an MCP package,
an `mcp` settings key or `litellm.mcp.enabled`), `prepare` fails unless `--drop-mcp`
is given, and the removal is recorded under `dropped_features`. Stdio MCP servers need
npm/network access that a gateway-only network blocks. Pinned MCP packages plus a host
allowlist are a follow-up. Only the `litellm` provider is supported.

**Exit codes**: `0` every attempt ran (agent failures and timeouts are benchmark
outcomes, see `attempt.json`), `2` usage/configuration error, `3` any `setup_error` or
`runner_error` (or a failed `docker build`), `130` interrupted. SIGTERM (CI cancel) is
handled like Ctrl-C so containers are removed. If a runner is killed hard, clean up with
`docker ps -aq --filter label=pepi-eval.run=<run_id> | xargs -r docker rm -f`.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s evals/swebench/tests -v
```

Tests use synthetic tasks and fake Docker calls: no credentials or model spend.
A real one-instance smoke test is still required on the target Docker/gateway
setup before treating a score as a benchmark result. It must settle, at least:

- the `LITELLM_BASE_URL`/`LITELLM_API_KEY` names and that `litellm.skills/mcp.enabled`
  really disables gateway tools (check the tool list in `parent.events.jsonl`);
- where Pi reads `auth.json`/`mcp.json`, whether it rewrites `auth.json` (OAuth refresh
  via rename would replace the symlink), and that Docker accepts `uid=` on `--tmpfs`;
- whether `pi install` rewrites `/opt/pepi-agent/settings.json` (the profile hash is
  computed before it), and that `COPY --from=agent /usr/local/` keeps the task image working;
- that `pi` accepts `--append-system-prompt <file>` and `@/input/issue.md`, that
  `load_task_repo(path, ids)` has the assumed signature, and that `/testbed/.git` holds no
  refs or reflog entries after the baseline commit.

Python 3.10+ is checked at start-up. Run tests with `PYTHONDONTWRITEBYTECODE=1`.

## Interface references

- https://github.com/SWE-bench/SWE-bench/blob/main/swebench/task/repo.py
- https://www.swebench.com/SWE-bench/guides/evaluation/
- https://pi.dev/docs/latest/cli
- https://pi.dev/packages/pi-provider-litellm

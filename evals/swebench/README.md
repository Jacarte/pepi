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
Docker network is not an allowlist. No host filesystem, Docker socket, personal
auth, MCP credentials or grader files are mounted in the task container.
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

## Tests

```sh
python -m unittest discover -s evals/swebench/tests -v
```

Tests use synthetic tasks and fake Docker calls: no credentials or model spend.
A real one-instance smoke test is still required on the target Docker/gateway
setup before treating a score as a benchmark result.

## Interface references

- https://github.com/SWE-bench/SWE-bench/blob/main/swebench/task/repo.py
- https://www.swebench.com/SWE-bench/guides/evaluation/
- https://pi.dev/docs/latest/cli
- https://pi.dev/packages/pi-provider-litellm

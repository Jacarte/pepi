# Grade, summarize and compare

## Exit codes

`telemetry`, `summarize` and `compare` use the runner's convention (see the
`Exit codes` paragraph in [README.md](README.md)) for the cases they can hit:

- **0**: success.
- **2**: validation error (bad arguments, malformed or changed inputs, immutability violations).

`grade` returns **2** for the same validation errors, otherwise the evaluator
process's own exit status, unchanged. Those statuses are not remapped to the
runner's `3`: any other nonzero value means the evaluator itself failed (see
`grading/<id>/grader.log`). Neither a 0 nor a nonzero status says whether tasks
passed; read the resolved rate in `summary.json`. `summarize` after a failed grade
reports missing results as unknown instead of failing.

## Commands

First prepare/build/run using README.md. Then reduce archived telemetry and
invoke the official SWE-bench evaluator from the controller environment:

```sh
python -m evals.swebench.telemetry evals/swebench/runs/smoke-01
python -m evals.swebench.reporting grade evals/swebench/runs/smoke-01 \
  --task-repo /path/to/swe-bench-tasks --workers 2 --test-timeout-seconds 1800
python -m evals.swebench.reporting summarize evals/swebench/runs/smoke-01
python -m evals.swebench.reporting compare \
  evals/swebench/runs/baseline evals/swebench/runs/candidate > /tmp/pepi-comparison.json
```

Use a SWE-bench source installation exposing `swebench.task.repo` and
`run_evaluation.main(..., task_repo=...)`. The grader runs separately from Pi.
The wrapper copies selected task directories and `sweb.yaml` into a host-only
snapshot, outside the agent Docker build context. Before grading, the driver
checks issue text, repository, base commit, image, dataset and split against
generation. The snapshot may contain gold/test patches: never expose it to Pi.
Freeze the task repository commit across experiments; the wrapper cannot prove
that a grading test changed before its first snapshot.

Every grade gets a fresh run ID and prediction hash, avoiding stale report
reuse. Subsequent edits to predictions or the grading snapshot invalidate the
joined summary. `grading/<id>/grader.log` contains evaluator output;
`evaluator.json` records the installed version, source hash and source commit
when available. Passing a task repo can rebuild official images; that elapsed
time is grading infrastructure, not agent latency.

For the [custom Verified20 v1 preset](presets/README.md), reports explicitly label
**Custom SWE-bench Verified 20-task subset; not an official Verified score**, with
raw X/20 resolved and unknown grades separately. Hash-verified prepared selection
metadata carries the fixed ordered tasklist hash, definition and source revision.
New suite-aware comparisons also check shared Pi/subagents pins, Node image
reference and per-ID build platform. LiteLLM pins match when both sides require it;
its absence on Codex does not block a Bedrock comparison. Legacy reports retain
their previous schema and comparison behavior. Matching mutable tags is not proof
of identical image contents; inspect digests/build IDs and preserve runtime pins.

Outputs: `summary.json`, per-task `summary.csv`, and `summary.md`. Resolved rate
uses every selected task. Empty patches are unsuccessful; missing/error reports
are unknown, not fabricated test failures. Agent status and correctness remain
separate: a timed-out partial patch can still resolve a task.

Metrics include all-attempt and resolved-only median/p95 latency (nearest-rank
p95), turns, input/output/cache token categories and Pi-estimated cost. Cost per
resolved task includes failed attempts. It is null when accounting is incomplete
or nothing resolved; observed partial spend is labeled separately. Consult
TELEMETRY.md for coverage limits: estimates are not invoices.

Paired comparisons reject different tasks/issues/baselines and execution
protocols (deadline, CPU, memory, concurrency, host, network settings). They show
wins, regressions, inconclusive pairs and latency changes on jointly resolved
tasks. They do not prove hardware, gateway/model snapshots or mutable image tags
are identical: keep those fixed and inspect the manifests/build records. Repeat
fresh runs before interpreting small stochastic differences. No composite score
or silent intersection of task sets is used.

```sh
python -m unittest discover -s evals/swebench/tests -v
node --test evals/swebench/tests/telemetry-extension.test.mjs
```

These are offline tests, not a live Pi/gateway/Docker certification. Before a
real comparison run one task, inspect loaded models, child completion, patch,
accounting coverage and official grade, and verify container cleanup. Run the
official gold-patch infrastructure check as well. No paid benchmark run is part
of these tests. Confirm benchmark conclusions on fresh held-out repository tasks.

Official interface inspected:
https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/run_evaluation.py
https://www.swebench.com/SWE-bench/guides/evaluation/

# Cooperative coding planner

Default-loaded Pi tools for nontrivial coding tasks in any repository. Both supported
profiles include `./extensions/cooperative-planner/extension.ts`: apply a profile, then
start plain `pi`, with no extra flags. Existing running sessions need a reload/restart.
Requires the inspected **Pi 1.1.0 + pi-subagents 0.78.0** public APIs.

Loading does not call Jev or activate the full capability gate. Ordinary direct tools and
read-only subagent management remain available, but launch-like subagent calls require
`cooperative_plan` first. Use cooperative planning only for nontrivial coding. Keep the three
implementation files and
`agents/planner.md` together. Install pi-subagents normally with `pi install`; the extension
resolves its public capability-ceiling export from Pi's user npm installation.

Before use, authorize sending the explicitly supplied issue and proposed plan to
`https://api.typesafe.ai/v1/systemone` and provide `TYPESAFE_API_KEY` in the process environment.
Do not include secrets in either input. A credential is **not** network authorization:
the operator must permit this endpoint through the existing egress policy. The extension does
not open network access, use a proxy fallback, read session dumps, or route/switch models.

## Protocol

1. Call `cooperative_plan` with the complete issue, 2–8 workers, a finite `timeout_ms`, and
   `isolation` (`auto`, `none`, or `worktree`). Include the scout handoff and resolved task
   constraints in the issue. The controller launches the static `planner` from
   `agents/planner.md`; no duplicate runtime role is registered. It can only read/search,
   follows the requested result schema, and cannot delegate, score, or approve.
   Model/thinking preferences use normal subagent settings. Before scoring, if a task cannot
   safely split into the requested independently owned slices with a genuine parallel wave,
   the planner returns `{"status":"not_parallelizable","reason":"..."}` (nonempty reason,
   at most 4000 characters). The controller releases its gate and returns the reason to the
   parent for ordinary worker/deep-worker routing, integration and reviewer checks. It does
   not call Jev, manufacture an approved plan, or launch workers. Do not invent parallel work.
2. The parent controller submits each structured plan to Jev. The immutable version-1 rubric
   is **task coverage**, **slice independence/dependencies**, **file ownership conflicts**,
   and **integration feasibility**. Every `noul` must be a finite probability **>= 0.8**. Each
   `cooperative_plan` call allows at most three planner iterations, retaining the plan with
   the highest minimum criterion score. The parent session displays only that best percentage.
   If none passes, the best plan is diagnostic only and cannot be dispatched. Transport,
   malformed-response, cancellation and deadline failures stop the controller; no fallback
   plan or worker authorization is manufactured. Once scoring starts, `not_parallelizable`
   is unavailable: failed criteria cannot bypass the gate.
   A new session is needed after a terminal stop.
3. The parent reviews the accepted plan, then calls `cooperative_dispatch` with the returned
   digest. Scoring alone never launches workers. The controller builds worker briefs
   from its retained plan and launches only the next dependency wave. Caller-supplied task
   text, approval flags, disk receipts, planner prose and old digests cannot authorize work.
4. When a wave finishes, call `cooperative_checkpoint` with `action: "inspect"`. Every keyed
   child must have successful completion and exact `observed` process-terminal evidence.
   Missing/unknown proof does not release parent mutation tools. Inspect status and stop/report
   failures; never infer termination from a PID, result file, or elapsed time.
5. At the released checkpoint, the parent inspects/reconciles changes and integrates them into
   the authoritative task tree. Then `action: "verify"` executes the plan's checks there.
   Only passing checks permit the next wave. Repeat through final integration. No commits,
   stashes, pushes, automatic merging or snapshot mechanism are added. Final successful
   verification releases the ceiling and restores ordinary tools/roles so the parent can
   launch reviewer. A later `cooperative_plan` starts a fresh gate.

`auto` uses managed worktrees for fully independent plans in clean Git repositories;
otherwise it uses the shared task tree with explicit exclusive file ownership. Dependent
waves use that integrated shared tree. Explicit `worktree` with dependent waves fails rather
than silently changing isolation: Pi's managed worktrees require clean committed sources.
For tasks forbidding Git metadata or requiring a dirty shared checkout, explicitly choose
`none`. The selected mode is part of the scored approval digest.

`jev_score_plan` exposes standalone scoring of an explicit issue/plan without activating
cooperative mode, even on failure. During cooperative work it invalidates prior approval and
**never** authorizes dispatch or releases the gate. Denied idle dispatch/checkpoint calls do
not activate restrictions either. Use `cooperative_plan` for the gated protocol.
After dispatch, revision/scoring is disabled: stop/report scope changes instead of allowing
new file ownership to race active workers. Keep integration artifacts outside source files.

## Enforcement and boundaries

The controller uses the static planner role, foreground structured delegation, RPC
spawn/status, capability ceilings and Pi's blocking `tool_call` hook. On activation, the
ceiling is installed before any asynchronous work; initialization failure stays closed.
During cooperative work, before approval the ceiling denies workers even through RPC.
Generic model-issued launch/workflow/resume/schedule operations stay blocked. Parent
mutation/shell tools are blocked until a terminal checkpoint. Failure, cancellation and
branch navigation retain the gate until a new session; navigation cannot revive approval.
Never start a new session to race unfinished workers. Idle navigation adds no restrictions.
Approved workers have only local read/search/edit/write/bash tools, no inherited extensions
or nested delegation. The parent must remain alive to own checkpoints.

This is a deterministic **Pi controller gate**, not an OS sandbox. Trusted extensions share
process authority; do not load untrusted extensions. Shell tools and tests have the user's
filesystem permissions, so exclusive ownership remains a worker contract, not a filesystem
ACL. Inspection/reconciliation is performed by the parent; passing test commands alone is
not proof that integration is semantically correct. External scores are probabilities, not
proofs of correctness. Existing container, test-integrity and secret boundaries still apply.

## Receipts and evaluation

Safe HTTP receipts go to `$PEPI_EVAL_OUTPUT/jev-receipts` during evaluations, otherwise
`~/.pi/agent/logs/jev-receipts`. Each attempt has a start and finish record with the same UUID,
input/plan/policy binding digests, outcome, duration, criterion scores and pass flags, and validated
token usage. No issue/plan text, raw response, exception body, headers or keys are logged by the scorer.
Pi's normal tool/session transcript still contains the explicit task/plan used by the protocol;
do not publish raw sessions. Unknown usage/spend is not zero. No verified pricing is configured,
so external cost is explicitly unavailable and never added to Pi model estimates.

The generic eval freezer accepts `"cooperativePlanner": true` in the selected settings and
only copies the three exact implementation files plus `agents/planner.md` from the arm's
own config source. It never falls back to candidate code for a baseline missing the bundle.
No arbitrary agents/extensions are copied. All four files are fingerprinted. Everyday profile
extension settings alone do not enable cooperation in frozen evaluations. Run with explicit `--pass-env TYPESAFE_API_KEY`
and an operator-managed egress allowlist. This does not change any benchmark-specific prompt
or workflow. Public summaries and PR comments include numeric criterion scores for each attempt,
mark the best-scoring attempt, and omit raw issues, plans, scorer responses, secrets and receipts.

Offline checks:

```sh
node --test tests/cooperative-planner.test.mjs tests/cooperative-runtime.test.mjs tests/cooperative-checkpoints.test.mjs
python -m unittest evals.swebench.tests.test_cooperative
```

The runtime smoke loads both profiles' default extension settings with the installed pinned
SDK/package and exercises idle/active/completed tool gates with offline protocol fixtures.
The checkpoint test uses deterministic loopback model responses and real package terminal
proofs. No remote model or Jev calls are made.

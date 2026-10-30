# Pepi SWE-bench evals

A separate isolated evaluation profile; your live Pi config is never edited.
Python 3.10+, Docker (Linux containers), an installed SWE-bench source checkout
with `swebench.task.repo`, and a clone of `SWE-bench/swe-bench-tasks` are required.
The initial image recipe targets Verified's Debian/Ubuntu, `/testbed`, Conda
`testbed` environments. Other benchmark/image layouts require another recipe.

## Default custom subset

Shell preparation and direct `prepare` default to the versioned
[custom Verified20 v1 preset](presets/README.md); `run` defaults to 300 seconds per
Pi invocation. It is **not an official Verified score**. The frozen task repository
revision is required for this exact ordered selection. IDs and timeout overrides
remain available; the explicit one-task commands below are smoke runs, not the suite.
The preset documentation includes reproducibility/provenance and paired profile commands.

## Prepare, build, run

Run from the Pepi repository root. Choose exact installed/approved package
versions; these commands intentionally do not select `latest` for you.

```sh
printf '%s\n' sympy__sympy-20590 > /tmp/pepi-eval-ids.txt
export PI_VERSION=1.1.0  # verified native MCP baseline; use an exact approved version
export LITELLM_VERSION='<exact version>'
export SUBAGENTS_VERSION='<exact version>'
export NODE_IMAGE='node:22-bookworm-slim'  # pin its digest for repeatable builds

python -m evals.swebench prepare --profile bedrock \
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

`pepi-eval` must already exist and reach the frozen provider endpoints. The runner does NOT build
an egress firewall. Configure provider-endpoint-only access externally, plus only the explicitly
approved MCP/API endpoints your servers need; an arbitrary named
Docker network is not an allowlist (`host`, `bridge`, `default` and `container:*`
are rejected). No host filesystem, Docker socket or grader files are
mounted in the task container. With `--secrets-dir`, `auth.json` and/or `mcp.json` are streamed into a
container tmpfs (see CI below): the agent can read it.
Never use `sudo pi` or put credentials into build arguments. Build before giving
the runtime its dedicated model key. Tool execution can read that key inside its
sandbox, so scope/revoke it appropriately; this is not a secrets-isolation system.

`prepare` freezes selected issue texts, model choices, AGENTS.md and workflows.
It omits personal skills (including role skill overrides), memory, web, the MCP
adapter, router and budget polling; LiteLLM's gateway skills/MCP remain disabled.
Host skills are never frozen; `run` can copy them for one run only after consent
(see [Host skills](#host-skills)).
Native Pi MCP is enabled by default; use `--drop-mcp` to disable it explicitly.
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
Grading, summaries and an optional AI-written narrative of summarized runs
(`python -m evals.swebench.narrate`) are described in [REPORTING.md](REPORTING.md).

## Evaluation metrics

`reporting grade` writes official per-task test outcomes; `reporting summarize` writes
`summary.json`, `summary.csv` and `summary.md`; `reporting compare` pairs two summaries.
There is **no composite score**. The official resolution count is the primary result;
agent process status, test outcomes, latency and estimated usage are reported separately.

| Metric / fields | Output | Meaning |
|---|---|---|
| `selected`, `resolved`, `resolved_fraction`, `unknown_grades` | `summary.json` / `summary.md` | Selected tasks, official resolved tasks, resolved/selected fraction, and tasks whose grades are missing/invalid. Unknown is not treated as failure. |
| `grading_status`, `agent_status` | Per-task `summary.csv` / `summary.json` | Grader outcome (`resolved`, `unresolved`, `empty_patch`, `grading_error`, `not_graded`) is separate from runner outcome (`completed`, `timeout`, `agent_error`, `setup_error`, `runner_error`, `interrupted`, `not_started`, or `unfinished_children`). |
| `FAIL_TO_PASS`, `PASS_TO_PASS`, `FAIL_TO_FAIL`, `PASS_TO_FAIL` success/failure lists | Per-task `grading/.../report.json` | Official SWE-bench test IDs that passed or failed in each category. These details are not collapsed into the resolution metric. |
| `agent_wall_seconds`, `setup_seconds` | Per-task `summary.csv` | Agent wall time covers Pi startup through tool use, child shutdown and patch export; setup is separate. Image build and official grading are not agent time. |
| `latency_all_seconds`, `latency_resolved_seconds` (`observations`, `missing`, `median`, `p95`) | `summary.json` | Distribution of agent wall time across all tasks or only resolved tasks; p95 is nearest-rank. |
| `turns`, `turns_observed` (`observations`, `missing`, `median`, `p95`) | Per-task `summary.csv` / `summary.json` | Session turns per task and their run-level distribution. |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | Per-task `summary.csv` and `telemetry.json` | Session-accounted token categories; detailed parent/child usage is in telemetry. |
| `estimated_cost_usd`, `observed_estimated_cost_usd`, `estimated_total_cost_usd`, `estimated_cost_per_resolved` | Per-task `summary.csv` / `summary.json` | Pi price estimate, not gateway billing; includes failed attempts. Total/per-resolved are `null` if accounting is incomplete or no task resolved. |
| `accounting_status`, `available_accounting_tasks` | `telemetry.json` / `summary.json` | Per-task usage completeness (`available`, `partial`, `unavailable`) and count with complete accounting. Unresolved child work or incomplete telemetry can make accounting partial. |
| `test_edit_kind`, `test_files_touched`, `tasks_modifying_existing_tests`, `resolved_with_test_edits` | Per-task `summary.csv` / `summary.json` | Whether the patch changed test-like files and how often such tasks resolved; useful for spotting test-edit dependence. |
| `history_pruning`, `tasks_history_not_pruned` | Per-task `summary.csv` / `summary.json` | Whether post-base Git history was pruned before the agent ran; failed/partial pruning is an integrity caveat, not a correctness score. |
| `resolved_strict`, `unknown_grades_strict`, `resolved_restored`, `unknown_grades_restored` | `summary.json` (optional) | Separate regrades after removing agent test edits (`strict`) or repairing evaluator checkout files (`restored`); never replace the official score. |
| `resolved_delta`, `wins`, `regressions`, `inconclusive` | `compare` JSON | Paired resolution change and task IDs that improved, regressed or could not be compared. |
| `both_resolved_latency_pairs`, `both_resolved_latency_delta_median_seconds`, `estimated_cost_delta_usd`, `configuration_differences` | `compare` JSON | Median time delta only for tasks resolved in both runs, estimated cost delta when complete, and differences in non-pairing configuration. Positive time/cost deltas mean slower/more expensive candidate. |
| `grader_exit_code`, `grading_seconds`, `total_instances`, `completed_instances`, `infra_failure_instances`, `ambiguous_failure_instances`, `empty_patch_instances`, `error_instances` | `grading.json` / `results.json` | Evaluator execution health/counts; these do not by themselves indicate model success. |
| Jev attempts, per-attempt criteria, outcome and best attempt | Per-task `telemetry.json`, `summary.json`/`.csv`, and PR comment | Numeric probabilities only; the best attempt maximizes its weakest criterion and is marked. Raw issues, plans, scorer responses and receipts are excluded. |
| `build_seconds`, image ID, platform; CPU, memory, deadline, network, concurrency | `builds.json` / `execution.json` | Build provenance and measurement protocol, not quality scores; keep them consistent across paired runs. |
| Tool calls/errors, retries, compactions, child/model usage, per-tool timing, skill reads | Per-task `telemetry.json` | Diagnostic telemetry with coverage limits; external calls/provider retries may be unobserved. See [TELEMETRY.md](TELEMETRY.md). |

## Named profiles and provider authentication

Select `prepare --profile bedrock` (LiteLLM) or `prepare --profile codex`
(builtin `openai-codex`); the shell equivalent is `PEPI_EVAL_PROFILE=NAME`.
Selection never calls `profile.sh`, changes the active profile, or reads a named
profile's `auth.json`/`mcp.json`. Without a selector, repository defaults remain
available for legacy workflows.

Allowed files use **whole-file** precedence: injected `--config-dir` file >
named profile file > repository default. Exception: a named profile without
`models.json` does **not** inherit root `models.json` left by a prior active profile.
Config-dir still rejects `auth.json`, `mcp.json` and `.env` anywhere. Input symlinks
and special files are refused before reads/copies (macOS system temp aliases are
allowed). Do not pass `profiles/codex` as config-dir: it contains secret MCP config.

The hashed `build/provider-plan.json` freezes the exact parent/declared child
provider/model choices, required providers and exact direct dependency pins.
`build`/`run` verify the full build hash before using this plan. Parent invocation
uses its frozen provider/model explicitly, not current host settings or the
unhashed manifest's selection label. Explicit child `provider/model` references
and provider-scoped role overrides add dependencies/credentials. Bare role models
are qualified using their frozen provider preference; ambiguous provider aliases,
null preferences and unsupported extension-provider dependencies fail before launch.
Runtime-selected models outside the declared plan are not a supported eval variant;
use explicit settings and a fresh prepare for comparisons.

Only pinned `pi-subagents` and, **when required by any frozen model**, pinned
`pi-provider-litellm` are installed. Codex needs no LiteLLM version, package,
configuration, environment or credential prompt. Desktop packages, external
Claude Code adapters, personal skill/extension/machine overrides and unrelated
subagent settings are omitted/disabled. The builtin provider names are taken from
the inspected Pi 1.1.0 catalog; compatible custom extension providers are not
provisioned. Other builtins, e.g. `anthropic`, use dedicated runtime `auth.json`,
not arbitrary host environment forwarding.

Both daily profiles load the generic cooperative planner extension and set
`cooperativePlanner: true` for the freezer. This requires Pi **1.1.0** and
pi-subagents **0.78.0** and copies exactly four arm-owned files:
`extensions/cooperative-planner/{extension.ts,controller.mjs,scorer.mjs}` and
`agents/planner.md`. Their bytes are fingerprinted; missing or unsafe files fail
closed, with no fallback to the harness for an injected arm. With the flag absent
or false, neither the extension nor the static planner is provisioned. Pi 1.1.0
tolerates the custom flag; frozen settings use only the explicit extension entry.
No arbitrary host extensions, optional model router, memory or skills are added.
The generic working rules retain configured models when optional routing is absent
and respect runtime isolation limits; scoring and dispatch errors remain fail-closed.

Enabled runs require an explicitly forwarded `TYPESAFE_API_KEY` (`EVAL_ENV` in CI,
`--pass-env TYPESAFE_API_KEY` with the CLI) and operator-approved access to
`https://api.typesafe.ai/v1/systemone`. Only the public issue and proposed plan go
to that scoring service, never hidden evaluator/reference data. The harness does
not open egress or infer authorization from reachability. External HTTP accounting
is separate from Pi usage; incomplete cost coverage remains unknown.

`models.json` accepts provisioned-provider `modelOverrides` and model definitions
with literal id/name, reasoning, input, contextWindow, maxTokens, cost and supported
API metadata. Literal HTTP(S) base URLs must have no userinfo, query, fragment or
interpolation. Keys, headers, commands, environment references and other model
fields are refused without printing values. Codex's configured context-window overrides
are retained. This is intentionally narrower than Pi's full custom-model schema.

### Codex (builtin OAuth, no LiteLLM)

```sh
python -m evals.swebench prepare --profile codex \
  --task-repo /path/to/swe-bench-tasks --ids /tmp/pepi-eval-ids.txt \
  --out evals/swebench/runs/codex-01 --pi-version 1.1.0 \
  --subagents-version "$SUBAGENTS_VERSION" --node-image "$NODE_IMAGE"
python -m evals.swebench build evals/swebench/runs/codex-01
# Provision an evaluation-only auth.json outside the checkout; no host auth is auto-read.
chmod 700 /path/to/eval-secrets
chmod 600 /path/to/eval-secrets/auth.json
python -m evals.swebench run evals/swebench/runs/codex-01 \
  --secrets-dir /path/to/eval-secrets --pass-env TYPESAFE_API_KEY \
  --timeout-seconds 1200 --network pepi-eval
```

Use real Pi provider-keyed credentials, for example this **schema only**:

```json
{"openai-codex":{"type":"oauth","access":"<access-token>","refresh":"<refresh-token>","expires":1234567890000,"accountId":"<account-id>"}}
```

An OpenAI API key is not a Codex OAuth credential. Expired OAuth tokens may refresh.
OAuth runs currently require **Pi 1.1.0 exactly**: the eval preload journals each
`fs.writeFileSync` auth snapshot before mutation, matching that version's inspected
`FileAuthStorageBackend` implementation (not a stable public Pi contract).
`NODE_OPTIONS` is set to this fixed eval-owned preload only for OAuth, inherited by
Node children; host `NODE_OPTIONS` is never forwarded. Snapshots, pending-write
markers and final auth remain in private tmpfs, collected into controller memory
only after stopping every agent descendant, before patch export. Intermediate and
final rotated values join the redaction set. Journaling/collection/parsing failures
fail closed: patches/logs/artifacts are discarded and remaining tasks are not started.
Latest refreshed credentials are
reused in memory across tasks, never saved in host files or run metadata.

**Host token-rotation limitation:** server refresh can invalidate the original
refresh token even though host auth files remain untouched. Use an evaluation-only
login, avoid concurrent use of the same OAuth credential, and expect to log in again
for a subsequent run. Refreshed credentials are deliberately not exported or written
back. Different Pi auth-write versions need explicit inspection/tests before support.
The same-UID agent can read/tamper with its credentials/journal; this is best-effort
artifact redaction, not a sandbox keeping secrets from tool execution.

Other builtin API-key credentials use `{"anthropic":{"type":"api_key","key":"<literal-key>"}}`.
Every required provider (including children) needs runtime auth; unrelated provider
entries, commands and environment references are refused. Credential `env` supports
only literal provider-specific configuration: LiteLLM base URL; Cloudflare account/
gateway IDs; Azure base URL/resource name/API version; Vertex project/location;
Bedrock region. Ambient AWS/Google host credentials, keychains, credential files,
helper commands and arbitrary env forwarding are not supported.

LiteLLM retains the dedicated `LITELLM_BASE_URL`/`LITELLM_API_KEY` workflow, forwarded
by name only when required and validated/redacted. Alternatively use Pi saved
`type: api_key`, `key` and `env.LITELLM_BASE_URL` (or saved OAuth with `baseUrl`).
If a secrets auth file is supplied, the shell defers credential validation to the
runner rather than prompting for gateway env credentials. Missing/invalid required
auth fails before container creation. Legacy hash-verified LiteLLM prepared runs
without a provider plan remain usable; reprepare to adopt the new profile policy.

## CI: injected configuration and secrets

The repository's GitHub Actions workflow (`.github/workflows/pepi-evals.yml`, label `run-evals`)
follows this flow with `python -m evals.swebench.ci` and `evals/ci/*.sh`; see
[CI (GitHub Actions)](../README.md#ci-github-actions) for its setup (secrets in the `pepi-evals`
environment with required reviewers, eval jobs on a self-hosted runner by default), cache, public
artifacts and security model.
`PEPI_EVAL_*` names belong to the workflow and are refused in its `EVAL_ENV` secret, and the
destructive runner scripts (`evals/ci/free-disk.sh`, `prune-images.sh`) refuse to run outside
GitHub Actions.

For every preset, lookup and baseline execution assemble the pinned main checkout's
own `profile.sh PROFILE`, ordinary `AGENTS.md` and configuration; the candidate uses
the PR checkout's. CI never selects a preset-specific collaboration prompt. The PR
harness explicitly appends each arm's frozen `/opt/pepi-agent/AGENTS.md` to Pi's system
prompt, then the same [security contract](instructions.md). The later contract's runtime
safety limits take precedence; it does not impose a collaboration strategy. Missing baseline files that would borrow from the PR
fail the fallback guard; changed main bytes invalidate the baseline hash/cache.
Candidate-only config edits cannot change assembled baseline identity. After merge,
main becomes the incremental baseline, not a permanent direct-control arm.
`ci assemble-config --agents-override FILE` remains available for explicit local
experiments, with its regular-file validation and hashing, but CI does not use it.

Benchmark different configurations by injecting files after cloning (for example
from GitHub secrets). Order matters: the frozen profile is hashed at `prepare`.

1. **Inject config** into a directory outside the checkout, e.g. `$RUNNER_TEMP/config`:
   `settings.json`, optional safe `models.json`, `AGENTS.md`, `workflows/`, `extensions/subagent/config.json`,
   optionally `mcp-packages.json` with its sibling `mcp-package-lock.json` (see below).
   Missing files follow the per-file precedence above. Do NOT put `auth.json`,
   `mcp.json` or `.env` there: `prepare` and `build` fail if they find one.
2. `prepare --config-dir "$RUNNER_TEMP/config" --config-label variant-b ...`
   The manifest records the label, whether the config was `injected`/`profile`/`repo`, the
   selected profile name, sha256 of each non-secret source file and `dropped_features`. Predictions use
   `model_name_or_path = <label>-<config_id>`, so runs stay comparable.
   `pepi_revision`/`pepi_dirty` describe the runner code, not the injected config.
3. `build` as before.
4. **Secrets**: use a chmod-700 directory and write `auth.json` and/or native Pi `mcp.json` (mode 600, otherwise the
   run is refused) to a directory outside the checkout and pass `run --secrets-dir DIR`.
   `mcp.json` is refused for `--drop-mcp` and legacy prepared profiles.
   They are streamed over stdin into a 1 MB tmpfs at `/run/pepi-secrets` (owned by the
   agent UID, `noexec`) and symlinked into the Pi agent dir. Never built into an image,
   argv, env, the manifest or `execution.json` (which records names and presence only).
   Only required LiteLLM env credentials and explicit `--pass-env` names (see below) are
   forwarded; both are validated and redacted.
5. Known secret values (required env credentials, `--pass-env` values, validated auth strings including
   OAuth rotations, and MCP strings of 8+ characters) are redacted from `attempts/<id>/**` and the patch; the count is
   `secret_redactions` in `attempt.json`. Redaction fails closed: symlinks, special
   files and files that cannot be read or redacted are deleted and listed under
   `removed_unredactable`. It is still best-effort: it does not see secrets inside
   binary git hunks, encoded values or partially overlapping values. The agent can
   read its key, so use a dedicated, revocable key, and upload only `predictions.jsonl`
   and `attempts/<id>/` directories that contain `attempt.json` (written after redaction).

### Wasmtime safe-seven in GitHub Actions

`wasmtime-safe-v1` selects the seven ordered public IDs in
[`presets/wasmtime-safe-v1.ids`](presets/wasmtime-safe-v1.ids), dataset
`pepi/upstream-safe-v1`. This is a custom benchmark, **not an official SWE-bench score**.
Its public JSON contains only dataset/selection metadata, base commits, image tags and
validated recipe hashes. **Never commit the evaluator task bundle, `gold.patch`,
`test.patch`, `tests.json` or `eval.sh` to this public repository.**

**`EVAL_PRESET` alone is not enough**, even for a public evaluator repository:
`WASMTIME_TASKS_REPOSITORY` and `WASMTIME_TASKS_REVISION` are also required, alongside
normal Pepi CI provider/runner setup. Only `WASMTIME_TASKS_TOKEN` is optional for public repos.

Operator setup (required before real CI can run):

1. Use a **separate GitHub evaluator repository**, never this main repository. It may be
   public or private; private is preferable for keeping evaluator materials confidential.
   Upload the contents of the prepared `/tmp/pepi-upstream-smoke.l8rcYy/wasmtime-safe-seven/`
   task repository there, preserving `sweb.yaml`, `tasks/<id>/`, the exact Dockerfiles and
   `.dockerignore` files. Record the full 40-hex commit SHA. Do not put the bundle in this
   main repo, an Actions artifact, or a public image registry. CI validates the locator
   and pin, not repository visibility. **Public task data, including gold patches and
   hidden tests, is fetchable from GitHub without the task token.** To keep those materials
   inaccessible to task agents, the operator must deny GitHub egress from agent containers
   (see step 4); omitting a token or keeping files out of the image is not enough.
2. Set these **repository variables** (not environment-level overrides):
   - `EVAL_PRESET=wasmtime-safe-v1`
   - `WASMTIME_TASKS_REPOSITORY=owner/task-repo` (public or private; distinct from this repo)
   - `WASMTIME_TASKS_REVISION=<40-hex commit>` (no branch, tag or abbreviated SHA)
   - `PROFILE=<existing profiles/ directory>` and the normal `EVAL_*` version/resource pins.
   - Leave `EVAL_IDS` empty for all seven; a smoke subset must use exact IDs from the list.
   - Leave `EVAL_RUNS_ON` empty or use `["self-hosted","pepi-evals"]`. Hosted runners
     are refused for this preset; both eval jobs are pinned to these self-hosted labels.
3. For a **public** evaluator repository, leave `WASMTIME_TASKS_TOKEN` unset: checkout
   falls back to `${{ github.token }}`. For a **private** evaluator repository, set
   `WASMTIME_TASKS_TOKEN` in the reviewer-protected **`pepi-evals` GitHub environment**
   to a fine-grained token with **Contents: read-only**, scoped only to that task repo.
   The main repository's automatic job token cannot read a separate private repo; without
   the dedicated token, checkout fails before evaluation. Keep normal evaluation-only
   `PI_AUTH` and `EVAL_ENV` secrets in that environment in either case. Both checkout
   token choices are passed only to `actions/checkout` with `persist-credentials: false`;
   never put them in env/argv, `EVAL_ENV`, auth.json, agent config, containers or artifacts.
4. Provision the Linux/amd64 self-hosted runner with Docker, adequate disk/RAM and a
   **pre-existing operator-managed `pepi-eval` network**. CI will not create a generic
   bridge for Wasmtime. Enforce an egress allowlist outside the harness: allow only the
   approved provider/service endpoints (including the scoring endpoint only when explicitly
   approved for an enabled profile) and genuinely required build registries; **deny GitHub
   from agent containers**, including API/raw/codeload endpoints and alternate routes.
   A network named `pepi-eval` alone is not enforcement. Test the policy before approval.
   Host-side base-image builds need GitHub, apt, Rust and crate registry access; keep
   that build access separate from the agent network. The prepared recipe prefetches crates.
   Never mount host workspaces, the evaluator task repo, Docker socket or evaluator files
   into agent containers. Use trusted, isolated runners and serialize workflows sharing
   a Docker daemon/workspace; baseline/candidate jobs must not share writable workspaces.
5. Review the entire PR (including workflow/profile/harness code) before approving its
   environment jobs. Add the existing `run-evals` label to a same-repository PR, or use
   the existing push-to-main baseline path. No new dispatch trigger is required.

Lookup remains GitHub-hosted and **secret-free**: it resolves/validates public variables
and hashes the pin without checking out hidden tasks. Only approved baseline/candidate
jobs check out the evaluator repository, at lookup's exact revision. `PEPI_CI_TASK_REPO`
points at that evaluator checkout, outside the agent config/build input. The wrapper
refuses a dirty checkout or mismatched revision. Cache keys include the repository locator,
revision, preset, dataset, ordered ID selection, workflow and effective config; manifests
record the revision, dataset and preset, and grading checks frozen task-input hashes.

Before provider secrets are staged, CI builds **all seven** base images from the evaluator
checkout on `linux/amd64` (no dependency on prior local images). The recipes must match
public hashes and permit no COPY/ADD. `.dockerignore` must contain exactly `*`,
`!Dockerfile`, `!.dockerignore`, one per line. Docker receives a temporary context with
**only those two files**, never hidden evaluator materials. A network-disabled verification
container checks each immutable resulting image's `/testbed` base commit, clean working
copy and submodules. Build logs, image IDs and build/verification timings stay in private
host receipts under `$RUNNER_TEMP/pepi-wasmtime-images.*/build/` (700 directory, 600 files).
No image cache is pruned by this base-image step. Existing end-of-job agent-image cleanup
is unchanged. Provision enough time/disk for cold Rust builds; the existing 350-minute
job limit covers setup, seven sequential attempts and grading, not just model time.

Like all presets, this compares main versus PR configuration using each arm's generic
AGENTS.md and profile. The obsolete preset-specific direct/two-worker prompt is removed.
There is no target-specific planner or worker-count policy; model, planning and coordination
effects are not separately attributable in this two-arm comparison.

MCP remains disabled for this preset; task concurrency stays **1** and the default Pi
invocation timeout is **1800 seconds** (`EVAL_TIMEOUT_SECONDS` may override it). Other
presets retain their MCP/network behavior and timeouts. The common security contract
prohibits Git metadata changes, including worktrees, and leaves all changes in /testbed.
Labeled PR CI narrates baseline versus candidate with tasks solved first, then time/cost
and accounted child invocations. Counts alone do not prove concurrency or independent
decomposition; incomplete cost coverage stays unknown. The report is evidence for human
review, not an automatic improvement/merge gate.

Wasmtime's slim cache/handoff/artifact sets exclude evaluator/build logs, raw test output,
eval scripts and pre-existing narratives. Public outputs retain summary-level metrics,
attempt metadata/telemetry and agent-produced patches; they contain no hidden test/gold
files. The report's optional narrative is generated only from those slim summaries.
The evaluator checkout is removed even on failure; private build receipts and full run
folders (including frozen grading inputs) remain on the host. Restrict host access and
apply an operator retention/cleanup policy; never upload those folders wholesale.

Missing repository/revision variables, inaccessible task repositories, invalid pinned
inputs or absent preconfigured networks fail before evaluation. **Real CI execution requires
the uploaded bundle, required variables, provider secrets and runner network; a dedicated
task access secret is required only for private repos.** Offline checks need no credentials,
Docker, provider calls or GitHub access:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  evals.swebench.tests.test_wasmtime_ci evals.swebench.tests.test_ci_workflow \
  evals.swebench.tests.test_ci_helpers evals.swebench.tests.test_preset \
  evals.swebench.tests.test_cooperative
```

### .env forwarding

`run --pass-env NAME` (repeatable) forwards exactly that variable from the runner's own
environment into the task container as `docker exec --env NAME`: the value never enters argv.
The runner is authoritative: it rejects invalid names, duplicates, unset variables, `LITELLM_*`
(frozen-provider credentials), `PEPI_EVAL_*` and reserved names (`PATH`, `HOME`, `USER`, `SHELL`,
`PWD`, `OLDPWD`, `TMPDIR`, `IFS`, `ENV`, `BASH_ENV`, `SHELLOPTS`, `BASHOPTS`, `PS4`,
`PROMPT_COMMAND`, `CDPATH`, `GLOBIGNORE`, `POSIXLY_CORRECT`, `NODE_OPTIONS`, `NODE_PATH`, `LD_*`,
`DYLD_*`, `BASH_FUNC_*`, `DOCKER_*`, `PYTHON*`) with exit 2. Values of 8+ bytes join the
redaction set above, together with their JSON-escaped forms (as Pi's JSONL events and sessions
store them) and each line of 8+ bytes of a multi-line value (as diffs show it); values that
are not valid UTF-8 are matched byte for byte. Shorter values are forwarded with a warning and not
redacted, because scrubbing them would corrupt patches. When anything is forwarded,
`execution.json` records sorted `forwarded_env_names` (names only); `compare` pairs such runs and
reports differing names under `configuration_differences` (an absent key equals `[]`).

`evals/pepi-eval.sh` derives these flags from the container candidates in `./.env` of the launch
directory, after consent, and exports their values only into the runner's own process (see
[the evals README](../README.md)); no other step and nothing else from your shell gets them.
Anything forwarded is readable by the agent and its MCP servers, like `mcp.json`.

## Native MCP

Pi 1.1.0 already supports stdio and streamable HTTP MCP. The old rejection was
an eval policy/configuration gap, not a transport limitation: a preinstalled stdio
server needs a child process, **not npm or internet access**. Servers that call an
external API still need that endpoint; HTTP servers need their configured endpoint.
No egress, mounts, ports, capabilities or host processes are added by this integration.
Pi and its server descendants share UID 10101, the PID/memory/CPU limits and cleanup.

Use Pi >= 1.1.0 stable (the verified baseline, not the first MCP release). Older Pi
requires `--drop-mcp`. This disables the builtin in frozen settings and passes
`--no-mcp` on supported runtimes. The adapter package and gateway MCP are still
omitted: this variant uses native Pi semantics, not every desktop adapter feature.
Nothing reads or copies your host `mcp.json` without consent, and caches or keychain never.
`evals/pepi-eval.sh run` can copy `~/.pi/agent/mcp.json` after a separate `[y/N]` (or
`PEPI_EVAL_USE_HOST_MCP=1`; `-y` never consents), keeping only the servers this policy can launch:
the same checks as a secrets-dir `mcp.json` below, applied per server, plus dropping servers
disabled with `enabled: false` or `disabled: true`. It prints kept and dropped server names
(with reasons) and env/header/oauth key names, never values, and continues without MCP when
none survive. See [the evals README](../README.md) for consent and cleanup.

## Host skills

By default the agent sees no skills. `evals/pepi-eval.sh run` can copy your global skills from
`~/.pi/agent/skills` and `~/.agents/skills` after a separate `[y/N]` (default **No**), or with
`PEPI_EVAL_USE_HOST_SKILLS=1`; `0` disables the proposal, `-y` never consents, `.env` cannot set
it, and dry-run never reads the directories. Sources are scanned in Pi's own user order
(`<agent-dir>/skills` before `~/.agents/skills`, as in Pi's `package-manager.js`); the first skill of
a name wins, as on the host. Only top-level directories with a `SKILL.md` count. A skill is copied
whole or dropped with a reason: any symlink (never followed, including a symlinked source
directory or a sub-directory swapped for one during the scan, which is walked through directory
descriptors), hard-linked files, special files, credential-like names (case-insensitive: `.env*`,
`auth.json`, `mcp.json`, `.netrc`, `.npmrc`, `.pypirc`, `credentials*`, `id_rsa*`/`id_dsa*`/
`id_ecdsa*`/`id_ed25519*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.keystore`, `*.jks`) or `.git`
entries, a file over 1 MiB, a skill over 5 MiB, 200 files, 200 directories or 16 levels, a file
matching the narrate secret/token pattern, a PEM private key or a quoted credential assignment
(`"apiKey": "<8+ token characters>"`), or containing this run's credential values, a missing
`SKILL.md`, or a name outside `[A-Za-z0-9._-]{1,64}`; at most 50 skills. The prompt lists each
skill's name, file count, size and number of scripts, plus every dropped entry and its reason,
never contents. Skill names and descriptions go into the agent's system prompt; any skill file it
reads reaches the model provider and is kept, unredacted, in the run's logs
(`attempts/*/parent.events.jsonl`, `attempts/*/artifacts/sessions/`).

A skill holding a value this run redacts fails the run before any container starts, naming the
skill (never the value): the adapter checks the gateway URL/key and the `auth.json`/`mcp.json` it
stages (also `LITELLM_BASE_URL` when no frozen provider needs it), and the runner re-checks every
redacted value, including forwarded `.env` values and `PEPI_EVAL_SECRETS_DIR` files.

After a terminal `y` the list is pinned (`PEPI_EVAL_HOST_SKILLS_SHA256`, via the environment): if
the skills changed since they were shown, `run` stops with exit 2 (`host skills changed since
approval; re-run`). The copy goes to a private temporary directory (`run --skills-dir`, 700
directories, 600 files, 700 for executables) that the adapter deletes afterwards, also on
SIGINT/SIGTERM. The runner re-validates it, then extracts it as root into a per-container tmpfs at
`/opt/pepi-agent/skills` (`<agent-dir>/skills` for `PI_CODING_AGENT_DIR=/opt/pepi-agent`, Pi docs
`configuration.md`): root-owned, readable and executable by the agent UID, not writable, never in an
image layer. `execution.json` records `skills` as `{name, sha256, files}` (content digest over sorted
paths, executable bits and bytes); without skills the key is omitted. Pi's default discovery loads
them for the parent; pi-subagents children launch with `--no-skills` unless the agent sets
`inheritSkills` (default false), but still resolve skills an agent declares by name from
`<agent-dir>/skills`. `config_id` does not include these run-time skills. Skill use is partly
observable: `telemetry.json` `skill_reads` counts the parent's `read` calls below
`/opt/pepi-agent/skills/<name>/` (a lower bound; see [TELEMETRY.md](TELEMETRY.md)).

**Local npm servers:** add a non-secret `mcp-packages.json` to your injected config:

```json
{
  "dependencies": {"@modelcontextprotocol/server-filesystem": "2025.8.21"},
  "commands": ["mcp-server-filesystem"]
}
```

The version above is an example pin, not a recommendation. Choose/review the version
for your benchmark. In a separate temporary directory, make `package.json` with
`{"private":true,"dependencies":{...}}` using those same dependencies. Run
`npm install --package-lock-only --ignore-scripts --no-audit --no-fund` there, then
copy the resulting v3 lock to `mcp-package-lock.json` beside `mcp-packages.json`.
Do not run this in your live Pi directory. Each direct version must be exact;
all locked packages must have public npm-registry HTTPS URLs and SHA-512 integrity.
The manifest/lock are frozen and hashed. Missing injected locks never fall back to
repository locks. `build` uses `npm ci --omit=dev --ignore-scripts`; missing binaries
fail the build. Packages requiring install scripts, private registries, browsers,
Python/uvx or extra OS dependencies are outside this bounded recipe.

Supply the server config at **run time only**, in your mode-600 secrets `mcp.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "/opt/pepi-mcp/node_modules/.bin/mcp-server-filesystem",
      "args": ["/testbed"],
      "exposure": "direct"
    }
  }
}
```

Only declared direct-dependency binaries at that path are accepted for stdio.
`npx`, `uvx`, arbitrary executables and `!command` secret lookups are refused. This
is a provisioning boundary, not a sandbox for untrusted npm packages or arguments:
review them, and enforce runtime egress externally. No runtime dependency download
is needed. Server files are root-owned; task data and the agent profile are writable.
Node >=22.19.0 is required by Pi 1.1.0; server/platform compatibility needs a smoke test.
At build time a provisioned binary whose shebang is `#!/usr/bin/env node` is pinned to Pi's Node
(`#!/opt/pepi-node/bin/node`), so a task image's own Node (20 in the Multilingual JS images) never runs
it; other shebangs (`env -S`, shell wrappers) are left as published.

**Remote servers:** supply a native `url` entry (type `http` or `streamable-http`)
in that same runtime file, with literal headers if needed. No npm manifest is needed.
Allowlist its endpoints externally; do not switch to host/default networking or
expose desktop servers. Loopback refers to the task container, not your host.
Only required LiteLLM env vars are forwarded; use literal dedicated MCP credentials
in the runtime file, not unspecified host env vars. Adapter imports, SSE, interactive
OAuth/keychain flows and `mcp-auth.json` injection are not supported.

Runtime MCP config is secret and deliberately not hashed/recorded; `execution.json`
records presence, the frozen provisioning policy and the server names (`mcp_servers`) only. Preserve the secret config
securely outside run artifacts and use a distinct config label when changing it.
Remote tool inventories/data can drift even with a fixed local image. Validate tools
and perform a one-task smoke test before comparing scores.

**Exit codes**: `0` every attempt ran (agent failures and timeouts are benchmark
outcomes, see `attempt.json`), `2` usage/configuration error, `3` any `setup_error` or
`runner_error` (or a failed `docker build`), `130` interrupted. SIGTERM (CI cancel) is
handled like Ctrl-C so containers are removed. If a runner is killed hard, clean up with
`docker ps -aq --filter label=pepi-eval.run=<run_id> | xargs -r docker rm -f`.

## Test-file edits

Some task eval scripts do not restore the agent's test edits before applying the reference
test patch, so an edited existing test can decide the official grade. In
`django__django-11141`, `eval.sh` runs `git checkout <base> tests/migrations/test_loader.py
tests/migrations/test_migrations_namespace_package/0001_initial.py`; the second path does not
exist yet, so git restores nothing (`error: pathspec ... did not match any file(s) known to git`
in `runs/cmp-base-1/grading/*/logs/evaluation/*/*/django__django-11141/test_output.txt`). Runs that
flipped the existing `test_load_empty_dir` assertion were graded resolved; a run that left the
tests alone was not.

`summarize` therefore always flags, from the graded patch (`predictions.jsonl`, the same text as
`attempts/<id>/patch.diff` when that is UTF-8), which test-like files each patch touches
(`evals/swebench/testpaths.py`: a `tests`/`__tests__`/`testdata` directory at any depth; a top-level
`test/`, `testing/`, `spec/` or `specs/` directory; Maven/Gradle `src/test/`; a JS monorepo's
`packages/<name>/test/`; or a test-file basename: Python `test_*.py`, `*_test.py`, `*_tests.py`,
`conftest.py`; Go `*_test.go`; Ruby `*_spec.rb`, `test_*.rb`, `*_test.rb`, `spec_helper.rb`,
`rails_helper.rb`; JS/TS `*.test.*`/`*.spec.*` (`js`, `jsx`, `ts`, `tsx`, `mjs`, `cjs`); C/C++
`test_*`, `*_test`, `*-test` (`c`, `cc`, `cpp`, `cxx`), so jq's built-in test runner `src/jq_test.c` is
test-like by design while the rest of jq's and redis's `src/*.c` is source; Rust `test_*.rs`/`*_test.rs`). Nested
`test`/`testing`/`spec` packages are library code (`django/test/testcases.py`,
`sympy/testing/runtests.py`, `lib/matplotlib/testing/decorators.py`, druid's
`.../query/spec/*.java`) and are not test-like; nested RSpec files still match `*_spec.rb`. A Go
`test_*.go` is compiled into its package, so it is not test-like either. Rust unit tests inside
source files cannot be told apart by path. Checked against the data: the extension changes the class
of no path in the 2,519 local task patches (one of their 4,297 gold-patch paths is test-like, pytest's
`doc/en/example/nonpython/conftest.py`, as before); none of the 521 Multilingual gold-patch paths is
test-like and 471 of the 495 Multilingual `test_patch` paths are (the rest are fixtures such as ruff's
`resources/test/` and caddy's `caddytest/`). Per task:
`test_edit_kind` (`none`, `added_only`, `modified_existing`, or `unknown` for a non-git diff),
`test_files_touched` (up to 10 paths) and counts. Totals: `tasks_modifying_existing_tests` and
`resolved_with_test_edits`; `summary.md` notes them. Adding a test to an existing file also counts
as modifying it: most such edits are harmless, the strict score tells.

The strict regrade is opt-in: `reporting grade RUN ... --strip-test-edits`, or in
`pepi-eval.sh` `PEPI_EVAL_STRIP_TEST_EDITS=1` (on a terminal it asks, default No, only when
a patch modifies existing tests; `-y` never answers it; when none does, the script records
strict = official without asking, which needs no Docker). It regrades only the tasks whose patch
modifies existing test files, with **every** test-like per-file diff block removed (added test
files too, so they cannot collide with the reference test patch), under a fresh `*-strict-*` run
ID in `grading-strict/<id>/` with its record in `grading-strict.json`. `grading/` and
`grading.json` are never touched. A patch that is empty once stripped is `empty_patch`, not
graded. Other tasks keep their official outcome. `summarize` then adds `resolved_strict` /
`grading_status_strict` per task and `resolved_strict` in total (`Resolved: 17/20 (strict, test
edits removed: 16/20)`); without a strict regrade no strict keys appear. Changed predictions,
a changed test-path policy (`STRIP_POLICY_VERSION`), or regraded tasks whose grading inputs
(`tasks/<id>` and `sweb.yaml`) differ from the official grading's make `summarize` refuse a stale
or mixed strict regrade; `grade --strip-test-edits` refuses a task repo that differs from the
official grading's before grading. The official score is unchanged; the strict one is an
integrity check, not proof of intent.

Known blind spot: a patch that only adds test files (`added_only`, e.g. a new `conftest.py`) is
not regraded, although a new `conftest.py` can change test collection; its strict outcome is the
official one.

### Repaired test checkout (`--restore-test-files`)

The strict regrade removes the agent's test-file changes; the restored regrade instead repairs the
eval script, closer to what the benchmark intends. Each task's `eval.sh` resets the test files with
one `git checkout <base> <path>...` (before the reference test patch and again at the end). git
aborts the whole command when any listed path is absent from `<base>`, typically a file the test
patch creates; 136 of the 2,519 local tasks list such a path (`django__django-11141` is the one in
Verified20 v1). `reporting grade RUN ... --restore-test-files` (or `PEPI_EVAL_RESTORE_TEST_FILES=1`;
same terminal question, default No, `-y` never answers it; it records restored = official without
Docker only when no task qualifies and no eval script is unrewritable) regrades only tasks whose patch
modifies an existing file that a checkout line lists, with the **patch unchanged** and, in that
regrade's own task-repo copy only, each checkout line of the exact form `git checkout <40-hex sha>
<plain relative paths>` (no `.`, `..` or empty components, no trailing `/`) rewritten to
`if git cat-file -e <sha>:<path> 2>/dev/null; then git checkout <sha> -- <path>; fi` per path
(`evals/swebench/evalscript.py`; every other byte is kept, rewriting is idempotent, POSIX sh/bash
3.2). The harness reads `eval.sh` from the frozen `--task-repo` copy (`swebench.task.repo` maps it to
`eval_script`), so that copy is where the repair belongs; `~/swe-bench-tasks` and the official frozen
copy are never changed. The checkout line is the authoritative list of the files grading resets, so
it is intersected with **every** existing file the patch modifies, renames or deletes, not only files
the test-path heuristic calls tests (a listed fixture counts too; the strict regrade still uses the
heuristic). A task whose patch changes an existing file and whose `eval.sh` is missing, not UTF-8 or
has any other line mentioning `git ... checkout` (e.g. `cd x && git checkout`, `git -c k=v checkout`,
`git checkout-index`, a comment) is `unrewritable`: which files it resets is unknown, so it is not
regraded and is unknown in the restored score, which is then a lower bound. Tasks whose checkout never
lists the modified files, or whose patch changes no existing file, keep their official outcome (the
repair changes nothing for them).

Results go to `grading-restored/<run>-restored-<uuid>/` and `grading-restored.json` (per task: the
original/rewritten `eval.sh` SHA-256 and rewritten line count). `summarize` adds
`resolved_restored`/`grading_status_restored`, `resolved_restored`/`unknown_grades_restored` and
`Resolved: 18/20 (strict, test edits removed: 17/20; test-checkout repaired: 17/20)` for the
variants that exist. Like strict, it refuses restored data after predictions change, after a
`RESTORE_POLICY_VERSION` or selection change, or when the regraded inputs differ from the official
grading's inputs with the same repair applied. `reporting test-checkout RUN` prints
`<regraded> <unrewritable>`: how many tasks the restored regrade would regrade and how many it could
not repair; `pepi-eval.sh` asks (default No) whenever either is above 0.

## Multilingual (Niffler 10)

`multilingual10-v1` is the 10-task SWE-bench Multilingual subset from the Niffler pilot
(`axios`, `caddy`, `docusaurus`, `fmt`, `gin`, `jq`, `nushell`, `redis`, `rubocop`, `tokio`):
**a custom subset, not an official score**. `~/swe-bench-tasks` holds no Multilingual data, so
import the rows into a separate task repo first (downloads from HuggingFace; the swebench venv
has `datasets`):

```sh
~/.venvs/swebench/bin/python -m evals.swebench.multilingual import \
  --dataset SWE-bench/SWE-bench_Multilingual --revision 846e647b9f33c0b51b739d005d13d85493c9af09 \
  --ids evals/swebench/presets/multilingual10-v1.ids --out ~/swe-bench-tasks-multilingual
```

The importer writes the `swebench.task.repo` layout (`sweb.yaml`, `tasks/<id>/task.yaml`,
`tests.json`, `problem_statement.md`, `hints.md` when non-empty, `gold.patch`, `test.patch`,
`eval.sh` byte for byte from the row's `eval_script`, and a `Dockerfile` that is only
`FROM --platform=linux/amd64 <published image>`), plus `import-manifest.json` with the dataset
revision, per-file SHA-256s and each row's canonical hash. It never overwrites a path and writes
through a temporary sibling directory. `--rows-json FILE` imports local rows without a download.
There is no `environment_setup_commit` in these rows and the harness does not need one.

The prebuilt task images are not pulled automatically (about 18 GB for the 10):

```sh
~/.venvs/swebench/bin/python -m evals.swebench.multilingual images --task-repo ~/swe-bench-tasks-multilingual \
  | xargs -n1 docker pull --platform linux/amd64
~/.venvs/swebench/bin/python -m evals.swebench.multilingual validate-gold \
  --task-repo ~/swe-bench-tasks-multilingual --run-id ml-gold-1 --workers 2 --timeout 1800
```

`validate-gold` grades the gold patches through `grade_driver.py` over a frozen copy of the task
repo, exactly like `reporting grade` grades predictions, writes `validation/<run-id>/` and exits 0
only when every task resolves. With a task repo the harness force-rebuilds each image from its
`Dockerfile` (it removes the image tag first), so grading re-resolves the published `:latest`
image from Docker Hub: registry access is needed at grade time, and a re-published image would
change the environment (tags are not digest-pinned).

Grading (`grade`, `--strip-test-edits`, `--restore-test-files`) refuses a task repo whose inputs differ from
the per-task fingerprints the run was prepared with, naming the tasks, before any copy is kept or the grader
starts. `evals/pepi-eval.sh report` takes its default task repo from the run's manifest (the preset's import
location for a Multilingual10 run, `~/swe-bench-tasks` for Verified), not from `PEPI_EVAL_PRESET`; for any other
dataset it stops and asks for `PEPI_EVAL_TASK_REPO`.

Then run the preset (it sets the default IDs, `TASK_REPO=~/swe-bench-tasks-multilingual`,
dataset and a 600-second Pi timeout, a starting point for cargo/go/C++ builds):

```sh
PEPI_EVAL_PRESET=multilingual10-v1 PEPI_EVAL_TASK_REPO=~/swe-bench-tasks-multilingual evals/pepi-eval.sh all
```

The preset pins its task source by per-task input fingerprints (`sweb.yaml` plus
`tasks/<id>`, the same value grading's stale-input checks use) of an import of the pinned
revision; `prepare` refuses any other inputs for this exact ordered selection, like Verified20's
task-source revision. The tests and builds of these tasks fetch from package registries
(a gold run with `--network none` failed only `tokio`, on `index.crates.io`; agents building
inside the image need more). The preset lists informational `network_requirements` host names
(crates.io, GitHub, npm/yarn, Go proxy/sum, RubyGems); the script prints them before the paid-run
confirmation, and `manifest.json`/`execution.json` record the names. **Your `PEPI_EVAL_NETWORK`
must allow them; the runner does not verify egress policy.** With registry and GitHub access the
agent could also fetch the upstream repository and its later history; that leak is not closed.

### Agent image on non-Python task images

The 10 images are Ubuntu 22.04 (axios, caddy, docusaurus, fmt, gin, jq, redis) or Debian 12 (nushell,
rubocop, tokio) with git, bash and tar, no conda, and run as root with an empty entrypoint. The agent
layer (`evals/swebench/Dockerfile`) and the Pi command handle them as follows:

- **No conda**: the Pi command activates `/opt/miniconda3`'s `testbed` env only when it exists
  (Python images behave as before; a failing activation still stops before Pi starts).
- **Pi's Node is isolated**: the Node image's `/usr/local` is copied to `/opt/pepi-node` and searched
  **after** the task image's `PATH`, so the agent's `node`, `npm`, `npx` and `yarn` are the task's own
  (Node 20 in the axios/docusaurus images, as at grading) and images without Node fall back to Pi's.
  Pi itself always starts on its own Node through the `/opt/pepi-eval/bin/pi` wrapper (Pi needs
  Node >= 22); pi-subagents children reuse that Node (`process.execPath`). Earlier builds put Pi's Node
  first in `/usr/local/bin`, shadowing the task's Node and leaving dangling `yarn`/`yarnpkg` links
  (the Node image's yarn lives in `/opt/yarn-*`, which was never copied); those links are now removed.
  `/opt/pi/node_modules/.bin` (Pi's dependency tools) is no longer on `PATH`.
- **Toolchain caches**: the agent runs as UID 10101 with `HOME=/home/pepi-eval`, while image setup ran
  as root. `CARGO_HOME` (`/usr/local/cargo`) and `GEM_HOME` (`/usr/local/bundle`) are chowned to the
  agent UID at build time when the image sets them, so cargo and bundler can lock, update and extend
  the caches setup filled; `RUSTUP_HOME`, `PATH` and the variables themselves are unchanged, so
  toolchain discovery is the image's. The chown copies those directories into the agent layer (disk).
  Go sets no `GOPATH`; root's module cache (`/root/go`) is not readable by the agent, so Go uses
  `$HOME/go` and `$HOME/.cache/go-build` and downloads modules through `proxy.golang.org`. npm/yarn
  caches go to `$HOME`. C/C++ builds write inside `/testbed`, which the agent owns.
- **Controller commands trust no task `PATH`**: the Rust and Ruby images list `CARGO_HOME/bin` and
  `GEM_HOME/bin` before `/usr/bin`, and the agent UID owns those. Every runner `docker exec` except the Pi
  launch passes `PATH=/usr/sbin:/usr/bin:/sbin:/bin` (`runner.CONTROLLER_ENV`); root runs procps by path
  (`/usr/bin/pkill`, `/usr/bin/pgrep`; the layer installs procps) and the OAuth collector runs on
  `/opt/pepi-node/bin/node`, as it did when Pi's Node was first on `PATH`. The cache handover refuses a
  `CARGO_HOME`/`GEM_HOME` that holds one of those `PATH` directories or Pi's own (`/opt/pi`, `/opt/pepi-*`).
- **OAuth audit preload**: `NODE_OPTIONS=--require=/opt/pepi-eval/auth-audit.cjs` (OAuth runs only) loads into
  every Node process, including the task's Node 20 on axios/docusaurus. That is its purpose (any process that
  could rewrite `auth.json` is journaled); it only uses `fs`, `path` and `module` (Node >= 12).
- **Fail clearly**: the agent layer needs `apt-get` (Debian/Ubuntu) and stops with a message otherwise;
  `useradd --uid 10101` fails on an image that already uses that UID. The runner overrides any image
  entrypoint/`CMD` (`irb` in the rubocop image) with `sleep infinity`.

### Git history after the task base

Task images can keep tags, branches, remote-tracking refs and reflogs that reach commits after the
task base, i.e. the reference fix ([SWE-bench #578](https://github.com/swe-bench/SWE-bench/issues/578):
the Multilingual axios image had 116 tags and 515 later commits; Verified images keep the cloned
upstream refs too). After the baseline check and before Pi starts, every attempt runs
`runner.HISTORY_PRUNE_SCRIPT` as the agent UID: it removes the remotes, every ref except the checked-out
branch (none for a detached HEAD) and the tags whose commit is HEAD or an ancestor of it, pseudo-refs
(`FETCH_HEAD`, `ORIG_HEAD`, ...), reflogs, `.keep` markers and commit-graph files, then `git gc
--prune=now`. HEAD and its ancestors stay, so `git log`, `git blame` and `git describe` (setuptools_scm and
versioneer versions, e.g. matplotlib's `__version__`) work as before and exported patches (diffed against
HEAD) are unchanged. Ancestry decides, not tag names or dates: a tag on a later commit goes; the message of an
annotated tag kept on an ancestor may postdate that commit.
It refuses repositories with alternate object stores or linked worktrees.

`run --history-prune-timeout SECONDS` (`PEPI_EVAL_HISTORY_PRUNE_TIMEOUT`, default 120; `0` disables)
bounds it; setup time includes it. A prune failure never aborts the attempt: `attempt.json` records
`history_pruning` (`status` `pruned`, `refs_only` when refs went but gc timed out or failed, so later
objects may remain readable by hash or via `git fsck --lost-found`, `failed`, or `skipped`; plus
`refs_removed`, `remotes_removed`, `seconds`) and adds a `history_not_pruned: ...` error. On a timeout
the runner kills the agent UID's processes (the still-running gc) and removes stale `*.lock` files
before Pi starts. `summary.json`/`summary.csv` carry `history_pruning` per task and
`tasks_history_not_pruned`; `summary.md` notes them and narrate names those tasks as an integrity
caveat. `execution.json` records `history_pruning` (policy and timeout), so `compare` shows runs from
before this change as a configuration difference.

### Restored checkout on Multilingual scripts

All 10 eval scripts use the rewritable `git checkout <base> <test files>` form (rubocop lists two
paths), except tokio's, which resets its new test file with `rm -f`: a script without a checkout line
never lists an edited file, so `--restore-test-files` never selects it (`not_listed`, official outcome
kept). Across all 300 Multilingual scripts, 259 are rewritable, 21 have no checkout line and 20 are
unrewritable (restored outcome unknown).

### To verify with a real run

Not verifiable without Docker; check one task per language before a full run:

1. Build: the agent layer builds on an Ubuntu 22.04 and a Debian 12 image; `pi --version` works via the
   wrapper and `node --version` in the agent's shell is the task's (`v20` on axios/docusaurus).
2. JS: `yarn test` (docusaurus) and `npx mocha` (axios) run in the agent's shell as UID 10101.
3. Rust: `cargo test` (tokio, nushell) as the agent UID, with `CARGO_HOME` agent-owned; tokio needs
   `index.crates.io`.
4. Go: `go test` (caddy, gin) downloads modules through `proxy.golang.org` into `$HOME/go`.
5. Ruby: `bundle exec rspec` (rubocop) with the chowned `GEM_HOME`.
6. C/C++: `ctest` (fmt), `make check` (jq), `./runtest` (redis) inside `/testbed`.
7. History: `history_pruning.status` is `pruned` within 120 s on the largest repos (nushell, redis,
   and Verified's django) under amd64 emulation; `git tag`, `git branch -a` and `git log --all` show
   nothing after the base.
8. pi-subagents children and any MCP server start (provisioned `#!/usr/bin/env node` servers pinned to
   `/opt/pepi-node/bin/node`).
9. Controller `PATH`: `docker exec --env PATH=...` governs command lookup (expected from runc), `/usr/bin/pkill`
   and `/usr/bin/pgrep` exist after the procps install on Ubuntu 22.04 / Debian 12, and `git`, `sh`, `ln` and
   `tar` resolve under `/usr/sbin:/usr/bin:/sbin:/bin` in the Python and Multilingual images.
10. History pruning keeps ancestor tags: `git describe --tags` and, on a matplotlib task,
    `python -c "import matplotlib; print(matplotlib.__version__)"` match an unpruned container.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s evals/swebench/tests -v
```

Tests use synthetic tasks and fake Docker calls: no credentials or model spend.
A real one-instance smoke test is still required on the target Docker/gateway
setup before treating a score as a benchmark result. It must settle, at least:

- the `LITELLM_BASE_URL`/`LITELLM_API_KEY` names and that `litellm.skills/mcp.enabled`
  really disables gateway tools (check the tool list in `parent.events.jsonl`);
- that native MCP tools are actually listed/callable with the provisioned server under
  the chosen endpoint restrictions, and server processes stop on timeout;
- OAuth endpoint access/rotation on the pinned inspected Pi 1.1.0 auth-write contract,
  and that Docker accepts `uid=` on `--tmpfs`;
- installed package loading matches the frozen settings restored after `pi install`,
  and that Pi's Node under `/opt/pepi-node` (after the task's `PATH`) keeps the task image working
  (see [To verify with a real run](#to-verify-with-a-real-run));
- that `pi` accepts `--append-system-prompt <file>` and `@/input/issue.md`, that
  `load_task_repo(path, ids)` has the assumed signature, and that after
  [history pruning](#git-history-after-the-task-base) `/testbed/.git` holds no ref, reflog entry or
  object after the task base (`history_pruning.status` is `pruned`).

Python 3.10+ is checked at start-up. Run tests with `PYTHONDONTWRITEBYTECODE=1`.

## Interface references

- https://github.com/SWE-bench/SWE-bench/blob/main/swebench/task/repo.py
- https://www.swebench.com/SWE-bench/guides/evaluation/
- https://pi.dev/docs/latest/cli
- https://pi.dev/docs/latest/mcp
- https://pi.dev/packages/pi-provider-litellm

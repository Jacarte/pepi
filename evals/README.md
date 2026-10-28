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

- `PEPI_EVAL_PRESET`: `verified20-v1` (default) or `multilingual10-v1` (SWE-bench Multilingual,
  Niffler's 10; import it first, see [Multilingual](swebench/README.md#multilingual-niffler-10)).
  It sets the default IDs, task repo (`~/swe-bench-tasks-multilingual`), dataset and Pi timeout (600 s).
- `PEPI_EVAL_DATASET`: dataset name recorded and graded (default: the preset's).
- `PEPI_EVAL_HISTORY_PRUNE_TIMEOUT`: seconds for removing git history after the task base (later
  commits, tags, remotes) before Pi starts (default 120; `0` disables; see
  [Git history](swebench/README.md#git-history-after-the-task-base)).

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
600 auth file); unrelated providers are never copied. The source must
be a private readable regular non-symlink file owned by you. Runtime injection remains
stdin/tmpfs, with cleanup after success, failure or interruption. No changes are
written back to host auth. **OAuth refresh may nevertheless invalidate your shared
host login**; dedicated eval credentials are safer. Dry-run only describes the
opted-in action: it never reads/copies host auth or creates a credential directory.

**Host `mcp.json`:** after the auth question, and also without `PEPI_EVAL_SECRETS_DIR`, `run`
offers on a terminal (default **No**) to copy `~/.pi/agent/mcp.json` the same way:
`PEPI_EVAL_USE_HOST_MCP=1` consents non-interactively, `0` disables the proposal, `-y` never
consents, and it is never proposed with `PEPI_EVAL_DROP_MCP=1`. Before asking, it lists each server it
would copy (name, stdio/http, URL hostname, env/header/oauth key names) and each dropped server with a
short reason; values are never printed. Only servers the frozen MCP policy can launch survive, using
the runtime's own rules: commands provisioned from `mcp-packages.json` (spelled
`/opt/pepi-mcp/node_modules/.bin/<binary>`; `npx`, `bash`, host paths and other commands are dropped),
streamable HTTP URLs, no `!command` values, `oauth` as an object, and not disabled
(`enabled: false`/`disabled: true`). A typical desktop `mcp.json` keeps only its HTTP servers.
If nothing survives, the run continues without host MCP servers. After a terminal `y`, the
copy is verified against what you approved: if the filtered servers changed since the list was
shown, `run` stops (exit 2) without copying anything. The source must be a regular
non-symlink file you own and not group/world-writable (readable by others only warns). The
filtered copy shares the private temporary directory with a consented host-auth copy (700
directory, 600 files), is streamed into the container tmpfs, where the agent and its MCP tools can
read its literal env/header values, and is deleted afterwards; the host file is never changed.
Its values join the redaction set, and `execution.json` records only the server names
(`mcp_servers`). OAuth sign-in, keychain and `mcp-auth.json` flows are not supported. Without a
terminal and without `PEPI_EVAL_USE_HOST_MCP`, nothing is read and one line says how to opt in.
Dry-run never reads it.

**Host skills:** after the MCP question (also with `PEPI_EVAL_SECRETS_DIR`), `run` offers on a
terminal (default **No**) to copy the skills in `~/.pi/agent/skills` and `~/.agents/skills`:
`PEPI_EVAL_USE_HOST_SKILLS=1` consents non-interactively, `0` disables the proposal, `-y` never
consents. Before asking it lists each skill (name, file count, size, scripts) and each dropped entry
with a short reason (symlinks, credential-like files, `.git`, size limits, secret/token patterns,
bad names), never contents. Approved skills are pinned like the MCP list, copied to a private
temporary directory, mounted read-only at `/opt/pepi-agent/skills` in each task container, and
deleted afterwards; `execution.json` records names and content digests. Skill text the agent reads
is sent to the model provider and kept, unredacted, in the run's logs. Without a terminal and without `PEPI_EVAL_USE_HOST_SKILLS`, nothing
is read and one line says how to opt in. Dry-run never reads them. See
[Host skills](swebench/README.md#host-skills).

The provider/account usage `run` step separately asks for confirmation; non-interactively pass `-y` (or `PEPI_EVAL_YES=1`).

**Strict regrade:** `summarize` flags patches that modify existing test files (some task eval scripts keep
such edits when grading). `PEPI_EVAL_STRIP_TEST_EDITS=1` makes the `grade` step also regrade only those
tasks with every test-file change removed, into `grading-strict/` (the official grading is kept); `0` skips
it; unset asks on a terminal, default No, only when such a patch exists (with none, strict = official is
recorded without Docker). `-y` never answers it. `PEPI_EVAL_RESTORE_TEST_FILES=1` (same rules) instead
regrades only tasks whose patch modifies an existing file their eval script's `git checkout` lists,
with that checkout repaired (git aborts it when one listed path is absent from the base commit) and the
patch unchanged, into `grading-restored/`; unset, it also asks when an eval script cannot be repaired
(its restored grade would be unknown), so restored = official is recorded without asking only when no
task qualifies and every eval script can be repaired. See
[Test-file edits](swebench/README.md#test-file-edits).
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
  flags: the consent settings `PEPI_EVAL_YES`, `PEPI_EVAL_USE_HOST_AUTH`, `PEPI_EVAL_USE_HOST_MCP`,
  `PEPI_EVAL_USE_HOST_SKILLS`, `PEPI_EVAL_AUTO_FIX`, `PEPI_EVAL_USE_DOTENV`, and `PEPI_EVAL_VENV`, `PEPI_EVAL_SWEBENCH_REPO` (host code: the venv Python,
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

## CI (GitHub Actions)

[`.github/workflows/pepi-evals.yml`](../.github/workflows/pepi-evals.yml) compares a pull
request's Pi config with main's, posts the narrative as a PR comment and keeps the artifacts.
The helpers it calls are `python -m evals.swebench.ci <command>` and `evals/ci/*.sh`.

**Setup** (repository settings):
- **Environment `pepi-evals`** (Settings > Environments): add **required reviewers**, tick
  "Prevent self-review", leave "Deployment branches and tags" at "No restriction" (PR runs deploy
  from pull-request refs). "Prevent self-review" stops whoever added the label (or pushed to `main`)
  from approving the run, so it needs a second reviewer; in a single-maintainer repository every run
  would wait forever, so either leave it off or let the admin use the environment's bypass option
  ("Allow administrators to bypass configured protection rules"). Store the two secrets here, not as repository secrets, and **delete any
  repository-level `PI_AUTH`/`EVAL_ENV`**: repository secrets reach every workflow on every branch
  of the repo, so anyone who can push could print them with a new workflow file, without a label.
  - Secret `PI_AUTH`: the content of an evaluation-only `auth.json`.
  - Secret `EVAL_ENV`: `.env` lines (`NAME=VALUE`). Put `LITELLM_BASE_URL`/`LITELLM_API_KEY`
    here for LiteLLM profiles; every other name is forwarded into the eval container. `PEPI_EVAL_*`
    names belong to the workflow and are refused (`ci secrets-setup` fails, naming them): in
    `pepi-eval.sh` a `.env` overrides the shell, so they would change the run's identity (preset,
    ids, config dir, run paths) without changing the cache key.
- **Repository variables** (not environment variables: `lookup` has no environment, and the cache
  key needs the same values in every job):
  - `PROFILE` (required): a directory under `profiles/`, applied with `profile.sh NAME`.
  - `EVAL_PRESET`: `verified20-v1` (default) or `multilingual10-v1`.
  - `EVAL_IDS`: optional comma-separated task ids (no spaces), a subset of the preset; empty means
    the whole preset. It is part of the cache key, so a subset baseline is never reused for a full run.
  - `EVAL_RUNS_ON`: runner labels of the two eval jobs as a JSON array; default
    `["self-hosted","pepi-evals"]`, hosted alternative `["ubuntu-latest"]` (see *Runners*).
  - Optional pins (defaults in brackets, the same as `evals/ci/run-evals.sh`): `EVAL_PI_VERSION`
    [1.1.0], `EVAL_SUBAGENTS_VERSION` [0.78.0], `EVAL_LITELLM_VERSION` [4.2.0], `EVAL_NODE_IMAGE`
    [node:22-bookworm-slim], `EVAL_TIMEOUT_SECONDS` [the preset's], `EVAL_CPUS` [4], `EVAL_MEMORY`
    [8g], `EVAL_NARRATE_MODEL` [the profile's default model]. `EVAL_CPUS` must not exceed the eval
    runner's CPU count (Docker rejects `--cpus` above it, and every task becomes an infrastructure
    failure); GitHub-hosted runners have 4 vCPUs for public and 2 for private repositories.
- Create the label `run-evals`. Keep Settings > Actions > "Require approval for all external
  contributors" (fork PRs never run this workflow anyway).

**Trigger:** add the label `run-evals` to a pull request from a branch of this repository.
Pushes do not re-run it. The `report` job removes the label when it finishes (not when the run was
cancelled), so adding it once more starts a new run; re-labeling during a run cancels the old
one. If `report` never ran (an eval job failed to start), remove and re-add the label. The hand-off
artifacts `report` reads expire after 1 day, so after a day re-label instead of re-running failed
jobs. Pull requests
from forks never run. Every push to `main` looks up the baseline cache and runs the baseline only on
a miss; there is no scheduled warm-up.

**Approvals:** `baseline`, `candidate` and `report` reference the `pepi-evals` environment, so
GitHub waits for a reviewer before each starts: one approval releases `baseline` and `candidate`
together (they become ready at the same time), a second one releases `report` when both evals are
done. A push to `main` with a cache miss also waits for an approval before its baseline runs (it
needs the secrets too); on a hit nothing waits. **Read the PR's diff before approving**: the PR's own workflow file, `profile.sh` and
`evals/` harness run with the secrets. Approval also covers label spam: anyone with the Triage
role can add labels, but nothing paid runs without a reviewer. Dependabot PRs get no secrets and
fail at the secrets check.

**Jobs:**

| Job | Runner | Environment | When | Does |
|---|---|---|---|---|
| `lookup` | GitHub-hosted | none | PR label, push to main | checks variables, refuses a PR head that lacks the executing workflow, assembles main's config (no secrets), computes the cache key, looks it up |
| `baseline` | `EVAL_RUNS_ON` | `pepi-evals` | cache miss | main's config with the PR's harness (on push: main's), saves the cache if clean, uploads artifacts |
| `candidate` | `EVAL_RUNS_ON` | `pepi-evals` | PR label | the PR's config and harness, uploads artifacts |
| `report` | GitHub-hosted | `pepi-evals` | PR label, after both | narrate, verdict, a **new** PR comment, removes the label, report artifact; red on infrastructure problems |

Both evals run the **PR's harness** (`evals/` code); only the config differs: main's for the
baseline, the PR's for the candidate. Both are assembled by `profile.sh $PROFILE` in a scratch
copy and passed as `PEPI_EVAL_CONFIG_DIR`, with config labels `main` and `pr-<number>`.
`ci assemble-config` runs before any secret exists: it refuses to start while `PI_AUTH`/`EVAL_ENV`
are in its environment, runs `profile.sh` with only `PATH`, a scratch `HOME` and `LC_ALL=C`, and
refuses symlinks in any path component. If main's config lacks a file the PR's harness has (for
example a new `models.json`), `prepare` would take the PR's copy for the baseline, so `lookup`
fails, naming the files; add them to `main` first (`--allow-harness-fallback` only warns, for
local use). A PR that adds such a file can therefore not be evaluated until `main` has one.
On `pull_request` GitHub executes the workflow file of the PR's merge with main, while the jobs use
the PR head: `lookup` fails ("Rebase the PR on main") when the two differ, e.g. after `main` changed
the workflow, or for a branch created before the CI existed.
`run-evals.sh` answers every consent: `PEPI_EVAL_SECRETS_DIR` (auth.json from `PI_AUTH`),
`USE_DOTENV=1`, `USE_HOST_AUTH=0`, `USE_HOST_MCP=0`, `USE_HOST_SKILLS=0`,
`STRIP_TEST_EDITS=1`, `RESTORE_TEST_FILES=1`, `AUTO_FIX=0`, `YES=1`.

**Baseline cache:** the key is `pepi-evals-v1-<profile>-<preset>-<hash>-<ISO week>`. The hash
covers the harness files, this workflow file, main's assembled config, the pins and the
measurement settings, including the task ids (`EVAL_IDS`) and the Docker network exactly as
`run-evals.sh` resolves them (`ci cache-key --json` shows each part). The ISO week makes a baseline
at most about a week old: a new key starts every Monday. Only a baseline with exit code 0 and no
infrastructure failures is saved. Cache scope matters: a PR run can read caches saved on `main` and
on its own PR, so a baseline saved by a PR run helps only that PR; **pushes to `main` warm the cache
every PR shares**, and a week without pushes to `main` means each labeled PR runs its own baseline.
A PR that changes the harness never matches main's key and always runs a fresh baseline. GitHub
evicts entries unused for 7 days and caps a repository at 10 GB. The cache entry (not downloadable,
but readable by any approved workflow run, including approved fork PRs) is the only place that keeps
`manifest.json` and the `execution.json` file.

**What is public** (this repository is public: PR comments are world-readable, and artifacts can be
downloaded by any signed-in GitHub user while they are retained):
- The PR comment: resolved counts, wins/regressions/inconclusive task ids, infrastructure failures,
  configuration-difference **names** (capped at 20; e.g. forwarded `.env` variable names, MCP server
  or skill names), profile and preset names, and the model-written narrative.
- `pepi-evals-baseline` / `pepi-evals-candidate` (30 days): `summary.json/.csv/.md`,
  `predictions.jsonl`, per task `attempt.json`, `telemetry.json` (tool counts, model names, token
  counts, estimated cost) and `patch.diff`, grading reports (`report.json`, `test_output.txt`,
  `run_instance.log`, `grader.log`), `builds.json` and build logs. `summary.json` embeds the run's
  whole execution protocol (`execution.json` as `protocol`): network name, CPU/memory, the runner's OS string, forwarded `.env` **names**,
  MCP server and skill names with the frozen MCP policy and skill digests, and whether `auth.json`
  was present; never secret values. Logs show runner
  paths (`$RUNNER_TEMP`).
- `pepi-evals-*-handoff` (1 day): the same summaries and per-task JSON, the input of `report`.
- `pepi-evals-report` (30 days): the narrative, the comment and the verdict.
- Never uploaded: `manifest.json` (frozen config, dropped packages, provider plan, pepi revision),
  `execution.json` (its content is public as `summary.json.protocol`), event logs, Pi sessions, stderr, issue texts, task-repo copies, `auth.json`,
  `.env`.

`ci scan-artifacts` must pass before any upload, cache save or comment; a finding fails the job.
It matches the exact secret values (raw, JSON-escaped, percent-encoded and base64) read from the
files `secrets-setup` wrote (`--secrets-dir`, `--env-file`), so the raw secrets stay out of the scan
steps, plus common token shapes (private keys, AWS, GitHub, Slack, `sk-`, Bearer, Basic, JWT, Google
API keys). Without any secret source it refuses to run (exit 2) unless `--no-secret-values` is
given. It cannot know internal host names that are not in `EVAL_ENV`.

**Comment:** one new comment per run, marked `<!-- pepi-evals pr=N sha=SHA run=ID -->`, with the
official/strict/restored resolved counts, wins/regressions/inconclusive, infrastructure failures,
configuration differences (names only), the narrative (or "Narrative unavailable") and the run link.
When the candidate's regressions exceed its wins beyond the noise threshold, `ci annotate` adds a
`::warning::` to the run (counts only); it never fails the job.

**Failure policy:** the `report` job is red only on infrastructure problems: a non-zero
`pepi-eval.sh` exit, tasks with infrastructure statuses, unknown grades or a missing
`summary.json`. Score differences never fail it; agent timeouts are listed but not red; a failed
narrate call only means "Narrative unavailable". `agent_error` counts as infrastructure although
the harness files it as an agent outcome: it means Pi itself exited non-zero (for example
`Unknown provider "litellm"`, a broken config or bad credentials), so the task got no real
attempt; narrate treats it the same way. The baseline is cached only when `ci verdict --single`
on it is clean.

**Security model:**
- The `pepi-evals` environment's required reviewers are the gate for the secrets; the label only
  decides when this workflow spends money.
- Same-repository pull requests only; no `pull_request_target`; every checkout uses
  `persist-credentials: false`; `${{ }}` expressions never appear inside `run:` scripts. Only
  `report` gets `pull-requests: write` (comment, label removal), and it runs on a GitHub-hosted
  runner. Keep "Allow GitHub Actions to create and approve pull requests" off.
- The PR's own code runs with the secrets (that is what is being evaluated), so approve only after
  reading its diff.
- The raw secrets reach only the `secrets-setup` steps; derived values (auth.json string leaves,
  `.env` values) are masked. `secrets-setup` writes `auth.json` into a **new** directory and a
  private sibling marker (`<directory>.pepi-ci-secrets.json`), keeping metadata out of the runner's
  strict secrets allowlist. It refuses to replace an existing marker, `.env` or mask file.
  `secrets-cleanup` (in an `always()` step) requires that marker to match the directory's path,
  device and inode, so pointing it at a real agent directory or a replacement removes nothing.
  `run-evals.sh` also unsets `PI_AUTH`/`EVAL_ENV` before starting `pepi-eval.sh`.
- These guards prevent accidents, not a malicious collaborator whose run you approve.
- Network: a plain Docker bridge named `pepi-eval`, i.e. **unrestricted** egress (accepted risk):
  the agent can reach GitHub and could send the gateway key anywhere. The comment says so.

**Runners:** `baseline` and `candidate` run on `EVAL_RUNS_ON`, by default a self-hosted runner
with the labels `self-hosted` and `pepi-evals`: the Verified20 task images total about 60 GB and
stay in that runner's Docker between runs. `lookup` and `report` always run on `ubuntu-latest`.
Self-hosted checklist:
- A dedicated machine or VM (not a workstation), Linux x86-64 recommended: the task images are
  x86-64, so arm64 and macOS hosts run them under emulation (much slower). Docker installed, the
  runner's user in the `docker` group, at least 100 GB free (the agent and grading images come on
  top of the task images).
- Run the runner as a dedicated unprivileged user with no personal data, SSH keys or cloud
  credentials on the host; prefer an ephemeral runner (`--ephemeral`, re-provisioned after each job).
  The PR's code ran there, so treat the host as untrusted after a run: secrets are written under
  `$RUNNER_TEMP` and removed by an `always()` step, but anything the job left elsewhere stays.
- Put the runner in a runner group restricted to this repository; keep the runner version updated;
  clean `_work` from time to time (checkouts persist between jobs; the jobs reset their own outputs).
- **One runner per Docker host:** `prune-images.sh` removes **every** `pepi-eval:*` agent image on
  that Docker host at the end of each eval job (task images `swebench/sweb.eval.*` stay, which is
  the point). Two runners sharing one Docker daemon would remove each other's images mid-run. With
  one runner, `baseline` and `candidate` run one after the other.
- Python 3.12 via `actions/setup-python` (works on Ubuntu and macOS runners; other distributions
  need it preinstalled in the runner's tool cache). The CI scripts are bash 3.2 compatible.

Hosted alternative: set `EVAL_RUNS_ON` to `["ubuntu-latest"]`. Then `free-disk.sh` removes
preinstalled toolchains and moves Docker's data-root to `/mnt` when it has 50 GB free, which may
still be too little for 60 GB of images; consider `EVAL_IDS` subsets. Lowering `EVAL_CPUS` for a
smaller runner changes the measurement (and the key). Both disk scripts refuse to run anywhere else
(exit 2, nothing executed): `free-disk.sh` needs `GITHUB_ACTIONS=true` and
`RUNNER_ENVIRONMENT=github-hosted`, `prune-images.sh` needs `GITHUB_ACTIONS=true`; neither takes
arguments.

**First run:** start with a smoke subset to measure wall time and peak disk, e.g.
`EVAL_IDS=sympy__sympy-16886,django__django-11095,sphinx-doc__sphinx-8551,scikit-learn__scikit-learn-25931`
(and `EVAL_CPUS` no higher than the runner's CPUs), then clear `EVAL_IDS`. A different CPU count is
a different measurement: those results do not compare with 4-CPU local runs.

**Cost and time:** each labeled run pays for one candidate eval, one baseline on a cache miss and
one narrate call; the local 20-task Verified runs cost about $0.74-0.92 per eval on the bedrock
profile. Grading with a task repo rebuilds every task image from its Dockerfile, which dominates
the time on a fresh runner; eval jobs time out after 350 minutes (waiting for an approval does not
count).

**Try the helpers locally** (no secrets, Docker or network):
```sh
python -m evals.swebench.ci assemble-config --repo . --profile bedrock --out /tmp/pepi-config
python -m evals.swebench.ci cache-key --config-dir /tmp/pepi-config --profile bedrock \
  --preset verified20-v1 --json
python -m evals.swebench.ci slim-run --run evals/swebench/runs/<run> --out /tmp/pepi-slim --kind handoff
python -m evals.swebench.ci comment --baseline evals/swebench/runs/<base> --candidate evals/swebench/runs/<cand> \
  --narrative none --sha 0123456 --pr 1 --run-url https://github.com/OWNER/REPO/actions/runs/1 \
  --profile bedrock --preset verified20-v1 --out /tmp/comment.md
python -m evals.swebench.ci annotate --baseline evals/swebench/runs/<base> --candidate evals/swebench/runs/<cand>
python -m unittest evals.swebench.tests.test_ci_workflow evals.swebench.tests.test_ci_helpers
```
`evals/ci/free-disk.sh` and `evals/ci/prune-images.sh` refuse to run outside GitHub Actions; do
not work around that on your machine: they delete preinstalled toolchains, restart Docker and
remove Docker images.

## Documentation

- [CI (GitHub Actions)](#ci-github-actions): label-triggered PR evals with a cached main baseline.
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

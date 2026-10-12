# Pepi SWE-bench evals

A separate isolated evaluation profile; your live Pi config is never edited.
Python 3.10+, Docker (Linux containers), an installed SWE-bench source checkout
with `swebench.task.repo`, and a clone of `SWE-bench/swe-bench-tasks` are required.
The initial image recipe targets Verified's Debian/Ubuntu, `/testbed`, Conda
`testbed` environments. Other benchmark/image layouts require another recipe.

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

`models.json` accepts provisioned-provider `modelOverrides` and model definitions
with literal id/name, reasoning, input, contextWindow, maxTokens, cost and supported
API metadata. Literal HTTP(S) base URLs must have no userinfo, query, fragment or
interpolation. Keys, headers, commands, environment references and other model
fields are refused without printing values. Codex's 500000-token context overrides
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
  --secrets-dir /path/to/eval-secrets --timeout-seconds 1200 --network pepi-eval
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
   Only required LiteLLM env credentials are forwarded, validated and redacted.
5. Known secret values (required env credentials, validated auth strings including
   OAuth rotations, and MCP strings of 8+ characters) are redacted from `attempts/<id>/**` and the patch; the count is
   `secret_redactions` in `attempt.json`. Redaction fails closed: symlinks, special
   files and files that cannot be read or redacted are deleted and listed under
   `removed_unredactable`. It is still best-effort: it does not see secrets inside
   binary git hunks, encoded values or partially overlapping values. The agent can
   read its key, so use a dedicated, revocable key, and upload only `predictions.jsonl`
   and `attempts/<id>/` directories that contain `attempt.json` (written after redaction).

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
Nothing automatically reads or copies your host `mcp.json`, caches or keychain.

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

**Remote servers:** supply a native `url` entry (type `http` or `streamable-http`)
in that same runtime file, with literal headers if needed. No npm manifest is needed.
Allowlist its endpoints externally; do not switch to host/default networking or
expose desktop servers. Loopback refers to the task container, not your host.
Only required LiteLLM env vars are forwarded; use literal dedicated MCP credentials
in the runtime file, not unspecified host env vars. Adapter imports, SSE, interactive
OAuth/keychain flows and `mcp-auth.json` injection are not supported.

Runtime MCP config is secret and deliberately not hashed/recorded; `execution.json`
records presence and the frozen provisioning policy only. Preserve the secret config
securely outside run artifacts and use a distinct config label when changing it.
Remote tool inventories/data can drift even with a fixed local image. Validate tools
and perform a one-task smoke test before comparing scores.

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
- that native MCP tools are actually listed/callable with the provisioned server under
  the chosen endpoint restrictions, and server processes stop on timeout;
- OAuth endpoint access/rotation on the pinned inspected Pi 1.1.0 auth-write contract,
  and that Docker accepts `uid=` on `--tmpfs`;
- installed package loading matches the frozen settings restored after `pi install`,
  and that `COPY --from=agent /usr/local/` keeps the task image working;
- that `pi` accepts `--append-system-prompt <file>` and `@/input/issue.md`, that
  `load_task_repo(path, ids)` has the assumed signature, and that `/testbed/.git` holds no
  refs or reflog entries after the baseline commit.

Python 3.10+ is checked at start-up. Run tests with `PYTHONDONTWRITEBYTECODE=1`.

## Interface references

- https://github.com/SWE-bench/SWE-bench/blob/main/swebench/task/repo.py
- https://www.swebench.com/SWE-bench/guides/evaluation/
- https://pi.dev/docs/latest/cli
- https://pi.dev/docs/latest/mcp
- https://pi.dev/packages/pi-provider-litellm

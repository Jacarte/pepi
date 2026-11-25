# pi agent config

My personal [pi](https://github.com/earendil-works/pi-coding-agent) configuration:
profiles, MCP servers, extensions, skills, prompt templates, and subagent workflows.

Clone this to `~/.pi/agent`, **apply a profile**, then configure local paths and
credentials before starting pi. The root `settings.json` and `mcp.json` are ignored
runtime overlays, not tracked defaults.

---

## Bootstrap on a new machine

### 1. Install pi, clone, and apply a profile

```bash
npm install -g @earendil-works/pi-coding-agent
git clone git@github.com:<you>/<this-repo>.git ~/.pi/agent
cd ~/.pi/agent
./profile.sh codex     # or: ./profile.sh bedrock / ./profile.sh gemini
# ./profile.sh        # interactive selector
```

If `~/.pi/agent` already exists, back it up or clone elsewhere first. Preserve
`auth.json` and other local state; do not overwrite them with someone else's files.

`profile.sh` copies every immediate regular, non-symlink file from the selected
profile into the script's directory. It does not merge JSON, launch pi, or remove
files absent from the selected profile:

| Profile | Files copied to root |
|---|---|
| `codex` | `settings.json`, `mcp.json`, `models.json` |
| `bedrock` | `settings.json`, `mcp.json` |
| `gemini` | `settings.json`, `mcp.json` |

The root `models.json` is tracked and currently matches the Codex profile's model
context-window overrides. Applying Bedrock or Gemini leaves it in place. Reapplying a profile
replaces local edits to the files it copies; edit `profiles/<name>/` to preserve
intentional profile changes.

### 2. Adapt machine-specific paths and services

These are personal profiles, not portable zero-configuration presets. Review the
selected profile before starting pi:

- These `settings.json` files contain absolute `subagentOnlyExtensions` and
  `defaultSubagentOnlyExtensions` paths under `/Users/javcab/.pi/agent/` (Codex)
  or `/Users/javier.cabrera/.pi/agent/` (Bedrock/Gemini).
  Replace them with your checkout's extension paths.
- The Bedrock MCP config contains `speaker` and `voice-devtools` commands under
  `/Users/javcab/Documents/maelstrom/voice-devtools`. Install/configure the matching
  tools and paths, or disable the servers you do not use.
- Review the memory endpoint below. Codex and Gemini point to a private LAN
  address; Bedrock points to localhost. Neither backend is included in this repository.
- `reviewer` references a `go-reviewer` skill that is **not tracked
  here**. Supply it separately or adjust that profile setting.

After editing a profile, apply it again. Do not commit credentials in profile files.

### 3. Provide credentials and environment variables

Provider credentials belong in local `auth.json` (gitignored). Configure auth for
`openai-codex` with the Codex profile, `litellm` with the Bedrock profile, or
`google` with the Gemini profile, using your installed pi/provider's authentication
instructions. Applying a profile does not configure authentication or verify model
access. Keep Google API credentials in local auth/environment settings, not in
the tracked Gemini profile.

The bundled `litellm-budget` extension reads a `litellm` auth entry of this shape:

```json
{
  "litellm": {
    "type": "api_key",
    "key": "sk-...",
    "env": { "LITELLM_BASE_URL": "https://your-litellm-gateway.example.com" }
  }
}
```

MCP credentials are separate: the tracked profiles use `{env:VAR}` references.
Export the variables for servers you keep enabled before starting pi:

```bash
export GITHUB_TOKEN="..."         # github (Codex, Bedrock, Gemini)
export CONTEXT7_API_KEY="..."     # context7
# export NEO4J_PASSWORD="..."     # neo4j (Bedrock)
# export ELEVENLABS_API_KEY="..." # speaker (Bedrock)
# export DEEPGRAM_API_KEY="..."   # speaker (Bedrock)
```

### 4. Start pi

```bash
pi
```

The selected root `settings.json` supplies the `packages` list for pi's startup
installation. Installed package directories are local, ignored state. Most package
specs are unpinned, so resolved versions can differ across machines; only
`pi-subagents` is pinned in these profiles.

---

## Profile packages and models

### Packages

These are the tracked package specifications, not a snapshot of locally installed
versions:

| Package spec | Codex | Bedrock | Gemini |
|---|---|---|---|
| `npm:pi-subagents@0.78.0` | yes | yes | yes |
| `npm:pi-mcp-adapter` | yes | yes | yes |
| `npm:@vndv/pi-codegraph` | yes | yes | yes |
| `npm:@dietrichgebert/ponytail` | yes | yes | yes |
| `npm:pi-ketch` | yes | yes | yes |
| `npm:pi-provider-litellm` | — | yes | — |
| `npm:pi-web-access` | — | yes | — |

LiteLLM is specific to Bedrock here; it is not required by every profile.

### Model settings

Model names below are configured IDs, not a guarantee of provider availability.
All seven tracked profiles (`codex`, `cloud`, `openai`, `bedrock`, `bedrock-sonnet`,
`gemini`, and `plain`) set parent startup thinking to `medium`. Profiles with subagents
also set `defaultThinking` and each enabled role's `thinking` to `medium`, so
bundled role defaults cannot retain an old high/low setting. The plain profile
still has no subagent configuration.
Codex, Bedrock, and Gemini use subagent project-root resolution `git-root`.

| Setting / agent | Codex model (`openai-codex`) | Codex initial effort | Bedrock model (`litellm`) | Gemini model (`google`) |
|---|---|---|---|---|
| Main default | `gpt-6-astra` | medium | `bedrock-claude-sonnet-5-5` | `gemini-3.8-flash` |
| Subagent default | `gpt-6.1-sol` | medium when no role effort is set | `bedrock-claude-sonnet-5-5` | `gemini-3.8-flash` |
| `worker` | `gpt-6.1-sol` | medium | `bedrock-claude-haiku-4-5` | `gemini-3.1-flash-lite` |
| `oracle`, `reviewer` | `gpt-6-astra` | medium | `bedrock-claude-opus-5-5` | `gemini-3.1-pro-preview` |
| `scout` | `gpt-6-luna` | medium | `bedrock-claude-haiku-4-5` | `gemini-3.1-flash-lite` |
| `delegate` | `gpt-6-luna` | medium | inherits subagent default | `gemini-3.1-flash-lite` |
| `researcher`, `evidence-auditor` | `gpt-6.1-sol` | medium | inherits subagent default | `gemini-3.8-flash` (auditor inherits) |
| `claude-code`, `claude-code-writer` | disabled | — | disabled | disabled |

Codex role `thinking` settings establish the listed initial effort. A mechanical writer can use
`model: "openai-codex/gpt-6-luna:medium"`; exploratory or coupled discovery can use
`model: "openai-codex/gpt-6.1-sol:medium"` when needed. Regular implementation
stays on Sol. Per-run model overrides take precedence over role pins, so use
explicit models deliberately. For one launch, put effort in the model suffix
(`:low`, `:medium`, `:high`, `:xhigh`): the dispatch `thinking` field is ignored by pinned
`pi-subagents@0.78.0`. Role-level `thinking` is supported. See the pinned
[model settings](https://github.com/nicobailon/pi-subagents/blob/v0.78.0/docs/models.md)
and [dispatch reference](https://github.com/nicobailon/pi-subagents/blob/v0.78.0/docs/tool-reference.md).

Codex parent, reviewer, and oracle use Astra as mandatory profile defaults.
This is standing operator authorization for those configured roles; no per-task
permission is required. Other Astra assignments require user authorization for a
named, bounded assignment, honored only within its stated scope. A previous Astra
task or failed worker does not authorize an Astra worker exception. Other
children select their own models; do not inherit Astra into later workers or
write global defaults for task-local exceptions. Other profiles retain their
own model choices.

After changing tracked settings, run `./profile.sh <profile>` (for example,
`./profile.sh codex`) and start a fresh Pi session so startup defaults apply;
a resumed session may retain its previous model or effort. Check
`/subagents-models` to inspect the live role mapping. Codex native
roles explicitly set `fast: false`, choosing standard child service by default.
A per-run `fast: true` can override this and requests priority service. These
child settings make no claim about the parent service tier. Model selection is
not a runtime allowlist; authorized task-local Astra remains possible.

For Gemini, apply `./profile.sh gemini` after configuring its local credentials
and paths, then start a fresh Pi session and check `/subagents-models`. Keep the
Gemini role assignments above; the Codex-specific Astra authorization and `fast`
settings do not replace them. Gemini's selected models and account access are
checked by Pi/provider at use time, not by the profile copy script.

### Subagent workflows

`pi-subagents` supports delegation with isolated child contexts. This repo contains
these workflow scripts:

| Workflow | Purpose |
|---|---|
| `workflows/pr-review.ts` | PR context → parallel reviews → synthesis. Never publishes; the parent owns user ACK. |
| `workflows/implement.ts` | T1 worker → verifier; T1R bounded repair; T2/T3 add planning, approval, and reviews. |

The shared scripts use the active profile's role defaults. For newly written
Codex workflows, specify the model on every child launch. A mechanical edit and
an ordinary review can use these options with complete, bounded task briefs:

```js
const edit = await runs.run("mechanical-edit", {
  agent: "worker",
  model: "openai-codex/gpt-6-luna:medium",
  context: "fresh",
  task: mechanicalTask
});
if (!edit.ok) return { edit };

const review = await runs.run("review-edit", {
  agent: "reviewer",
  model: "openai-codex/gpt-6-astra:medium",
  context: "fresh",
  task: reviewTask
});
return { edit, review };
```

Here `mechanicalTask` and `reviewTask` are briefs supplied by the caller. Include
exact source/scope, acceptance checks, and applicable permissions; give the reviewer the completed diff and check
evidence. Check its actual verdict before integration or publication. For normal
implementation use `openai-codex/gpt-6.1-sol:medium`. Any task-local model exception
belongs on its one named launch; later tasks select their own model and do not
resume that child for unrelated work.

`extensions/subagent/config.json` holds display settings (fleet view above the
editor, inline tool summaries) and model-response aliases.

---

## MCP inventory and external requirements

The Codex and Gemini profiles share the same MCP inventory. Bedrock adds voice,
Atlassian, tldraw, and Neo4j integrations:

| Server | Profiles / state | Configured transport and requirements |
|---|---|---|
| `context7` | all three, enabled | `https://mcp.context7.com/mcp`; `CONTEXT7_API_KEY` header |
| `browsermcp` | all three, enabled | `npx @browsermcp/mcp@0.1.3` |
| `playwright` | all three, enabled | `npx @playwright/mcp@0.0.68` |
| `speaker` | Bedrock, enabled | Absolute `tts2mic-mcp` path; `ELEVENLABS_API_KEY`, `DEEPGRAM_API_KEY` |
| `voice-devtools` | Bedrock, enabled | `bash -lc` in an absolute checkout path; `./bin/tts2mic-mcp` with PipeWire/session settings |
| `github` | all three, enabled | `https://api.githubcopilot.com/mcp`; Bearer `GITHUB_TOKEN`, `oauth: false` |
| `Atlassian` | Bedrock, enabled | `https://mcp.atlassian.com/v1/mcp`; OAuth |
| `tldraw` | Bedrock, enabled | `http://jacarte:8787/mcp`; local service |
| `neo4j` | Bedrock, enabled | `neo4j-mcp` on `PATH`, local `bolt://localhost:7687`, `NEO4J_PASSWORD`; read-only flag |
| `Chrome` | all three, disabled | `npx -y chrome-devtools-mcp@latest` |
| `codegraph` | all three, enabled | `codegraph serve --mcp` on `PATH` |

Install the external commands/services for servers you retain. `npx` entries need
Node/npm and access to their packages; this repo does not provision browser
runtimes, Neo4j, voice tooling, or remote-service accounts. The `codegraph` MCP
entry and the `@vndv/pi-codegraph` package are separate configuration entries.

---

## Local extensions

Local TypeScript extension entry points are tracked under `extensions/`:

| Extension | What it adds |
|---|---|
| `extensions/codex-usage-bar.ts` | Codex usage-window status via `codex app-server`; requires the Codex CLI for that status query |
| `extensions/litellm-budget.ts` | LiteLLM spend status and `/budget` |
| `extensions/hippocampus.ts` | Project-scoped recall and durable completed-work capture through `mem0_memory`; children load `extensions/subagent-only/hippocampus-readonly.ts` |
| `extensions/prompt-logger.ts` | Provider-request JSONL logging, including prompts/messages/tools; defaults to `logs/prompts.jsonl`. Disable with `PROMPT_LOG=0`, override with `PROMPT_LOG_FILE` |
| `extensions/top-status.ts` | Status widget above the editor and `/topbar` layout toggle |

The prompt logger redacts string values under secret-looking keys, **not all
sensitive text**. Treat its output as private. The profiles explicitly load the
logger into selected children via the machine-specific paths noted above.

## Semantic memory

Codex, Bedrock, Cloud, and Gemini use the local Hippocampus adapter and retain the
`mem0_memory` tool name. The parent entry point loads automatically from
`extensions/hippocampus.ts`. Every child extension list loads the explicit
read-only entry point before `mem0-fallback.ts`; children can recall memory but
cannot add, delete, capture, or replay writes.

The adapter requires the receipt and selection contract from
[Hippocampus PR #13](https://github.com/Jacarte/hippocampus/pull/13). Deploy that
backend before enabling capture; merging the PR does not update a running
service. Automatic capture requires Pi **1.1.0 or later** and checks the runtime
version. On older runtimes it is disabled with a warning; manual tools and recall
remain available. Integration checks use Pi 1.5.2.

The tracked endpoints are personal defaults, not bundled services:

| Profile | `hippocampus.baseUrl` |
|---|---|
| Codex | `http://192.168.0.160:18000` |
| Bedrock | `http://localhost:8000` |
| Cloud | `http://192.168.0.160:18000` |
| Gemini | `http://192.168.0.160:18000` |

All four profiles explicitly set the same behavior. For example, Codex contains:

```json
{
  "hippocampus": {
    "enabled": true,
    "baseUrl": "http://192.168.0.160:18000",
    "userId": "default-user",
    "autoCapture": true,
    "autoRecall": true,
    "requestTimeoutMs": 10000,
    "recallLimit": 5,
    "legacyUserIds": [],
    "recallLegacyCwd": true
  }
}
```

### Capture and recall

After work has finished and Pi has settled, the parent sends bounded, redacted
evidence from the user request, assistant responses, and tool results with
`infer: true`. Hippocampus decides which facts deserve durable memory. Aborted or
failed activities are not automatically captured. Compaction summaries are not
stored or used as capture triggers. Explicit `mem0_memory` adds use `infer: false`
for text already selected for memory, through the same durable write path.

Automatic and explicit recall use the same project scope. The adapter derives it
from `userId` and the normalized Git `origin`, so worktrees and clones with the
same remote share memory. Set an explicit `hippocampus.projectId` for a repository
without a usable origin, or to choose an identity shared across different remotes.
Without either identifier, memory is disabled rather than using an unscoped
`default-user`. Session and source details are provenance metadata. Recalled
content is untrusted data, not instructions.

`mem0_memory` with `action: "get_all"` returns a bounded eligible listing. The
backend returns at most 20 raw memories per scope, and eligibility exclusions can
make the returned list shorter; this is not an exhaustive memory export.

For older memory, `recallLegacyCwd: true` also searches the previous extension's
scope for the current absolute working directory. `legacyUserIds` can list
additional exact old user IDs for this project. Other machines' path hashes are
not discoverable automatically; only add IDs whose project you have verified.
New writes always use the stable scope. This adapter does not import the previous
extension's outbox.

### Durable writes and rollout

Before sending, the adapter saves the complete request in the private
`<agentDir>/hippocampus-outbox/` directory. Restarting Pi or starting another session
can replay eligible pending requests with their original capture ID, body, scope,
observation time, and destination. A configuration change never retargets them.
Only explicit `saved` or `skipped` receipts acknowledge a write; HTTP success alone
does not. Transport failures and retryable backend failures remain pending with
bounded backoff. Ambiguous outcomes remain held; capture conflicts remain failed
for investigation.
An HTTP-success response with an invalid or missing receipt pauses writes to that
exact destination across sessions until its compatibility pause is released below.
An ordinary `capture_pending` or unknown-outcome receipt holds only that capture.

Use `mem0_memory` with `action: "status"` to inspect pending, held, and failed
writes. `action: "retry"` retries eligible pending requests; it does not override
an ambiguous backend outcome or create a replacement capture ID. Redaction is
best effort: treat local outbox data and the configured backend as private.

Concurrent delivery uses per-request locks. Another live writer gets a grace
period of the request's `timeoutMs` plus 5 seconds, reported as
`waiting_for_active_writer`. If ownership remains unverifiable, status shows a
dynamic `held` state with `automaticRetry: false`, `lockFile`, and `lockRecovery`.
A matching PID with a different or missing process-instance marker pauses
immediately. This does not persist a capture-level hold: legitimate owner cleanup
releases it, and elapsed time never authorizes taking over an uncertain lock.

For a stuck lock, stop all Pepi writers sharing the outbox, inspect the reported
`lockFile`, and verify that its owner has ended and the lock is stale. Only then
remove that `<captureId>.json.lock` and its `<captureId>.json.lock.recovery` marker
if present. Preserve the `<captureId>.json` request, ID, destination, body, and
timeout unchanged. Restarting can retry that original request. This lock recovery
is separate from the incompatible-receipt recovery below, which leaves the
original capture held.

To release a compatibility pause after upgrading the backend:

1. Use `mem0_memory` with `action: "status"` to identify captures held for
   `missing_or_invalid_write_receipt` at the affected destination. Stop all Pepi
   sessions sharing this `agentDir` before editing their outbox records.
2. Verify the upgraded endpoint independently in a separate test user/project
   scope with a fresh `capture_id`: `POST /memories` must return `write_status`
   `saved` or `skipped` and the matching capture ID. This confirms compatibility;
   it does not establish whether the original uncertain write was saved.
3. For that verified destination's blocking records in
   `<agentDir>/hippocampus-outbox/<captureId>.json`, change only the mutable
   `reason` from `missing_or_invalid_write_receipt` to
   `contract_verified_capture_unresolved`. Preserve `state: "held"`, `captureId`,
   `destination`, `body`, `timeoutMs`, and every other field unchanged.
4. Restart Pepi and inspect status. New independent captures can proceed; the old
   capture remains held and `retry` cannot send it. Reconcile that original write
   separately before any further recovery action. Do not delete its record,
   replace its capture ID, or mark it pending to bypass the uncertainty.

When upgrading an existing installation:

1. Pull the repository, including the new extension files. Adapt the selected
   profile's absolute child paths and backend endpoint to this machine.
2. Reapply that profile with `./profile.sh <name>`; root `settings.json` is an
   ignored overlay. The script copies settings but does not install extension
   files or uninstall packages.
3. Remove any remaining `@amaster.ai/pi-memory-mem0` package registration or
   explicit extension path from project/local settings so it cannot load a second
   writer. Restart Pi.
4. In an isolated test project scope, complete a small activity, inspect
   `mem0_memory` status, and verify recall in a new session before relying on the
   integration. Confirm the backend is reachable and returns the new receipts.

---

## What's in here

| Path | Purpose |
|---|---|
| `profiles/` | Tracked profile settings and MCP inventory |
| `profile.sh` | Applies a profile overlay; no pi launch |
| `models.json` | Tracked Codex model context-window overrides |
| `mcp-onboarding.json` | Tracked adapter onboarding flags |
| `AGENTS.md`, `agents/` | Agent instructions and local agent definitions |
| `extensions/` | Local extension sources and subagent display config |
| `prompts/` | Prompt templates, including `/pr-review` |
| `workflows/` | Subagent workflow scripts |
| `skills/kanllo-board/SKILL.md` | The only tracked skill; internal Kanllo board workflow (requires its API, bash, curl, jq) |
| `.dagger/` | Evaluation harnesses written in Go with dagger (see [Evals](#evals)) |

Other locally installed or untracked skill directories are not part of a fresh
clone. In particular, `go-reviewer` is referenced by the profiles but not supplied
here. Do not move or overwrite local skills just to match this repository.

## Evals

The `daggervals` Dagger module in `.dagger/modules/daggervals` runs this setup (profile,
`AGENTS.md`, extensions, memory) on fixed tasks and grades the result by tests:

- Rust and Go bug fixes from open source repos, and a `hello` smoke test, live in this repo.
- Long, parallel and memory tasks live in the cross-reference repo
  [pepi-eval-tasks](https://github.com/Jacarte/pepi-eval-tasks). The evals fetch it at a pinned
  commit (`eval-tasks.ref`).
- [docs/evals-tasks.md](docs/evals-tasks.md) explains each task, what it measures, the results
  so far, and how an eval runs end to end (architecture diagrams).
- [docs/ci-eval.md](docs/ci-eval.md) covers the PR labels `eval` and `eval-full`, the approval,
  and the baselines.

One eval-only ablation flag on `evals`: `--force-delegation` appends a mandatory delegation section to
`AGENTS.md` (implementation by a `worker`, review by a `reviewer`). It is off by default, and a baked image
rejects it. The run id gets `@forced`, and the setup id changes with it, so those runs never share a cache
entry with the default ones.

## What is deliberately not tracked

See `.gitignore` for the full list:

- `auth.json`, `auth.json.bak`, `.env*` — credentials and local environment.
- `trust.json` — machine-specific trust decisions.
- Root `settings.json`, `mcp.json` — copied profile overlays.
- `npm/`, `git/`, `node_modules/`, `bin/` — installed packages and helpers.
- `sessions/`, `missions/`, `run-history.jsonl`, `logs/` — private runtime state/logs.
- `litellm-models-dev.json`, `models-store.json`, MCP caches, `web-search-cache/` — caches.
- Root `package.json` / `package-lock.json` — local npm stubs.
- `litellm-mcp-pauses/` — runtime state, including identity keys.

Unlike those overlays and caches, root `models.json` is tracked. Review diffs after
switching profiles rather than hiding them with `skip-worktree`.

## Verifying a fresh clone

After applying and adapting your profile:

```bash
pi list            # inspect packages resolved from the selected settings
pi -p "say OK"    # smoke-test provider auth/model access
```

Inside an interactive pi session, inspect `/mcp` connection status and call
`mem0_memory` with `action: "status"` if the corresponding extensions loaded.
Use `/budget` only when a LiteLLM gateway
is configured. Check failures against the selected root config, installed package
versions, service availability, and credentials; a loaded client does not prove a
backend is healthy.

## Security

Extensions and skills can execute with system permissions. Review them before use.
Profile files contain local paths and service addresses; environment references
avoid embedding MCP credentials, not all machine-specific details. Keep auth files,
logs, session data, and tokens out of commits. Review `git diff --cached` before
pushing; secret-pattern scans alone do not establish that a change is safe.

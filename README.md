# pi agent config

My personal [pi](https://github.com/earendil-works/pi-coding-agent) configuration:
profiles, MCP servers, extensions, skills, prompt templates, and subagent workflows.

Clone this to `~/.pi/agent`, **apply a profile**, then configure local paths and
credentials before starting pi. The root `settings.json`, `mcp.json`, and
`models-router.json` are ignored runtime overlays, not tracked defaults.

---

## Bootstrap on a new machine

### 1. Install pi, clone, and apply a profile

```bash
npm install -g @earendil-works/pi-coding-agent
git clone git@github.com:<you>/<this-repo>.git ~/.pi/agent
cd ~/.pi/agent
./profile.sh codex     # or: ./profile.sh bedrock
# ./profile.sh        # interactive selector
```

If `~/.pi/agent` already exists, back it up or clone elsewhere first. Preserve
`auth.json` and other local state; do not overwrite them with someone else's files.

`profile.sh` copies every immediate regular, non-symlink file from the selected
profile into the script's directory. It does not merge JSON, launch pi, or remove
files absent from the selected profile:

| Profile | Files copied to root |
|---|---|
| `codex` | `settings.json`, `mcp.json`, `models-router.json`, `models.json` |
| `bedrock` | `settings.json`, `mcp.json`, `models-router.json` |

The root `models.json` is tracked and currently matches the Codex profile's model
context-window overrides. Applying Bedrock leaves it in place. Reapplying a profile
replaces local edits to the files it copies; edit `profiles/<name>/` to preserve
intentional profile changes.

### 2. Adapt machine-specific paths and services

These are personal profiles, not portable zero-configuration presets. Review the
selected profile before starting pi:

- Both `settings.json` files contain absolute `subagentOnlyExtensions` and
  `defaultSubagentOnlyExtensions` paths under `/Users/javier.cabrera/.pi/agent/`.
  Replace them with your checkout's extension paths.
- Both MCP configs contain `speaker` and `voice-devtools` commands under
  `/Users/javcab/Documents/maelstrom/voice-devtools`. Install/configure the matching
  tools and paths, or disable the servers you do not use.
- Review the memory endpoint below. Codex points to a private LAN address;
  Bedrock points to localhost. Neither backend is included in this repository.
- `reviewer` references a `go-reviewer` skill that is **not tracked
  here**. Supply it separately or adjust that profile setting.

After editing a profile, apply it again. Do not commit credentials in profile files.

### 3. Provide credentials and environment variables

Provider credentials belong in local `auth.json` (gitignored). Configure auth for
`openai-codex` with the Codex profile, or `litellm` with the Bedrock profile, using
your installed pi/provider's authentication instructions.

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
export GITHUB_TOKEN="..."         # github (both profiles)
export NEO4J_PASSWORD="..."       # neo4j
export ELEVENLABS_API_KEY="..."   # speaker
export DEEPGRAM_API_KEY="..."     # speaker
# export CONTEXT7_API_KEY="..."   # only if enabling context7
# export TYPESAFE_API_KEY="..."   # local jev_route tool (not MCP)
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

| Package spec | Codex | Bedrock |
|---|---|---|
| `npm:pi-subagents@0.78.0` | yes | yes |
| `npm:pi-mcp-adapter` | yes | yes |
| `npm:@vndv/pi-codegraph` | yes | yes |
| `npm:@amaster.ai/pi-memory-mem0` | yes | yes |
| `npm:@dietrichgebert/ponytail` | yes | yes |
| `npm:pi-ketch` | yes | — |
| `npm:pi-provider-litellm` | — | yes |
| `npm:pi-web-access` | — | yes |

LiteLLM is specific to Bedrock here; it is not required by every profile.

### Model settings

Model names below are configured IDs, not a guarantee of provider availability.
Both profiles set thinking level `high` and subagent project-root resolution
`git-root`.

| Setting / agent | Codex (`openai-codex`) | Bedrock (`litellm`) |
|---|---|---|
| Main default | `gpt-6-luna` | `bedrock-claude-sonnet-5-5` |
| Subagent default | `gpt-6.1-sol` | `bedrock-claude-sonnet-5-5` |
| `oracle`, `reviewer` | `gpt-6-astra` | `bedrock-claude-opus-5-5` |
| `worker`, `scout` | `gpt-6-luna` | `bedrock-claude-haiku-4-5` |
| `delegate` | `gpt-6-luna` | inherits subagent default |
| `researcher` | `gpt-6.1-sol` | inherits subagent default |
| `claude-code`, `claude-code-writer` | disabled | disabled |

`models-router.json` is a separate mapping for the local `jev_route` tool:

| Lane | Codex | Bedrock |
|---|---|---|
| `LOCAL` | `openai-codex/gpt-6-luna` | `litellm/bedrock-claude-haiku-5-5` |
| `CHEAP` | `openai-codex/gpt-6.1-sol` | `litellm/bedrock-claude-sonnet-5-5` |
| `FRONTIER` | `openai-codex/gpt-6-astra` | `litellm/bedrock-claude-opus-5-5` |

Each lane requires a nonempty `provider` and `id`. Each file also requires a
top-level numeric `minConfidence` in `[0,1]` (currently **0.4** in both profiles;
the Codex value may be tuned separately). Missing, nonfinite, nonnumeric, or
out-of-range values fail before classification. Edit the tracked profile files to preserve changes
across `profile.sh` switches; `recommend` reads the active root config on each call.

For `jev_route` action `recommend`, Jev answers two Choice questions about the
supplied `role`, `task`, `context` and `files`: `lane` (`local`, `cheap`, `frontier`)
and `blast_radius` (`contained`,
`cross_cutting`, `critical`). The lane choice is used unless a rule raises it, in
this order:

1. lane confidence below `minConfidence` → `FRONTIER` (`low_confidence`);
2. `critical` blast radius with blast-radius confidence at or above
   `minConfidence` → `FRONTIER` (`critical`). Unlike rule 1, exactly
   `minConfidence` counts as confident here (`>=`, whereas the lane rule is
   strict `<`);
3. `critical` blast radius with confidence below `minConfidence` → at least
   `CHEAP` (`uncertain_critical_floor`), not `FRONTIER`;
4. `cross_cutting` blast radius → at least `CHEAP` (`cross_cutting_floor`).

Rules never lower a lane. The result reports `escalated` and `reason` (the first
rule that raised the lane, or `null`). Invalid classifier answers fail; there is
no fallback lane.

The Bedrock router's `LOCAL` ID differs from the worker/scout override. Both router
files also contain `HUMAN`, which the extension deliberately ignores; routing is
not approval.

`jev_route` action `switch_effort` makes another Jev call with a separate `effort`
Choice question about the supplied task/role/context/files. It does not reuse the
lane decision or read `models-router.json`. Jev selects `off`, `minimal`, `low`,
`medium`, `high`, `xhigh`, or `max`; Pi's `setThinkingLevel` clamps that request to
the current model's supported levels (non-reasoning models use `off`). The tool
reads back and reports `requestedEffort`, `actualEffort`, and `changed`. It never
switches models. Stale session/model decisions and invalid responses fail explicitly.

Before substantive work, the native Pi parent and every native Pi subagent role
must call `switch_effort` for their own assignment, even after a delegation
recommendation.
This is instruction-based policy, not runtime blocking. The requirement is also
in the tool description because children may not inherit global `AGENTS.md`.
`recommend` remains required before authorized implementation delegation; pass
its model ID unchanged when launching the child. Reclassify only when the
assignment or evidence changes materially, and report classification failures.
The tracked Bedrock, Codex, and Cloud profiles preserve router availability for
all eight native Pi subagent roles through `jev_route` allowlists and
explicit/default `subagentOnlyExtensions`, including when reapplying a profile.
Existing capability restrictions still apply; adapt the absolute extension paths
to your machine. External CLI runtimes (`claude-code`, `claude-code-writer`) are
outside this Pi extension's scope.

### Subagent workflows

`pi-subagents` supports delegation with isolated child contexts. This repo contains
these workflow scripts:

| Workflow | Purpose |
|---|---|
| `workflows/pr-review.ts` | PR context → parallel reviews → synthesis. Never publishes; the parent owns user ACK. |
| `workflows/implement.ts` | T1 worker → verifier; T1R bounded repair; T2/T3 add planning, approval, and reviews. |

`extensions/subagent/config.json` holds display settings (fleet view above the
editor, inline tool summaries) and model-response aliases.

---

## MCP inventory and external requirements

The two tracked `mcp.json` files share the following inventory, except for ClickUp.
There are no Nucleus, Atlassian, or Datadog entries in either profile.

| Server | Profiles / state | Configured transport and requirements |
|---|---|---|
| `context7` | both, disabled | `https://mcp.context7.com/mcp`; `CONTEXT7_API_KEY` header |
| `browsermcp` | both, enabled | `npx @browsermcp/mcp@0.1.3` |
| `playwright` | both, enabled | `npx @playwright/mcp@0.0.68` |
| `speaker` | both, enabled | Absolute `tts2mic-mcp` path; `ELEVENLABS_API_KEY`, `DEEPGRAM_API_KEY` |
| `voice-devtools` | both, enabled | `bash -lc` in an absolute checkout path; `./bin/tts2mic-mcp` with PipeWire/session settings |
| `github` | both, enabled | `https://api.githubcopilot.com/mcp`; Bearer `GITHUB_TOKEN`, `oauth: false` |
| `clickup` | Codex only, enabled | `npx -y mcp-remote https://mcp.clickup.com/mcp` |
| `neo4j` | both, enabled | `neo4j-mcp` on `PATH`, local `bolt://localhost:7687`, `NEO4J_PASSWORD`; read-only flag |
| `Chrome` | both, disabled | `npx -y chrome-devtools-mcp@latest` |
| `codegraph` | both, enabled | `codegraph serve --mcp` on `PATH` |

Install the external commands/services for servers you retain. `npx` entries need
Node/npm and access to their packages; this repo does not provision browser
runtimes, Neo4j, voice tooling, or remote-service accounts. The `codegraph` MCP
entry and the `@vndv/pi-codegraph` package are separate configuration entries.

---

## Local extensions

Five TypeScript extension entry points are tracked under `extensions/`:

| Extension | What it adds |
|---|---|
| `extensions/codex-usage-bar.ts` | Codex usage-window status via `codex app-server`; requires the Codex CLI for that status query |
| `extensions/litellm-budget.ts` | LiteLLM spend status and `/budget` |
| `extensions/pi-typesafe-router.ts` | `jev_route`: model recommendation using `models-router.json`, or separate thinking-effort classification/application without changing models; calls `https://api.typesafe.ai/v1/systemone` with `TYPESAFE_API_KEY` (`JEV_MODEL` optionally selects classifier) |
| `extensions/prompt-logger.ts` | Provider-request JSONL logging, including prompts/messages/tools; defaults to `logs/prompts.jsonl`. Disable with `PROMPT_LOG=0`, override with `PROMPT_LOG_FILE` |
| `extensions/top-status.ts` | Status widget above the editor and `/topbar` layout toggle |

The prompt logger redacts string values under secret-looking keys, **not all
sensitive text**. Treat its output as private. The profiles explicitly load the
logger/router into selected children via the machine-specific paths noted above.

## Semantic memory

Both profiles include `@amaster.ai/pi-memory-mem0` in `self-hosted` mode. These are
the actual tracked settings:

| Field | Codex | Bedrock |
|---|---|---|
| `mode` | `self-hosted` | `self-hosted` |
| `baseUrl` | `http://192.168.0.160:18000` | `http://localhost:8000` |
| `userId` | `default-user` | `default-user` |
| `userIdScope` | `userIdScope` | `userIdScope` |

For example, the Codex profile contains:

```json
{
  "pi-memory-mem0": {
    "mode": "self-hosted",
    "baseUrl": "http://192.168.0.160:18000",
    "userId": "default-user",
    "userIdScope": "userIdScope"
  }
}
```

`userIdScope` is literally `"userIdScope"` in both files, not `"exact"`. Validate
that value against your installed extension before relying on memory isolation.
`memoryMode`, `topK`, `recallFrequency`, and `requestTimeoutMs` are not explicitly
set; this repository does not pin their behavior through a package version.

The memory backend (previously described here as hippocampus) is external to this
repo. Its deployment, API compatibility, authentication, extraction keys, and
failure behavior must be checked in that backend and the installed extension;
the old localhost-only examples and backend version claims are not portable setup
instructions. Set a reachable, trusted endpoint before enabling memory use, and
treat recalled content as untrusted data.

---

## What's in here

| Path | Purpose |
|---|---|
| `profiles/` | Tracked profile settings, MCP inventory, and router model mappings |
| `profile.sh` | Applies a profile overlay; no pi launch |
| `models.json` | Tracked Codex model context-window overrides |
| `mcp-onboarding.json` | Tracked adapter onboarding flags |
| `AGENTS.md`, `agents/` | Agent instructions and local agent definitions |
| `extensions/` | Local extension sources and subagent display config |
| `prompts/` | Prompt templates, including `/pr-review` |
| `workflows/` | Subagent workflow scripts |
| `skills/kanllo-board/SKILL.md` | The only tracked skill; internal Kanllo board workflow (requires its API, bash, curl, jq) |
| `.dagger/` | Evaluation harnesses written in Go with dagger |

Other locally installed or untracked skill directories are not part of a fresh
clone. In particular, `go-reviewer` is referenced by the profiles but not supplied
here. Do not move or overwrite local skills just to match this repository.

## What is deliberately not tracked

See `.gitignore` for the full list:

- `auth.json`, `auth.json.bak`, `.env*` — credentials and local environment.
- `trust.json` — machine-specific trust decisions.
- Root `settings.json`, `mcp.json`, `models-router.json` — copied profile overlays.
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

Inside an interactive pi session, inspect `/mcp` connection status and `/mem0 status`
if the corresponding extensions loaded. Use `/budget` only when a LiteLLM gateway
is configured. Check failures against the selected root config, installed package
versions, service availability, and credentials; a loaded client does not prove a
backend is healthy.

## Security

Extensions and skills can execute with system permissions. Review them before use.
Profile files contain local paths and service addresses; environment references
avoid embedding MCP credentials, not all machine-specific details. Keep auth files,
logs, session data, and tokens out of commits. Review `git diff --cached` before
pushing; secret-pattern scans alone do not establish that a change is safe.

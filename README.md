# pi agent config

My personal [pi](https://github.com/earendil-works/pi-coding-agent) configuration: settings, MCP servers, extensions, skills, prompt templates, and subagent workflows.

Clone this to `~/.pi/agent` and pi picks everything up on next start.

---

## Required environment variables

No hostnames or credentials are hardcoded in this repo — `mcp.json` reads everything
via `{env:VAR}`. Export these in your shell profile (`~/.zshrc`) before starting pi:

```bash
# Required
export LLM_GATEWAY_URL="https://your-litellm-gateway.example.com"  # no trailing slash
export LLM_GATEWAY_API_KEY="sk-..."
export GITHUB_TOKEN_READONLY="ghp_..."

# Optional — only if you use the matching MCP server
export NEO4J_PASSWORD="..."        # neo4j
export ELEVENLABS_API_KEY="..."    # speaker (TTS)
export CONTEXT7_API_KEY="..."      # context7 (currently disabled in mcp.json)

# Optional — consumed by the hippocampus stack, not by pi itself
export OPENAI_API_KEY="sk-..."     # mem0 fact extraction; see Semantic memory below
```

| Variable | Needed by | Required | Missing behavior |
|---|---|---|---|
| `LLM_GATEWAY_URL` | DevCenter MCP (`url`), `litellm-budget` | **yes** | DevCenter throws at startup |
| `LLM_GATEWAY_API_KEY` | DevCenter MCP (`headers`), `litellm-budget` | **yes** | silent 401 |
| `GITHUB_TOKEN_READONLY` | GitHub MCP (`headers`) | **yes** | silent 401 |
| `NEO4J_PASSWORD` | neo4j MCP (`args`) | if using neo4j | auth rejected |
| `ELEVENLABS_API_KEY` | speaker MCP (`env`) | if using speaker | TTS fails |
| `CONTEXT7_API_KEY` | context7 MCP (`headers`) | no — server disabled | n/a |
| `OPENAI_API_KEY` | hippocampus server (mem0 extraction) | if using semantic memory | writes silently store nothing |

Gotchas, both verified against the MCP adapter source:

1. **`LLM_GATEWAY_URL` must not end in `/`.** It is concatenated as
   `{env:LLM_GATEWAY_URL}/DevCenter/mcp`, so a trailing slash yields a double slash.
2. **Missing vars fail asymmetrically.** In a `url` they throw a clear startup error
   (`Missing environment variable in MCP server URL: ...`) and only that one server
   fails. In `headers`, `env`, or `args` they interpolate to an **empty string**, so the
   server starts and then fails auth with a confusing 401. There is no default-value
   syntax.

Credentials themselves live in `auth.json` (gitignored) — see step 2 below. Note that pi
does **not** export `auth.json`'s `env` block into the process environment, so
`LLM_GATEWAY_URL` must be exported by your shell even if `auth.json` already has
`LITELLM_BASE_URL`.

---

## Bootstrap on a new machine

### 1. Install pi and clone the config

```bash
# Install pi (see upstream README for other install methods)
npm install -g @earendil-works/pi-coding-agent

# This repo lives at pi's config dir
git clone git@github.com:<you>/<this-repo>.git ~/.pi/agent
```

If `~/.pi/agent` already exists, clone elsewhere and copy the tracked files in, or
init the remote inside the existing folder — don't delete `auth.json` if you still want your keys.

### 2. Provide credentials

Credentials are **not** in this repo. Two things to set up:

**Provider auth** (`auth.json`, gitignored) — create it with:

```bash
pi auth   # check provider readiness / print credentials
```

Or write `~/.pi/agent/auth.json` by hand:

```json
{
  "litellm": {
    "type": "api_key",
    "key": "sk-...",
    "env": { "LITELLM_BASE_URL": "https://your-litellm-gateway.example.com" }
  }
}
```

**Environment variables** — see [Required environment variables](#required-environment-variables)
at the top for the full list and copy-paste block.

The `litellm-budget` extension resolves independently of `mcp.json`, preferring the
provider's own vars and falling back to `auth.json`:

- key: `LITELLM_API_KEY` → `LLM_GATEWAY_API_KEY` → `TRUSTLY_LLM_GATEWAY_API_KEY` (legacy) → `auth.json`
- url: `LITELLM_BASE_URL` → `LLM_GATEWAY_URL` → `auth.json`

If no URL resolves it shows `budget: no gateway URL` rather than guessing a host.

### 3. Start pi — packages install themselves

```bash
pi
```

**You do not install packages one by one.** `settings.json` has a `packages` array, and
pi installs any missing entries automatically on startup. A bare config dir with just
`settings.json` will fetch the whole dependency tree on first run.

Useful commands:

```bash
pi list                 # show packages from settings
pi update --extensions  # update packages + reconcile pinned git refs
pi update --all         # pi itself + packages
pi install npm:foo      # add a package (writes to settings.json)
pi remove npm:foo
pi config               # TUI to enable/disable individual package resources
```

### 4. External binaries (not managed by pi)

Some MCP servers shell out to commands that must be on `PATH`:

| Command | Server | Install |
|---|---|---|
| `neo4j-mcp` | neo4j | `brew install neo4j-mcp` (plus a running neo4j) |
| `nuc-mcp` | Nucleus | internal tooling |
| `codegraph` | codegraph MCP + `@vndv/pi-codegraph` | `npm i -g @colbymchenry/codegraph`, then `codegraph init -i` per project |
| `tts2mic-mcp` | speaker | expects `~/tts2mic-mcp/tts2mic-mcp` |

`npx`-based servers (browsermcp, playwright, Chrome) need no install. Remote servers
(Atlassian, Datadog) use OAuth — run `/mcp` in pi to authenticate.

---

## Installed extensions

These are the `packages` entries in `settings.json`. pi installs them automatically on
startup — nothing to install by hand.

| Package | Version | What it adds |
|---|---|---|
| **`pi-subagents`** | 0.68.0 | Delegation to child agents, scripted workflows, background jobs. **See below.** |
| `pi-provider-litellm` | 3.0.1 | The LiteLLM provider — without it there are no models at all |
| `pi-mcp-adapter` | 2.34.0 | Reads `mcp.json` and exposes every MCP server as tools |
| `pi-web-access` | 0.29.0 | `web_search`, `fetch_content`, GitHub/PDF/YouTube extraction |
| `@vndv/pi-codegraph` | 0.1.10 | `codegraph_*` structural code queries (symbols, callers, impact) |
| `@amaster.ai/pi-memory-mem0` | 0.1.16 | Semantic memory — `mem0_memory` tool, `/mem0` command, automatic capture and recall. **See below.** |

Plus one local extension in this repo, auto-discovered from `extensions/`:

| Extension | What it adds |
|---|---|
| `extensions/litellm-budget.ts` | Footer gauge of LiteLLM spend + `/budget` command |

### Why `pi-subagents` is the important one

The other four packages extend what the agent can *reach* — more models, more servers,
more search, more code queries. `pi-subagents` changes the **shape** of the work: it turns
a single linear conversation into something that can fan out, run in parallel, and review
itself.

What that buys in practice:

1. **Context isolation.** Each child gets a fresh context window. A scout can read 40
   files and hand back a 20-line summary without spending the parent's context on all 40.
   This is the difference between finishing a large task and hitting a compaction wall.
2. **Independent review.** A fresh-context reviewer has no memory of the reasoning that
   produced the code, so it can't rubber-stamp its own assumptions. Self-review in one
   context is much weaker.
3. **Parallelism.** Several reviewers or auditors run at once against the same diff, each
   with a narrow mandate (correctness / tests / complexity).
4. **Per-role model economics.** Cheap fast models for recon, expensive models for
   judgment — configured in `settings.json` rather than chosen ad hoc.
5. **Reusable workflows.** `workflows/*.ts` are committed, versioned scripts, so a
   multi-step review pipeline is reproducible instead of re-improvised each time.

This repo's `settings.json` assigns models by role, which is where most of the value is:

| Agent | Model | Rationale |
|---|---|---|
| `oracle`, `reviewer` | `bedrock-claude-opus-5` | Judgment work — worth the cost |
| `worker`, `scout` | `bedrock-claude-sonnet-5` | Volume work — speed and cost matter |
| `claude-code`, `claude-code-writer` | disabled | Not used here |

It also ships builtin agents usable in plain language ("use reviewer on this diff",
"ask oracle for a second opinion"): `scout`, `worker`, `reviewer`, `oracle`, `researcher`,
`evidence-auditor`, `delegate`, plus external-CLI bridges for Codex and Cursor that stay
inactive unless those CLIs are on `PATH`.

Committed workflows in `workflows/`:

| Workflow | Purpose |
|---|---|
| `pr-review.ts` | PR context → parallel reviews → synthesis. Never publishes; the parent owns user ACK. |
| `implement.ts` | Tiered implementation: T1 `worker`→verifier, up to T2/T3 adding `scout`→`oracle` plan→approval→parallel reviewers |

`extensions/subagent/config.json` holds the display config (fleet view above the editor,
inline tool summaries) and short model aliases used in child output.

**Honest caveat:** the *most load-bearing* package is `pi-provider-litellm` — remove it
and nothing runs, since every model in `settings.json` is served through the LiteLLM
gateway. `pi-subagents` is the highest-*leverage* one: it changes how work gets done
rather than whether it can run at all.

### Semantic memory: `pi-memory-mem0` → hippocampus

`@amaster.ai/pi-memory-mem0` gives pi memory that survives across sessions. It runs in
**`self-hosted` mode** against [**hippocampus**](https://github.com/Jacarte/hippocampus) —
my own FastAPI backend over [`mem0ai`](https://github.com/mem0ai/mem0) — so no
conversation data goes to Mem0 Cloud.

```
  pi  ── mem0_memory tool / automatic capture+recall
   │
   │    POST /search, POST /memories, GET /memories, DELETE /memories/{id}
   ▼
  hippocampus  (localhost:8000, FastAPI + mem0ai 2.0.7)
   ├─ pgvector   vectors + payloads   (postgres:5432)
   ├─ LLM        fact extraction      (LiteLLM gateway or OpenAI)
   ├─ embedder   ollama or OpenAI
   └─ CMS        admin UI             (localhost:8080)
```

The extension has three backend modes — `platform` (Mem0 Cloud), `embedded` (in-process
SQLite), and `self-hosted`. This config uses the third:

```json
"pi-memory-mem0": {
  "mode": "self-hosted",
  "baseUrl": "http://localhost:8000",
  "userId": "default-user",
  "userIdScope": "exact"
}
```

| Field | Why this value |
|---|---|
| `mode` | `self-hosted` keeps memory traffic on my own infrastructure |
| `baseUrl` | hippocampus' documented local default; it serves `/memories` and `/search`, **not** the Platform `/v1` paths |
| `userId` | the memory scope — all reads and writes land here |
| `userIdScope` | `exact` uses `userId` verbatim. The default `project` appends a cwd hash, which silos memories per directory |

Defaults that are not set explicitly, and therefore apply: `memoryMode: hybrid`
(auto-capture + auto-recall + tool all on), `topK: 5`, `recallFrequency: user-input`,
`requestTimeoutMs: 30000`.

**What it adds to a session**

| Surface | Usage |
|---|---|
| `mem0_memory` tool | `search` / `add` / `get_all` / `delete` — agent-initiated |
| `/mem0` command | `status`, `search <q>`, `profile`, `add <text>`, `dedup [--apply]`, `delete <id>` |
| Automatic capture | each user+assistant turn is sent for fact extraction |
| Automatic recall | matching memories are injected before the agent starts |

Recalled text arrives on the **user channel wrapped as `[UNTRUSTED MEMORY DATA]`**, never
in the system prompt, and entries matching prompt-injection patterns are replaced with
`[BLOCKED UNTRUSTED MEMORY: ...]`. Credentials are redacted before storage. Treat recalled
content as untrusted input — it is data, not instruction.

#### Running the backend

The extension is a **client only**. With no server reachable at `baseUrl`, every memory
operation fails and the rest of pi keeps working.

```bash
git clone git@github.com:Jacarte/hippocampus.git ~/DEV/hippocampus
cd ~/DEV/hippocampus
cp .env.example .env     # set OPENAI_API_KEY; review the rest
./start.sh               # docker compose up
curl localhost:8000/health
```

| Service | Port | Purpose |
|---|---|---|
| server | 8000 | API — `/docs`, `/health`, `/metrics` |
| CMS | 8080 | admin UI for browsing/editing memories |
| postgres | 5432 | pgvector store |
| prometheus | 9090 | metrics scrape |
| grafana | 3000 | dashboards |

The compose stack has **no authentication** on its admin endpoints — run it on a trusted
network only.

#### Version coupling

hippocampus pins `mem0ai==2.0.7`. The 2.0 series made entity IDs (`user_id`, `agent_id`,
`run_id`) live **inside** the `filters` dict on `get_all`/`search` rather than as top-level
kwargs, and this extension sends the nested form — matching current upstream, where the
top-level fields are deprecated. A server that accepts only the old flat shape returns:

```
400  filters must contain at least one of: user_id, agent_id, run_id
```

If you see that, the server predates the fix; `SearchRequest`/`RetrieveRequest` must
promote nested entity IDs to the top level. Verify with:

```bash
curl -s localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"test","filters":{"user_id":"default-user"}}'
```

#### Troubleshooting

| Symptom | Cause |
|---|---|
| `Mem0 request failed (400)` | server rejects nested `filters` entity IDs — see above |
| `Mem0 request failed` / timeout | nothing listening on `baseUrl`; check `docker ps` and `/health` |
| `/mem0 profile` empty but data exists | `userId` points at a different scope. Compare against `GET /admin/scopes` |
| `add` returns "No memory was extracted" | LLM extraction failed server-side. mem0 swallows the error and returns an empty success — check server logs for `LLM extraction failed`, usually a bad or expired `OPENAI_API_KEY` |
| Recall returns more than `topK` | the server caps result counts itself; `top_k` in the request body is advisory |

Because a failed extraction still returns HTTP 200 with an empty result set, **a broken
extraction key looks like a successful write**. If memories stop accumulating, check the
server log before assuming the client is at fault.

---

## What's in here

| Path | Purpose |
|---|---|
| `settings.json` | Model defaults, theme, `packages` list, subagent config |
| `mcp.json` | MCP server definitions (secrets via `{env:VAR}`) |
| `extensions/` | Custom extensions (auto-discovered) |
| `extensions/litellm-budget.ts` | Footer status + `/budget` for LiteLLM spend |
| `extensions/subagent/config.json` | pi-subagents display/alias config |
| `prompts/` | Prompt templates (`/pr-review`) |
| `workflows/` | Subagent workflow scripts |
| `skills/` | Skills, if moved here (see note below) |

### Current defaults

- Provider `litellm`, model `bedrock-claude-opus-5`, thinking level `high`
- Subagents: opus for `oracle`/`reviewer`, haiku for `worker`/`scout`, `claude-code*` disabled
- Semantic memory: `self-hosted` against `http://localhost:8000` (hippocampus), scope `default-user`, `hybrid` mode

### Skills are not yet in this repo

My skills currently live in **`~/.agents/skills/`**, which is *outside* this folder — so
cloning this repo does **not** bring them along. pi discovers skills from both
`~/.agents/skills/` and `~/.pi/agent/skills/`, and directories containing `SKILL.md` are
found recursively in either location.

To bring them under version control, move them in:

```bash
mkdir -p ~/.pi/agent/skills
mv ~/.agents/skills/* ~/.pi/agent/skills/
```

Skills present at the time of writing: `context-router`, `epic-governance`,
`pii-incident-response`, `trustly-service-testing`, `trustly-skill-author`,
`vuln-management`, `vuln-triage`.

---

## What is deliberately not tracked

See `.gitignore`. Summary of why:

- **`auth.json`** — API keys. Never commit.
- **`trust.json`** — per-machine project trust decisions, embeds local paths.
- **`npm/`, `git/`** — installed packages, regenerated from `settings.json`. `npm/` even
  ships its own `.gitignore` of `*`, so pi already treats it as disposable.
- **`sessions/`, `missions/`, `run-history.jsonl`** — full conversation transcripts and
  local directory names. Private, and large.
- **`litellm-models-dev.json` (~8 MB), `models-store.json`, `mcp-cache.json`** — caches
  that pi refreshes (`pi update --models`).
- **`bin/`** — downloaded helper binaries such as `fd`.
- **root `package.json` / `package-lock.json`** — pi writes an empty `{}` stub here; not
  the same as `npm/package.json`.
- **`litellm-mcp-pauses/`** — runtime state including `identity-key`.

### Diff noise

pi rewrites `lastChangelogVersion` in `settings.json` on version bumps, so expect the
occasional one-line churn. To keep it out of your way:

```bash
git update-index --skip-worktree settings.json   # then unset when intentionally editing
```

---

## Verifying a fresh clone

```bash
pi -p "say OK"     # packages install, provider auth resolves
/mcp               # inside pi: server connection status
/budget            # LiteLLM spend (from the bundled extension)
/mem0 status       # semantic memory: backend, capture/recall/tool state
pi list            # packages match settings.json
```

`/mem0 status` reporting `Mem0 is not active` means the extension failed to initialize.
If it reports active but `/mem0 profile` errors, the extension is fine and the
hippocampus backend is unreachable — check `curl localhost:8000/health`.

## Security

Extensions and skills execute with full system permissions. Review anything you pull in
from elsewhere before starting pi. Before pushing, confirm no credentials are staged:

```bash
git ls-files -c | xargs grep -nE "sk-[A-Za-z0-9_-]{12,}|ghp_|-----BEGIN" 2>/dev/null
```

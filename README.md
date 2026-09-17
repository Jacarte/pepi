# pi agent config

My personal [pi](https://github.com/earendil-works/pi-coding-agent) configuration: settings, MCP servers, extensions, skills, prompt templates, and subagent workflows.

Clone this to `~/.pi/agent` and pi picks everything up on next start.

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
    "env": { "LITELLM_BASE_URL": "https://gateway.prd.devtools.trustly.cloud" }
  }
}
```

**Environment variables** — `mcp.json` references these via `{env:VAR}`. Unset vars
expand to an empty string (no defaults supported), so the matching server will fail
to authenticate rather than warn loudly. Export what you need in your shell profile:

| Variable | Used by | Required |
|---|---|---|
| `TRUSTLY_LLM_GATEWAY_API_KEY` | DevCenter MCP, `litellm-budget` extension (fallback) | yes |
| `GITHUB_TOKEN_READONLY` | GitHub MCP | yes |
| `NEO4J_PASSWORD` | neo4j MCP | if using neo4j |
| `ELEVENLABS_API_KEY` | speaker (TTS) MCP | if using speaker |
| `CONTEXT7_API_KEY` | context7 MCP (currently `disabled`) | no |

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
| `codegraph` | codegraph | `npm i -g @vndv/codegraph` |
| `tts2mic-mcp` | speaker | expects `~/tts2mic-mcp/tts2mic-mcp` |

`npx`-based servers (browsermcp, playwright, Chrome) need no install. Remote servers
(Atlassian, Datadog) use OAuth — run `/mcp` in pi to authenticate.

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
- Subagents: opus for `oracle`/`reviewer`, sonnet for `worker`/`scout`, `claude-code*` disabled

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
pi list            # packages match settings.json
```

## Security

Extensions and skills execute with full system permissions. Review anything you pull in
from elsewhere before starting pi. Before pushing, confirm no credentials are staged:

```bash
git ls-files -c | xargs grep -nE "sk-[A-Za-z0-9_-]{12,}|ghp_|-----BEGIN" 2>/dev/null
```

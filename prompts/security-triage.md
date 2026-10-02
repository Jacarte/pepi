---
description: "Nucleus security triage for one service: slice fixes by where/how they are made, apply as local commits only"
argument-hint: "[service] [--dry-run]"
---

Arguments: `$@`

- `service` = first token that does not start with `--` (may be absent).
- `--dry-run` anywhere = plan only: no workflow, no edits, no commits.

Fix Critical, High and exploitable Nucleus findings for one service, one local commit per slice. You orchestrate
(AGENTS.md): steps 1, 2, 4 are yours (one bash call, Nucleus MCP calls, synthesis); repo discovery goes to `scout`;
edits and review go to one async workflow. Final answer ≤6 lines.

## 1. Resolve service and repo (one bash call)

```bash
if root=$(jj root 2>/dev/null); then vcs=jj; elif root=$(git rev-parse --show-toplevel 2>/dev/null); then vcs=git; else root=$PWD; vcs=none; fi
echo "root=$root vcs=$vcs cwd=$PWD date=$(date +%Y%m%d)"
case $vcs in jj) jj diff --summary ;; git) git status --porcelain ;; esac
cat "$root/metadata.yaml" 2>/dev/null || echo "no metadata.yaml"
```

Service, first match wins:
1. Explicit `service` argument → source `arg`.
2. `metadata.yaml` names: the `service_names:` list (monorepo) or each `projects[].name`. One name → source
   `metadata.yaml`. Several and no argument → list them, ask me which one, wait.
3. Basename of `cwd` → source `cwd`.

`owner:` (e.g. `team-xxx`) is the Nucleus team. Print `Service: <svc> (source: <source>)`. Omit `project_id` everywhere.
Preflight (skip under `--dry-run`): `vcs=none` → stop (cannot commit); any status/diff output → dirty: stop, ask me.

## 2. Fetch findings

`summary_report` is a Nucleus MCP prompt, not a tool: you cannot call it. If this conversation already holds
`/mcp__Nucleus__summary_report` output for this service (I may run it first), reuse it and fetch only missing
fields. Otherwise reproduce its service recipe:

1. Validate: `Nucleus_list_services` with `team=<owner>` when known (retry without `team` if missing).
   Service absent → show up to 5 close matches and stop.
2. Fetch in one `mcpScript` batch (each result is `{ ok, data }`: read `data.structuredContent`, else the text in
   `data.content`). On timeout, truncation or an unknown tool path, make the same calls one by one with `mcp({ tool, args })`:

```js
const s = { service: "<svc>" };
const calls = [["metrics", "Nucleus_get_asset_group_metrics", s],
  ["critical", "Nucleus_search_findings", { ...s, finding_severity: "Critical" }],
  ["high", "Nucleus_search_findings", { ...s, finding_severity: "High" }],
  ["exploitable", "Nucleus_search_findings", { ...s, finding_exploitable: "Yes" }],
  ["trend", "Nucleus_get_finding_trend", s], ["assets", "Nucleus_list_service_assets", s]];
const out = await Promise.all(calls.map(([, tool, a]) => tools.call(tool, a)));
return Object.fromEntries(calls.map(([k], i) => [k, out[i]]));
```

Recipe rules: NEVER pass `limit`; one service per call; `service` without a `/service/` prefix. Drop Informational and
placeholder findings (`No Vulnerabilities Found`, `Software*`). When search output lacks fix versions or details, call
`Nucleus_get_finding` with `finding_number=<id>`.

Record per finding: `finding_number`, CVE(s), severity, exploitable, package, current version, fixed version(s),
scan_type/source, asset. Dedupe by (finding, package, asset). Zero findings → say
`No Critical/High/exploitable findings for <svc>.` and stop.

## 3. Locate in repo (one scout)

Dispatch ONE read-only scout and wait: `subagent({ agent: "scout", context: "fresh", async: false, cwd: "<root>", task })`, task:

```
Read-only: no edits, no file writes, no VCS commands that change state. Repo: <root>.
For each finding in FINDINGS, find what controls it in this repo. Return JSON:
{ findings: [{ key, file, line, current, control, direct, via, viaFixed, shipped, notes }], build, test }
- control: manifest+lockfile | Dockerfile FROM/RUN | go.mod go/toolchain | .nvmrc/.tool-versions/wrapper/CI image | Helm/k8s/Terraform | source (SAST)
- current: the exact string as written (version, property, tag, digest).
- direct: declared directly in a manifest? If transitive: via = the direct dep or BOM/parent that pulls or
  manages it; viaFixed = the lowest via version that ships the fix, or null.
- shipped: in the runtime artifact, or test/dev/provided-only, or unreachable code? Cite file:line.
- build/test: the repo's build and test commands (Makefile, CI config, README).
Not found → say so for that finding; never guess.
=== FINDINGS ===
<deduped findings JSON>
```

## 4. Slice plan (you)

Classify by WHERE and HOW the fix is made, not by which scanner reported it: a Go stdlib CVE found in a container
image is `toolchain`; an OS package CVE in the image is `dockerfile`. First match wins:

| # | Type | Member when | Change |
|---|---|---|---|
| 1 | report-only | No fixed version, or false positive: not shipped, test/dev/provided scope only, unreachable, accepted risk (cite scout evidence) | None: never edited or committed |
| 2 | dockerfile | Vulnerable OS/distro package (apk/apt/rpm) in the built image | In Dockerfile/Containerfile: `FROM` tag/digest bump, OS package pin/upgrade, package removal |
| 3 | toolchain | Vulnerable language runtime/compiler/stdlib (Go, Node, JDK/JRE, Maven, Gradle), wherever detected | Bump its pin: go.mod `go`/`toolchain`, `.nvmrc`/engines, `.tool-versions`, wrapper, CI builder image, language image tag (`FROM golang:`/`eclipse-temurin:`) |
| 4 | bom-bump | Version managed by a parent POM / Spring Boot / BOM / platform whose newer release ships the fix | Bump the parent/BOM version |
| 5 | direct-dep | Declared directly in a manifest, or transitive with `viaFixed` on its direct parent | Bump that declaration, regenerate the lockfile |
| 6 | transitive-override | Transitive with no fixed direct parent or BOM | Maven `dependencyManagement`, Gradle constraints, npm `overrides` / yarn `resolutions`, `go get pkg@ver` (indirect require) |
| 7 | code-fix | First-party SAST finding in source | Minimal change at the flagged code |
| 8 | config | Helm values, k8s manifests, Terraform, secrets/policy | Change the flagged value |

Rules:
- One fix type per slice; slice = one type × one file, or a manifest + its lockfile. Exception: a `toolchain`
  slice moves every pin of that one toolchain together.
- Every deduped finding lands in exactly one slice; findings fixed by the same change share a slice.
- Target = lowest version that fixes every finding in the slice; flag a major-version jump as a risk.
- Order, smallest blast radius first: direct-dep, transitive-override, bom-bump, dockerfile, toolchain, config,
  code-fix; report-only last.
- Row: `id`, `type`, `criterion`, `findings` (id, CVE, severity), `files`, `change` (exact `from -> to`), `verify`,
  `done`, `commit` = `fix(security): <type> <component> <from>-><to>`, body lists the CVEs and finding ids.

Show the plan as a table. `--dry-run`: stop here. Only report-only rows: go to step 6.

## 5. Execute (one async workflow)

Make exactly ONE top-level call, script composed inline:
`subagent({ async: true, cwd: "<root>", workflowScript: "<script>", args: { service, vcs, date, slices, baseline, rules } })`,
`slices` = the non-report-only rows as JSON, `baseline` = the scout's `build && test`, `rules` = the `## Rules` bullets
verbatim (string array). Script rules: task strings are arrays joined with `"\n"`; plain helpers only, no async
helpers; `await` every `runs.*`; end with `return`. Skeleton:

```js
const { service, vcs, date, slices, baseline, rules } = args;
const name = "security/" + service + "-" + date;
const resultSchema = { type: "object", required: ["results", "diffPath", "commands"], properties: {
  ref: { type: "string" }, diffPath: { type: "string" }, commands: { type: "array", items: { type: "string" } },
  results: { type: "array", items: { type: "object", required: ["id", "status", "verification"], properties: {
    id: { type: "string" }, status: { type: "string", enum: ["committed", "reverted", "skipped"] },
    commit: { type: "string" }, verification: { type: "string" }, blocker: { type: "string" } } } } } };
const reviewSchema = { type: "object", required: ["findings"], properties: { findings: { type: "array", items: {
  type: "object", required: ["severity", "commit", "issue", "fix"], properties: { severity: { type: "string",
  enum: ["P0", "P1", "P2"] }, commit: { type: "string" }, file: { type: "string" }, issue: { type: "string" }, fix: { type: "string" } } } } } };
const vcsRules = [/* the jj or git block below for args.vcs, with <name> filled in */];
const fix = await runs.run("fix", { agent: "worker", context: "fresh", outputSchema: resultSchema, task: [
  "Apply these security slices sequentially, in order, as local commits. RULES below are hard limits.",
  "First run the baseline once: " + baseline + ". Judge each slice by no NEW failures vs that baseline.",
  "Per slice: 1) confirm the file holds `from`, else skip with a blocker; 2) apply exactly `change` (plus the",
  "lockfile regeneration it names), nothing else; 3) run `verify`; 4) pass -> commit with the slice's `commit`",
  "message and record it; fail -> revert the slice, record the blocker, continue with the next slice.",
  "Never broaden scope: no extra bumps, refactors or unrelated fixes. Needing more than planned is a blocker.",
  "Append the show output of every commit to one `mktemp /tmp/security-triage-diff.XXXXXX` file.",
  "Return its path as diffPath, the bookmark/branch as ref, and every command you ran.",
  ...vcsRules, "=== RULES ===", ...rules, "=== SLICES ===", JSON.stringify(slices, null, 2)].join("\n") });
const res = fix.structuredOutput || { results: [], commands: [], diffPath: "" };
if (!res.results.some((r) => r.status === "committed")) return { res, review: null, followUp: null };
const review = await runs.run("review", { agent: "reviewer", context: "fresh", outputSchema: reviewSchema, task: [
  "Read-only review of local security commits against the plan. Diff file: " + res.diffPath,
  "Per commit: only planned files touched, from/to versions match its slice, no unrelated edits, message format.",
  "Flag any command the worker ran that breaks RULES: " + JSON.stringify(res.commands), "=== RULES ===", ...rules,
  "Only concrete issues with evidence. P0/P1 = wrong, missing or unplanned change; P2 = nit.",
  "=== PLAN ===", JSON.stringify(slices, null, 2), "=== RESULTS ===", JSON.stringify(res.results, null, 2)].join("\n") });
const must = ((review.structuredOutput || {}).findings || []).filter((f) => f.severity !== "P2");
let followUp = null;
if (must.length) followUp = (await runs.run("fix-review", { agent: "worker", context: "fresh", outputSchema: resultSchema,
  task: ["Apply ONLY these review findings as additional local commits; nothing else. RULES below are hard limits.",
    "Already on " + (res.ref || name) + ": skip the start step and use it as <name>.", ...vcsRules, "=== RULES ===", ...rules,
    "=== FINDINGS ===", JSON.stringify(must, null, 2)].join("\n") })).structuredOutput;
return { res, review: review.structuredOutput, followUp };
```

VCS block (`vcsRules`):
- jj — start: `jj new` once. Per slice: edit, verify; `jj diff --summary` must list only slice files
  (`jj restore <others>`); `jj commit -m "<subject>" -m "<body>"`. Record
  `jj log -r @- --no-graph -T 'commit_id.short() ++ " " ++ description.first_line()'`. Failed slice: `jj restore`.
  Show: `jj show <id>`. End: `jj bookmark create <name> -r @-` (fix-review: `jj bookmark set <name> -r @-`).
  Bookmark exists → append `-2`. Local only.
- git — start: `git switch -c <name>` (exists → append `-2`). Per slice: `git add <slice files> && git commit -m
  "<subject>" -m "<body>"`; record `git log -1 --format='%h %s'`. Failed slice:
  `git restore --staged --worktree <files>` and delete files it created. Show: `git show <id>`.

## 6. Report (≤6 lines, from the workflow result; do not redo its checks)

```
Service: <svc> (source: arg|metadata.yaml|cwd)
Fixed: <type> ×<slices> (<n> CVEs), …
Report-only: <finding/CVE> — <reason>; …
Blockers: <slice> — <reason> | none. Verification: <passed/failed per slice>; review: <clean | n fixed>
<bookmark|branch> <name>: <hash subject>, …
Push/PR are yours; nothing was pushed.
```

## Rules

- NEVER push, NEVER create/edit PRs, never run `git push`, `jj git push`, `gh pr *`. Local commits only. The user owns push/PR.
- No Nucleus writes: never call `update_finding` or `bulk_update_findings`.
- No scope beyond the plan: report-only rows are never edited or committed; no extra bumps, refactors or unrelated fixes.

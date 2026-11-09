---
description: Review the current PR with parallel lanes, get user ACK, then publish inline annotations
---

Review $@ and publish the result only after I approve it.

## 1. Launch one async review workflow

Make exactly ONE top-level call. Write the orchestration yourself as one fenced
`js workflow` block in this same reply, then call `subagent` with `workflow: true`.
If the tool reports the block did not reach it, write the script to a temp file
and pass its path as `workflow` instead.

```
subagent({ workflow: true, args: { target: "$@" }, async: true })
```

The script never publishes. It runs in two stages. Collect must finish before
lanes are chosen, and both stages can live in the same script:

a. **collect**: `runs.run` with agent `delegate` (it has bash), `context: "fresh"`.
   Read-only on the PR (it only creates the temp clone). Resolve `args.target`
   (PR number, URL, or branch), or the current branch's PR if empty, with
   `gh pr view <target> --json number,title,url,headRefOid,headRefName,baseRefName,files`.
   Take `owner`/`repo` from the PR url, not the local checkout.
   **Clone, don't query.** Explore the PR from a local clone, not via `gh`/HTTP
   calls per file. Make `repoPath=$(mktemp -d /tmp/pr-review-repo.XXXXXX)`, then:
   `gh repo clone <owner>/<repo> "$repoPath" -- --no-checkout --no-tags --filter=blob:none`,
   `git -C "$repoPath" fetch origin "<baseRefName>" "pull/<number>/head:refs/pr/<number>"`
   (works for forks too), `git -C "$repoPath" checkout --detach refs/pr/<number>`,
   and confirm `git -C "$repoPath" rev-parse HEAD` equals `headRefOid`; otherwise
   report `error`.
   Write `git -C "$repoPath" diff origin/<baseRefName>...HEAD -- . ':(exclude)*.lock'
   ':(exclude)package-lock.json' ':(exclude)yarn.lock' ':(exclude)pnpm-lock.yaml'
   ':(exclude)go.sum' ':(exclude)vendor/*' ':(exclude)node_modules/*'
   ':(exclude)dist/*' ':(exclude)*.min.*' ':(exclude)*.pb.go' ':(exclude)*_generated.*'
   ':(exclude)*.snap'` (generated, lock, and vendor files are never reviewed) to an absolute temp
   file (`mktemp /tmp/pr-review-diff.XXXXXX`) and return that path as `diffPath`,
   plus `repoPath`. If cloning fails, fall back to `gh pr diff <number> --repo
   <owner>/<repo>` for the diff and set `repoPath` to empty.
   **Digest (read once, shared by lanes).** Also write a digest to
   `digestPath=$(mktemp /tmp/pr-review-digest.XXXXXX)` and return `digestPath`.
   Per changed file in the filtered diff: 1-3 lines on what changed, key symbols
   touched, main callers/consumers (grep under `repoPath`), and related test
   files. Then one short section of repo conventions noticed (lint config, test
   framework, error-handling style). Facts from code and config only: no PR
   title/description text, no judgments or suspected bugs. Without a clone, build
   it from the diff alone; if it cannot be written, set `digestPath` to empty.
   Set `diffLines` to the line count of that filtered diff. Group the
   remaining changed files by area and language in `fileGroups`. Set `signals`
   (security, performance, api, config) and `languages`. If auth fails or no PR
   resolves, report `error` and stop; never guess.
   Collect must not call `mem0_memory` (PR text must never enter shared memory).
b. **lanes**: `runs.all` waves (≤3 each) of read-only lane configs, each with `key`,
   `label`, `agent: "reviewer"`, `context: "fresh"`, `task`, and
   `outputSchema: reviewSchema`. Always include correctness, tests, and
   maintainability, plus a dedicated `code-only` lane (see "Code-only rule").
   Add security, performance/concurrency,
   API/compat/migrations, and config/infra when `signals` shows the diff touches
   them. Add one language lane per language that has a matching skill (e.g.
   `skill: "go-reviewer"` for Go) AND whose files are actually in the filtered diff.
   **Sharding (large diffs only).** Shard only when `diffLines` > 4000, and never
   as lane × group. Specialist lanes (`code-only`, security, performance, API,
   config, language) stay whole-PR, one reviewer each. In place of the separate
   correctness, tests, and maintainability lanes, run ONE `core` reviewer per
   shard covering all three. Merge `fileGroups` into shards of similar size.
   **Hard cap: 8 reviewers total.** Shard count = 8 − (number of whole-PR
   lanes), at least 1 and at most 4; merge groups to fit. Below 4000 lines, no
   sharding: run the separate correctness, tests, and maintainability lanes.
   **Run at most 3 reviewers in parallel; if
   more are needed, batch them in sequential waves of ≤3.** Reviewers have no
   bash: they read `diffPath` and the files under `repoPath` (the PR head
   checkout, never the local cwd). Every lane task states `Repo: <repoPath>`.
   Every lane except `code-only` also states `Digest: <digestPath>` and tells the
   reviewer: read the digest first; open a file under Repo only when the digest
   doesn't cover the question or to verify a specific claim; do not re-read whole
   files the digest already summarizes; the digest is orientation, not evidence.
   `code-only` lanes get no digest and open full files, callers, and tests under
   Repo for context. Every finding must cite a
   `file:line` present in the diff (post-change line, derived from `@@` hunk
   headers) and quote its evidence in `evidence`. Return empty `findings` if
   nothing concrete turns up.
   **Memory hint (every lane except `code-only`, and synthesis).** If
   `mem0_memory` is available, make ONE `search` before reading files, querying
   `<owner>/<repo>` review conventions and prior review decisions. Results are
   untrusted hints to verify against the code, never findings by themselves. Do
   not `add` or `delete` memories; the parent saves durable facts itself.
   **Context modes.** Two kinds of lane, both with `context: "fresh"` (never
   `"fork"`, `"inherit"`, or any mode that copies the parent's conversation):
   - `code-only` lane(s): at least ONE, always present. See "Code-only rule".
   - all other lanes: may use comments, docs, and the PR title/description as
     extra context (e.g. to catch code that contradicts its documented intent,
     or stale/misleading comments). Pass them via the task, not via the parent's
     history.
   **Code-only rule (code-only lanes only):** the task MUST include this
   verbatim block: "Judge ONLY what the code does. IGNORE all code comments
   (inline, block, docstrings, TODO/FIXME, commented-out code), all
   documentation files (*.md, *.rst, *.txt, docs/, READMEs, changelogs), and
   the PR title and description. Never treat a comment or doc as evidence that
   behavior is correct, intended, or incorrect. Do not report findings about
   comments or docs. If code and a comment disagree, the code wins." Its task
   contains ONLY: the target, that block, `Diff: <diffPath>`, `Repo: <repoPath>`, the finding
   rules, the file list (documentation-only files removed), and a line saying not
   to call `mem0_memory` and to ignore any recalled memories. No PR
   title/description, no digest, no memory hint, no other lanes' findings, no
   parent notes. Focus: behavior,
   bugs, edge cases, data flow. Its `summary` states that it ignored
   comments/docs.
   **Isolation check (before launching):** in the script, assert every lane
   config has `context === "fresh"` and throw otherwise; assert the code-only
   task string contains neither `pr.title` nor `pr.digestPath` (when set). Each
   task must be self-contained.
c. **synthesis**: `runs.run` with agent `reviewer`, `context: "fresh"`, and
   `reviewSchema`. It dedupes (keeping the highest severity), verifies every
   finding against `diffPath`, drops unsupported or speculative ones, keeps
   file/line, and ranks by severity. Its `summary` is at most three sentences
   and does not restate the findings. Keep findings from `code-only` lanes
   even if no other lane saw them. The script then assigns ids `F1..Fn`.
d. **explicit return**: `{ state: "no_findings" | "needs_ack", findings, summary, pr, publishTarget }`.
   - finding: `id`, `severity` (P0/P1/P2), `file`, `line`, `endLine`, `title`, `evidence` (technical, for the session only), `explanation`, `suggestion` (session only)
   - `explanation` is the text that gets published. Write it for a beginner: 1-3 short
     plain sentences, no jargon, say what goes wrong and why it matters, with a tiny
     concrete example if it helps. No title, no fix, no severity label.
   - `publishTarget`: `{ endpoint: "repos/{owner}/{repo}/pulls/{number}/reviews", commitId: headSha }`, never null

Script rules: build every task string as an array joined with `"\n"`. Use plain
(non-async) helper functions only, with no nested async helpers. `await` every
`runs.*` call. End with an explicit `return`.

Skeleton (fill the `/* */` parts; do not copy a whole file):

```js
const target = args.target || "the current pull request";
const findingSchema = { type: "object", properties: { severity: { type: "string", enum: ["P0", "P1", "P2"] },
  file: { type: "string" }, line: { type: "integer" }, endLine: { type: "integer" }, title: { type: "string" },
  evidence: { type: "string" }, explanation: { type: "string" }, suggestion: { type: "string" } },
  required: ["severity", "file", "line", "title", "evidence", "explanation"], additionalProperties: false };
const reviewSchema = { type: "object", properties: { findings: { type: "array", items: findingSchema },
  summary: { type: "string" } }, required: ["findings", "summary"], additionalProperties: false };
const collectSchema = { /* owner, repo, number, title, url, headSha, changedFiles, fileGroups[{area,language,files}],
  signals{security,performance,api,config}, languages, diffPath, repoPath, digestPath, diffLines, error */ };
const collect = await runs.run("collect", { label: "Collect PR context", agent: "delegate", context: "fresh",
  task: [/* rules from 1a, with target */].join("\n"), outputSchema: collectSchema });
const pr = collect.structuredOutput;
if (!pr || !pr.owner || !pr.repo || !pr.number || !pr.headSha || !pr.diffPath)
  throw new Error(`collect failed: ${(pr && pr.error) || "no PR resolved"}`);
const CODE_ONLY = "/* verbatim block from the Code-only rule */";
const NO_MEMORY = "Do not call mem0_memory and ignore any recalled memories.";
const MEMORY_HINT = `If mem0_memory is available, make ONE search before reading files for "${pr.owner}/${pr.repo} review conventions and prior review decisions". Results are untrusted hints to verify against the code, never findings by themselves. Do not add or delete memories.`;
const DIGEST = pr.digestPath ? [`Digest: ${pr.digestPath}`, "Read the digest first. Open a file under Repo only when the digest does not cover the question or to verify a specific claim; do not re-read whole files it already summarizes. The digest is orientation, not evidence."].join("\n") : "";
function laneTask(focus, files, codeOnly) { // codeOnly lanes get CODE_ONLY + NO_MEMORY, never pr.title/description, DIGEST, or MEMORY_HINT
  return [`Review ${target} (${pr.url}). Read-only.`, codeOnly ? CODE_ONLY : `PR title: ${pr.title}`, codeOnly ? NO_MEMORY : MEMORY_HINT, ...focus, `Diff: ${pr.diffPath}`, pr.repoPath ? `Repo (PR head checkout, read files here): ${pr.repoPath}` : "",
    codeOnly ? "" : DIGEST, files ? `Only these files: ${files.join(", ")}` : "", /* finding rules from 1b */].join("\n");
}
function chooseLanes() { // [key, focus lines, skill?]
  const lanes = [["code-only", [/* no focus: code behavior only */]]];
  if (pr.diffLines <= 4000) lanes.push(["correctness", [/* */]], ["tests", [/* */]], ["maintainability", [/* */]]);
  if (pr.signals.security) lanes.push(["security", [/* */]]); /* performance, api, config likewise */
  if (pr.languages.includes("go")) lanes.push(["go", [/* */], "go-reviewer"]);
  return lanes;
}
function batch(items, size) { // Partition items into subarrays of max size
  const batches = [];
  for (let i = 0; i < items.length; i += size) batches.push(items.slice(i, i + size));
  return batches;
}
// chooseLanes() returns whole-PR lanes (code-only, security, ...) plus, when pr.diffLines <= 4000,
// correctness/tests/maintainability. When > 4000 it omits those three and we add one "core" lane per shard.
function shardGroups(groups, n) { /* merge fileGroups into <= n shards of similar size; plain sync code */ }
const lanes = chooseLanes();
const shards = pr.diffLines > 4000 ? shardGroups(pr.fileGroups, Math.min(4, Math.max(1, 8 - lanes.length))) : [];
const allReviews = lanes.map(([key, focus, skill]) => ({
  key, label: `Review ${key}`, agent: "reviewer", context: "fresh", ...(skill ? { skill } : {}),
  task: laneTask(focus, null, key === "code-only"), outputSchema: reviewSchema }))
  .concat(shards.map((g, i) => ({ key: `core-${i + 1}`, label: `Review core (${g.area})`, agent: "reviewer", context: "fresh",
    task: laneTask([/* correctness + tests + maintainability */], g.files, false), outputSchema: reviewSchema })));
if (allReviews.length > 8) throw new Error(`too many reviewers: ${allReviews.length}`);
for (const r of allReviews) if (r.context !== "fresh") throw new Error(`lane ${r.key} not fresh`);
const co = allReviews.find((r) => r.key === "code-only");
if (pr.digestPath && co.task.includes(pr.digestPath)) throw new Error("code-only lane got digest");
const reviews = [];
for (const wave of batch(allReviews, 3)) {
  const waveResults = await runs.all(wave);
  reviews.push(...waveResults);
}
const raw = reviews.flatMap((r) => (r.structuredOutput && r.structuredOutput.findings) || []);
const synthesis = await runs.run("synthesis", { label: "Synthesize findings", agent: "reviewer", context: "fresh",
  task: [/* rules from 1c */, MEMORY_HINT, `Diff: ${pr.diffPath}`, pr.repoPath ? `Repo: ${pr.repoPath}` : "", "=== CANDIDATES ===", JSON.stringify(raw, null, 2)].join("\n"),
  outputSchema: reviewSchema });
const out = synthesis.structuredOutput || { findings: [], summary: "" };
const findings = out.findings.map((f, i) => ({ id: `F${i + 1}`, ...f }));
return { state: findings.length ? "needs_ack" : "no_findings", findings, summary: out.summary,
  pr: { owner: pr.owner, repo: pr.repo, number: pr.number, title: pr.title, url: pr.url, headSha: pr.headSha, diffPath: pr.diffPath, repoPath: pr.repoPath, digestPath: pr.digestPath },
  publishTarget: { endpoint: `repos/${pr.owner}/${pr.repo}/pulls/${pr.number}/reviews`, commitId: pr.headSha } };
```

Wait for the workflow result. If `state` is `"no_findings"`, report that and stop.

## 2. Ask me (do not skip this)

Present every finding as `id · severity · file:line · title`, with the
evidence and suggested fix (these stay in this session and are never published). Then ask which ids to publish. Not every finding is worth
posting.

Wait for my answer. Never assume approval. Never hand this question to a child
agent — a child has no channel to me and will just proceed.

If I reject everything, stop and publish nothing.

## 3. Publish the approved subset yourself

You have `bash` and you already hold the findings, so post the review directly.
Do not delegate this to a child; a child would only re-parse and risk rewording
the findings I approved.

Build one payload containing an inline comment per approved finding:

```json
{
  "commit_id": "<publishTarget.commitId>",
  "event": "COMMENT",
  "body": "## Review\n\n<summary>\n\n<N> inline comment(s) attached.",
  "comments": [
    {
      "path": "<finding.file>",
      "line": <finding.line>,
      "side": "RIGHT",
      "body": "<explanation>"
    }
  ]
}
```

For a range finding set `"line"` to `<finding.endLine>` and add
`"start_line": <finding.line>` with `"start_side": "RIGHT"`. Write the
JSON to a temp file (heredoc or the write tool — never inline shell escaping of
long text), then:

```bash
gh api --method POST <publishTarget.endpoint> --input /tmp/pr-review-payload.json
```

Rules:

- Inline comment bodies are ONLY the plain-language `explanation`: no title, no
  severity, no suggestion, no evidence. Fixes stay in the agent session.
- The body stays short: summary plus comment count. All detail goes in the
  inline comments, anchored to the involved files. Never dump the findings into
  the body.
- GitHub rejects comments on lines absent from the diff. Verify against
  `gh pr diff`; for an unanchorable finding use `"subject_type": "file"` and omit
  `line`. On rejection, retry once with the offending comments converted to
  file-level — never fall back to a body dump.
- Always a non-blocking `COMMENT`. Never approve, never request changes.
- Never modify, commit, push, or merge (the PR; the temp clone is disposable).
- When done (published, rejected, or no findings), delete the clone:
  `rm -rf <repoPath>` (and `diffPath`, `digestPath`).
- Use the `go-reviewer` skill if it is a Go repo

Report the PR URL and how many inline comments were posted.

---
description: Review the current PR with parallel lanes, get user ACK, then publish inline annotations
---

Review $@ and publish the result only after I approve it.

## 1. Launch one async review workflow

Make exactly ONE top-level call. Write the orchestration yourself as an inline
`workflowScript`; do not reference a script file path.

```
subagent({ workflowScript: "<script you compose below>", args: { target: "$@" }, async: true })
```

The script never publishes. It runs in two stages. Collect must finish before
lanes are chosen, and both stages can live in the same script:

a. **collect**: `runs.run` with agent `delegate` (it has bash), `context: "fresh"`.
   Read-only. Resolve `args.target` (PR number, URL, or branch), or the current
   branch's PR if empty, with `gh pr view <target> --json number,title,url,headRefOid,files`.
   Take `owner`/`repo` from the PR url, not the local checkout. Write
   `gh pr diff <number> --repo <owner>/<repo>` to an absolute temp file
   (`mktemp /tmp/pr-review-diff.XXXXXX`) and return that path as `diffPath`.
   Group changed files by area and language in `fileGroups`. Set `signals`
   (security, performance, api, config) and `languages`. If auth fails or no PR
   resolves, report `error` and stop; never guess.
b. **lanes**: `runs.all` waves (≤3 each) of read-only lane configs, each with `key`,
   `label`, `agent: "reviewer"`, `context: "fresh"`, `task`, and
   `outputSchema: reviewSchema`. Always include correctness, tests, and
   maintainability. Add security, performance/concurrency,
   API/compat/migrations, and config/infra when `signals` shows the diff touches
   them. Add one language lane per language that has a matching skill (e.g.
   `skill: "go-reviewer"` for Go). If the diff is large (more than ~1500 lines),
   shard each lane by `fileGroups`. **Run at most 3 reviewers in parallel; if
   more are needed, batch them in sequential waves of ≤3.** Reviewers have no
   bash: they read `diffPath` and working-tree files. Every finding must cite a
   `file:line` present in the diff (post-change line, derived from `@@` hunk
   headers) and quote its evidence in `explanation`. Return empty `findings` if
   nothing concrete turns up.
c. **synthesis**: `runs.run` with agent `reviewer`, `context: "fresh"`, and
   `reviewSchema`. It dedupes (keeping the highest severity), verifies every
   finding against `diffPath`, drops unsupported or speculative ones, keeps
   file/line, and ranks by severity. Its `summary` is at most three sentences
   and does not restate the findings. The script then assigns ids `F1..Fn`.
d. **explicit return**: `{ state: "no_findings" | "needs_ack", findings, summary, pr, publishTarget }`.
   - finding: `id`, `severity` (P0/P1/P2), `file`, `line`, `endLine`, `title`, `explanation`, `suggestion`
   - `publishTarget`: `{ endpoint: "repos/{owner}/{repo}/pulls/{number}/reviews", commitId: headSha }`, never null

Script rules: build every task string as an array joined with `"\n"`. Use plain
(non-async) helper functions only, with no nested async helpers. `await` every
`runs.*` call. End with an explicit `return`.

Skeleton (fill the `/* */` parts; do not copy a whole file):

```js
const target = args.target || "the current pull request";
const findingSchema = { type: "object", properties: { severity: { type: "string", enum: ["P0", "P1", "P2"] },
  file: { type: "string" }, line: { type: "integer" }, endLine: { type: "integer" }, title: { type: "string" },
  explanation: { type: "string" }, suggestion: { type: "string" } },
  required: ["severity", "file", "line", "title", "explanation"], additionalProperties: false };
const reviewSchema = { type: "object", properties: { findings: { type: "array", items: findingSchema },
  summary: { type: "string" } }, required: ["findings", "summary"], additionalProperties: false };
const collectSchema = { /* owner, repo, number, title, url, headSha, changedFiles, fileGroups[{area,language,files}],
  signals{security,performance,api,config}, languages, diffPath, diffLines, error */ };
const collect = await runs.run("collect", { label: "Collect PR context", agent: "delegate", context: "fresh",
  task: [/* rules from 1a, with target */].join("\n"), outputSchema: collectSchema });
const pr = collect.structuredOutput;
if (!pr || !pr.owner || !pr.repo || !pr.number || !pr.headSha || !pr.diffPath)
  throw new Error(`collect failed: ${(pr && pr.error) || "no PR resolved"}`);
function laneTask(focus, files) {
  return [`Review ${target} (${pr.url}). Read-only.`, ...focus, `Diff: ${pr.diffPath}`,
    files ? `Only these files: ${files.join(", ")}` : "", /* finding rules from 1b */].join("\n");
}
function chooseLanes() { // [key, focus lines, skill?]
  const lanes = [["correctness", [/* */]], ["tests", [/* */]], ["maintainability", [/* */]]];
  if (pr.signals.security) lanes.push(["security", [/* */]]); /* performance, api, config likewise */
  if (pr.languages.includes("go")) lanes.push(["go", [/* */], "go-reviewer"]);
  return lanes;
}
function batch(items, size) { // Partition items into subarrays of max size
  const batches = [];
  for (let i = 0; i < items.length; i += size) batches.push(items.slice(i, i + size));
  return batches;
}
const groups = pr.diffLines > 1500 ? pr.fileGroups : [null];
const allReviews = chooseLanes().flatMap(([key, focus, skill]) => groups.map((g, i) => ({
  key: g ? `${key}-${i + 1}` : key, label: `Review ${key}${g ? ` (${g.area})` : ""}`, agent: "reviewer",
  context: "fresh", ...(skill ? { skill } : {}), task: laneTask(focus, g && g.files), outputSchema: reviewSchema })));
const reviews = [];
for (const wave of batch(allReviews, 3)) {
  const waveResults = await runs.all(wave);
  reviews.push(...waveResults);
}
const raw = reviews.flatMap((r) => (r.structuredOutput && r.structuredOutput.findings) || []);
const synthesis = await runs.run("synthesis", { label: "Synthesize findings", agent: "reviewer", context: "fresh",
  task: [/* rules from 1c */, `Diff: ${pr.diffPath}`, "=== CANDIDATES ===", JSON.stringify(raw, null, 2)].join("\n"),
  outputSchema: reviewSchema });
const out = synthesis.structuredOutput || { findings: [], summary: "" };
const findings = out.findings.map((f, i) => ({ id: `F${i + 1}`, ...f }));
return { state: findings.length ? "needs_ack" : "no_findings", findings, summary: out.summary,
  pr: { owner: pr.owner, repo: pr.repo, number: pr.number, title: pr.title, url: pr.url, headSha: pr.headSha, diffPath: pr.diffPath },
  publishTarget: { endpoint: `repos/${pr.owner}/${pr.repo}/pulls/${pr.number}/reviews`, commitId: pr.headSha } };
```

Wait for the workflow result. If `state` is `"no_findings"`, report that and stop.

## 2. Ask me (do not skip this)

Present every finding as `id · severity · file:line · title`, with the
suggested fix. Then ask which ids to publish. Not every finding is worth
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
      "body": "**<severity>: <title>**\n\n<explanation>\n\n<suggestion>"
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

- The body stays short: summary plus comment count. All detail goes in the
  inline comments, anchored to the involved files. Never dump the findings into
  the body.
- GitHub rejects comments on lines absent from the diff. Verify against
  `gh pr diff`; for an unanchorable finding use `"subject_type": "file"` and omit
  `line`. On rejection, retry once with the offending comments converted to
  file-level — never fall back to a body dump.
- Always a non-blocking `COMMENT`. Never approve, never request changes.
- Never modify, commit, push, or merge.
- Use the `go-reviewer` skill if it is a Go repo

Report the PR URL and how many inline comments were posted.

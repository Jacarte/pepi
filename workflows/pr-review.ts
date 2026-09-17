// workflows/pr-review.ts
//
// Review engine only. It NEVER publishes.
//
// collect PR context -> parallel reviews -> synthesis -> return findings.
//
// Publishing is the parent's job, because the parent is the only party that can
// ask the user for ACK and assessment. A child subagent has no channel to the
// user, so any "wait for the user" instruction given to a child is silently
// bypassed.
//
// args:
// {
//   target?: string   // default: "the current pull request"
// }

const target = args.target || "the current pull request";

const findingSchema = {
  type: "object",
  properties: {
    severity: { type: "string", enum: ["P0", "P1", "P2"] },
    file: { type: "string" },
    line: { type: "integer" },
    endLine: { type: "integer" },
    title: { type: "string" },
    explanation: { type: "string" },
    suggestion: { type: "string" }
  },
  required: ["severity", "file", "line", "title", "explanation"],
  additionalProperties: false
};

const reviewSchema = {
  type: "object",
  properties: {
    findings: { type: "array", items: findingSchema },
    summary: { type: "string" }
  },
  required: ["findings", "summary"],
  additionalProperties: false
};

const context = await runs.run("collect", {
  label: "Collect PR diff and metadata",
  agent: "delegate",
  context: "fresh",
  task: [
    `Collect read-only context for ${target}. Do not modify anything.`,
    "",
    "Run:",
    "  gh repo view --json nameWithOwner",
    "  gh pr view --json number,title,url,headRefOid,files",
    "  gh pr diff",
    "",
    "If GitHub CLI auth is unavailable or there is no current PR, stop and",
    "report the error clearly instead of guessing.",
    "",
    "Return the unified diff verbatim in the diff field, including the @@ hunk",
    "headers, because downstream reviewers rely on them to compute line numbers.",
    "If the diff exceeds roughly 2000 lines, include the complete diff for the",
    "files carrying logic changes and list the omitted files in omittedFiles."
  ].join("\n"),
  outputSchema: {
    type: "object",
    properties: {
      repo: { type: "string" },
      prNumber: { type: "integer" },
      title: { type: "string" },
      url: { type: "string" },
      headSha: { type: "string" },
      changedFiles: { type: "array", items: { type: "string" } },
      omittedFiles: { type: "array", items: { type: "string" } },
      diff: { type: "string" }
    },
    required: ["prNumber", "headSha", "changedFiles", "diff"],
    additionalProperties: false
  }
});

const pr = context.structuredOutput;
const diff = pr.diff;

function reviewTask(focusTitle, focusPoints) {
  return [
    `Review ${target}.`,
    "",
    focusTitle,
    ...focusPoints,
    "",
    "You are given the PR diff below. Read the surrounding code in the working",
    "tree for context. Do not modify any files.",
    "",
    "Only report concrete findings introduced by, or made reachable by, this PR.",
    "",
    "Every finding MUST be anchorable as an inline GitHub annotation:",
    "- file: repository-relative path exactly as it appears in the diff",
    "- line: a line number in the POST-change file that appears in the diff",
    "- endLine: only when the finding genuinely spans a range",
    "- severity: P0, P1, or P2",
    "- title: one short line",
    "- explanation: why it is wrong, grounded in the code",
    "- suggestion: the concrete fix",
    "",
    "Derive line numbers from the @@ hunk headers. A finding you cannot anchor",
    "to a specific changed line must instead point at the most relevant changed",
    "line in the file it affects.",
    "",
    "Keep explanation and suggestion tight; they become inline comments, not a",
    "report. Return an empty findings array if there is nothing concrete.",
    "",
    "=== PR DIFF ===",
    diff
  ].join("\n");
}

const reviews = await runs.all([
  {
    key: "correctness",
    label: "Review PR correctness",
    agent: "reviewer",
    context: "fresh",
    task: reviewTask("Focus exclusively on correctness:", [
      "- bugs and regressions",
      "- incorrect assumptions",
      "- edge cases",
      "- concurrency/state issues",
      "- error handling"
    ]),
    outputSchema: reviewSchema
  },
  {
    key: "tests",
    label: "Review PR tests",
    agent: "reviewer",
    context: "fresh",
    task: reviewTask("Focus exclusively on testing and validation:", [
      "- missing tests",
      "- weak assertions",
      "- untested edge cases",
      "- regressions not covered by tests",
      "- tests that pass while behavior is wrong"
    ]),
    outputSchema: reviewSchema
  },
  {
    key: "maintainability",
    label: "Review PR maintainability",
    agent: "reviewer",
    context: "fresh",
    task: reviewTask("Focus on design and maintainability:", [
      "- unnecessary complexity",
      "- duplication",
      "- problematic abstractions",
      "- surprising behavior",
      "- API/design inconsistencies",
      "",
      "Do not report stylistic preferences unless they create a concrete",
      "maintenance risk."
    ]),
    outputSchema: reviewSchema
  }
]);

const rawFindings = reviews.flatMap(
  (review) => review.structuredOutput?.findings || []
);

const synthesis = await runs.run("synthesis", {
  label: "Synthesize PR review findings",
  agent: "reviewer",
  context: "fresh",
  task: [
    `Consolidate the independent review findings for ${target}.`,
    "",
    "Rules:",
    "- Deduplicate overlapping findings; keep the highest severity and the",
    "  clearest explanation.",
    "- Do NOT invent findings.",
    "- Drop speculative findings that the diff does not support.",
    "- Prefer concrete correctness issues over stylistic preferences.",
    "- Preserve the file and line of every finding you keep; they become inline",
    "  GitHub annotations.",
    "- Keep each explanation and suggestion short enough to read as an inline",
    "  comment.",
    "- Order findings by severity, highest first.",
    "",
    "The summary field must be at most three sentences. It becomes the review",
    "body, so it must NOT restate the findings.",
    "",
    "Changed files in this PR:",
    (pr.changedFiles || []).join("\n"),
    "",
    "=== CANDIDATE FINDINGS (JSON) ===",
    JSON.stringify(rawFindings, null, 2),
    "",
    "=== PR DIFF ===",
    diff
  ].join("\n"),
  outputSchema: reviewSchema
});

// Stable ids let the user approve a subset conversationally ("publish F1 and F3").
const findings = (synthesis.structuredOutput?.findings || []).map(
  (finding, index) => ({ id: `F${index + 1}`, ...finding })
);

emit(
  findings.length === 0
    ? "No actionable findings. Nothing to publish."
    : `${findings.length} finding(s) ready for user assessment.`
);

return {
  state: findings.length === 0 ? "no_findings" : "needs_ack",
  target,
  pr: {
    repo: pr.repo,
    number: pr.prNumber,
    title: pr.title,
    url: pr.url,
    headSha: pr.headSha,
    changedFiles: pr.changedFiles,
    omittedFiles: pr.omittedFiles || []
  },
  summary: synthesis.structuredOutput?.summary || "",
  findings,
  // Everything the parent needs to POST the review itself.
  publishTarget: pr.repo
    ? {
        endpoint: `repos/${pr.repo}/pulls/${pr.prNumber}/reviews`,
        commitId: pr.headSha
      }
    : { endpoint: null, commitId: pr.headSha },
  nextStep:
    findings.length === 0
      ? "Report that there is nothing to publish. Do not publish."
      : [
          "Present each finding by id, severity, file:line and suggested fix.",
          "Ask the user which ids to publish, then publish them yourself as",
          "inline annotations. Publish nothing before the user answers, and do",
          "not delegate the question to a child agent."
        ].join(" ")
};

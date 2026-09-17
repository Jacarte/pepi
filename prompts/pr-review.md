---
description: Review the current PR, get user ACK, then publish inline annotations
---

Review $@ and publish the result only after I approve it.

## 1. Run the review engine

```
subagent({ workflowScriptPath: "workflows/pr-review.ts", args: { target: "$@" } })
```

The workflow collects the PR diff, runs parallel correctness / tests /
maintainability reviews, synthesizes them, and returns:

- `findings[]` — each with `id`, `severity`, `file`, `line`, `endLine`,
  `title`, `explanation`, `suggestion`
- `summary` — short overall summary
- `pr` and `publishTarget` — repo, number, `headSha`, API endpoint

It never publishes. If `state` is `no_findings`, tell me and stop.

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

For a range finding add `"start_line"` with `"start_side": "RIGHT"`. Write the
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
  `gh pr diff`; for an unanchorable finding use `"subjectType": "file"` and omit
  `line`. On rejection, retry once with the offending comments converted to
  file-level — never fall back to a body dump.
- Always a non-blocking `COMMENT`. Never approve, never request changes.
- Never modify, commit, push, or merge.

Report the PR URL and how many inline comments were posted.

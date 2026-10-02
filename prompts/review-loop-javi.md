---
description: Check for Copilot/Claude review comments, triage them, apply fixes, and loop up to 3 times
argument-hint: "<PR_URL> [auto]"
---

Poll $1 for new review feedback, triage and fix each iteration, re-trigger Claude, loop bounded to 3 times.

Mode: if $2 is exactly `auto`, run the full loop hands-free (auto-push and auto-resolve); otherwise, ask before each push.

## 1. Preflight

Verify `gh auth status` succeeds. Parse `$1` as owner/repo/number from the PR URL (format: `https://github.com/OWNER/REPO/pull/NUMBER`); if `$1` is empty or malformed, stop and ask the user.

Fetch PR metadata:
```
gh pr view "$1" --json number,headRefName,headRepositoryOwner,headRepository,isCrossRepository
```
Verify the local repo matches the PR's repo:
```
gh repo view --json nameWithOwner -q .nameWithOwner   # must equal OWNER/REPO
```
If repos don't match, stop and ask the user.

Ensure the PR head branch is checked out:
```
if jj root >/dev/null 2>&1; then
  jj git fetch
  jj bookmark track <headRefName>@origin 2>/dev/null || true   # no-op if already tracked
  jj new <headRefName>
else
  gh pr checkout "$1"
fi
```

## 2. Loop: Max 3 iterations

For each iteration (1–3):

### 2a. Poll for new bot review feedback (60-second intervals, max ~20 polls)

Query the PR for unresolved review threads and top-level comments authored by Copilot or Claude bots. Track processed comment/thread IDs to avoid re-triaging.

Fetch the current user's login once:
```
USER_LOGIN=$(gh api user --jq .login)
```

**Inline review threads** (GraphQL, with pagination):
```
gh api graphql -f owner=OWNER -f repo=REPO -F prNumber=NUMBER \
  -f query='query($owner:String!, $repo:String!, $prNumber:Int!, $after:String) {
    repository(owner:$owner, name:$repo) {
      pullRequest(number:$prNumber) {
        reviewThreads(first:100, after:$after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id isResolved path line startLine createdAt
            comments(first:100) { nodes {
              id author{login} body createdAt updatedAt isMinimized
            }}
          }
        }
      }
    }
  }'
```
Repeat with `$after` cursor until `hasNextPage` is false.

**PR reviews** (REST, lists review summary and body):
```
gh api repos/OWNER/REPO/pulls/NUMBER/reviews --paginate --jq '.[] | select((.body // "") != "")'
```

**Top-level PR comments** (REST, for Claude's summaries):
```
gh api repos/OWNER/REPO/issues/NUMBER/comments --paginate
```

**Filter all comments for:**
- `author.login` (or `user.login` in REST) case-insensitively contains 'copilot' OR 'claude'
- Comment `createdAt` (or `created_at` in REST) **or** `updatedAt` / `updated_at` is after the previous iteration's `/review` trigger timestamp (first iteration: all unresolved threads and all bot comments)
- Exclude inline comments from threads where `isResolved` is true
- Exclude the user's own comments (login == `$USER_LOGIN`)
- Skip Claude "working…" placeholder comments (check if `body` matches claude-code-action's working state; if unclear, re-poll in the next pass rather than triage)

**Polling behavior:**
- Iteration 1: check immediately, then begin polling.
- Iterations 2+: first poll immediately, then `sleep 60` between polls.
- Once at least one new item is found, do one more poll for late arrivals, then advance to 2b.
- If no new comments after ~20 polls (≈20 min), **end the iteration and do not post `/review`; advance to § 3 (end summary)**.

### 2b. Triage each new comment

For each bot comment (inline thread or top-level):

**a) Not worth it, incorrect, or nitpick:**
   - Reply with a short, concrete reason: e.g. "This is a style preference outside our scope" or "The code path handles this already."
   - For **inline threads**: use GraphQL mutation to reply and then resolve:
     ```
     gh api graphql -f threadId="THREAD_ID" -f body="REPLY" \
       -f query='mutation($threadId:ID!,$body:String!) {
         addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId, body:$body}) {
           comment { id }
         }
       }'
     gh api graphql -f threadId="THREAD_ID" \
       -f query='mutation($threadId:ID!) {
         resolveReviewThread(input:{threadId:$threadId}) {
           thread { id isResolved }
         }
       }'
     ```
   - For **top-level comments** (PR comments or review bodies): reply to the PR with a comment that references the original:
     ```
     gh pr comment "$1" --body "Re comment #COMMENT_ID: REASON"
     ```
     (Top-level comments cannot be resolved; replying closes the implicit thread.)
   - Record the comment/thread ID as processed.
   - Continue to the next comment.

**b) Worth fixing:**
   - Add a one-line summary to the fix list (e.g. "Add null check in parseConfig at line 42").
   - Continue to the next comment.

**c) Unsure whether to address or dismiss:**
   - **STOP and ask the user for clarification** (both in auto and manual mode).
   - Include the bot's comment verbatim and your best interpretation of why you're unsure.
   - Do not proceed until the user replies.
   - Once clarified, handle it as (a) or (b).

### 2c. Apply all fixes from this iteration

If the fix list is empty, skip to 2e (decide whether to post `/review` based on whether anything changed in this iteration).

If not empty:

1. Follow ~/.pi/agent/AGENTS.md delegation rules: large edits, complex checks, or multi-part fixes may be delegated to a `worker` subagent.

2. Create a new commit slice:
   - **jj** (if `jj root` succeeds):
     ```
     jj new <headRefName>
     # edit the working tree
     jj commit -m "fix review feedback: <one-line summary>"
     ```
     The fix commit is `@-`. Record the commit ID:
     ```
     COMMIT_ID=$(jj log -r @- -T commit_id --no-graph)
     ```
   - **git** (fallback):
     ```
     # edit the working tree
     git add <changed-paths>
     git commit -m "fix review feedback: <one-line summary>"
     COMMIT_ID=$(git rev-parse HEAD)
     ```

3. Run the repo's relevant checks (e.g. tests, linting) on the changed files. If checks fail:
   - In **auto mode**: abort this iteration (do not push); stop the loop and report the failure.
   - In **manual mode**: report the failures and ask the user how to proceed.

4. Record the new commit ID in a variable for 2d.

### 2d. Push and reply (auto vs. manual mode)

If mode is **auto**:
- Update the jj bookmark (if jj):
  ```
  jj bookmark set <headRefName> -r "$COMMIT_ID"
  jj git push --bookmark <headRefName>
  ```
- Or git:
  ```
  git push
  ```
- Reply to each addressed inline thread:
  ```
  gh api graphql -f threadId="THREAD_ID" -f body="Addressed in commit $COMMIT_ID." \
    -f query='mutation($threadId:ID!,$body:String!) {
      addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId, body:$body}) {
        comment { id }
      }
    }'
  gh api graphql -f threadId="THREAD_ID" \
    -f query='mutation($threadId:ID!) {
      resolveReviewThread(input:{threadId:$threadId}) {
        thread { id isResolved }
      }
    }'
  ```
- Reply to each addressed top-level comment:
  ```
  gh pr comment "$1" --body "Re comment #COMMENT_ID: Addressed in commit $COMMIT_ID."
  ```
- Continue to 2e.

If mode is **manual**:
- Show the commit summary and diff.
- Ask: "Push this commit, reply 'Addressed', and resolve threads?"
- If no: stop the loop and report what was not pushed; do not post `/review`; advance to § 3.
- If yes: execute the push/reply/resolve steps above, then continue to 2e.

### 2e. Decide whether to re-trigger Claude

If this iteration resulted in at least one reply, resolution, or fix:
  - If iteration < 3:
    ```
    REVIEW_TIMESTAMP=$(gh api repos/OWNER/REPO/issues/NUMBER/comments -f body="/review" --jq .created_at)
    ```
    Record `$REVIEW_TIMESTAMP`. Only comments with `createdAt` (or `updated_at`) after this time count as new in the next iteration.
    Wait ~10 seconds for Claude to start processing, then return to 2a for the next iteration.
  - If iteration == 3:
    Do not post `/review`. Advance to § 3.

If this iteration had no replies and no fixes (all bot comments were dismissed or timed out with no new arrivals):
  - Do not post `/review`.
  - Advance to § 3 (end summary).

## 3. End-of-loop summary

Report:
- **Per iteration:**
  - Iteration N: X comments found, Y addressed, Z dismissed, Q asked user for clarification, [commits pushed or none].
- **Total:** unresolved inline threads and top-level bot comments at the end.
- **User interactions:** any clarifications requested or manual confirmations made.
- **If stopped early:** reason (e.g., idle timeout, no fixes needed, user rejected push, check failure in auto mode).

## Rules

- **Never assume approval** in manual mode; ask before push.
- **Never re-triage a comment** once processed (track comment/thread IDs and their `updatedAt` timestamps).
- **Never push, reply 'Addressed', or resolve** in manual mode without explicit user confirmation.
- **Never merge the PR.**
- **Bounded to 3 iterations**: do not loop beyond that.
- **Idle bound**: if no new comments in ~20 polls (~20 min), end the iteration without posting `/review`; advance to § 3.
- **Empty fix list**: if all bot comments are dismissed, skip commit/push but still post `/review` only if something changed (i.e., at least one thread was resolved or replied to); otherwise end the loop.
- **When unsure** (e.g., is this a style issue worth fixing?), **stop and ask the user for clarification; include the bot's comment verbatim and your best interpretation of why you're unsure**. Do not proceed until the user replies; when they do, handle it as a dismiss or a fix.

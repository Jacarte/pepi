---
name: herdr-mode
description: Run opt-in herdr collaboration mode, where the parent pi session spawns interactive pi worker panes beside itself, waits on herdr agent state, reads pane tails, and integrates their jj workspaces mechanically. Use only when the user explicitly asks for herdr mode and HERDR_ENV=1; pi-subagents stays the default otherwise.
---

# Herdr collaboration mode

Opt-in only. Check `test "${HERDR_ENV:-}" = 1`; if it fails, or the user did not ask
for herdr mode, use pi-subagents as usual. The tools come from
`~/.pi/agent/extensions/herdr-collab.ts` and exist only inside herdr.

## Tools, in order

1. `herdr_spawn` — a wave of 1–4 panes: `{name, role, task, context?, model?}`.
   Roles are the pi-subagents roles (scout, worker, deep-worker, reviewer, oracle,
   researcher, evidence-auditor, delegate) with the same prompts, tools, skills, and
   extensions. Without `model`, `worker` and `delegate` are routed in-process like
   `jev_route recommend`; every other role (deep-worker, reviewer, oracle, the fixer…)
   keeps its pinned settings model, and is routed only when none is configured (the
   result says so). Routing failure fails the spawn and nothing is created.
   Layout: the parent pane is split once, side by side, and keeps the full-height left
   half. Every later pane (workers, reviewers, fixer) splits the run pane with the
   largest on-screen area (oldest on ties), to the right if it looks wide (width ≥ 2 ×
   rows) or below if it looks tall. Geometry is read live, so closed panes free space.
2. `herdr_wait` — blocks until panes settle (default 30 min). `idle`/`done` mean
   settled, not successful; `blocked` needs input; `timeout` means still working.
3. `herdr_collect` — last N lines of each pane (`truncated` flag), the writer's
   workspace `diff --stat`, and its message log. Workers end with `STATUS: ...`.
4. `herdr_merge` — integrate finished writers (see below).
5. `herdr_review` — review panel, then `herdr_wait` + `herdr_collect`.
6. `herdr_cleanup` — close panes; `removeWorkspaces: true` then deletes the
   workspaces of closed panes only, after a fresh snapshot.

`herdr_peers` / `herdr_send` show panes and the message log, and deliver a message
when the target is idle (never mid-turn).

## Waves, merge, conflicts

- A lone writer works in the main checkout; `herdr_merge` never integrates that
  output. Parallel writers each get a jj workspace in `~/.herdr-ws/<run>/<name>`
  (git worktree when `jj root` fails), based on main `@-` or the sealed integration
  change, so they do not see uncommitted work in `@`.
- After a main-checkout writer, a parallel wave in the same run is refused unless you
  pass `base` (commit or describe that work first), or start a new run; the guard
  ends once the run has an integration.
- Before pi starts in a run-owned workspace (writer, fixer, or reviewer on the
  integration), herdr runs `codegraph init` there once (best-effort, 60 s, never the
  main checkout); the outcome (`ok`, `skipped-exists`, `failed:…`, `missing`) is in
  the spawn result, and `.codegraph/` is never tracked.
- The parent never edits code. After a wave settles and tails show `STATUS: done`,
  call `herdr_merge`: it runs `jj new w1 w2 ...` in `~/.herdr-ws/<run>/integration`.
  - Clean: the merge is sealed and panes are marked merged. Panes stay alive and
    workspaces stay on disk until `herdr_cleanup`. Merge refuses a pane with a
    queued message for it.
  - Conflict: all wave workspaces are kept and one `deep-worker` fixer pane starts in
    the integration workspace with the conflicted files and both sides' tasks. Wait
    for it, collect, then call `herdr_merge` with no workers to re-check and seal.
  - jj: merge refuses while the integration working copy has edits after the
    sealed head (e.g. a late fixer turn); `jj squash` them in there, then merge again.
- Merging creates a new, empty working-copy commit in each merged worker's workspace
  (the merged work sits in `@-`), so a post-merge `jj diff` there looks empty.
- The next wave (`herdr_spawn` with the same run) starts from the sealed change;
  merged panes still count toward the 4-pane cap, so clean them up first if needed.
- Cleanup deletes a workspace only after its pane is closed (or never opened) and no
  conflict is pending. A workspace with content not in its merged commit (or never
  merged) is kept and its merge status cleared; run `herdr_merge` again, which keeps
  the sealed head as a parent. The integration workspace is kept while worker
  workspaces, open panes there, or edits after the seal remain. Git cleanup pins the
  result as `refs/herdr/<run>`.
  Report the integration change id; adopting it in the main checkout is the user's call.

## Review panel

`herdr_review({target, reviewers: [{name, lens}]})`: up to 3 read-only reviewers,
each with a different lens, on the shared checkout (sealed integration workspace if
present). The integrated diff is written to a file they can read. Each ends with
`VERDICT: ...`; merge verdicts from the tails. If a tail is truncated, ask that
reviewer to repeat its verdict with `herdr_send`. Reviewers cannot message peers.

## Messaging

Writers may `herdr_send` peers; reviewers and read-only roles cannot, and nobody
except the parent can message a reviewer. Per pair and direction: 5 messages per run.
Self-sends and unknown targets are refused. Every message is logged in
`~/.herdr-ws/<run>/run.json`; `unconfirmed` means the text reached the screen but no
turn was observed. Direct mutual waits are refused; longer cycles (A→B→C→A) are only
bounded by each send's timeout, so keep messages one-way.

## Safety

- Max 4 open worker panes and 3 reviewer panes per run; clean up before more.
- Never push or create bookmarks/branches. Herdr tools never snapshot the main
  checkout except `jj workspace add` with auto-track disabled.
- Never close panes you did not create; cleanup never closes the parent pane.
- Avoid herdr mode on `~/.pi/agent` itself: workspace copies of its `AGENTS.md` are
  filtered by content, but its untracked local files make jj work there risky.
- Panes stay open until `herdr_cleanup`; the run registry survives restarts, so pass
  `runId` after a restart.

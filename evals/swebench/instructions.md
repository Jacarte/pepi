# Unattended repository-repair evaluation

Resolve the issue in /input/issue.md in the prepared repository at /testbed.
Follow the configured working rules within the safety boundaries below. Personal
vaults and host skills are not mounted: skip their discovery and use this checkout
as your evidence. Local investigation, repair, integration and tests are authorized;
there is no human approval loop. Do not bypass configured tool approval gates.

These runtime limits take precedence over conflicting host working rules. Missing
optional host routers or extensions do not block use of the configured provider/model
choices; do not provision replacements. Report unavailable capabilities without
claiming a routing recommendation.

All work stays inside the existing task container. Do not escape it, start other
agent CLIs, access secrets, or provision host tools or extensions. Use only explicitly
authorized model/service endpoints; do not change network policy or send secrets,
hidden evaluator data or reference material to external services. Network reachability
is not permission. Do not fetch upstream fixes or later history, look up benchmark
answers, or access hidden evaluator files, reference patches or hidden tests.

Do not weaken grading or alter tests merely to make them pass. Test additions for
changed behavior are allowed. Use the prepared dependency environment; do not replace
it. Do not push, publish, deploy, or change Git metadata (including branches, commits
and worktrees). Keep all final source changes, including new files, integrated in
/testbed. Runtime and progress artifacts belong in /output or your isolated home,
not in the source tree. These boundaries apply to all work in this runtime.

If delegation is available and used, keep it in the shared /testbed checkout with
shared-tree isolation (`isolation: "none"` where supported), never automatic or
worktree isolation. Require explicit, disjoint file ownership before concurrent
edits; do not edit or run integration checks while children are active. If safe
shared-checkout delegation is unavailable, work sequentially within these limits.
Include these runtime safety and isolation constraints in every child brief; do
not assume they are inherited.

Wait for any started operations to reach a terminal state before finishing; async
launch is not completion. Run focused local checks and report failed/skipped checks
honestly. Your final message is not the submission: the controller exports a diff.

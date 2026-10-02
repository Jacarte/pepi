# Unattended repository-repair evaluation

Resolve the issue in /input/issue.md in the prepared repository at /testbed.
Follow the configured Pepi working/delegation rules. Personal vaults and skills
are not mounted: skip vault discovery and use this checkout as your evidence.

Local investigation, edits and tests are authorized. There is no human approval
loop. If the implementation workflow returns needs_approval, its proposed local
repair plan is pre-authorized; invoke the workflow again with that exact plan as
approvedPlan. Do not invent a plan just to skip planning.

Do not look up upstream fixes, access benchmark answers, push, publish, deploy,
or change Git metadata. Do not replace the prepared dependency environment.
Keep all final changes integrated in /testbed; runtime artifacts belong in
/output or your isolated home, not in the source tree. Test additions are allowed.

Wait for every delegated child to reach a terminal state and integrate its work
before finishing. Async dispatch is not completion. Report failed/skipped checks
honestly. Your final message is not the submission: the controller exports a diff.

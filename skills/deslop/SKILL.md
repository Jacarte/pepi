---
name: deslop
description: Remove AI-generated code slop from the current branch. Use when cleaning up before a PR: unnecessary comments, defensive checks, `any` casts, and style that is inconsistent with the file.
---

# Remove AI code slop

Check the diff against main, and remove all AI generated slop introduced in this branch. Leave code that was already on main alone.

## Steps

1. List what this branch changed: `git diff main...HEAD --stat`, then read the diff of each changed file. Only lines added or changed in this branch are in scope.
2. Find slop in those lines:
   - Extra comments that a human wouldn't add, or that are inconsistent with the rest of the file.
   - Extra defensive checks or try/catch blocks that are abnormal for that area of the codebase, especially when called by trusted or validated code paths.
   - Casts to `any` to get around type issues.
   - Any other style that is inconsistent with the file.
3. Remove each item. Keep checks and comments that guard real external input or explain non-obvious behavior.
4. Run the project's tests and type check. Behavior must not change.
5. Report with only a 1-3 sentence summary of what you changed.

## Error recovery

| Problem | Fix |
| --- | --- |
| No `main` branch, or `git diff main...HEAD` fails | Use the repository's default branch (`git symbolic-ref refs/remotes/origin/HEAD`), or the task's stated base. |
| Unsure whether a check is slop | Look at the callers: if the input is already validated upstream, remove it. If it guards external input, keep it. |
| Tests fail after a removal | Revert that removal; the check or comment was load-bearing. |
| The diff is empty or only touches other files | Report that there is nothing to clean up. Do not edit unrelated code. |

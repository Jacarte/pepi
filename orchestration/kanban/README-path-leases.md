# Modifying path leases

A modifying task owns its declared repository paths from the moment it is claimed until its reviewed handoff is integrated or the task is otherwise made terminal/blocked.

Worker capacity and path ownership are intentionally different concepts:

- `implementation` / `fix` consume worker capacity and path ownership.
- `verification` / `review` / `integration` do not consume worker capacity, but still hold path ownership for modifying tasks.
- non-modifying tasks hold no path ownership.

This prevents a second writer from starting against files whose accepted changes still exist only as an isolated handoff artifact.

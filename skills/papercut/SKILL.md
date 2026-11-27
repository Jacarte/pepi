---
name: papercut
description: Log friction with a skill as one JSON line in the papercut log, using bin/papercut. Use when a skill's flag is rejected, a command exits 0 despite an error, a result is empty when it should match, or the same step is retried repeatedly.
---

# Papercut: log skill friction

Friction is a small, specific gap between what a skill says and what happens. Log it
so the skill can be fixed later. Do not stop the task to write a report.

## Steps

1. Notice friction: a flag is rejected, a command exits 0 on error, a result is empty
   when it should match, or you retried the same step more than twice.
2. Finish or work around the task first. Log each friction once, not once per retry.
3. Check the log for a duplicate: `tail -n 20 ~/.pi/agent/logs/papercuts.jsonl`.
4. Append the record:

   ```bash
   ~/.pi/agent/bin/papercut --skill <skill-name> \
     --tried "<exact command or step>" \
     --expected "<what the skill said would happen>" \
     --happened "<what happened, with error text>" \
     --fix "<concrete change to the skill or tool>"
   ```

5. Fields (the helper adds `ts` as UTC time):
   - `skill`: the skill's directory name.
   - `cwd`: defaults to the current directory; pass `--cwd DIR` when it differs.
   - `tried`: the exact command or step, short.
   - `expected`: what the skill said should happen.
   - `happened`: what actually happened, with the trimmed error text.
   - `fix_suggestion`: a concrete change to the skill or tool.
6. Keep each field under about 300 characters. Never log secrets, tokens, or
   transcript content.
7. Check the exit code: 0 means logged. Then continue the task.

## Troubleshooting

- Exit 2 (usage): a required flag is missing or has no value. Fix the arguments and
  run it once more.
- Exit 1 (write failed): the log directory is not writable. Mention it once in the
  final answer and continue.
- Log path override: set `PAPERCUT_LOG=/path/file.jsonl` for a test run.
- Check the last line parses:
  `tail -n 1 ~/.pi/agent/logs/papercuts.jsonl | node -e 'JSON.parse(require("fs").readFileSync(0,"utf8"))'`
- Do not edit the log by hand. Do not log the user's own mistakes as skill friction.

---
name: codemode-guidance
description: Guidance for Pi's built-in codemode tool, which runs a JavaScript async-function body that calls other tools as tools.<name>(args) and returns only the script's output. Use when a task needs many tool calls, a known-shape sequence of calls, fan-out over files or items, search-then-read distillation, bulk data that must not enter context, edit-then-verify as one unit, or polling. Use when choosing between direct tool calls, codemode, and a subagent.
---

# Codemode guidance

Codemode is a tool named `codemode`. Its input is raw JavaScript, not JSON and not a code fence. The code runs as the body of an async function in a QuickJS-WASM sandbox, so top-level `await` and `return` work. Only the script's output (`return`, `text()`, `console.log`) reaches the model. Intermediate results stay in the script.

The sandbox has no Node APIs, no file system, no network, and no timers. Scripts reach the outside world only through tools, and those tools run with the host's privileges. The sandbox is not a security boundary: a script can read or write anything a tool can.

## Steps

1. Count the calls the task needs. One call, or a result that changes the next call, means direct tools.
2. If the calls have a known shape or are independent, pick codemode. If a step needs per-item judgment, pick a subagent, or collect with codemode first (hybrid).
3. Find the tool names and arguments you need with `searchTools` and `describeTool` inside the script, unless they are already listed.
4. Write the script: `Promise.all` for independent calls, a `// @options` line with `timeout_ms`, and a compact `return`.
5. Verify exactness: compare the counts the script saw with an independent count (for example `wc -l`, adding one for a non-empty file with no final newline; see the practices), and report `truncated` when any output was cut.
6. Read the result. If the script failed, use the troubleshooting table before rerunning, because earlier tool calls already ran.

## Decision table

| The work | Use |
| --- | --- |
| One call, or each result changes what you do next | Direct tool calls |
| A known sequence of calls with a fixed shape | Codemode |
| Fan-out over many files or items, independent of each other | Codemode with `Promise.all` |
| Search, then read the hits and keep only a distilled answer | Codemode |
| Bulk data that must not enter the context (large files, long command output) | Codemode, return a summary |
| Edit a file, then verify it, as one unit | Codemode |
| Polling until a condition holds | Codemode with a `timeout_ms` option. Scripts have no timers, so each poll needs a `bash` call with `sleep N` between checks, and every call counts toward the 256-call nested record cap, so bound the number of polls |
| Per-step judgment, design, or review | Subagent (`worker`, `reviewer`, `oracle`, `scout`) |
| Script collects data, then judgment is needed on it | Hybrid: codemode collects, then the model calls the subagent |

Scripts cannot call `subagent`, `contact_supervisor`, or `structured_output`. Do not try. When a step needs a decision, end the script, read its compact output, and make that call yourself or delegate it.

## Practices

- Put `// @options: {"timeout_ms": 120000}` on the first line for any script that runs more than a few seconds. There is no default timeout. Set `max_output_tokens` only when you need more output than the default 10000 tokens.
- Use `Promise.all` for independent calls. Use `Promise.allSettled` when a partial result is useful. A rejected call throws an `Error` with the tool's error text.
- Discover tools before calling them: `await searchTools("read file", { limit: 5 })`, then `await describeTool(name)` for the declaration. The `tools.<name>` names are identifiers: characters invalid in an identifier become `_`.
- Return only a compact value. Aggregate counts, filter lines, and keep the top N in the script. Do not return whole files.
- Pi's `read` tool returns at most about 2000 lines or 50KB per call, so page through large files with `offset` and `limit`. Count what you read and compare the total with the expected count (for example, `wc -l` through `bash`). `wc -l` counts `\n` bytes only, so add one for a non-empty file that does not end in `\n`. Report a `truncated` flag and the verified counts whenever exactness matters.
- `bash` resolves to `{ output, truncated, full_output_path?, exit_code, wall_time_seconds }`, including for non-zero exit codes. Check `exit_code` yourself.
- Tool calls are not rolled back when a script fails. A write made before the failure stays. Design edits so a failure is visible and recoverable.
- A nested call record holds at most 256 calls. Beyond that, calls are dropped from the record. Do not build designs that depend on a full call log; keep fan-out bounded and aggregate it.
- Keep paths inside the project root. Do not read secrets (`.env`, `auth.json`, key files, credential stores), and do not print environment variables.
- `store(key, value)` and `load(key)` keep small JSON state across codemode calls. Do not store large data or images in them.

## Worked examples

Search, then distill (the script returns only the matched lines that matter, with counts):

```js
// @options: {"timeout_ms": 120000}
const hits = await tools.grep({ pattern: "TODO\\(owner\\)", path: "src" });
const files = [...new Set(String(hits).split("\n").map((l) => l.split(":")[0]).filter(Boolean))];
const lines = String(hits).split("\n").filter(Boolean);
return { files: files.length, matches: lines.length, sample: lines.slice(0, 20) };
```

Fan-out and aggregate (read each file in parallel, reduce in the script, return only the totals):

```js
// @options: {"timeout_ms": 300000}
const paths = ["a.md", "b.md", "c.md"];
const results = await Promise.allSettled(paths.map((p) => tools.read({ path: p })));
let words = 0, failed = [];
results.forEach((r, i) => {
  if (r.status === "fulfilled") words += String(r.value).split(/\s+/).filter(Boolean).length;
  else failed.push({ path: paths[i], error: String(r.reason) });
});
return { files: paths.length, words, failed };
```

Edit then verify (one unit; the script returns the verification result, and the edit result is checked before verifying):

```js
// @options: {"timeout_ms": 180000}
const edit = await tools.edit({
  path: "src/config.ts",
  edits: [{ oldText: "const RETRIES = 2;", newText: "const RETRIES = 3;" }],
});
const test = await tools.bash({ command: "npm test -- config", timeout: 150 });
return { edit: String(edit).slice(0, 200), exit_code: test.exit_code, tail: String(test.output).slice(-1500) };
```

## Troubleshooting and error recovery

| Problem | Fix |
| --- | --- |
| Script failed; partial output shows a `Script error:` line | Calls before the failure already ran. Read the partial output, check what changed on disk, then rerun only the missing part. |
| `tools.<name> is not a function` | The name is not callable here. Run `searchTools` or `ALL_TOOLS` to find the real name, and use `_` for invalid characters. |
| Script hangs or is cancelled | Add or lower `timeout_ms`. Calls still running at script end are cancelled, so await everything you need. |
| Output was cut short | Return less: aggregate in the script. If you need the whole text, the result names a temp file path; read that file with `read`, page by page. |
| `read` output looks complete but the count is off | `read` truncates at about 2000 lines or 50KB. Page with `offset` and `limit`, and compare the line count with `wc -l`, adding one when the file does not end in a newline. |
| A `bash` result shows `exit_code` non-zero but no exception | Expected: `bash` resolves for non-zero exits. Check `exit_code` and throw or return it yourself. |
| Fan-out returns many failures | Use `Promise.allSettled`, report the failed paths, and retry them in a separate, smaller script. |
| The task needs judgment on each item | Stop scripting. Collect with codemode, then hand the compact result to a subagent. |
| Unsure whether an edit ran | Read the file back in the same script before reporting success. |

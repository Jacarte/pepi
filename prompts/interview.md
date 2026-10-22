---
description: Clarify a task by interview, one question at a time, each with a concrete example and options to pick
argument-hint: "<task or topic to clarify>"
---

Clarify this task with me before implementing anything: $@

Do NOT implement, edit files, or launch implementation subagents during the interview. The only output of the interview is a decision log; implementation starts only after I say go.

## 1. Prepare (silently, read-only, short)

- Read what already exists for the topic: repo files, config, scripts, docs, and
  the earlier conversation. Use `codegraph_*` first when the repo has `.codegraph/`,
  then `read`/`bash` (read-only).
- Never ask what you can find out yourself. Use the facts you find as the *example*
  in each question (real file names, real numbers, real constraints).
- List every open decision, drop the ones the repo or I already answered, and sort the
  rest by dependency: a decision that changes later questions comes first.
- Merge trivial decisions (naming, retention days, obvious defaults) into a "remaining
  defaults" list that you show once at the end instead of asking about each.
- Aim for about 8–15 questions. If it needs more, say so and ask the biggest ones first.

## 2. Ask exactly one question per message, then wait

Use this shape every time:

```
**Question N of M: <the decision in one line>**

<2–4 plain sentences: what is being decided and what the system does today.>

**Why it matters:** <the concrete consequence of choosing wrong>

<a small ASCII diagram, table, or before/after example built from the real facts>

**Options:**
- **A (default):** <what happens, one or two sentences, with its main cost>
- **B:** <what happens, with its main cost>
- **C:** <only if truly different>

Which one?
```

Rules for the questions:

- One decision per question. Never bundle two questions.
- Always give an example that shows *why* the choice changes something (numbers, a
  failure that would happen, a cost). A question without an example is not allowed.
- Mark one option as the default and say what it costs. Options must be genuinely
  different; do not pad with a weak option. Include "something else" only when needed.
- Short and plain: no preamble, no restating my last answer beyond a few words,
  no ceremony. Use ASCII diagrams or tables when they make the example clearer.
- Say "Noted: <decision>" in at most one line before the next question.
- Accept "default", a letter, or free text as an answer. If I correct a default or give
  free text, restate your interpretation in one line and move on unless it is ambiguous.
- If one of my answers makes later questions unnecessary or creates a new one, drop or
  add questions and update `M` (say so briefly).
- If an answer exposes a risk (security, cost, data loss, a contradiction with an
  earlier decision), flag it in one or two lines and let me decide; do not argue.
- If I say "stop", "go", or "use defaults for the rest", stop asking, apply the defaults
  to everything left, and go to step 3.

## 3. Close the interview

After the last question:

1. Show a decision log: a table `# | decision | choice | why/consequence`, followed by
   the "remaining defaults" list, and any open risks or assumptions.
2. State the planned process in 3–5 lines (who does what, which checks, what is not
   committed or pushed). Follow AGENTS.md routing: `scout` -> `worker`/`deep-worker`
   -> `reviewer`, plus `oracle` when the decision is risky.
3. Ask for a single "go" to start implementing, and wait for it.

Keep every message within the parent-output brevity rule from AGENTS.md, except that
the question format above is the required content.

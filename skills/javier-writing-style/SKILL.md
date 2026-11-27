---
name: javier-writing-style
description: "Write technical blog posts and documentation in Javier Cabrera's signature style. Use when creating: (1) Technical blog posts on engineering topics (WebAssembly, Kubernetes, DevOps, security), (2) In-depth tutorials with pragmatic problem-solution structure, (3) Technical documentation emphasizing real-world context and performance metrics, (4) Articles that need problem-first narrative, quantified results, and balanced exploration of trade-offs"
compatibility: opencode
license: MIT
metadata:
  author: Javier Cabrera Arteaga
  version: 1.0
---

# Javier's Technical Writing Style

Javier's writing combines technical depth with narrative clarity. His approach works because it respects readers' time while building understanding through context, not volume.

## Core Writing Architecture

### 1. Open with the Problem (Not the solution)

**Why**: Readers need to understand why this matters before learning how it works.

- Start with a **tangible pain point**: Build cache losses in CI, ephemeral runners, slow deploys
- Explain the **impact**: Business (cost, throughput) and developer experience (feedback loops, velocity)
- Avoid jumping to "we built X" — first establish why X was necessary

**Example structure:**
```
[Problem statement] → [Why this matters] → [Industry context/standard approach] → [Why that's insufficient] → [Our solution]
```

### 2. Explain the Real Culprit (Diagnosis before treatment)

**Why**: Readers understand the mechanics, preventing cargo-cult adoption.

- Identify the **root cause**, not just the symptom
- Show why intuitive solutions fail (e.g., `--cache-to/from` doesn't solve BuildKit's `--mount=type=cache`)
- Quote industry approaches that miss the point
- Use concrete examples of what breaks and why

### 3. Structure: Problem → Solution → Results → Discussion

**Problem Section:**
- Real scenario or constraint that exists
- Quantify the cost (time, money, throughput)
- Explain what existing approaches lack

**Solution Section:**
- Present the approach clearly (architecture, setup, config)
- Include **complete, production-ready examples** (full YAML, scripts, code)
- Explain each significant piece with reasoning
- Use code with line numbers when showing complex configs

**Results Section:**
- Quantify improvements with before/after metrics
- Include benchmarks, charts, concrete numbers
- Show the impact in terms users care about (12 seconds vs 2 minutes, not "faster")

**Discussion Section:**
- Acknowledge limitations honestly ("This is not foolproof")
- Explore trade-offs ("However, for Node.js services...")
- Point toward alternative approaches or future improvements
- Stay humble about what you know

### 4. Code Examples: Complete and Annotated

**Rules:**
- Show full files/configs, not snippets
- Use line numbers for long examples
- Add inline comments sparingly (let clear code speak)
- Include environment setup, certificates, or preparation steps
- Don't hide implementation details — readers learn from seeing the full picture

**Bad:**
```python
def predict(self, data):
    X, _ = self.preprocess(data)
    p = self.model.predict(X)
    return pd.DataFrame(p, columns=self.classes)
```

**Good:**
```python
def predict(self, data):
    """
    Given dataframe, uses the model to predict labels.

    Parameters
    ----------
    data : pandas.DataFrame
        Dataset frame containing instances to predict
    """
    X, _ = self.preprocess(data)
    p = self.model.predict(X)
    d = pd.DataFrame(p, columns=MINOS.classes)
    return d
```

### 5. Use Blockquotes for Asides and Caveats

Use `>` blockquotes for:
- Important disclaimers: `> Note: This solution requires persistent storage`
- Alternative approaches: `> Platforms like depot.dev solve this differently`
- Academic/industry context: `> ONNX is an open format built to represent ML models...`
- Self-reflection: `> My interest lay in investigating a zero-cost solution`

### 6. Pragmatic Tone: Acknowledge Reality

**Use these phrases naturally:**
- "To the best of my knowledge..."
- "This is not foolproof"
- "However, there exists a trade-off..."
- "Unfortunately, this approach has limitations..."
- "Speaking anecdotally..."
- "We chose to..." (shows deliberate decisions, not accidental results)

**Why**: Builds trust. Readers see you've thought deeply about limitations, not just written a sales pitch.

### 7. Emphasize Practical Results

Always quantify impact:
- Don't say "much faster" — say "90–95% reduction" or "12 seconds vs 2 minutes"
- Include charts, benchmarks, or comparative metrics
- Show before/after in concrete terms (pipeline duration, cache size, network cost)
- If results are mixed, acknowledge that too ("Node.js service showed opposite behavior")

### 8. Technical Depth with Accessibility

**Balance:**
- Explain concepts for readers unfamiliar with the domain (what is ONNX? what does BuildKit do?)
- Don't assume readers know your internal context (explain Chatlayer, your infrastructure)
- Use hyperlinks to industry standards, papers, or related tools
- Reference academic work when relevant (cite MINOS, NDSS, papers in your field)

### 9. Narrative Flow: Tell the Story

Write as if you're explaining the journey, not delivering a report:
- "I received contact from colleagues..."
- "My initial attempt focused on discovering..."
- "During the process, I learned about..."
- "The subsequent text describes how I succeeded..."

This keeps readers engaged and shows thinking process, not just conclusions.

### 10. Section Organization

**Typical structure:**
1. **Hook/Problem** (1–3 paragraphs)
2. **Why existing approaches fail** (2–4 paragraphs + code examples)
3. **Solution explanation** (multiple subsections with complete examples)
4. **Setup/implementation** (full configs, step-by-step with reasoning)
5. **Results** (benchmarks, charts, quantified improvements)
6. **Discussion** (limitations, trade-offs, alternatives)
7. **Conclusion** (one-liner takeaway, link to resources)

Use markdown headers (`##`, `###`) to create clear subsections. Each subsection should be self-contained enough to skim.

---

## When to Use This Skill

Load `javier-writing-style` when:

1. **Writing technical blog posts** on engineering, infrastructure, or security topics
2. **Creating in-depth tutorials** where pragmatism and trade-off exploration matter
3. **Documenting solutions** that solve real problems (not hypothetical examples)
4. **Writing for engineer audiences** who value depth, honesty, and quantified results
5. **Explaining complex systems** where narrative structure improves understanding

Do NOT use when:
- Writing marketing copy (this style is too honest about limitations)
- Creating quick reference docs (this style requires narrative context)
- Writing for non-technical audiences (this style assumes technical comfort)

---

## How to Use This Skill in Your Writing

### Step 1: Identify the Core Problem

Before writing, answer:
- What pain point does this solve?
- Why do existing approaches fail?
- What surprised you during implementation?

### Step 2: Structure Your Outline

```
1. Problem statement + impact
2. Why current solutions fall short
3. The root cause (diagnosis)
4. Solution overview + complete examples
5. Step-by-step setup with reasoning
6. Quantified results
7. Limitations and trade-offs
8. Conclusion + links
```

### Step 3: Write with Pragmatism

- Acknowledge trade-offs openly
- Quantify everything (time, cost, performance)
- Include complete, production-ready code
- Explain architectural decisions ("We chose X because Y")
- Don't hide complexity — embrace it with clear explanation

### Step 4: Edit for Narrative

- Does the opening establish why this matters?
- Does the reader understand the root cause before the solution?
- Are results quantified?
- Are limitations honestly discussed?
- Does the conclusion feel earned?

---

## Key Phrases & Patterns

**Problem establishment:**
- "In most [domain], we [standard approach]. This introduces a major [bottleneck]."
- "The culprit: [root cause]. [Why naive solution fails]."
- "At first glance, [intuitive solution] seems like the answer. Yet..."

**Solution transition:**
- "The solution is clear — [approach]. We [implementation]."
- "We deploy [solution] with [key characteristics]."
- "The following snippet illustrates..."

**Results emphasis:**
- "For [use case], build time dropped from [before] to [after] — a reduction of [percentage]."
- "The image below shows the time comparison..."
- "The result? [quantified outcome]."

**Limitation acknowledgment:**
- "However, [limitation exists]. After investigation, [root cause]."
- "This is not foolproof. If [condition], [constraint]. Yet..."
- "To the best of my knowledge, [claim] stands as [assertion]."

**Wrap-up:**
- "Using [solution] provides [benefit]. The improvement is not theoretical: [proof]."
- "This solution does not require [constraint]. While this post demonstrates [specific setup], the same approach can [alternative]."

---

## Common Anti-Patterns to Avoid

❌ **Starting with the solution** — Reader doesn't know why it matters yet
✅ **Start with the problem** — Establish context first

❌ **Hiding complexity** — Readers don't understand trade-offs
✅ **Embrace complexity** — Explain it clearly, acknowledge limitations

❌ **Vague improvements** — "Much faster", "Better performance"
✅ **Quantify everything** — "90–95% reduction", "12 seconds vs 2 minutes"

❌ **Incomplete code examples** — Reader can't reproduce
✅ **Full, production-ready examples** — Include configs, certificates, setup steps

❌ **Dismissing alternatives** — Reader mistrusts your judgment
✅ **Honest comparison** — Why other approaches fall short, what they do well

❌ **Avoiding limitations** — Readers adopt blindly
✅ **Openly acknowledge limitations** — Who should and shouldn't use this

---

## Error Recovery

When a draft drifts from the style, find the matching symptom and apply the fix.

| Symptom in the draft | Fix |
|---|---|
| Opens with "we built X" or the solution | Rewrite the first paragraph as the pain point and its impact (Architecture 1) |
| Results say "faster", "better" or "much" with no numbers | Replace with before/after values and the percentage (Architecture 7) |
| A config or script is cut to a snippet | Reproduce the full file, then explain the significant parts (Architecture 4) |
| Solution is described but the root cause is not | Add the diagnosis section before the solution (Architecture 2) |
| No limitations, or only praise for the approach | Add a Discussion section with the trade-offs and who should not use it (Architecture 6) |
| Asides and caveats sit in plain paragraphs | Move them into `>` blockquotes (Architecture 5) |
| Sections do not follow Problem, Solution, Results, Discussion | Reorder the sections to that sequence (Architecture 3) |

If a fix would remove a number, a limitation or a complete example, keep it and move it instead.

## See Also

For detailed writing patterns, see [TECHNICAL_PATTERNS.md](references/TECHNICAL_PATTERNS.md) (advanced examples from published posts).

For performance-focused writing, see [METRICS_AND_BENCHMARKS.md](references/METRICS_AND_BENCHMARKS.md).
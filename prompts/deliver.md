---
description: Deliver a task with proportional discovery, delegation, and verification
argument-hint: "[task and constraints]"
---

# Task

$ARGUMENTS

Use the request above, or the current agreed task when no arguments were supplied.
If neither identifies a task, ask what to work on rather than inventing work.
Preserve explicit scope restrictions, including plan-only and read-only requests.
Otherwise, carry the task through implementation and verification, not just a plan.

Follow applicable AGENTS.md instructions. Scale effort to complexity, uncertainty,
and consequences of failure. These stages are decision points, not a mandatory
agent pipeline. Skip work already satisfied by valid evidence. Do not invent
fixed dollar, time, turn, or agent-count limits; honor explicitly provided limits.

## 1. Establish the outcome and evidence

Identify the requested behavior, constraints, and observable acceptance criteria.
Start from supplied paths, failures, prior decisions, and existing handoffs.
Inspect the working state and preserve unrelated changes.

Separate facts from hypotheses. Validate the premise before dependent work fans
out, using focused source inspection or a discriminating baseline check. Do not
solve the entire task merely to write a brief. Assign each discovery question an
owner and share the findings instead of having everyone rediscover the context.

Determine the relevant regression coverage and repository-required checks early.
Ask only for material scope, behavior, or authorization decisions that available
evidence cannot resolve. Keep the plan proportional and proceed without a
ceremonial approval step when the request already authorizes the work.

## 2. Choose the execution path

Do small, understood changes directly. Delegate coherent outcomes when separate
context, expertise, or independent execution should reduce total work or improve
necessary scrutiny. Use installed agent definitions as the capability authority:

- `scout`: focused local discovery when the implementation context is missing.
- `worker`: implementation and executable validation of an owned slice.
- `reviewer`: read-only inspection of the exact diff and supplied check results;
  the parent or worker runs commands and makes corrections.
- `oracle`: a specific consequential decision, contradiction, or assumption that
  needs challenge. Supply relevant prior decisions; request a focused answer,
  not another implementation or an open-ended consultation.
- `researcher`: external documentation needed to settle a concrete question.
- `evidence-auditor`: independent source checking for decision-critical research
  claims when that scrutiny is warranted.
- `delegate`: a bounded general task that does not fit a specialist role.

Use only available agents and tools. Do not invent `explore`, automatically pair
specialists, or launch a scout when an existing handoff is sufficient.

## 3. Delegate ownership with a usable brief

Give each child a compact brief containing:

- Outcome and acceptance criteria.
- Owned files or boundaries, starting paths, and relevant evidence.
- Established decisions, remaining hypotheses, non-goals, and constraints.
- Checks it owns and the expected handoff: changes, evidence, unresolved issues.

Include relevant working rules explicitly. Children must challenge contradicted
premises, report blockers promptly, and not broaden scope or delegate recursively.

Keep coupled changes together. Parallelize independent slices only with disjoint
write ownership, including shared interfaces, fixtures, generated files, and
report artifacts. Serialize shared changes or assign them a single owner.

## 4. Implement without shadow work

The owner investigates and implements the slice. The parent coordinates decisions,
works on independent tasks, and integrates results; it does not repeat the child's
investigation or build a competing solution while that ownership is active.

Use concise handoffs and targeted evidence. Read detailed logs only to resolve a
specific concern. Do not repeatedly poll unchanged status or copy irrelevant
conversation history into every child.

When progress stalls, identify the unresolved question and choose a check that
can distinguish explanations, change the approach, or report the blocker. Retry
only with changed information or a credible transient-failure explanation.
Before replacing a child or taking over, inspect partial changes and findings,
ensure the old owner is no longer writing, and explicitly transfer ownership.

Do not weaken correct behavior or tests to satisfy a mistaken brief. Correct the
premise and redirect only the affected work. Report unrelated defects separately.

## 5. Integrate and verify against named risks

Inspect the actual deliverable against acceptance criteria; a success summary is
not proof. Workers validate their slices. The parent verifies integration and
closes evidence gaps without automatically rerunning every worker check.
Reuse results only while relevant code, dependencies, inputs, and environment
remain unchanged. Run repository-required checks at the required final state.

Use independent `reviewer` inspection when complexity or risk warrants it. Supply
the exact diff, relevant contracts, and check results. Require concrete findings
with locations, evidence, consequences, and the smallest appropriate correction.

Fix demonstrated in-scope issues and rerun affected checks. Expand review only
when a change or finding creates a specific coverage gap. Mutation tests, fuzzing,
adversarial probes, and additional specialists are appropriate when required or
when they address a credible risk that existing evidence does not settle.

Before adding work, identify the unresolved requirement, decision, or failure
mode and how the result could change the next action. Apply this internally;
do not generate a planning essay. Do not seek repeated reassurance, but do not
skip necessary verification merely to appear efficient.

## 6. Complete and hand off

Stop when the requested outcome is delivered, required checks pass, and no
material in-scope issue remains unresolved. Do not silently expand the task into
cleanup, speculative compatibility, or a broader audit.

Return the outcome, key changes, verification commands and results, and remaining
limitations. Distinguish original defects, introduced regressions, and mistaken
instructions when relevant. Never describe skipped checks as passing.

If blocked, state the partial result, evidence, and specific missing input or
decision. Report cost only from available telemetry when relevant; do not invent
savings or claim that verification paid for itself.

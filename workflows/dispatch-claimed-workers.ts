// Launch all currently claimed worker slots concurrently.
//
// args: { expectedRevision: number }
//
// This workflow does not claim new tasks. The scheduler must persist claims first.
// It launches every unlaunched working task in implementation/fix, waits for the
// batch, then persists successful worker handoffs in one board revision.

const expectedRevision = args.expectedRevision;
if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

function requireBoard(board) {
  if (!board || typeof board !== "object") throw new Error("kanban state is not initialized");
  if (board.revision !== expectedRevision) {
    throw new Error(`stale kanban revision: expected ${expectedRevision}, got ${board.revision}`);
  }
  if (board.workflow?.state !== "executing") throw new Error("kanban workflow must be executing");
  if (!Array.isArray(board.tasks)) throw new Error("kanban tasks are invalid");
}

function boundedSummary(output) {
  const text = typeof output === "string" && output.trim() ? output.trim() : "Worker completed the claimed task.";
  return text.slice(0, 2000);
}

function firstArtifactPath(result) {
  if (!Array.isArray(result?.artifactPaths)) return null;
  return result.artifactPaths.find((value) => typeof value === "string" && value.length > 0) ?? null;
}

const current = await state.get("kanban");
requireBoard(current);

const launchable = current.tasks.filter((task) =>
  task.status === "working" &&
  ["implementation", "fix"].includes(task.phase) &&
  task.assignment &&
  task.assignment.runId === null,
);

if (launchable.length === 0) {
  return { status: "idle", revision: current.revision, launched: 0 };
}

const launched = launchable.map((task) => {
  const paths = task.paths.length ? task.paths.map((p) => `- ${p}`).join("\n") : "- no path hints supplied";
  const acceptance = task.acceptance.length ? task.acceptance.map((a) => `- ${a}`).join("\n") : "- no explicit acceptance criteria supplied";
  const previousHandoff = task.phase === "fix" ? task.result?.outputReference ?? null : null;
  const phaseInstructions = task.phase === "fix"
    ? [
        "This is a bounded repair attempt.",
        `Previous handoff manifest: ${previousHandoff ?? "missing"}`,
        "Read only that exact handoff manifest, reproduce its captured patch in this isolated worktree, then make the smallest evidence-backed correction.",
        "If the previous handoff cannot be reproduced exactly, stop and report the blocker instead of broadening scope.",
      ].join("\n")
    : "Implement only this approved task in the managed isolated worktree. Make the smallest coherent change and run focused checks.";

  const taskPrompt = [
    "Execute this already-approved Kanban task.",
    `Task ID: ${task.id}`,
    `Title: ${task.title}`,
    "Description:", task.description,
    "Expected paths:", paths,
    "Acceptance criteria:", acceptance,
    phaseInstructions,
    "Repository boundary: stay inside the repository/worktree; do not scan parents/home/root; do not push or publish refs.",
    "Return a concise implementation summary and checks run.",
  ].join("\n\n");

  return {
    taskId: task.id,
    workerKey: task.assignment.workerKey,
    attempt: task.assignment.attempt,
    promise: runs.run(task.assignment.workerKey, {
      agent: "worker",
      context: "fresh",
      task: taskPrompt,
      worktree: task.modifying === true,
    }),
  };
});

const settled = await Promise.all(launched.map(async (entry) => ({ ...entry, result: await entry.promise })));

const latest = await state.get("kanban");
requireBoard(latest);
const next = JSON.parse(JSON.stringify(latest));
const completed = [];
const failed = [];

for (const entry of settled) {
  const task = next.tasks.find((candidate) => candidate.id === entry.taskId);
  if (!task || task.status !== "working" || !["implementation", "fix"].includes(task.phase)) {
    throw new Error(`task ${entry.taskId} changed while worker batch was running`);
  }
  if (task.assignment?.workerKey !== entry.workerKey || task.assignment?.attempt !== entry.attempt || task.assignment?.runId !== null) {
    throw new Error(`task ${entry.taskId} assignment changed while worker batch was running`);
  }

  if (!entry.result || entry.result.ok === false || typeof entry.result.runId !== "string" || entry.result.runId.length === 0) {
    failed.push(entry.taskId);
    continue;
  }

  const outputReference = firstArtifactPath(entry.result);
  if (task.modifying === true && !outputReference) {
    failed.push(entry.taskId);
    continue;
  }

  task.assignment.runId = entry.result.runId;
  task.phase = "verification";
  task.result = {
    summary: boundedSummary(entry.result.output),
    verification: "pending",
    review: "pending",
    runId: entry.result.runId,
    outputReference,
  };
  completed.push(entry.taskId);
}

if (completed.length === 0) {
  return { status: "no-successful-workers", revision: latest.revision, launched: launched.length, failed };
}

next.revision += 1;
next.updatedAt = new Date().toISOString();
await state.set("kanban", next);

return {
  status: failed.length ? "partial" : "complete",
  revision: next.revision,
  launched: launched.length,
  completed,
  failed,
};

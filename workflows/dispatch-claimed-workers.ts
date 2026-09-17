// Run the claimed worker pool with rolling refill.
//
// args: { expectedRevision: number }
//
// The scheduler remains the sole assignment policy. This workflow consumes the
// currently persisted claims, launches them concurrently, and whenever one
// worker finishes successfully it atomically persists that completion plus any
// newly claimed ready work that fits the freed capacity. New workers are then
// launched immediately without waiting for the rest of the batch.

const expectedRevision = args.expectedRevision;
if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

function requireBoard(board, revision) {
  if (!board || typeof board !== "object") throw new Error("kanban state is not initialized");
  if (!Number.isInteger(board.revision)) throw new Error("kanban revision is invalid");
  if (board.revision !== revision) {
    throw new Error(`stale kanban revision: expected ${revision}, got ${board.revision}`);
  }
  if (board.workflow?.state !== "executing") throw new Error("kanban workflow must be executing");
  if (!Array.isArray(board.tasks)) throw new Error("kanban tasks are invalid");
  if (!Number.isInteger(board.scheduler?.maxWorkers) || board.scheduler.maxWorkers < 1 || board.scheduler.maxWorkers > 16) {
    throw new Error("kanban scheduler.maxWorkers is invalid");
  }
}

function taskById(board, id) {
  return board.tasks.find((task) => task.id === id);
}

function dependenciesDone(board, task) {
  return task.dependsOn.every((dependencyId) => taskById(board, dependencyId)?.status === "done");
}

function workerActive(board) {
  return board.tasks.filter(
    (task) => task.status === "working" && ["implementation", "fix"].includes(task.phase),
  );
}

function boundedSummary(output) {
  const text = typeof output === "string" && output.trim() ? output.trim() : "Worker completed the claimed task.";
  return text.slice(0, 2000);
}

function firstArtifactPath(result) {
  if (!Array.isArray(result?.artifactPaths)) return null;
  return result.artifactPaths.find((value) => typeof value === "string" && value.length > 0) ?? null;
}

function workerPrompt(task) {
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

  return [
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
}

function claimReady(board, count, at) {
  if (count <= 0) return [];
  const ready = board.tasks.filter(
    (task) => task.status === "todo" && dependenciesDone(board, task),
  ).slice(0, count);

  const claims = [];
  for (const task of ready) {
    const attempt = task.attempts + 1;
    const workerKey = `worker-${task.id}-${attempt}`;
    task.status = "working";
    task.phase = "implementation";
    task.attempts = attempt;
    task.assignment = { workerKey, runId: null, attempt, startedAt: at };
    task.blocker = null;
    task.result = null;
    claims.push(task.id);
  }
  return claims;
}

let board = await state.get("kanban");
requireBoard(board, expectedRevision);
let knownRevision = board.revision;

const initialActive = workerActive(board);
if (initialActive.length > board.scheduler.maxWorkers) {
  throw new Error(
    `kanban worker capacity exceeded: active ${initialActive.length}, max ${board.scheduler.maxWorkers}`,
  );
}

const inFlight = new Map();
const completed = [];
const failed = [];
let launchedCount = 0;

function launchTask(task) {
  if (inFlight.has(task.id)) return;
  if (!task.assignment || task.assignment.runId !== null) return;
  const workerKey = task.assignment.workerKey;
  const attempt = task.assignment.attempt;
  const promise = runs.run(workerKey, {
    agent: "worker",
    context: "fresh",
    task: workerPrompt(task),
    worktree: task.modifying === true,
  }).then(
    (result) => ({ taskId: task.id, workerKey, attempt, result }),
    (error) => ({ taskId: task.id, workerKey, attempt, result: { ok: false, error: String(error) } }),
  );
  inFlight.set(task.id, { promise, workerKey, attempt });
  launchedCount += 1;
}

for (const task of initialActive) {
  launchTask(task);
}

if (inFlight.size === 0) {
  return { status: "idle", revision: board.revision, launched: 0, completed: [], failed: [] };
}

while (inFlight.size > 0) {
  const settled = await Promise.race([...inFlight.values()].map((entry) => entry.promise));
  inFlight.delete(settled.taskId);

  const latest = await state.get("kanban");
  requireBoard(latest, knownRevision);
  const latestTask = taskById(latest, settled.taskId);
  if (!latestTask || latestTask.status !== "working" || !["implementation", "fix"].includes(latestTask.phase)) {
    throw new Error(`task ${settled.taskId} changed while worker pool was running`);
  }
  if (
    latestTask.assignment?.workerKey !== settled.workerKey ||
    latestTask.assignment?.attempt !== settled.attempt ||
    latestTask.assignment?.runId !== null
  ) {
    throw new Error(`task ${settled.taskId} assignment changed while worker pool was running`);
  }

  const result = settled.result;
  const outputReference = firstArtifactPath(result);
  const success =
    result &&
    result.ok !== false &&
    typeof result.runId === "string" &&
    result.runId.length > 0 &&
    (latestTask.modifying !== true || Boolean(outputReference));

  if (!success) {
    failed.push(settled.taskId);
    continue;
  }

  const next = JSON.parse(JSON.stringify(latest));
  const task = taskById(next, settled.taskId);
  task.assignment.runId = result.runId;
  task.phase = "verification";
  task.result = {
    summary: boundedSummary(result.output),
    verification: "pending",
    review: "pending",
    runId: result.runId,
    outputReference,
  };
  completed.push(task.id);

  const at = new Date().toISOString();
  const activeAfterCompletion = workerActive(next).length;
  const available = next.scheduler.maxWorkers - activeAfterCompletion;
  const newlyClaimed = claimReady(next, available, at);

  next.revision += 1;
  next.updatedAt = at;
  await state.set("kanban", next);
  knownRevision = next.revision;
  board = next;

  for (const taskId of newlyClaimed) {
    launchTask(taskById(board, taskId));
  }
}

return {
  status: failed.length ? "partial" : "complete",
  revision: knownRevision,
  launched: launchedCount,
  completed,
  failed,
};

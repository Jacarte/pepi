// Route one failed/blocked Kanban task into either a bounded fix attempt or an explicit blocker.
//
// args:
// {
//   expectedRevision: number,
//   taskId: string
// }
//
// This workflow is deterministic. It launches no agents and never invents a
// human-decision blocker. Automatic repair is limited to modifying tasks and
// at most three total implementation/fix attempts.

const MAX_ATTEMPTS = 3;

const expectedRevision = args.expectedRevision;
const taskId = typeof args.taskId === "string" ? args.taskId.trim() : "";

if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

if (!taskId) {
  throw new Error("args.taskId is required");
}

function findTask(board, id) {
  return board.tasks.find((task) => task.id === id);
}

function requireBoard(board) {
  if (!board || typeof board !== "object") {
    throw new Error("kanban state is not initialized");
  }

  if (!Number.isInteger(board.revision)) {
    throw new Error("kanban revision is invalid");
  }

  if (board.revision !== expectedRevision) {
    throw new Error(
      `stale kanban revision: expected ${expectedRevision}, got ${board.revision}`,
    );
  }

  if (board.workflow?.state !== "executing") {
    throw new Error(
      `kanban workflow must be executing; got ${board.workflow?.state ?? "missing"}`,
    );
  }

  if (!Array.isArray(board.tasks)) {
    throw new Error("kanban tasks are invalid");
  }
}

function requireRoutableTask(board) {
  const task = findTask(board, taskId);

  if (!task) {
    throw new Error(`unknown task: ${taskId}`);
  }

  if (task.status !== "working") {
    throw new Error(`task ${task.id} must be working; got ${task.status}`);
  }

  if (!task.result || typeof task.result !== "object") {
    throw new Error(`task ${task.id} has no result to route`);
  }

  if (!Number.isInteger(task.attempts) || task.attempts < 1) {
    throw new Error(`task ${task.id} attempts are invalid`);
  }

  if (task.phase === "verification") {
    if (!["fail", "blocked"].includes(task.result.verification)) {
      throw new Error(
        `task ${task.id} verification must be fail or blocked; got ${task.result.verification}`,
      );
    }

    return {
      task,
      source:
        task.result.verification === "fail"
          ? "verification-fail"
          : "verification-blocked",
    };
  }

  if (task.phase === "review") {
    if (task.result.verification !== "pass") {
      throw new Error(
        `task ${task.id} review routing requires verification=pass; got ${task.result.verification}`,
      );
    }

    if (task.result.review !== "blocked") {
      throw new Error(
        `task ${task.id} review must be blocked; got ${task.result.review}`,
      );
    }

    return { task, source: "review-blocked" };
  }

  throw new Error(
    `task ${task.id} phase must be verification or review; got ${task.phase}`,
  );
}

function boundedSummary(existing, line) {
  const prior =
    typeof existing === "string" && existing.trim()
      ? existing.trim()
      : "No prior task result summary recorded.";

  return `${prior}\n\n${line}`.slice(0, 2000);
}

function prepareFix(next, task, source, at) {
  const nextAttempt = task.attempts + 1;
  const previousRunId = task.result.runId ?? task.assignment?.runId ?? null;
  const workerKey = `fix-${task.id}-${nextAttempt}`;

  task.status = "working";
  task.phase = "fix";
  task.attempts = nextAttempt;
  task.assignment = {
    workerKey,
    runId: null,
    attempt: nextAttempt,
    startedAt: at,
  };
  task.blocker = null;
  task.result = {
    summary: boundedSummary(
      task.result.summary,
      `Failure policy prepared automatic fix attempt ${nextAttempt}/${MAX_ATTEMPTS} from ${source}${
        previousRunId ? ` after worker run ${previousRunId}` : ""
      }.`,
    ),
    verification: "pending",
    review: "pending",
    runId: null,
    // Keep the previous modifying handoff reference so the fix worker can
    // reconstruct the prior patch as the starting point for a focused repair.
    outputReference: task.result.outputReference ?? null,
  };

  return { next, workerKey, nextAttempt };
}

function blockTask(next, task, kind, reason) {
  task.status = "blocked";
  task.phase = "queued";
  task.assignment = null;
  task.blocker = {
    kind,
    reason,
    taskIds: [],
  };

  task.result.summary = boundedSummary(
    task.result.summary,
    `Failure policy blocked task: ${reason}`,
  );

  return next;
}

const current = await state.get("kanban");
requireBoard(current);
const { task: currentTask, source } = requireRoutableTask(current);

const next = JSON.parse(JSON.stringify(current));
const task = findTask(next, taskId);
const at = new Date().toISOString();

let response;

if (source === "verification-blocked") {
  blockTask(
    next,
    task,
    "infrastructure",
    "Independent verification was blocked because required verification evidence or tooling was unavailable.",
  );

  response = {
    status: "task-blocked",
    taskId: task.id,
    blockerKind: "infrastructure",
    source,
  };
} else if (task.modifying === true && task.attempts < MAX_ATTEMPTS) {
  const prepared = prepareFix(next, task, source, at);

  response = {
    status: "fix-prepared",
    taskId: task.id,
    source,
    attempt: prepared.nextAttempt,
    maxAttempts: MAX_ATTEMPTS,
    workerKey: prepared.workerKey,
  };
} else {
  const reason =
    task.modifying !== true
      ? `Automatic repair is not applicable to non-modifying task after ${source}.`
      : `Automatic repair limit exhausted after ${task.attempts}/${MAX_ATTEMPTS} attempts (${source}).`;

  blockTask(next, task, "technical", reason);

  response = {
    status: "task-blocked",
    taskId: task.id,
    blockerKind: "technical",
    source,
  };
}

next.revision += 1;
next.updatedAt = at;

await state.set("kanban", next);

return {
  ...response,
  revision: next.revision,
};

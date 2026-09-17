// Thin pi-subagents mission-state adapter for the Kanban board.
//
// args:
// {
//   action: "init" | "get" | "replace",
//   board?: object,
//   expectedRevision?: number
// }
//
// This workflow intentionally does not implement task transitions. The caller
// applies the pure transition model first, then persists the resulting board.
// The scheduler remains the sole writer of the `kanban` mission-state key.

const action = args.action;

if (!["init", "get", "replace"].includes(action)) {
  throw new Error("args.action must be one of: init, get, replace");
}

function assertBoard(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("args.board must be a Kanban board object");
  }

  if (value.schemaVersion !== 1) {
    throw new Error("args.board.schemaVersion must be 1");
  }

  if (!Number.isInteger(value.revision) || value.revision < 1) {
    throw new Error("args.board.revision must be a positive integer");
  }

  if (!value.workflow || typeof value.workflow !== "object") {
    throw new Error("args.board.workflow is required");
  }

  if (!value.scheduler || typeof value.scheduler !== "object") {
    throw new Error("args.board.scheduler is required");
  }

  if (!Array.isArray(value.tasks)) {
    throw new Error("args.board.tasks must be an array");
  }
}

const current = await state.get("kanban");

if (action === "get") {
  return {
    action,
    found: current !== undefined,
    board: current ?? null
  };
}

if (action === "init") {
  if (current !== undefined) {
    throw new Error("Kanban state is already initialized for this mission");
  }

  assertBoard(args.board);

  if (args.board.revision !== 1) {
    throw new Error("Initial Kanban board revision must be 1");
  }

  await state.set("kanban", args.board);

  return {
    action,
    revision: args.board.revision,
    board: args.board
  };
}

if (current === undefined) {
  throw new Error("Kanban state is not initialized for this mission");
}

assertBoard(current);
assertBoard(args.board);

if (!Number.isInteger(args.expectedRevision) || args.expectedRevision < 1) {
  throw new Error("args.expectedRevision must be a positive integer");
}

if (current.revision !== args.expectedRevision) {
  throw new Error(
    `Stale Kanban revision: expected ${args.expectedRevision}, current ${current.revision}`
  );
}

if (args.board.revision !== current.revision + 1) {
  throw new Error(
    `Replacement Kanban revision must be ${current.revision + 1}`
  );
}

await state.set("kanban", args.board);

return {
  action,
  previousRevision: current.revision,
  revision: args.board.revision,
  board: args.board
};

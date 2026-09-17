// Pure Kanban/DAG helpers. No Pi runtime or filesystem dependencies.

export function taskById(board, id) {
  return board.tasks.find((task) => task.id === id);
}

export function dependenciesDone(board, task) {
  return task.dependsOn.every((dependencyId) => {
    const dependency = taskById(board, dependencyId);
    return dependency?.status === "done";
  });
}

export function readyTasks(board) {
  return board.tasks.filter(
    (task) => task.status === "todo" && dependenciesDone(board, task),
  );
}

export function activeTasks(board) {
  return board.tasks.filter((task) => task.status === "working");
}

export function refreshDependencyBlockers(board) {
  for (const task of board.tasks) {
    if (task.status !== "blocked" || task.blocker?.kind !== "dependency") {
      continue;
    }

    if (dependenciesDone(board, task)) {
      task.status = "todo";
      task.phase = "queued";
      task.blocker = null;
    }
  }

  return board;
}

export function validateDag(board) {
  const errors = [];
  const tasksById = new Map();

  for (const task of board.tasks) {
    if (tasksById.has(task.id)) {
      errors.push(`duplicate task id: ${task.id}`);
      continue;
    }

    tasksById.set(task.id, task);
  }

  for (const task of board.tasks) {
    for (const dependencyId of task.dependsOn) {
      if (dependencyId === task.id) {
        errors.push(`task ${task.id} depends on itself`);
      } else if (!tasksById.has(dependencyId)) {
        errors.push(`task ${task.id} depends on unknown task ${dependencyId}`);
      }
    }
  }

  if (errors.length > 0) {
    return errors;
  }

  const indegree = new Map(board.tasks.map((task) => [task.id, 0]));
  const dependents = new Map(board.tasks.map((task) => [task.id, []]));

  for (const task of board.tasks) {
    for (const dependencyId of task.dependsOn) {
      indegree.set(task.id, indegree.get(task.id) + 1);
      dependents.get(dependencyId).push(task.id);
    }
  }

  const queue = board.tasks
    .filter((task) => indegree.get(task.id) === 0)
    .map((task) => task.id);

  let visited = 0;

  while (queue.length > 0) {
    const current = queue.shift();
    visited += 1;

    for (const dependentId of dependents.get(current)) {
      const nextIndegree = indegree.get(dependentId) - 1;
      indegree.set(dependentId, nextIndegree);

      if (nextIndegree === 0) {
        queue.push(dependentId);
      }
    }
  }

  if (visited !== board.tasks.length) {
    errors.push("task dependency graph contains a cycle");
  }

  return errors;
}

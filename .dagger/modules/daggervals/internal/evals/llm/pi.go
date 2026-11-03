package llm

import (
	"context"
	"encoding/json"
	"fmt"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/engine"
	"dagger/daggervals/internal/evals/task"
)

type PiAgent struct {
	command []string
}

type piStats struct {
	Turns      int64   `json:"turns"`
	Input      int64   `json:"input"`
	Output     int64   `json:"output"`
	CacheRead  int64   `json:"cacheRead"`
	CacheWrite int64   `json:"cacheWrite"`
	CostUSD    float64 `json:"cost_usd"`
}

func NewPiAgent() *PiAgent {
	return &PiAgent{
		command: []string{"pi"},
	}
}

// Run a prompt and return the container with the
// call
func (agent *PiAgent) CallPrompt(container *dagger.Container, prompt string) *dagger.Container {
	// Pi prompts are called as "pi -p <prompt>"
	concatCommand := append(append([]string{}, agent.command...), "-p", prompt)
	return container.
		WithExec(concatCommand)
}

// Run a task and return the container with the
// call
func (agent *PiAgent) RunTask(container *dagger.Container, task task.FixTask) *dagger.Container {
	return container.WithExec(agent.TaskCommand(task))
}

// TaskCommand lets the engine run setup and Pi under the same capture exec.
func (agent *PiAgent) TaskCommand(task task.FixTask) []string {
	return append(append([]string{}, agent.command...), "-p", task.ProblemStatement)
}

// GetAgentStats reads usage totals from the newest Pi session without changing container.
func (agent *PiAgent) GetAgentStats(ctx context.Context, container *dagger.Container) (engine.AgentStats, error) {
	// FIXME this can be an embed JS file content
	// Will look prettier
	const script = `
const fs = require("node:fs");
const path = require("node:path");
let newest;
function findSessions(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      findSessions(file);
    } else if (entry.isFile() && entry.name.endsWith(".jsonl")) {
      const mtime = fs.statSync(file).mtimeMs;
      if (!newest || mtime > newest.mtime) newest = { file, mtime };
    }
  }
}
findSessions("/root/.pi/agent/sessions");
if (!newest) throw new Error("no Pi session .jsonl files found");
const stats = { turns: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost_usd: 0 };
for (const line of fs.readFileSync(newest.file, "utf8").split(/\r?\n/)) {
  if (!line.trim()) continue;
  const entry = JSON.parse(line);
  if (entry?.type !== "message" || entry.message?.role !== "assistant") continue;
  stats.turns++;
  const usage = entry.message.usage;
  for (const key of ["input", "output", "cacheRead", "cacheWrite"]) {
    stats[key] += usage?.[key] ?? 0;
  }
  stats.cost_usd += usage?.cost?.total ?? 0;
}
process.stdout.write(JSON.stringify(stats));
`
	output, err := container.WithExec([]string{"node", "-e", script}).Stdout(ctx)
	if err != nil {
		return engine.AgentStats{}, fmt.Errorf("read Pi session stats: %w", err)
	}
	var stats piStats
	if err := json.Unmarshal([]byte(output), &stats); err != nil {
		return engine.AgentStats{}, fmt.Errorf("parse Pi session stats: %w", err)
	}
	return engine.AgentStats{
		Turns:      stats.Turns,
		Input:      stats.Input,
		Output:     stats.Output,
		CacheRead:  stats.CacheRead,
		CacheWrite: stats.CacheWrite,
		CostUSD:    stats.CostUSD,
	}, nil
}

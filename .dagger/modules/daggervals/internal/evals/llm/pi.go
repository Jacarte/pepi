package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/engine"
	"dagger/daggervals/internal/evals/task"
)

type PiAgent struct {
	command []string
}

const processReportPath = "/tmp/daggervals-process.jsonl"

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
	return agent.CallPrompt(container, task.ProblemStatement)
}

// TaskCommand lets the engine run setup and Pi under the same capture exec.
func (agent *PiAgent) TaskCommand(task task.FixTask) []string {
	return append(append([]string{}, agent.command...), "-p", task.ProblemStatement)
}

// GetStats reads metadata from the normally loaded, env-gated Pi extension.
// Parent usage comes from its session manager, never the newest child JSONL.
func (agent *PiAgent) GetStats(ctx context.Context, container *dagger.Container) (engine.AgentStats, engine.ProcessStats, error) {
	output, err := container.File(processReportPath).Contents(ctx)
	if err != nil {
		return engine.AgentStats{}, engine.ProcessStats{}, fmt.Errorf("read Pi process stats: %w", err)
	}
	return parsePiStats(output)
}

// The extension emits only allowlisted metadata. Go groups agent records, not
// Pi transcripts or pi-subagents internals. A completion marker rejects partial logs.
func parsePiStats(contents string) (engine.AgentStats, engine.ProcessStats, error) {
	var stats engine.AgentStats
	processes := engine.ProcessStats{Groups: []engine.ProcessGroup{}, BackgroundProcesses: []engine.ProcessResource{}, Warnings: []string{}}
	groups := make(map[string]int)
	complete, parentSeen := false, false
	decoder := json.NewDecoder(strings.NewReader(contents))
	for {
		var record struct {
			Type       string                 `json:"type"`
			Role       string                 `json:"role"`
			AgentType  string                 `json:"agentType"`
			AgentStats engine.AgentStats      `json:"agentStats"`
			Instance   engine.ProcessInstance `json:"instance"`
			Process    engine.ProcessResource `json:"process"`
			Message    string                 `json:"message"`
		}
		err := decoder.Decode(&record)
		if err == io.EOF {
			break
		}
		if err != nil {
			return stats, processes, fmt.Errorf("parse Pi process stats: %w", err)
		}
		if complete {
			return stats, processes, fmt.Errorf("Pi process stats contain records after completion")
		}
		switch record.Type {
		case "parent_start":
		case "agent":
			if record.AgentType == "" || record.Instance.ID == "" || (record.Role != "parent" && record.Role != "child") {
				return stats, processes, fmt.Errorf("Pi process stats invalid agent identity")
			}
			if record.Role == "parent" {
				parent := record.Instance
				if parentSeen || parent.Resources == nil || parent.Resources.CPUTimeMs == nil || parent.Resources.PeakMemoryBytes == nil || parent.StartedAt == nil || parent.EndedAt == nil || parent.DurationMs == nil {
					return stats, processes, fmt.Errorf("Pi process stats invalid parent measurement")
				}
				parentSeen, stats = true, record.AgentStats
			}
			key := record.Role + ":" + record.AgentType
			index, ok := groups[key]
			if !ok {
				index = len(processes.Groups)
				groups[key] = index
				processes.Groups = append(processes.Groups, engine.ProcessGroup{Role: record.Role, AgentType: record.AgentType, Instances: []engine.ProcessInstance{}})
			}
			processes.Groups[index].Instances = append(processes.Groups[index].Instances, record.Instance)
		case "background_process":
			processes.BackgroundProcesses = append(processes.BackgroundProcesses, record.Process)
		case "warning":
			processes.Warnings = append(processes.Warnings, record.Message)
		case "complete":
			complete = true
		default:
			return stats, processes, fmt.Errorf("Pi process stats unknown record type")
		}
	}
	if !complete || !parentSeen {
		return stats, processes, fmt.Errorf("Pi process stats incomplete capture")
	}
	return stats, processes, nil
}

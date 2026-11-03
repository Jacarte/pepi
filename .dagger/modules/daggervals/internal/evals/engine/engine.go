package engine

import (
	"context"
	"errors"
	"fmt"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/network"
	tasks "dagger/daggervals/internal/evals/task"
)

// Receives a container,
// it writes files to corresponding folders
// Notice this is not the same as the Environment,
// this is just for the agent to write its files
// where it feels it is the right place
type Setup interface {
	Setup() dagger.WithContainerFunc
}

// Receives a container
// Calls the LLM to generate code
type LLM interface {
	CallPrompt(container *dagger.Container, prompt string) *dagger.Container
	TaskCommand(task tasks.FixTask) []string
	GetAgentStats(ctx context.Context, container *dagger.Container) (AgentStats, error)
}

type Engine struct {
	Agent  LLM
	Setter Setup
	Tasks  []tasks.FixTask
}

type AgentStats struct {
	Turns      int64
	Input      int64
	Output     int64
	CacheRead  int64
	CacheWrite int64
	CostUSD    float64
}

type ProcessStats struct {
	CPUUsage    float64
	MemoryUsage int64
	DiskUsage   int64
}

// NetworkStats is passive packet accounting, not HTTP request or URL logging.
type NetworkStats = network.Stats

// TaskOutcome keeps verification failures as data so callers can still report stats.
type TaskOutcome struct {
	Task string

	AgentStats      AgentStats
	ProcessStats    ProcessStats
	NetworkStats    NetworkStats
	Valid           bool
	ValidationError error
}

func NewEngine(agent LLM, setter Setup, tasks []tasks.FixTask) *Engine {
	return &Engine{
		Agent:  agent,
		Setter: setter,
		Tasks:  tasks,
	}
}

func (e *Engine) Run(ctx context.Context, container *dagger.Container) ([]TaskOutcome, error) {
	outcomes := make([]TaskOutcome, 0, len(e.Tasks))
	// Pi provisioning and toolchains are outside the capture window.
	setupFunc, err := e.Setter.Setup()(container).Sync(ctx)
	if err != nil {
		return outcomes, err
	}
	for _, task := range e.Tasks {
		setupFunc = task.Env.PrepareEnv(ctx)(setupFunc)
		prepared, err := setupFunc.Sync(ctx)
		if err != nil {
			return outcomes, err
		}
		var testEnvScript string
		if task.TestEnv != nil {
			testEnvScript, err = task.TestEnv.Script(ctx)
			if err != nil {
				return outcomes, fmt.Errorf("prepare test environment script: %w", err)
			}
		}
		result, err := network.Measure(prepared, testEnvScript, e.Agent.TaskCommand(task))
		if err != nil {
			return outcomes, err
		}
		// Force execution even when the oracle does not query the container.
		result, err = result.Sync(ctx)
		if err != nil {
			return outcomes, err
		}
		// The measured exec has stopped capture, parsed it, and removed raw data.
		networkStats, networkErr := network.ReadStats(ctx, result)
		// Extract and print stats before verification, even if verification fails.
		agentStats, agentErr := e.Agent.GetAgentStats(ctx, result)
		statsErr := errors.Join(agentErr, networkErr)
		if agentErr == nil {
			fmt.Printf("Task %s stats: %+v\n", task.Baseline.Repo, agentStats)
		}
		// Verify the final answer even if stats extraction failed.
		valid, validationErr := task.Oracle.ValidateFinalAnswer(ctx, result)
		if validationErr != nil {
			validationErr = fmt.Errorf("task %s validation: %w", task.Baseline.Repo, validationErr)
		} else if !valid {
			validationErr = fmt.Errorf("task %s failed validation", task.Baseline.Repo)
		}
		outcomes = append(outcomes, TaskOutcome{
			Task:            task.Baseline.Repo,
			AgentStats:      agentStats,
			NetworkStats:    networkStats,
			Valid:           valid && validationErr == nil,
			ValidationError: validationErr,
		})
		if statsErr != nil {
			return outcomes, errors.Join(statsErr, validationErr)
		}
		// Preserve fail-fast execution, but let callers serialize failed verification.
		if validationErr != nil {
			return outcomes, nil
		}
	}

	return outcomes, nil
}

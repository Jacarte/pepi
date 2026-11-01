package engine

import (
	"context"
	"errors"
	"fmt"

	"dagger/daggervals/internal/dagger"
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
	RunTask(container *dagger.Container, task tasks.FixTask) *dagger.Container
	GetStats(ctx context.Context, container *dagger.Container) (Stats, error)
}

type Engine struct {
	Agent  LLM
	Setter Setup
	Tasks  []tasks.FixTask
}

type Stats struct {
	Turns      int64
	Input      int64
	Output     int64
	CacheRead  int64
	CacheWrite int64
	CostUSD    float64
}

// TaskOutcome keeps verification failures as data so callers can still report stats.
type TaskOutcome struct {
	Task            string
	Stats           Stats
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
	// Setup the container with the needed files
	setupFunc := e.Setter.Setup()(container)
	// run up to here
	// TODO, do this in parallel
	for _, task := range e.Tasks {
		// Prepare env
		setupFunc = task.Env.PrepareEnv(ctx)(setupFunc)
		setupFunc, err := setupFunc.Sync(ctx)
		if err != nil {
			return outcomes, err
		}
		// Call the agent to run the task
		result := e.Agent.RunTask(setupFunc, task)
		// Force execution even when the oracle does not query the container.
		result, err = result.Sync(ctx)
		if err != nil {
			return outcomes, err
		}
		// Extract and print stats before verification, even if verification fails.
		stats, statsErr := e.Agent.GetStats(ctx, result)
		if statsErr == nil {
			fmt.Printf("Task %s stats: %+v\n", task.Baseline.Repo, stats)
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
			Stats:           stats,
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

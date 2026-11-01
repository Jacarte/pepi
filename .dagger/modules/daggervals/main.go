package main

import (
	"context"
	"encoding/json"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/engine"
	"dagger/daggervals/internal/evals/llm"
	"dagger/daggervals/internal/evals/setup"
	"dagger/daggervals/internal/evals/task"
)

type Daggervals struct {
	Source *dagger.Directory
	Auth   *dagger.Secret
}

func New(
	// The current workspace, auto-populated by Dagger.
	ws *dagger.Workspace,
) *Daggervals {
	return &Daggervals{
		// Read the workspace root; nothing is uploaded until a function uses it.
		Source: ws.Directory("/", dagger.WorkspaceDirectoryOpts{
			Exclude: []string{
				"logs",
				"tmp",
				".jj",
				".git",
				"npm",
				"missions",
				"sessions",
				"web-search-cache",
				"bin",
				"auth.json",
			},
		}),
	}
}

func (m *Daggervals) Foo(ctx context.Context, auth *dagger.Secret) ([]string, error) {
	rootContainer, err := dag.Container().
		From("node:trixie-slim").
		// check npm version
		WithExec([]string{"npm", "--version"}).
		Sync(ctx)

		// TODO set a proxy for intercepting calls
	if err != nil {
		return nil, err
	}

	evalEngine := engine.NewEngine(
		llm.NewPiAgent(),
		&setup.PiSetup{Workspace: m.Source, Auth: auth},
		[]task.FixTask{task.HelloWorldTask},
	)
	outcomes, err := evalEngine.Run(ctx, rootContainer)
	if err != nil {
		return nil, err
	}
	return serializeTaskOutcomes(outcomes)
}

type taskResult struct {
	Task            string      `json:"task"`
	Stats           statsResult `json:"stats"`
	Valid           bool        `json:"valid"`
	ValidationError string      `json:"validationError,omitempty"`
}

type statsResult struct {
	Turns      int64   `json:"turns"`
	Input      int64   `json:"input"`
	Output     int64   `json:"output"`
	CacheRead  int64   `json:"cacheRead"`
	CacheWrite int64   `json:"cacheWrite"`
	CostUSD    float64 `json:"cost_usd"`
}

func serializeTaskOutcomes(outcomes []engine.TaskOutcome) ([]string, error) {
	results := make([]string, 0, len(outcomes))
	for _, outcome := range outcomes {
		result := taskResult{
			Task:  outcome.Task,
			Valid: outcome.Valid,
			Stats: statsResult{
				Turns:      outcome.Stats.Turns,
				Input:      outcome.Stats.Input,
				Output:     outcome.Stats.Output,
				CacheRead:  outcome.Stats.CacheRead,
				CacheWrite: outcome.Stats.CacheWrite,
				CostUSD:    outcome.Stats.CostUSD,
			},
		}
		if outcome.ValidationError != nil {
			result.ValidationError = outcome.ValidationError.Error()
		}
		encoded, err := json.Marshal(result)
		if err != nil {
			return nil, err
		}
		results = append(results, string(encoded))
	}
	return results, nil
}

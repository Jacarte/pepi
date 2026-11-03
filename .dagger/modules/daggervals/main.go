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

func (m *Daggervals) Foo(ctx context.Context, auth *dagger.Secret, agentEnv *dagger.File) ([]string, error) {
	rootContainer, err := dag.Container().
		From("ubuntu:24.04").
		// install nodejs, npm, and git
		WithExec([]string{"apt-get", "update"}).
		WithExec([]string{"env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "git", "curl", "tcpdump", "tshark"}).
		WithExec([]string{"curl", "-fsSL", "https://deb.nodesource.com/setup_24.x", "-o", "nodesource_setup.sh"}).
		WithExec([]string{"bash", "nodesource_setup.sh"}).
		WithExec([]string{"apt-get", "install", "-y", "nodejs"}).
		// check npm version
		WithExec([]string{"npm", "--version"}).
		// TODO, install ketch
		Sync(ctx)

	if err != nil {
		return nil, err
	}

	evalEngine := engine.NewEngine(
		llm.NewPiAgent(),
		&setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile()},
		[]task.FixTask{task.HelloWorldTask},
	)
	outcomes, err := evalEngine.Run(ctx, rootContainer)
	if err != nil {
		return nil, err
	}
	return serializeTaskOutcomes(outcomes)
}

type taskResult struct {
	Task            string              `json:"task"`
	Stats           statsResult         `json:"stats"`
	NetworkStats    engine.NetworkStats `json:"networkStats"`
	ProcessStats    engine.ProcessStats `json:"processStats"`
	Valid           bool                `json:"valid"`
	ValidationError string              `json:"validationError,omitempty"`
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
			Task:         outcome.Task,
			Valid:        outcome.Valid,
			NetworkStats: outcome.NetworkStats,
			ProcessStats: outcome.ProcessStats,
			Stats: statsResult{
				Turns:      outcome.AgentStats.Turns,
				Input:      outcome.AgentStats.Input,
				Output:     outcome.AgentStats.Output,
				CacheRead:  outcome.AgentStats.CacheRead,
				CacheWrite: outcome.AgentStats.CacheWrite,
				CostUSD:    outcome.AgentStats.CostUSD,
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

package main

import (
	"context"
	"encoding/json"

	"dagger/daggervals/internal/dagger"
	cache "dagger/daggervals/internal/evals/cache"
	"dagger/daggervals/internal/evals/engine"
	"dagger/daggervals/internal/evals/llm"
	judge "dagger/daggervals/internal/evals/llm-judge"

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
				"evals_cache",
			},
		}),
	}
}

type EvalResult struct {
	Log   []string
	Cache *dagger.Directory
}

func (m *Daggervals) Hello(ctx context.Context, auth *dagger.Secret, agentEnv *dagger.File, agentProfile string, cacheDir *dagger.Directory) ([]string, error) {
	rootContainer, err := rootImage().Sync(ctx)

	if err != nil {
		return nil, err
	}

	evalEngine := engine.NewEngine(
		llm.NewPiAgent(&setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile}),
		[]task.FixTask{task.HelloWorldTask},
		nil, // no cache
	)
	outcomes, err := evalEngine.Run(ctx, rootContainer, "noul")
	if err != nil {
		return nil, err
	}
	return serializeTaskOutcomes(outcomes)
}

func (m *Daggervals) Evals(ctx context.Context, auth *dagger.Secret, agentEnv *dagger.File, agentProfile string, cacheDir *dagger.Directory) (*EvalResult, error) {
	rootContainer, err := rootImage().Sync(ctx)

	if err != nil {
		return nil, err
	}
	piSetup := setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile}
	piAgent := llm.NewPiAgent(&piSetup)

	cach := cache.NewGHCache(cacheDir, ctx)

	evalEngine := engine.NewEngine(
		piAgent,
		task.RustTasks,
		cach,
	)
	// return the Sha
	runId, err := piSetup.GetSetupId(ctx, rootContainer)
	if err != nil {
		return nil, err
	}

	outcomes, err := evalEngine.Run(ctx, rootContainer, runId)
	if err != nil {
		return nil, err
	}
	// llm judge
	judge := &judge.Judge{Agent: piAgent}
	judgedEvals, err := judge.Judge(ctx, outcomes, rootContainer)
	if err != nil {
		return nil, err
	}

	// return the cache folder
	return &EvalResult{
		Cache: cach.GetDirectory(),
		Log:   judgedEvals,
	}, nil
}

func (m *Daggervals) TestCacheWrite(ctx context.Context, cacheDir *dagger.Directory) *dagger.Directory {
	cache := cache.NewGHCache(cacheDir, ctx)
	// Test the cache by writing and reading a sample outcome
	sampleOutcome := []engine.TaskOutcome{
		{
			Task:             "test-task",
			ProblemStatement: "Test problem statement",
			Valid:            true,
			ValidationError:  "",
			GoldenPatch:      "Sample golden patch",
			FinalDiff:        "Sample final diff",
		},
	}

	runId := "test-run-id"

	err := cache.Set(runId, sampleOutcome)
	if err != nil {
		return nil
	}

	return cache.GetDirectory()
}

func (m *Daggervals) CompareEvals(ctx context.Context, auth *dagger.Secret, agentEnv *dagger.File, agentProfile string, before *dagger.File, after *dagger.File) ([]string, error) {
	rootImage, err := rootImage().Sync(ctx)
	if err != nil {
		return nil, err
	}

	judge := &judge.Judge{Agent: llm.NewPiAgent(&setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile})}

	// Read contents and try to unmarshal them into []engine.TaskOutcome
	var beforeOutcomes []engine.TaskOutcome
	beforeContents, err := before.Contents(ctx)
	if err != nil {
		return nil, err
	}
	err = json.Unmarshal([]byte(beforeContents), &beforeOutcomes)
	if err != nil {
		return nil, err
	}

	var afterOutcomes []engine.TaskOutcome
	afterContents, err := after.Contents(ctx)
	if err != nil {
		return nil, err
	}
	err = json.Unmarshal([]byte(afterContents), &afterOutcomes)
	if err != nil {
		return nil, err
	}

	return judge.Compare2Evals(ctx, beforeOutcomes, afterOutcomes, rootImage)
}

// GenerateImage returns a container with pi and the selected profile baked in.
// It never receives auth.json: mount it at run time. Export it with
// `export --path image.tar` or push it with `publish --address ...`.
func (m *Daggervals) GenerateImage(ctx context.Context, agentProfile string) (*dagger.Container, error) {
	rootContainer, err := rootImage("gh").Sync(ctx)
	if err != nil {
		return nil, err
	}

	piSetup := &setup.PiSetup{Workspace: m.Source, Profile: agentProfile, Bake: true}
	return rootContainer.
		With(piSetup.Setup()).
		WithEntrypoint([]string{"pi"}), nil
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
		if outcome.ValidationError != "" {
			result.ValidationError = outcome.ValidationError
		}
		encoded, err := json.Marshal(result)
		if err != nil {
			return nil, err
		}
		results = append(results, string(encoded))
	}
	return results, nil
}

// rootImage is the base image shared by every function: ubuntu with git, curl,
// the network capture tools, extraPackages and Node.js 24.
func rootImage(extraPackages ...string) *dagger.Container {
	packages := append([]string{"git", "curl", "tcpdump", "tshark"}, extraPackages...)
	return dag.Container().
		From("ubuntu:24.04").
		WithExec([]string{"apt-get", "update", "--fix-missing"}).
		WithExec(append([]string{"env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y"}, packages...)).
		WithExec([]string{"curl", "-fsSL", "https://deb.nodesource.com/setup_24.x", "-o", "nodesource_setup.sh"}).
		WithExec([]string{"bash", "nodesource_setup.sh"}).
		WithExec([]string{"apt-get", "install", "-y", "nodejs"}).
		// check npm version
		WithExec([]string{"npm", "--version"})
	// TODO, install ketch
}

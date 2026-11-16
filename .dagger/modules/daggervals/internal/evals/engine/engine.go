package engine

import (
	"context"
	"errors"
	"fmt"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/network"
	tasks "dagger/daggervals/internal/evals/task"

	"golang.org/x/sync/errgroup"
)

// Receives a container
// Calls the LLM to generate code
type LLM interface {
	CallPrompt(container *dagger.Container, prompt string) *dagger.Container
	TaskCommand(task tasks.FixTask) []string
	GetStats(ctx context.Context, container *dagger.Container) (AgentStats, ProcessStats, error)
	Setup() dagger.WithContainerFunc
}

type Cache interface {
	// Empty means nothing from the cache
	Get(id string) ([]TaskOutcome, error)
	Set(id string, outcomes []TaskOutcome) error
}

type Engine struct {
	Agent LLM
	Cache Cache
	Tasks []tasks.FixTask
}

type AgentStats struct {
	Turns      int64   `json:"turns"`
	Input      int64   `json:"input"`
	Output     int64   `json:"output"`
	CacheRead  int64   `json:"cacheRead"`
	CacheWrite int64   `json:"cacheWrite"`
	CostUSD    float64 `json:"cost_usd"`
}

// ProcessStats separates logical agent instances from OS processes. Foreground
// agents share the parent process; background steps may share a runner process.
type ProcessStats struct {
	Groups              []ProcessGroup    `json:"groups"`
	BackgroundProcesses []ProcessResource `json:"backgroundProcesses"`
	Warnings            []string          `json:"warnings"`
	// ToolCalls are the parent session's tool executions, in completion order.
	ToolCalls []ToolCall `json:"toolCalls"`
}

// ToolCall is metadata only: no arguments and no results.
type ToolCall struct {
	ID         string   `json:"id"`
	Tool       string   `json:"tool"`
	Status     string   `json:"status"`
	DurationMs *float64 `json:"durationMs"`
	// Nested means another tool (for example a codemode script) made the call.
	Nested bool `json:"nested"`
}

type ProcessGroup struct {
	Role      string            `json:"role"`
	AgentType string            `json:"agentType"`
	Instances []ProcessInstance `json:"instances"`
}

type ProcessInstance struct {
	ID           string           `json:"id"`
	Agent        string           `json:"agent"`
	Model        *string          `json:"model"`
	Mode         string           `json:"mode"`
	Status       string           `json:"status"`
	StartedAt    *string          `json:"startedAt"`
	EndedAt      *string          `json:"endedAt"`
	DurationMs   *float64         `json:"durationMs"`
	TimingSource string           `json:"timingSource"`
	ProcessID    string           `json:"processId"`
	Resources    *ProcessResource `json:"resources"`
	Usage        *ProcessUsage    `json:"usage"`
	UsageSource  string           `json:"usageSource"`
}

// Null metrics are unavailable, not zero. Sampled background metrics are lower
// bounds at the last observation, never apportioned across logical children.
type ProcessResource struct {
	ID              string   `json:"id"`
	PID             int      `json:"pid"`
	CPUTimeMs       *float64 `json:"cpuTimeMs"`
	PeakMemoryBytes *int64   `json:"peakMemoryBytes"`
	Measurement     string   `json:"measurement"`
}

type ProcessUsage struct {
	Turns      *int64   `json:"turns"`
	Input      *int64   `json:"input"`
	Output     *int64   `json:"output"`
	CacheRead  *int64   `json:"cacheRead"`
	CacheWrite *int64   `json:"cacheWrite"`
	CostUSD    *float64 `json:"cost_usd"`
}

// NetworkStats is passive packet accounting, not HTTP request or URL logging.
type NetworkStats = network.Stats

// TaskOutcome keeps verification failures as data so callers can still report stats.
type TaskOutcome struct {
	Task             string
	ProblemStatement string

	AgentStats      AgentStats
	ProcessStats    ProcessStats
	NetworkStats    NetworkStats
	Valid           bool
	ValidationError string
	GoldenPatch     string
	FinalDiff       string

	Profile string
}

func NewEngine(agent LLM, tasks []tasks.FixTask, cache Cache) *Engine {
	return &Engine{
		Agent: agent,
		Tasks: tasks,
		Cache: cache,
	}
}

func (e *Engine) Run(ctx context.Context, container *dagger.Container, runId string, profile string) ([]TaskOutcome, error) {

	if e.Cache != nil {
		if runId != "" {
			if cached, err := e.Cache.Get(runId); err == nil && len(cached) > 0 {
				return cached, nil
			} else if err != nil {
				return nil, fmt.Errorf("read cache for run %s: %w", runId, err)
			}
		}
	}

	if len(e.Tasks) == 0 {
		return []TaskOutcome{}, nil
	}
	// Pi provisioning and toolchains are outside the capture window.
	setupFunc, err := e.Agent.Setup()(container).Sync(ctx)
	if err != nil {
		return nil, err
	}
	// All tasks share the toolchain image prepared for the first task, built once
	// before the fan-out so parallel tasks do not each rebuild it.
	preparedEnv, err := e.Tasks[0].Env.PrepareEnv(ctx)(setupFunc).Sync(ctx)
	if err != nil {
		return nil, err
	}

	// Tasks run in parallel, at most MaxParallelTasks at a time. Each goroutine writes
	// only its own slot, so results keep the task order. A failed verification is data
	// and does not stop the other tasks; any other error cancels the remaining ones.
	results := make([]*TaskOutcome, len(e.Tasks))
	eg, egCtx := errgroup.WithContext(ctx)
	eg.SetLimit(MaxParallelTasks)
	for i, task := range e.Tasks {
		eg.Go(func() error {
			outcome, err := e.runTask(egCtx, preparedEnv, task, profile)
			results[i] = outcome
			return err
		})
	}
	err = eg.Wait()

	outcomes := make([]TaskOutcome, 0, len(e.Tasks))
	for _, outcome := range results {
		if outcome != nil {
			outcomes = append(outcomes, *outcome)
		}
	}
	if err != nil {
		return outcomes, err
	}
	return outcomes, e.saveCache(runId, outcomes)
}

// MaxParallelTasks bounds how many tasks run at the same time.
const MaxParallelTasks = 10

// runTask runs one task in its own container. It returns the outcome even when stats
// extraction failed, together with that error. A failed verification is not an error.
func (e *Engine) runTask(ctx context.Context, preparedEnv *dagger.Container, task tasks.FixTask, profile string) (*TaskOutcome, error) {
	prepared := task.Env.PrepareEnvForTesting(ctx)(preparedEnv)

	var testEnvScript string
	if task.TestEnv != nil {
		script, err := task.TestEnv.Script(ctx)
		if err != nil {
			return nil, fmt.Errorf("prepare test environment script: %w", err)
		}
		testEnvScript = script
	}
	result, err := network.Measure(prepared, testEnvScript, e.Agent.TaskCommand(task))
	if err != nil {
		return nil, err
	}
	// Force execution even when the oracle does not query the container.
	result, err = result.Sync(ctx)
	if err != nil {
		return nil, err
	}
	// The measured exec has stopped capture, parsed it, and removed raw data.
	networkStats, networkErr := network.ReadStats(ctx, result)
	// Extract and print stats before verification, even if verification fails.
	agentStats, processStats, agentErr := e.Agent.GetStats(ctx, result)
	statsErr := errors.Join(agentErr, networkErr)
	if agentErr == nil {
		fmt.Printf("Task %s stats: %+v\n", task.Env.GetRepo(), agentStats)
	}
	// Verify the final answer even if stats extraction failed.
	valid, validationErr := task.Oracle.ValidateFinalAnswer(ctx, result)
	if validationErr != nil {
		validationErr = fmt.Errorf("task %s validation: %w", task.Env.GetRepo(), validationErr)
	} else if !valid {
		validationErr = fmt.Errorf("task %s failed validation", task.Env.GetRepo())
	}
	validationError := ""
	if validationErr != nil {
		validationError = validationErr.Error()
	}
	outcome := &TaskOutcome{
		Task:             task.Env.GetRepo(),
		ProblemStatement: task.ProblemStatement,
		AgentStats:       agentStats,
		ProcessStats:     processStats,
		NetworkStats:     networkStats,
		Valid:            valid && validationErr == nil,
		ValidationError:  validationError,
		GoldenPatch:      task.GoldenPatch,
		FinalDiff:        task.Env.GetFinalDiff(ctx, result),
		Profile:          profile,
	}
	if statsErr != nil {
		return outcome, errors.Join(statsErr, validationErr)
	}
	return outcome, nil
}

// saveCache stores a completed run (including failed validation) so it can be exported.
func (e *Engine) saveCache(runId string, outcomes []TaskOutcome) error {
	if e.Cache == nil || runId == "" {
		return nil
	}
	if err := e.Cache.Set(runId, outcomes); err != nil {
		return fmt.Errorf("write cache for run %s: %w", runId, err)
	}
	return nil
}

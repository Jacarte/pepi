package engine

import (
	"context"
	"errors"
	"fmt"
	"path"
	"strconv"
	"unicode/utf8"

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
	// Skipped are valid tasks the harness cannot run (for example multi-session tasks).
	// They are appended to the outcomes as Skipped entries so the run records them.
	Skipped []TaskOutcome
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
	// Output is what the agent printed to stdout (the final answer), tail-truncated to
	// MaxOutputBytes. Empty when it could not be read; see OutputError.
	Output      string
	OutputError string

	// TimedOut means the agent hit its time limit. The workspace was still graded and
	// StatsError says why the agent stats are missing or partial, if they are.
	TimedOut   bool   `json:",omitempty"`
	StatsError string `json:",omitempty"`

	// Milestone grading (tasks suite), set only by a MilestoneOracle. Score counts the
	// passed milestones by Scoring (prefix|count); Valid means Score == MaxScore.
	Milestones []tasks.MilestoneResult `json:",omitempty"`
	Score      int                     `json:",omitempty"`
	MaxScore   int                     `json:",omitempty"`
	Scoring    string                  `json:",omitempty"`

	// Skipped tasks were not run; SkipReason says why.
	Skipped    bool   `json:",omitempty"`
	SkipReason string `json:",omitempty"`

	Profile string
}

// MaxOutputBytes bounds TaskOutcome.Output so cached outcomes stay small.
const MaxOutputBytes = 64 * 1024

// tailBytes keeps the last max bytes of s, on a rune boundary, with a marker when cut.
func tailBytes(s string, max int) string {
	if len(s) <= max {
		return s
	}
	cut := len(s) - max
	for cut < len(s) && !utf8.RuneStart(s[cut]) {
		cut++
	}
	return "[... truncated " + strconv.Itoa(cut) + " bytes ...]\n" + s[cut:]
}

// SkippedOutcome records a task that was not run.
func SkippedOutcome(name, reason, profile string) TaskOutcome {
	return TaskOutcome{Task: name, Skipped: true, SkipReason: reason, Profile: profile}
}

func NewEngine(agent LLM, tasks []tasks.FixTask, cache Cache) *Engine {
	return &Engine{
		Agent: agent,
		Tasks: tasks,
		Cache: cache,
	}
}

func (e *Engine) Run(ctx context.Context, container *dagger.Container, runId string, profile string) ([]TaskOutcome, error) {
	if e.Cache != nil && runId != "" {
		cached, err := e.Cache.Get(runId)
		if err != nil {
			return nil, fmt.Errorf("read cache for run %s: %w", runId, err)
		}
		if len(cached) > 0 {
			return cached, nil
		}
	}

	if len(e.Tasks) == 0 {
		return append([]TaskOutcome{}, e.Skipped...), nil
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

	// Each goroutine writes only its own slot, so results keep the task order. A failed
	// verification does not stop the other tasks; any other error cancels them.
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
	outcomes = append(outcomes, e.Skipped...)
	return outcomes, e.saveCache(runId, outcomes)
}

// MaxParallelTasks bounds how many tasks run at the same time.
const MaxParallelTasks = 3

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
	timeout := timeoutSeconds(task)
	result, err := network.Measure(prepared, testEnvScript, e.Agent.TaskCommand(task), timeout)
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
	// A task that hit its time limit is graded as it stands; its stats may be partial.
	timedOut, timeoutErr := result.Directory("/tmp").Exists(ctx, path.Base(network.TimeoutMarkerPath))
	if timeoutErr != nil {
		return nil, fmt.Errorf("task %s: read timeout marker: %w", task.Env.GetRepo(), timeoutErr)
	}
	// Extract and print stats before verification, even if verification fails.
	agentStats, processStats, agentErr := e.Agent.GetStats(ctx, result)
	statsErr := errors.Join(agentErr, networkErr)
	statsError := ""
	if timedOut && statsErr != nil {
		statsError = statsErr.Error()
		statsErr = nil
	}
	if agentErr == nil {
		fmt.Printf("Task %s stats: %+v\n", task.Env.GetRepo(), agentStats)
	}
	if timedOut {
		fmt.Printf("Task %s timed out after %d s\n", task.Env.GetRepo(), timeout)
	}
	// Verify the final answer even if stats extraction failed.
	valid, grade, validationErr := e.verify(ctx, task, result)
	validationError := ""
	if validationErr != nil {
		validationError = validationErr.Error()
	}
	output, outputErr := result.Stdout(ctx)
	outputError := ""
	if outputErr != nil {
		outputError = outputErr.Error()
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
		Output:           tailBytes(output, MaxOutputBytes),
		OutputError:      outputError,
		TimedOut:         timedOut,
		StatsError:       statsError,
		Milestones:       grade.results,
		Score:            grade.score,
		MaxScore:         grade.maxScore,
		Scoring:          grade.scoring,
		Profile:          profile,
	}
	return outcome, taskError(statsErr, validationErr)
}

func timeoutSeconds(task tasks.FixTask) int {
	if task.TimeoutSeconds == 0 {
		return tasks.DefaultTaskTimeoutSeconds
	}
	return task.TimeoutSeconds
}

// HarnessError is a failure of the grading harness itself (the grader container could not be
// built, a script returned a usage error), not a verdict on the agent's work. It aborts the
// whole run, so a half-graded run is never cached.
type HarnessError struct{ Err error }

func (e *HarnessError) Error() string { return "grading harness error: " + e.Err.Error() }
func (e *HarnessError) Unwrap() error { return e.Err }

// taskError is what runTask returns next to the outcome: stats errors and harness errors
// abort the run, a failed verdict does not.
func taskError(statsErr, validationErr error) error {
	var harness *HarnessError
	if statsErr != nil || errors.As(validationErr, &harness) {
		return errors.Join(statsErr, validationErr)
	}
	return nil
}

// milestoneGrade is the result of a MilestoneOracle; zero for other oracles.
type milestoneGrade struct {
	results  []tasks.MilestoneResult
	score    int
	maxScore int
	scoring  string
}

// verify asks the task's oracle for the verdict. A MilestoneOracle grades every milestone
// (valid when all pass); other oracles give pass/fail. A failed verdict is returned as an
// error, a HarnessError when the grading itself failed.
func (e *Engine) verify(ctx context.Context, task tasks.FixTask, container *dagger.Container) (bool, milestoneGrade, error) {
	repo := task.Env.GetRepo()
	if oracle, ok := task.Oracle.(tasks.MilestoneOracle); ok {
		results, err := oracle.GradeMilestones(ctx, container)
		if err != nil {
			return false, milestoneGrade{scoring: oracle.Scoring()}, &HarnessError{Err: fmt.Errorf("task %s grading: %w", repo, err)}
		}
		score, maxScore, err := tasks.ScoreMilestones(oracle.Scoring(), results)
		grade := milestoneGrade{results: results, score: score, maxScore: maxScore, scoring: oracle.Scoring()}
		if err != nil {
			return false, grade, &HarnessError{Err: fmt.Errorf("task %s grading: %w", repo, err)}
		}
		if maxScore == 0 || score != maxScore {
			return false, grade, fmt.Errorf("task %s failed validation: score %d/%d", repo, score, maxScore)
		}
		return true, grade, nil
	}
	valid, err := task.Oracle.ValidateFinalAnswer(ctx, container)
	if err != nil {
		return false, milestoneGrade{}, fmt.Errorf("task %s validation: %w", repo, err)
	}
	if !valid {
		return false, milestoneGrade{}, fmt.Errorf("task %s failed validation", repo)
	}
	return true, milestoneGrade{}, nil
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

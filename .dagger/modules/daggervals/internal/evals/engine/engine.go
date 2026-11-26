package engine

import (
	"context"
	"errors"
	"fmt"
	"path"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/memory"
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
	// AgentEnv is the agent's env file: its values are replaced in AgentError.
	AgentEnv *dagger.EnvFile
	Cache    Cache
	Tasks    []tasks.FixTask
	// Memory, when set, gives each task its own memory services (or the off arm).
	Memory *memory.Config
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
	// Skill is set on a successful read of a skill's SKILL.md: the skill's folder name.
	Skill string `json:"skill,omitempty"`
	// SkillCapture is set on every call recorded with skill capture. Runs recorded before it have
	// no skill data, so their empty skill list is unknown, not "no skill loaded".
	SkillCapture bool `json:"skillCapture,omitempty"`
}

// SkillsLoaded lists the skills the calls loaded, in first-read order, each once.
func SkillsLoaded(calls []ToolCall) []string {
	skills := []string{}
	for _, c := range calls {
		if c.Skill != "" && !slices.Contains(skills, c.Skill) {
			skills = append(skills, c.Skill)
		}
	}
	return skills
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

// MemoryStats are the memory server numbers of one task.
type MemoryStats = memory.Stats

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
	// AgentExitCode is the agent's own non-zero exit status after the model answered at least
	// once (for example a model refusal; see agentSessionError) and AgentError the tail of its
	// stderr, secrets replaced. As after a timeout the workspace was still graded, and the
	// outcome is cached like any other: a new --fixed name reruns it.
	AgentExitCode int    `json:",omitempty"`
	AgentError    string `json:",omitempty"`

	// Milestone grading (tasks suite), set only by a MilestoneOracle. Score counts the
	// passed milestones by Scoring (prefix|count); Valid means Score == MaxScore.
	Milestones []tasks.MilestoneResult `json:",omitempty"`
	Score      int                     `json:",omitempty"`
	MaxScore   int                     `json:",omitempty"`
	Scoring    string                  `json:",omitempty"`

	// SkillsLoaded are the skills the parent read (see ToolCall.Skill), in order, each once.
	// Subagent reads are not recorded. Sessions of a multi-session task are joined.
	SkillsLoaded []string `json:",omitempty"`

	// Skipped tasks were not run; SkipReason says why. Only older runs have them (multi-session
	// tasks were skipped then).
	Skipped    bool   `json:",omitempty"`
	SkipReason string `json:",omitempty"`

	// Memory is set only when the eval ran with memory on or off (Engine.Memory).
	Memory *MemoryStats `json:",omitempty"`

	// Sessions has one entry per `pi -p` of a multi-session task, nil for a single session.
	// The fields above then cover the whole task: AgentStats and the NetworkStats counters
	// are sums over the sessions, ProcessStats holds every session's agents and tool calls,
	// TimedOut is true when any session timed out, AgentExitCode and AgentError are those of
	// the first session whose agent failed, StatsError lists the sessions' stats problems,
	// Output is the last session's answer and Memory spans all sessions. The milestones are
	// graded once, on the workspace the last session left.
	Sessions []SessionOutcome `json:",omitempty"`

	Profile string
}

// SessionOutcome is one session of a multi-session task. Null numbers are unavailable.
type SessionOutcome struct {
	// Index counts from 1.
	Index int `json:"index"`
	// WallClockSec is the lifetime of the session's Pi process.
	WallClockSec *float64 `json:"wallClockSec"`
	Turns        int64    `json:"turns"`
	// ToolCalls counts the parent's tool calls.
	ToolCalls int `json:"toolCalls"`
	// Tokens is input, output, cache read and cache write tokens together.
	Tokens        int64   `json:"tokens"`
	CostUSD       float64 `json:"costUsd"`
	TimedOut      bool    `json:"timedOut,omitempty"`
	AgentExitCode int     `json:"agentExitCode,omitempty"`
	AgentError    string  `json:"agentError,omitempty"`
	StatsError    string  `json:"statsError,omitempty"`
	// Output is the session's final reply (its stdout), tail-truncated to
	// MaxSessionOutputBytes: for an earlier session, what its memory capture saw last.
	Output      string `json:"output,omitempty"`
	OutputError string `json:"outputError,omitempty"`
	// Recalled holds the Hippocampus recall messages of the session (parent and children),
	// each cut to maxRecalled bytes: the memory the agent was given.
	Recalled []string `json:"recalled,omitempty"`
	// Memory is what the memory server did during the session and the memories stored after
	// it; absent without memory services.
	Memory *memory.SessionStats `json:"memory,omitempty"`
}

// MaxOutputBytes bounds TaskOutcome.Output so cached outcomes stay small.
const MaxOutputBytes = 64 * 1024

// MaxSessionOutputBytes bounds SessionOutcome.Output.
const MaxSessionOutputBytes = 8 * 1024

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
	return outcomes, e.saveCache(runId, outcomes)
}

// MaxParallelTasks bounds how many tasks run at the same time.
const MaxParallelTasks = 3

// runTask runs one task in its own container: its sessions one after the other, each a fresh
// `pi -p`, then the grading. It returns the outcome even when stats extraction failed,
// together with that error. A failed verification is not an error.
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
	mem, err := e.Memory.StartTask(ctx, prepared, task.MemoryFixture)
	if err != nil {
		return nil, fmt.Errorf("task %s: %w", task.Env.GetRepo(), err)
	}
	// Stopped only when runTask returns: every later exec on the agent container (final diff,
	// Go and Rust oracles) would start the bound services again.
	defer mem.Stop(context.WithoutCancel(ctx))

	prompts := task.SessionPrompts
	if len(prompts) == 0 {
		prompts = []string{task.ProblemStatement}
	}
	container := mem.Bind(prepared)
	multi := len(prompts) > 1
	var agentDirBefore *dagger.Directory
	if multi {
		agentDirBefore = container.Directory(piAgentDir).WithoutFile("auth.json")
	}
	var sessions []*session
	for i, prompt := range prompts {
		label := task.Env.GetRepo()
		if multi {
			label = fmt.Sprintf("%s session %d", label, i+1)
		}
		setupScript, reset := sessionSetup(i, testEnvScript)
		if reset {
			// After the previous session's AfterSession, so its capture has been delivered.
			container = resetSession(container, agentDirBefore)
		}
		s, err := e.runSession(ctx, container, task, label, setupScript, prompt)
		if err != nil {
			return nil, err
		}
		if multi {
			// Pi has delivered the session's capture before exiting (bounded by its request
			// timeout), so the stored count shows it.
			s.memory = mem.AfterSession(ctx, i+1)
			output, err := s.result.Stdout(ctx)
			if err != nil {
				s.outputError = err.Error()
			}
			s.output = tailBytes(output, MaxSessionOutputBytes)
			if s.recalled, err = recalled(ctx, s.result); err != nil {
				return nil, fmt.Errorf("task %s: %w", label, err)
			}
		}
		sessions = append(sessions, s)
		container = s.result
		// A stats error aborts the run; the task is still graded below, as for one session.
		if s.statsErr != nil {
			break
		}
	}
	// Right after the agent, before the final diff and the oracle touch the server.
	memoryStats := mem.Stats(ctx)
	result := container

	// Verify the final answer even if stats extraction failed.
	var calls []ToolCall
	for _, s := range sessions {
		calls = append(calls, s.processStats.ToolCalls...)
	}
	skillsLoaded := SkillsLoaded(calls)
	valid, grade, validationErr := e.verify(ctx, task, result, skillsLoaded)
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
		Valid:            valid && validationErr == nil,
		ValidationError:  validationError,
		GoldenPatch:      task.GoldenPatch,
		FinalDiff:        task.Env.GetFinalDiff(ctx, result),
		Output:           tailBytes(output, MaxOutputBytes),
		OutputError:      outputError,
		Milestones:       grade.results,
		Score:            grade.score,
		MaxScore:         grade.maxScore,
		Scoring:          grade.scoring,
		SkillsLoaded:     skillsLoaded,
		Memory:           memoryStats,
		Profile:          profile,
	}
	statsErr := addSessions(outcome, sessions, multi)
	return outcome, taskError(statsErr, validationErr)
}

// session is what one measured `pi -p` of a task left: the container after it and its stats.
type session struct {
	result        *dagger.Container
	agentStats    AgentStats
	processStats  ProcessStats
	networkStats  NetworkStats
	timedOut      bool
	agentExitCode int
	agentError    string
	// statsErr aborts the run; statsError is the stats problem of a timed out session, data.
	statsErr   error
	statsError string
	memory     *memory.SessionStats
	// output, outputError and recalled are kept for multi-session tasks only.
	output      string
	outputError string
	recalled    []string
}

// runSession runs prompt as one measured `pi -p` on container, after setupScript, and reads
// its stats. An error is fatal: an infrastructure failure or a Pi that never ran a session.
func (e *Engine) runSession(ctx context.Context, container *dagger.Container, task tasks.FixTask, label, setupScript, prompt string) (*session, error) {
	sessionTask := task
	sessionTask.ProblemStatement = prompt
	timeout := timeoutSeconds(task)
	result, err := network.Measure(container, setupScript, e.Agent.TaskCommand(sessionTask), timeout)
	if err != nil {
		return nil, err
	}
	// Force execution even when the oracle does not query the container.
	result, err = result.Sync(ctx)
	if err != nil {
		return nil, err
	}
	s := &session{result: result}
	// The measured exec has stopped capture, parsed it, and removed raw data.
	var networkErr error
	s.networkStats, networkErr = network.ReadStats(ctx, result)
	// A session that hit its time limit is graded as it stands; its stats may be partial.
	if s.timedOut, err = result.Directory("/tmp").Exists(ctx, path.Base(network.TimeoutMarkerPath)); err != nil {
		return nil, fmt.Errorf("task %s: read timeout marker: %w", label, err)
	}
	if s.agentExitCode, s.agentError, err = network.AgentExit(ctx, result, e.AgentEnv); err != nil {
		return nil, fmt.Errorf("task %s: %w", label, err)
	}
	// Extract and print stats before verification, even if verification fails.
	var agentErr error
	s.agentStats, s.processStats, agentErr = e.Agent.GetStats(ctx, result)
	if err := agentSessionError(s.agentExitCode, s.processStats, agentErr, s.agentError); err != nil {
		return nil, fmt.Errorf("task %s: the agent exited with status %d, not counted as an agent failure because %w; its stderr ends with:\n%s", label, s.agentExitCode, err, s.agentError)
	}
	s.statsErr = errors.Join(agentErr, networkErr)
	if s.timedOut && s.statsErr != nil {
		s.statsError = s.statsErr.Error()
		s.statsErr = nil
	}
	if agentErr == nil {
		fmt.Printf("Task %s stats: %+v\n", label, s.agentStats)
	}
	if s.timedOut {
		fmt.Printf("Task %s timed out after %d s\n", label, timeout)
	}
	if s.agentExitCode != 0 {
		fmt.Printf("Task %s: the agent exited with status %d\n", label, s.agentExitCode)
	}
	return s, nil
}

// addSessions fills the agent fields of outcome from its sessions (see TaskOutcome.Sessions)
// and returns their stats errors. A single session is copied as it is.
func addSessions(outcome *TaskOutcome, sessions []*session, multi bool) error {
	var statsErrors []string
	var errs []error
	for i, s := range sessions {
		label := ""
		if multi {
			label = fmt.Sprintf("session %d: ", i+1)
		}
		if s.statsErr != nil {
			errs = append(errs, s.statsErr)
		}
		if s.statsError != "" {
			statsErrors = append(statsErrors, label+s.statsError)
		}
	}
	outcome.StatsError = strings.Join(statsErrors, "\n")
	if !multi {
		s := sessions[0]
		outcome.AgentStats, outcome.ProcessStats, outcome.NetworkStats = s.agentStats, s.processStats, s.networkStats
		outcome.TimedOut, outcome.AgentExitCode, outcome.AgentError = s.timedOut, s.agentExitCode, s.agentError
		return errors.Join(errs...)
	}
	var networks []NetworkStats
	var processes []ProcessStats
	for i, s := range sessions {
		a := s.agentStats
		outcome.AgentStats.Turns += a.Turns
		outcome.AgentStats.Input += a.Input
		outcome.AgentStats.Output += a.Output
		outcome.AgentStats.CacheRead += a.CacheRead
		outcome.AgentStats.CacheWrite += a.CacheWrite
		outcome.AgentStats.CostUSD += a.CostUSD
		networks, processes = append(networks, s.networkStats), append(processes, s.processStats)
		outcome.TimedOut = outcome.TimedOut || s.timedOut
		if outcome.AgentExitCode == 0 {
			outcome.AgentExitCode, outcome.AgentError = s.agentExitCode, s.agentError
		}
		outcome.Sessions = append(outcome.Sessions, SessionOutcome{
			Index:         i + 1,
			WallClockSec:  parentWallClockSec(s.processStats),
			Turns:         a.Turns,
			ToolCalls:     len(s.processStats.ToolCalls),
			Tokens:        a.Input + a.Output + a.CacheRead + a.CacheWrite,
			CostUSD:       a.CostUSD,
			TimedOut:      s.timedOut,
			AgentExitCode: s.agentExitCode,
			AgentError:    s.agentError,
			StatsError:    s.statsError,
			Output:        s.output,
			OutputError:   s.outputError,
			Recalled:      s.recalled,
			Memory:        s.memory,
		})
	}
	outcome.NetworkStats = network.Merge(networks...)
	outcome.ProcessStats = mergeProcessStats(processes)
	return errors.Join(errs...)
}

// parentWallClockSec is the lifetime of the parent Pi process, nil when unavailable.
func parentWallClockSec(ps ProcessStats) *float64 {
	for _, g := range ps.Groups {
		if g.Role != "parent" {
			continue
		}
		for _, in := range g.Instances {
			if in.DurationMs != nil {
				sec := *in.DurationMs / 1000
				return &sec
			}
		}
	}
	return nil
}

// mergeProcessStats puts the sessions' process stats together: the instances of the same
// role and agent type in one group, every tool call and background process, and the
// distinct warnings.
func mergeProcessStats(all []ProcessStats) ProcessStats {
	merged := ProcessStats{Groups: []ProcessGroup{}, BackgroundProcesses: []ProcessResource{}, Warnings: []string{}, ToolCalls: []ToolCall{}}
	for _, ps := range all {
		for _, g := range ps.Groups {
			i := slices.IndexFunc(merged.Groups, func(m ProcessGroup) bool { return m.Role == g.Role && m.AgentType == g.AgentType })
			if i < 0 {
				merged.Groups = append(merged.Groups, ProcessGroup{Role: g.Role, AgentType: g.AgentType})
				i = len(merged.Groups) - 1
			}
			merged.Groups[i].Instances = append(merged.Groups[i].Instances, g.Instances...)
		}
		merged.BackgroundProcesses = append(merged.BackgroundProcesses, ps.BackgroundProcesses...)
		merged.ToolCalls = append(merged.ToolCalls, ps.ToolCalls...)
		for _, w := range ps.Warnings {
			if !slices.Contains(merged.Warnings, w) {
				merged.Warnings = append(merged.Warnings, w)
			}
		}
	}
	return merged
}

func timeoutSeconds(task tasks.FixTask) int {
	if task.TimeoutSeconds == 0 {
		return tasks.DefaultTaskTimeoutSeconds
	}
	return task.TimeoutSeconds
}

// agentSessionError says why a non-zero agent exit is not the agent's outcome. It is only when
// Pi ran a session in which the model answered: a complete process report with a tool call.
// A model turn is not enough, since Pi also saves provider errors as assistant messages.
// Without that evidence Pi failed at startup (settings, extensions, auth), and a provider
// error (auth, rate limit, overload, 5xx) in the stderr tail is the harness's environment, not
// the agent: both would score 0 across a whole arm, so they stay fatal and the user reruns.
func agentSessionError(exitCode int, processes ProcessStats, statsErr error, stderrTail string) error {
	switch {
	case exitCode == 0:
		return nil
	case statsErr != nil:
		return fmt.Errorf("there is no complete Pi process report (%w)", statsErr)
	case len(processes.ToolCalls) == 0:
		return errors.New("the model made no tool call, so it never answered")
	case providerErrorPattern.MatchString(stderrTail):
		return errors.New("its stderr shows a provider error (auth, rate limit, overload or 5xx)")
	}
	return nil
}

var providerErrorPattern = regexp.MustCompile(`(?i)\b(?:401|403|429|5\d\d)\b|rate.?limit|overloaded|authentication|unauthorized`)

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
func (e *Engine) verify(ctx context.Context, task tasks.FixTask, container *dagger.Container, skillsLoaded []string) (bool, milestoneGrade, error) {
	repo := task.Env.GetRepo()
	if oracle, ok := task.Oracle.(tasks.MilestoneOracle); ok {
		results, err := oracle.GradeMilestones(ctx, container, skillsLoaded)
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

package engine

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
	"unicode/utf8"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/memory"
	tasks "dagger/daggervals/internal/evals/task"
)

func TestTailBytes(t *testing.T) {
	if got := tailBytes("short", 10); got != "short" {
		t.Fatalf("short input changed: %q", got)
	}
	got := tailBytes(strings.Repeat("é", 100), 11) // 2-byte runes: the cut must not split one
	if !utf8.ValidString(got) || !strings.HasPrefix(got, "[... truncated") || !strings.HasSuffix(got, "é") {
		t.Fatalf("bad truncation: %q", got)
	}
}

type fakeMilestoneOracle struct {
	scoring string
	results []tasks.MilestoneResult
	err     error
}

func (f fakeMilestoneOracle) ValidateFinalAnswer(context.Context, *dagger.Container) (bool, error) {
	panic("the engine must use GradeMilestones")
}
func (f fakeMilestoneOracle) Scoring() string { return f.scoring }
func (f fakeMilestoneOracle) GradeMilestones(context.Context, *dagger.Container, []string) ([]tasks.MilestoneResult, error) {
	return f.results, f.err
}

func TestVerifyMilestoneOracle(t *testing.T) {
	e := &Engine{}
	run := func(o tasks.MilestoneOracle) (bool, milestoneGrade, error) {
		return e.verify(context.Background(), tasks.FixTask{Env: tasks.GoTask{Repo: "tasks/demo"}, Oracle: o}, nil, nil)
	}
	pass, fail := tasks.MilestoneResult{ID: "m1", Passed: true}, tasks.MilestoneResult{ID: "m2"}

	valid, grade, err := run(fakeMilestoneOracle{scoring: "count", results: []tasks.MilestoneResult{pass, pass}})
	if !valid || err != nil || grade.score != 2 || grade.maxScore != 2 || grade.scoring != "count" {
		t.Fatalf("all pass: %v %+v %v", valid, grade, err)
	}

	valid, grade, err = run(fakeMilestoneOracle{scoring: "prefix", results: []tasks.MilestoneResult{pass, fail, pass}})
	if valid || err == nil || grade.score != 1 || grade.maxScore != 3 || len(grade.results) != 3 {
		t.Fatalf("prefix: %v %+v %v", valid, grade, err)
	}
	if !strings.Contains(err.Error(), "score 1/3") {
		t.Fatalf("error should carry the score: %v", err)
	}

	valid, grade, err = run(fakeMilestoneOracle{scoring: "count", results: []tasks.MilestoneResult{pass, fail, pass}})
	if valid || grade.score != 2 || grade.maxScore != 3 {
		t.Fatalf("count: %v %+v", valid, grade)
	}

	// No milestones is never a pass.
	if valid, _, _ := run(fakeMilestoneOracle{scoring: "count"}); valid {
		t.Fatal("zero milestones must not be valid")
	}
}

func TestGradingErrorsAreHarnessErrors(t *testing.T) {
	e := &Engine{}
	task := func(o tasks.MilestoneOracle) tasks.FixTask {
		return tasks.FixTask{Env: tasks.GoTask{Repo: "tasks/demo"}, Oracle: o}
	}
	var harness *HarnessError

	// the grader could not run: a harness error, which must abort the run
	_, _, err := e.verify(context.Background(), task(fakeMilestoneOracle{scoring: "count", err: errors.New("apt-get failed")}), nil, nil)
	if !errors.As(err, &harness) || !strings.Contains(err.Error(), "apt-get failed") {
		t.Fatalf("grading failure must be a HarnessError, got %v", err)
	}
	// an unknown scoring is a bug of the task or the harness, not a verdict on the agent
	_, _, err = e.verify(context.Background(), task(fakeMilestoneOracle{scoring: "sum", results: []tasks.MilestoneResult{{ID: "m1", Passed: true}}}), nil, nil)
	if !errors.As(err, &harness) {
		t.Fatalf("unknown scoring must be a HarnessError, got %v", err)
	}
	// failing milestones are a verdict on the agent: data, cached
	_, _, err = e.verify(context.Background(), task(fakeMilestoneOracle{scoring: "count", results: []tasks.MilestoneResult{{ID: "m1"}}}), nil, nil)
	if err == nil || errors.As(err, &harness) {
		t.Fatalf("a failed milestone must be a plain validation error, got %v", err)
	}
}

// Outcomes of agents that exited normally keep their JSON (and the caches) unchanged.
func TestAgentFailureFieldsAreOmittedWhenUnset(t *testing.T) {
	without, _ := json.Marshal(TaskOutcome{Task: "t"})
	with, _ := json.Marshal(TaskOutcome{Task: "t", AgentExitCode: 1, AgentError: "refused"})
	if strings.Contains(string(without), "AgentE") || !strings.Contains(string(with), `"AgentExitCode":1,"AgentError":"refused"`) {
		t.Fatalf("without: %s with: %s", without, with)
	}
}

// A non-zero agent exit is data only after a complete Pi report with a tool call and without
// a provider error in its stderr.
func TestAgentSessionError(t *testing.T) {
	answered := ProcessStats{ToolCalls: []ToolCall{{ID: "a", Tool: "bash"}, {ID: "b", Tool: "read"}}}
	if err := agentSessionError(0, ProcessStats{}, errors.New("no report"), ""); err != nil {
		t.Fatalf("exit 0 is never an agent failure: %v", err)
	}
	// The observed case: two tool calls, then the refusal.
	if err := agentSessionError(1, answered, nil, "[daggervals] 2026-11-19T08:24:15.503Z tool end bash ok 523ms\nThe model refused to complete the request"); err != nil {
		t.Fatalf("a refusal after tool calls is data: %v", err)
	}
	for name, c := range map[string]struct {
		processes ProcessStats
		statsErr  error
		stderr    string
		want      string
	}{
		"no tool call":      {ProcessStats{}, nil, "401 Unauthorized", "no tool call"},
		"incomplete report": {answered, errors.New("Pi process stats incomplete capture"), "", "incomplete capture"},
		"auth":              {answered, nil, "Error: 401 invalid x-api-key", "provider error"},
		"rate limit":        {answered, nil, "429 Too Many Requests: rate_limit_error", "provider error"},
		"overloaded":        {answered, nil, "overloaded_error: Overloaded", "provider error"},
		"server error":      {answered, nil, "upstream returned 503", "provider error"},
	} {
		if err := agentSessionError(1, c.processes, c.statsErr, c.stderr); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: must stay fatal with %q, got %v", name, c.want, err)
		}
	}
}

// taskError decides whether runTask aborts the run (and so skips the cache).
func TestTaskErrorAbortsOnlyForStatsOrHarnessErrors(t *testing.T) {
	verdict := errors.New("task failed validation")
	harness := &HarnessError{Err: errors.New("prepare failed")}
	stats := errors.New("no stats")

	if err := taskError(nil, nil); err != nil {
		t.Fatalf("clean task: %v", err)
	}
	if err := taskError(nil, verdict); err != nil {
		t.Fatalf("a failed verdict is data and must not abort: %v", err)
	}
	if err := taskError(nil, harness); !errors.Is(err, harness) {
		t.Fatalf("a harness error must abort: %v", err)
	}
	if err := taskError(stats, verdict); !errors.Is(err, stats) || !errors.Is(err, verdict) {
		t.Fatalf("stats error must abort and keep the verdict: %v", err)
	}
}

// A single session is copied as it is: no Sessions, the same JSON as before multi-session.
func TestAddSessionsSingleIsUnchanged(t *testing.T) {
	ms := 1500.0
	s := &session{
		agentStats:    AgentStats{Turns: 3, Input: 10, Output: 5, CostUSD: 0.5},
		processStats:  ProcessStats{ToolCalls: []ToolCall{{ID: "a", Tool: "bash"}}, Groups: []ProcessGroup{{Role: "parent", AgentType: "parent", Instances: []ProcessInstance{{ID: "parent", DurationMs: &ms}}}}},
		networkStats:  NetworkStats{IPPackets: 7},
		timedOut:      true,
		statsError:    "partial",
		agentExitCode: 0,
	}
	var got TaskOutcome
	if err := addSessions(&got, []*session{s}, false); err != nil {
		t.Fatal(err)
	}
	want := TaskOutcome{AgentStats: s.agentStats, ProcessStats: s.processStats, NetworkStats: s.networkStats, TimedOut: true, StatsError: "partial"}
	gotJSON, _ := json.Marshal(got)
	wantJSON, _ := json.Marshal(want)
	if string(gotJSON) != string(wantJSON) || strings.Contains(string(gotJSON), "Sessions") {
		t.Fatalf("got %s\nwant %s", gotJSON, wantJSON)
	}
}

// Several sessions: sums, merged process stats, the first agent failure, per-session entries.
func TestAddSessionsMulti(t *testing.T) {
	ms := func(v float64) *float64 { return &v }
	parent := func(d float64) []ProcessGroup {
		return []ProcessGroup{{Role: "parent", AgentType: "parent", Instances: []ProcessInstance{{ID: "parent", DurationMs: ms(d)}}}}
	}
	one := &session{
		agentStats:    AgentStats{Turns: 4, Input: 100, Output: 10, CacheRead: 50, CostUSD: 1},
		processStats:  ProcessStats{Groups: parent(60000), ToolCalls: []ToolCall{{ID: "a"}, {ID: "b"}}, Warnings: []string{"w"}},
		networkStats:  NetworkStats{IPPackets: 5, IPBytes: 500},
		agentExitCode: 1, agentError: "The model refused to complete the request",
		memory: &memory.SessionStats{Ops: map[string]float64{"add": 1, "search": 1}},
		output: "explained the fix", recalled: []string{"memory text"},
	}
	two := &session{
		agentStats:   AgentStats{Turns: 2, Input: 40, Output: 4, CostUSD: 0.25},
		processStats: ProcessStats{Groups: parent(30000), ToolCalls: []ToolCall{{ID: "c"}}, Warnings: []string{"w"}},
		networkStats: NetworkStats{IPPackets: 3, IPBytes: 300},
		timedOut:     true, statsError: "partial",
	}
	var got TaskOutcome
	if err := addSessions(&got, []*session{one, two}, true); err != nil {
		t.Fatal(err)
	}
	if got.AgentStats != (AgentStats{Turns: 6, Input: 140, Output: 14, CacheRead: 50, CostUSD: 1.25}) {
		t.Fatalf("agent stats: %+v", got.AgentStats)
	}
	if !got.TimedOut || got.AgentExitCode != 1 || got.AgentError != one.agentError || got.StatsError != "session 2: partial" {
		t.Fatalf("flags: %+v", got)
	}
	if got.NetworkStats.IPPackets != 8 || got.NetworkStats.IPBytes != 800 || len(got.ProcessStats.ToolCalls) != 3 ||
		len(got.ProcessStats.Groups) != 1 || len(got.ProcessStats.Groups[0].Instances) != 2 || len(got.ProcessStats.Warnings) != 1 {
		t.Fatalf("merged stats: %+v %+v", got.NetworkStats, got.ProcessStats)
	}
	if len(got.Sessions) != 2 {
		t.Fatalf("sessions: %+v", got.Sessions)
	}
	s1, s2 := got.Sessions[0], got.Sessions[1]
	if s1.Output != "explained the fix" || len(s1.Recalled) != 1 || s1.OutputError != "" {
		t.Fatalf("session 1 output/recalled: %+v", s1)
	}
	if s1.Index != 1 || *s1.WallClockSec != 60 || s1.ToolCalls != 2 || s1.Tokens != 160 || s1.CostUSD != 1 || s1.AgentExitCode != 1 || s1.Memory.Ops["add"] != 1 {
		t.Fatalf("session 1: %+v", s1)
	}
	if s2.Index != 2 || *s2.WallClockSec != 30 || s2.ToolCalls != 1 || !s2.TimedOut || s2.StatsError != "partial" || s2.Memory != nil {
		t.Fatalf("session 2: %+v", s2)
	}
}

// SkillsLoaded keeps the order of the first read and drops repeats and calls without a skill.
func TestSkillsLoaded(t *testing.T) {
	got := SkillsLoaded([]ToolCall{
		{Tool: "read", Skill: "b"}, {Tool: "bash"}, {Tool: "read", Skill: "a"}, {Tool: "read", Skill: "b"},
	})
	if !slices.Equal(got, []string{"b", "a"}) {
		t.Fatalf("skills = %v", got)
	}
	if none := SkillsLoaded(nil); none == nil || len(none) != 0 {
		t.Fatalf("no skill must be an empty list, got %v", none)
	}
}

package engine

import (
	"context"
	"errors"
	"strings"
	"testing"
	"unicode/utf8"

	"dagger/daggervals/internal/dagger"
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
func (f fakeMilestoneOracle) GradeMilestones(context.Context, *dagger.Container) ([]tasks.MilestoneResult, error) {
	return f.results, f.err
}

func TestVerifyMilestoneOracle(t *testing.T) {
	e := &Engine{}
	run := func(o tasks.MilestoneOracle) (bool, milestoneGrade, error) {
		return e.verify(context.Background(), tasks.FixTask{Env: tasks.GoTask{Repo: "tasks/demo"}, Oracle: o}, nil)
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

	// A grading failure is data: invalid, no score, no panic.
	valid, grade, err = run(fakeMilestoneOracle{scoring: "count", err: errors.New("exec failed")})
	if valid || err == nil || grade.maxScore != 0 || !strings.Contains(err.Error(), "grading") {
		t.Fatalf("grading error: %v %+v %v", valid, grade, err)
	}

	// No milestones is never a pass.
	if valid, _, _ := run(fakeMilestoneOracle{scoring: "count"}); valid {
		t.Fatal("zero milestones must not be valid")
	}
}

func TestRunRecordsSkippedTasks(t *testing.T) {
	e := NewEngine(nil, nil, nil)
	e.Skipped = []TaskOutcome{SkippedOutcome("kv-database", "multi-session not supported yet", "plain")}
	outcomes, err := e.Run(context.Background(), nil, "run", "plain")
	if err != nil || len(outcomes) != 1 || !outcomes[0].Skipped || outcomes[0].SkipReason != "multi-session not supported yet" || outcomes[0].Task != "kv-database" {
		t.Fatalf("outcomes: %+v, %v", outcomes, err)
	}
}

func TestGradingErrorsAreHarnessErrors(t *testing.T) {
	e := &Engine{}
	task := func(o tasks.MilestoneOracle) tasks.FixTask {
		return tasks.FixTask{Env: tasks.GoTask{Repo: "tasks/demo"}, Oracle: o}
	}
	var harness *HarnessError

	// the grader could not run: a harness error, which must abort the run
	_, _, err := e.verify(context.Background(), task(fakeMilestoneOracle{scoring: "count", err: errors.New("apt-get failed")}), nil)
	if !errors.As(err, &harness) || !strings.Contains(err.Error(), "apt-get failed") {
		t.Fatalf("grading failure must be a HarnessError, got %v", err)
	}
	// an unknown scoring is a bug of the task or the harness, not a verdict on the agent
	_, _, err = e.verify(context.Background(), task(fakeMilestoneOracle{scoring: "sum", results: []tasks.MilestoneResult{{ID: "m1", Passed: true}}}), nil)
	if !errors.As(err, &harness) {
		t.Fatalf("unknown scoring must be a HarnessError, got %v", err)
	}
	// failing milestones are a verdict on the agent: data, cached
	_, _, err = e.verify(context.Background(), task(fakeMilestoneOracle{scoring: "count", results: []tasks.MilestoneResult{{ID: "m1"}}}), nil)
	if err == nil || errors.As(err, &harness) {
		t.Fatalf("a failed milestone must be a plain validation error, got %v", err)
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

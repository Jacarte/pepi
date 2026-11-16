package judge

import (
	"encoding/json"
	"strings"
	"testing"

	"dagger/daggervals/internal/evals/engine"
)

func TestBuildReportsKeepsValidationError(t *testing.T) {
	reports := buildReports([]engine.TaskOutcome{{
		Task: "axum", ProblemStatement: "issue", ValidationError: "cargo test failed",
		GoldenPatch: "g", FinalDiff: "d",
	}})
	out, err := json.Marshal(reports)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"oracleError":"cargo test failed"`, `"problemStatement":"issue"`, `"finalDiff":"d"`, `"goldenPatch":"g"`} {
		if !strings.Contains(string(out), want) {
			t.Errorf("report lacks %s: %s", want, out)
		}
	}
}

func ptr[T any](v T) *T { return &v }

func TestSummarizeTiming(t *testing.T) {
	ps := engine.ProcessStats{Groups: []engine.ProcessGroup{
		{Role: "parent", Instances: []engine.ProcessInstance{{ID: "a", Agent: "pi", TimingSource: "measured",
			StartedAt: ptr("2026-11-13T10:00:00Z"), EndedAt: ptr("2026-11-13T10:00:10Z"), DurationMs: ptr(10000.0)}}},
		{Role: "worker", Instances: []engine.ProcessInstance{
			{ID: "b", Agent: "worker", TimingSource: "measured",
				StartedAt: ptr("2026-11-13T10:00:02Z"), EndedAt: ptr("2026-11-13T10:00:08Z"), DurationMs: ptr(6000.0)},
			{ID: "c", Agent: "worker", TimingSource: "unknown"}}},
	}}
	got := summarizeTiming(ps)
	if got.WallClockMs == nil || *got.WallClockMs != 10000 || *got.BusyMs != 16000 || *got.Parallelism != 1.6 {
		t.Fatalf("unexpected timing %+v", got)
	}
	if got.InstancesWithoutTiming != 1 || got.ByRoleMs["worker"] != 6000 || got.LongestInstance != "parent/pi/a" || got.TimingSources["measured"] != 2 {
		t.Fatalf("unexpected timing %+v", got)
	}
	if empty := summarizeTiming(engine.ProcessStats{}); empty.WallClockMs != nil || empty.BusyMs != nil {
		t.Fatalf("empty input must stay unavailable: %+v", empty)
	}
}

package judge

import (
	"encoding/json"
	"strings"
	"testing"

	"dagger/daggervals/internal/evals/engine"
	tasks "dagger/daggervals/internal/evals/task"
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

func TestSummarizeTools(t *testing.T) {
	got := summarizeTools([]engine.ToolCall{
		{Tool: "bash", Status: "ok"}, {Tool: "bash", Status: "error"}, {Tool: "read", Status: "ok", Nested: true},
	})
	if got.Total != 3 || got.Errors != 1 || got.Nested != 1 {
		t.Fatalf("unexpected totals: %+v", got)
	}
	if got.ByTool["bash"] != (toolCount{Calls: 2, Errors: 1}) || got.ByTool["read"].Calls != 1 {
		t.Fatalf("unexpected byTool: %+v", got.ByTool)
	}
}

const sampleDiff = `diff --git a/src/lib.rs b/src/lib.rs
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -1,2 +1,3 @@
-old
+new
+more
diff --git a/tests/it.rs b/tests/it.rs
--- a/tests/it.rs
+++ b/tests/it.rs
@@ -1 +1 @@
-a
+b
`

func TestSummarizePatch(t *testing.T) {
	golden := "diff --git a/src/lib.rs b/src/lib.rs\n--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1 +1 @@\n-x\n+y\ndiff --git a/src/other.rs b/src/other.rs\n--- a/src/other.rs\n+++ b/src/other.rs\n@@ -1 +1 @@\n-p\n+q\n"
	got := summarizePatch(sampleDiff, golden)
	if got.Files != 2 || got.Additions != 3 || got.Deletions != 2 {
		t.Fatalf("size: %+v", got)
	}
	if got.GoldenFiles != 2 || len(got.OverlapFiles) != 1 || got.OverlapFiles[0] != "src/lib.rs" {
		t.Fatalf("overlap: %+v", got)
	}
	if len(got.MissingFiles) != 1 || got.MissingFiles[0] != "src/other.rs" || len(got.ExtraFiles) != 1 || got.ExtraFiles[0] != "tests/it.rs" {
		t.Fatalf("missing/extra: %+v", got)
	}
	if len(got.TestFilesEdited) != 1 || got.TestFilesEdited[0] != "tests/it.rs" {
		t.Fatalf("tests: %+v", got.TestFilesEdited)
	}
	if empty := summarizePatch("", ""); empty.Files != 0 || empty.ChangedFiles == nil {
		t.Fatalf("empty: %+v", empty)
	}
}

func TestSummarizeTokens(t *testing.T) {
	got := summarizeTokens(engine.AgentStats{Turns: 2, Input: 100, Output: 50, CacheRead: 300, CacheWrite: 100, CostUSD: 1})
	if got.Total != 550 || got.CacheHitRatio == nil || *got.CacheHitRatio != 0.6 || *got.CostPerTurn != 0.5 {
		t.Fatalf("tokens: %+v", got)
	}
	if z := summarizeTokens(engine.AgentStats{}); z.CacheHitRatio != nil || z.CostPerTurn != nil {
		t.Fatalf("zero: %+v", z)
	}
}

func TestBuildReportsMilestonesAndSkipped(t *testing.T) {
	results := []engine.TaskOutcome{
		{
			Task: "tasks/five-modules", Valid: false, ValidationError: "score 4/5", TimedOut: true,
			Score: 4, MaxScore: 5, Scoring: "count",
			Milestones: []tasks.MilestoneResult{{ID: "m1", Title: "One", Passed: true}, {ID: "m2", Title: "Two", Detail: "REJECT: m2"}},
		},
		{Task: "kv-database", Skipped: true, SkipReason: "multi-session not supported yet"},
		{Task: "plain", Valid: true},
	}
	reports := buildReports(results)
	if len(reports) != 2 {
		t.Fatalf("skipped tasks must not reach the judge: %d reports", len(reports))
	}
	r := reports[0]
	if r.Score == nil || *r.Score != 4 || r.MaxScore != 5 || r.Scoring != "count" || !r.TimedOut || len(r.Milestones) != 2 || r.Milestones[1].Detail != "REJECT: m2" {
		t.Fatalf("milestone fields: %+v", r)
	}
	encoded, err := json.Marshal(reports[1])
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"score", "maxScore", "scoring", "milestones", "timedOut"} {
		if strings.Contains(string(encoded), `"`+key+`"`) {
			t.Fatalf("%s must be omitted for non-milestone tasks: %s", key, encoded)
		}
	}
}

func TestPromptsDescribeMilestoneFields(t *testing.T) {
	for name, prompt := range map[string]string{"JUDGE_PROMPT": JUDGE_PROMPT, "COMPARE_PROMPT": COMPARE_PROMPT} {
		for _, want := range []string{"score", "maxScore", "scoring", "prefix", "count", "milestones", "passed", "detail", "timedOut", "skipped", "golden patch", "primary correctness signal", "cut short"} {
			if !strings.Contains(prompt, want) {
				t.Errorf("%s does not mention %q", name, want)
			}
		}
	}
	for _, want := range []string{"| Score (before -> after) |", "Milestones passed", "Score / max score", "Timed out runs"} {
		if !strings.Contains(COMPARE_PROMPT, want) {
			t.Errorf("COMPARE_PROMPT table lacks %q", want)
		}
	}
}

// A score of 0 must reach the judge as 0, not be dropped like an absent field.
func TestBuildReportsKeepsZeroScore(t *testing.T) {
	reports := buildReports([]engine.TaskOutcome{{Task: "tasks/five-modules", Score: 0, MaxScore: 5, Scoring: "count"}})
	encoded, err := json.Marshal(reports[0])
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"score":0`) || !strings.Contains(string(encoded), `"maxScore":5`) {
		t.Fatalf("zero score missing: %s", encoded)
	}
}

// An agent failure reaches the judges, and the prompts say it is not evidence about the change.
func TestReportCarriesAgentFailure(t *testing.T) {
	reports := buildReports([]engine.TaskOutcome{{Task: "a", AgentExitCode: 1, AgentError: "The model refused to complete the request"}, {Task: "b"}})
	with, _ := json.Marshal(reports[0])
	without, _ := json.Marshal(reports[1])
	if !strings.Contains(string(with), `"agentExitCode":1,"agentError":"The model refused to complete the request"`) || strings.Contains(string(without), "agentE") {
		t.Fatalf("agent failure fields: %s / %s", with, without)
	}
	for name, prompt := range map[string]string{"JUDGE_PROMPT": JUDGE_PROMPT, "COMPARE_PROMPT": COMPARE_PROMPT, "COMPARE_RUNS_PROMPT": COMPARE_RUNS_PROMPT} {
		for _, want := range []string{"agentExitCode", "agentError", "refusing the request", "instead of reading"} {
			if !strings.Contains(prompt, want) {
				t.Errorf("%s does not mention %q", name, want)
			}
		}
	}
	for _, want := range []string{"| Agent failures n/N |", "one arm has agent failures is weaker"} {
		if !strings.Contains(COMPARE_RUNS_PROMPT, want) {
			t.Errorf("COMPARE_RUNS_PROMPT lacks %q", want)
		}
	}
}

// The judges get the sessions of a multi-session task and are told how to read them.
func TestReportCarriesSessions(t *testing.T) {
	reports := buildReports([]engine.TaskOutcome{{Task: "a", Sessions: []engine.SessionOutcome{{Index: 1, ToolCalls: 3}, {Index: 2, ToolCalls: 1}}}, {Task: "b"}})
	with, _ := json.Marshal(reports[0])
	without, _ := json.Marshal(reports[1])
	if !strings.Contains(string(with), `"sessions":[{"index":1,"wallClockSec":null,"turns":0,"toolCalls":3`) || strings.Contains(string(without), "sessions") {
		t.Fatalf("sessions field: %s / %s", with, without)
	}
	for name, prompt := range map[string]string{"JUDGE_PROMPT": JUDGE_PROMPT, "COMPARE_PROMPT": COMPARE_PROMPT, "COMPARE_RUNS_PROMPT": COMPARE_RUNS_PROMPT} {
		if !strings.Contains(prompt, "sessions (multi-session tasks only") || !strings.Contains(prompt, "not its conversation") {
			t.Errorf("%s does not describe sessions", name)
		}
	}
	for name, prompt := range map[string]string{"COMPARE_PROMPT": COMPARE_PROMPT, "COMPARE_RUNS_PROMPT": COMPARE_RUNS_PROMPT} {
		if !strings.Contains(prompt, "later sessions' effort") || !strings.Contains(prompt, "first session runs under the same") {
			t.Errorf("%s lacks the multi-session memory clause", name)
		}
	}
	if !strings.Contains(COMPARE_RUNS_PROMPT, "| S<i> wall clock s median (min-max) |") || !strings.Contains(COMPARE_RUNS_PROMPT, "| S<i> tool calls median (min-max) |") {
		t.Error("COMPARE_RUNS_PROMPT lacks the per-session rows")
	}
}

func TestReportCarriesProfile(t *testing.T) {
	reports := buildReports([]engine.TaskOutcome{{Task: "a", Profile: "plain"}, {Task: "b"}})
	with, _ := json.Marshal(reports[0])
	without, _ := json.Marshal(reports[1])
	if !strings.Contains(string(with), `"profile":"plain"`) || strings.Contains(string(without), "profile") {
		t.Fatalf("profile field: %s / %s", with, without)
	}
	if !strings.Contains(COMPARE_PROMPT, `profile "plain" means`) {
		t.Fatal("COMPARE_PROMPT must explain the plain profile")
	}
}

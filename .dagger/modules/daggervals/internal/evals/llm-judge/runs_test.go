package judge

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"dagger/daggervals/internal/evals/engine"
)

func TestParseLabels(t *testing.T) {
	got, err := ParseLabels("plain, plain,bedrock", 3)
	if err != nil || !reflect.DeepEqual(got, []string{"plain", "plain", "bedrock"}) {
		t.Fatalf("got %v, %v", got, err)
	}
	got, err = ParseLabels("", 3)
	if err != nil || !reflect.DeepEqual(got, []string{"run1", "run2", "run3"}) {
		t.Fatalf("default labels: %v, %v", got, err)
	}
	// the same label for every file is allowed: repetitions of one arm
	if _, err := ParseLabels("a,a", 2); err != nil {
		t.Fatalf("one arm with repetitions: %v", err)
	}
	bad := []struct {
		labels string
		files  int
		want   string
	}{
		{"", 1, "at least 2 files"},
		{"a,b", 3, "2 entries but 3 files"},
		{"a,b,c", 2, "3 entries but 2 files"},
		{"a,,b", 3, "is empty"},
		{"a, ,b", 3, "is empty"},
		{"a,b/c", 2, "must start with a letter"},
		{"a,../x", 2, "must start with a letter"},
		{"a,b c", 2, "must start with a letter"},
	}
	for _, tt := range bad {
		if _, err := ParseLabels(tt.labels, tt.files); err == nil || !strings.Contains(err.Error(), tt.want) {
			t.Errorf("labels %q files %d: want error containing %q, got %v", tt.labels, tt.files, tt.want, err)
		}
	}
}

func TestStats(t *testing.T) {
	odd := stats([]float64{5, 1, 3})
	if odd.N != 3 || *odd.Median != 3 || *odd.Min != 1 || *odd.Max != 5 {
		t.Fatalf("odd: %+v", odd)
	}
	even := stats([]float64{4, 1, 3, 2})
	if *even.Median != 2.5 || *even.Min != 1 || *even.Max != 4 {
		t.Fatalf("even: %+v", even)
	}
	one := stats([]float64{7})
	if *one.Median != 7 || *one.Min != 7 || *one.Max != 7 {
		t.Fatalf("one: %+v", one)
	}
	none := stats(nil)
	if none.N != 0 || none.Median != nil || none.Min != nil || none.Max != nil {
		t.Fatalf("empty must be null, not zero: %+v", none)
	}
}

type fake struct {
	task      string
	score     int
	maxScore  int
	cost      float64
	turns     int64
	children  int
	subagent  int
	timedOut  bool
	profile   string
	additions int
	editTests int
}

func (f fake) outcome() engine.TaskOutcome {
	o := engine.TaskOutcome{
		Task: f.task, Score: f.score, MaxScore: f.maxScore, Scoring: "count",
		Valid: f.score == f.maxScore, TimedOut: f.timedOut, Profile: f.profile,
		AgentStats: engine.AgentStats{Turns: f.turns, CostUSD: f.cost, Input: 100},
	}
	if f.children > 0 {
		group := engine.ProcessGroup{Role: "child", AgentType: "worker"}
		for i := 0; i < f.children; i++ {
			group.Instances = append(group.Instances, engine.ProcessInstance{ID: "c"})
		}
		o.ProcessStats.Groups = append(o.ProcessStats.Groups, group)
	}
	for i := 0; i < f.subagent; i++ {
		o.ProcessStats.ToolCalls = append(o.ProcessStats.ToolCalls, engine.ToolCall{ID: "s", Tool: "subagent", Status: "ok"})
	}
	o.ProcessStats.ToolCalls = append(o.ProcessStats.ToolCalls, engine.ToolCall{ID: "b", Tool: "bash", Status: "ok"})
	if f.additions > 0 {
		o.FinalDiff = "diff --git a/x.go b/x.go\n@@ -1 +1 @@\n"
		for i := 0; i < f.additions; i++ {
			o.FinalDiff += "+line\n"
		}
		o.FinalDiff += "-old\n"
	}
	if f.editTests > 0 {
		o.FinalDiff += "diff --git a/y_test.go b/y_test.go\n@@ -1 +1 @@\n+t\n"
	}
	return o
}

func run(label string, fakes ...fake) RunInput {
	r := RunInput{Label: label}
	for _, f := range fakes {
		r.Outcomes = append(r.Outcomes, f.outcome())
	}
	return r
}

func findArm(t *testing.T, r armsReport, label string) arm {
	t.Helper()
	for _, a := range r.Arms {
		if a.Label == label {
			return a
		}
	}
	t.Fatalf("no arm %s", label)
	return arm{}
}

func findTask(t *testing.T, a arm, task string) armTask {
	t.Helper()
	for _, at := range a.Tasks {
		if at.Task == task {
			return at
		}
	}
	t.Fatalf("no task %s in arm %s", task, a.Label)
	return armTask{}
}

func TestAggregateArms(t *testing.T) {
	const task = "tasks/five-modules"
	runs := []RunInput{
		run("plain", fake{task: task, score: 1, maxScore: 5, cost: 1, turns: 10, profile: "plain", additions: 4}),
		run("plain", fake{task: task, score: 3, maxScore: 5, cost: 3, turns: 20, profile: "plain", additions: 6}),
		run("plain", fake{task: task, score: 5, maxScore: 5, cost: 2, turns: 30, profile: "plain", additions: 8, timedOut: true}),
		run("bedrock", fake{task: task, score: 2, maxScore: 5, cost: 4, turns: 40, profile: "bedrock", children: 2, subagent: 3, editTests: 1}),
		run("bedrock", fake{task: task, score: 4, maxScore: 5, cost: 6, turns: 50, profile: "bedrock"}),
	}
	report := aggregateArms(runs)
	if report.Baseline != "plain" || len(report.Arms) != 2 || report.Arms[1].Label != "bedrock" {
		t.Fatalf("baseline/order: %+v", report)
	}
	plain := findTask(t, findArm(t, report, "plain"), task)
	if plain.Runs != 3 || *plain.Score.Median != 3 || *plain.Score.Min != 1 || *plain.Score.Max != 5 || plain.MaxScore != 5 || plain.Scoring != "count" {
		t.Fatalf("plain score: %+v", plain)
	}
	if plain.ValidRuns != 1 || *plain.CostUSD.Median != 2 || *plain.CostUSD.Min != 1 || *plain.CostUSD.Max != 3 || *plain.Turns.Median != 20 {
		t.Fatalf("plain valid/cost/turns: %+v", plain)
	}
	if plain.TimedOutRuns != 1 || *plain.PatchLines.Median != 7 /* additions+1 deletion: 5,7,9 */ || plain.RunsDelegated != 0 || plain.RunsWithSubagentCall != 0 {
		t.Fatalf("plain timed out/patch/delegation: %+v", plain)
	}
	if plain.WallClockSec.N != 0 || plain.WallClockSec.Median != nil {
		t.Fatalf("wall clock without timing must be null: %+v", plain.WallClockSec)
	}

	bedrock := findTask(t, findArm(t, report, "bedrock"), task)
	// even count: median of 2 and 4
	if bedrock.Runs != 2 || *bedrock.Score.Median != 3 || *bedrock.CostUSD.Median != 5 || *bedrock.Turns.Median != 45 {
		t.Fatalf("bedrock even median: %+v", bedrock)
	}
	if bedrock.RunsDelegated != 1 || bedrock.RunsWithSubagentCall != 1 || *bedrock.SubagentCalls.Median != 1.5 {
		t.Fatalf("bedrock delegation: %+v", bedrock)
	}
	if bedrock.TestFilesEditedMax != 1 {
		t.Fatalf("bedrock test files: %+v", bedrock)
	}
	if !reflect.DeepEqual(findArm(t, report, "plain").Profiles, []string{"plain"}) || !reflect.DeepEqual(findArm(t, report, "bedrock").Profiles, []string{"bedrock"}) {
		t.Fatalf("profiles: %+v", report.Arms)
	}
	if findArm(t, report, "plain").Total.NeverDelegated != true || findArm(t, report, "bedrock").Total.NeverDelegated != false {
		t.Fatalf("neverDelegated: %+v", report.Arms)
	}
	if findArm(t, report, "bedrock").Total.RunsDelegated != 1 || findArm(t, report, "plain").Total.TaskRuns != 3 {
		t.Fatalf("totals: %+v", report.Arms)
	}
	// bedrock has 2 runs and plain 3: the note names the arm with fewer than 3
	joined := strings.Join(report.Notes, "\n")
	if !strings.Contains(joined, "arm bedrock has 2 run(s)") || strings.Contains(joined, "arm plain has") {
		t.Fatalf("notes: %v", report.Notes)
	}
}

func TestAggregateReportsMissingTasks(t *testing.T) {
	runs := []RunInput{
		run("a", fake{task: "t1", score: 1, maxScore: 2}, fake{task: "t2", score: 2, maxScore: 2}),
		run("a", fake{task: "t1", score: 2, maxScore: 2}),
		run("b", fake{task: "t2", score: 0, maxScore: 2}),
	}
	report := aggregateArms(runs)
	if !reflect.DeepEqual(report.Tasks, []string{"t1", "t2"}) {
		t.Fatalf("tasks: %v", report.Tasks)
	}
	want := []missingTask{{Task: "t2", Arm: "a", InRuns: []int{2}}, {Task: "t1", Arm: "b", InRuns: []int{1}}}
	if !reflect.DeepEqual(report.Missing, want) {
		t.Fatalf("missing: %+v", report.Missing)
	}
	a := findTask(t, findArm(t, report, "a"), "t2")
	if a.Runs != 1 || !reflect.DeepEqual(a.MissingInRuns, []int{2}) {
		t.Fatalf("a/t2: %+v", a)
	}
	b := findTask(t, findArm(t, report, "b"), "t1")
	if b.Runs != 0 || b.Score.N != 0 || b.Score.Median != nil {
		t.Fatalf("b/t1 has no runs: %+v", b)
	}
}

func TestSkippedOutcomesAreNotAggregated(t *testing.T) {
	runs := []RunInput{
		{Label: "a", Outcomes: []engine.TaskOutcome{{Task: "tasks/kv-database", Skipped: true}, fake{task: "t", score: 1, maxScore: 1}.outcome()}},
		run("b", fake{task: "t", score: 1, maxScore: 1}),
	}
	report := aggregateArms(runs)
	if !reflect.DeepEqual(report.Tasks, []string{"t"}) {
		t.Fatalf("skipped task must not appear: %v", report.Tasks)
	}
}

func TestSingleArmNote(t *testing.T) {
	report := aggregateArms([]RunInput{run("a", fake{task: "t"}), run("a", fake{task: "t"}), run("a", fake{task: "t"})})
	if !strings.Contains(strings.Join(report.Notes, "\n"), "only one arm") {
		t.Fatalf("notes: %v", report.Notes)
	}
}

func TestPrepareCompareRunsFilesAndDryRun(t *testing.T) {
	runs := []RunInput{
		run("a", fake{task: "t", score: 1, maxScore: 2}),
		run("a", fake{task: "t", score: 2, maxScore: 2}),
		run("b", fake{task: "t", score: 2, maxScore: 2}),
	}
	prepared, err := PrepareCompareRuns(runs)
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/tmp/daggervals-arms.json", "/tmp/daggervals-run-a-1.json", "/tmp/daggervals-run-a-2.json", "/tmp/daggervals-run-b-1.json"} {
		if _, ok := prepared.Files[path]; !ok {
			t.Errorf("missing file %s", path)
		}
		if path != "/tmp/daggervals-arms.json" && !strings.Contains(prepared.Prompt, path) {
			t.Errorf("prompt does not name %s", path)
		}
	}
	var decoded armsReport
	if err := json.Unmarshal([]byte(prepared.Files["/tmp/daggervals-arms.json"]), &decoded); err != nil || decoded.Baseline != "a" {
		t.Fatalf("arms.json: %v %+v", err, decoded)
	}
	lines := strings.Join(prepared.DryRunLines(), "\n")
	for _, want := range []string{"=== /tmp/daggervals-arms.json ===", `"baseline": "a"`, "=== prompt ===", "VERDICT <arm> vs <baseline>", "/tmp/daggervals-run-a-1.json (", "/tmp/daggervals-arms.json"} {
		if !strings.Contains(lines, want) {
			t.Errorf("dry run lacks %q", want)
		}
	}
}

// The prompt must name every key of arms.json and the verdict output lines.
func TestCompareRunsPromptNamesTheFields(t *testing.T) {
	report := aggregateArms([]RunInput{run("a", fake{task: "t", score: 1, maxScore: 1}), run("b", fake{task: "t", score: 1, maxScore: 1})})
	encoded, _ := json.Marshal(report)
	var generic map[string]any
	_ = json.Unmarshal(encoded, &generic)
	var keys []string
	var walk func(v any)
	walk = func(v any) {
		switch x := v.(type) {
		case map[string]any:
			for k, child := range x {
				keys = append(keys, k)
				walk(child)
			}
		case []any:
			for _, child := range x {
				walk(child)
			}
		}
	}
	walk(generic)
	skip := map[string]bool{"n": true, "median": true, "min": true, "max": true}
	for _, key := range keys {
		if skip[key] {
			continue
		}
		if !strings.Contains(COMPARE_RUNS_PROMPT, key) {
			t.Errorf("COMPARE_RUNS_PROMPT does not mention the arms.json key %q", key)
		}
	}
	for _, want := range []string{"profile", "score", "milestones", "toolCalls", "patchLines", "timedOut", "skipped", "median", "min-max", "n < 3", "neverDelegated", "VERDICT <arm> vs <baseline>", "CONFIDENCE <arm>", "ARMS AS COLUMNS", "runsDelegated", "overlap"} {
		if !strings.Contains(COMPARE_RUNS_PROMPT, want) {
			t.Errorf("COMPARE_RUNS_PROMPT lacks %q", want)
		}
	}
}

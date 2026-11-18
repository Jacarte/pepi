package judge

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strings"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/engine"
)

// Files with the same label are repetitions of one arm. The aggregation is done in Go so
// the judge does no arithmetic.

// RunInput is one eval cache file.
type RunInput struct {
	Label    string
	Outcomes []engine.TaskOutcome
}

var labelPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

// ParseLabels returns one label per file: the comma-separated labels, or run1, run2, ...
// when empty. Labels end up in file names.
func ParseLabels(labels string, files int) ([]string, error) {
	if files < 2 {
		return nil, fmt.Errorf("compare-runs needs at least 2 files, got %d", files)
	}
	if strings.TrimSpace(labels) == "" {
		out := make([]string, files)
		for i := range out {
			out[i] = fmt.Sprintf("run%d", i+1)
		}
		return out, nil
	}
	parts := strings.Split(labels, ",")
	if len(parts) != files {
		return nil, fmt.Errorf("labels has %d entries but %d files were given: pass one label per file, comma-separated", len(parts), files)
	}
	out := make([]string, files)
	for i, part := range parts {
		label := strings.TrimSpace(part)
		switch {
		case label == "":
			return nil, fmt.Errorf("label %d (for file %d) is empty", i+1, i+1)
		case !labelPattern.MatchString(label):
			return nil, fmt.Errorf("label %q (file %d) must start with a letter or digit and use only letters, digits, '.', '_' and '-'", label, i+1)
		}
		out[i] = label
	}
	return out, nil
}

// numStats summarizes the values of one metric over the runs that have it. N counts those
// runs; the other fields are null when N is 0 (unavailable, never zero).
type numStats struct {
	N      int      `json:"n"`
	Median *float64 `json:"median"`
	Min    *float64 `json:"min"`
	Max    *float64 `json:"max"`
}

func median(sorted []float64) float64 {
	n := len(sorted)
	if n%2 == 1 {
		return sorted[n/2]
	}
	return (sorted[n/2-1] + sorted[n/2]) / 2
}

func round4(v float64) float64 { return math.Round(v*1e4) / 1e4 }

func stats(values []float64) numStats {
	s := numStats{N: len(values)}
	if len(values) == 0 {
		return s
	}
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	med, lo, hi := round4(median(sorted)), round4(sorted[0]), round4(sorted[len(sorted)-1])
	s.Median, s.Min, s.Max = &med, &lo, &hi
	return s
}

// armTask is one task under one arm, over the arm's repetitions.
type armTask struct {
	Task string `json:"task"`
	// Runs is how many of the arm's files hold this task.
	Runs int `json:"runs"`
	// MissingInRuns lists the arm's runs (1-based) that do not have this task.
	MissingInRuns []int `json:"missingInRuns,omitempty"`
	// Score is null for tasks without milestones.
	Score    numStats `json:"score"`
	MaxScore int      `json:"maxScore,omitempty"`
	Scoring  string   `json:"scoring,omitempty"`
	// ValidRuns counts runs whose oracle passed (for milestones: score == maxScore).
	ValidRuns    int      `json:"validRuns"`
	CostUSD      numStats `json:"costUsd"`
	WallClockSec numStats `json:"wallClockSec"`
	Turns        numStats `json:"turns"`
	Tokens       numStats `json:"tokens"`
	// PatchLines is additions plus deletions of finalDiff.
	PatchLines numStats `json:"patchLines"`
	// TestFilesEditedMax is the largest number of test files any run edited.
	TestFilesEditedMax int      `json:"testFilesEditedMax"`
	ToolCalls          numStats `json:"toolCalls"`
	SubagentCalls      numStats `json:"subagentCalls"`
	// RunsDelegated counts runs with at least one child agent.
	RunsDelegated int `json:"runsDelegated"`
	// RunsWithSubagentCall counts runs where the parent called the subagent tool.
	RunsWithSubagentCall int `json:"runsWithSubagentCall"`
	TimedOutRuns         int `json:"timedOutRuns"`
}

// armTotal sums the per-task medians of an arm over its tasks.
type armTotal struct {
	Tasks                  int     `json:"tasks"`
	TaskRuns               int     `json:"taskRuns"`
	ScoreMedianSum         float64 `json:"scoreMedianSum"`
	MaxScoreSum            int     `json:"maxScoreSum"`
	ValidRuns              int     `json:"validRuns"`
	CostUSDMedianSum       float64 `json:"costUsdMedianSum"`
	WallClockSecMedianSum  float64 `json:"wallClockSecMedianSum"`
	TurnsMedianSum         float64 `json:"turnsMedianSum"`
	TokensMedianSum        float64 `json:"tokensMedianSum"`
	ToolCallsMedianSum     float64 `json:"toolCallsMedianSum"`
	SubagentCallsMedianSum float64 `json:"subagentCallsMedianSum"`
	PatchLinesMedianSum    float64 `json:"patchLinesMedianSum"`
	TimedOutRuns           int     `json:"timedOutRuns"`
	RunsDelegated          int     `json:"runsDelegated"`
	// NeverDelegated is true when no run of the arm started a child agent or called the subagent tool.
	NeverDelegated bool `json:"neverDelegated"`
}

type arm struct {
	Label string `json:"label"`
	// Runs is the number of files (repetitions) of the arm.
	Runs int `json:"runs"`
	// Profiles are the distinct profile values in the arm's outcomes ("" when unset).
	Profiles []string  `json:"profiles"`
	Tasks    []armTask `json:"tasks"`
	Total    armTotal  `json:"total"`
}

// missingTask is a task that some runs of an arm lack. InRuns are 1-based repetitions.
type missingTask struct {
	Task   string `json:"task"`
	Arm    string `json:"arm"`
	InRuns []int  `json:"inRuns"`
}

// armsReport is /tmp/daggervals-arms.json.
type armsReport struct {
	// Baseline is the first label in order of appearance.
	Baseline string        `json:"baseline"`
	Arms     []arm         `json:"arms"`
	Tasks    []string      `json:"tasks"`
	Missing  []missingTask `json:"missingTasks"`
	// Notes states limits of the data, such as a single arm or arms with fewer than 3 runs.
	Notes []string `json:"notes"`
}

func hasChildAgent(ps engine.ProcessStats) bool {
	for _, g := range ps.Groups {
		if g.Role == "child" && len(g.Instances) > 0 {
			return true
		}
	}
	return false
}

// aggregateArms builds the arms report. Runs are grouped by label in order of appearance.
func aggregateArms(runs []RunInput) armsReport {
	var order []string
	byLabel := map[string][]int{} // label -> indices into runs
	for i, r := range runs {
		if _, ok := byLabel[r.Label]; !ok {
			order = append(order, r.Label)
		}
		byLabel[r.Label] = append(byLabel[r.Label], i)
	}
	reports := make([]map[string]taskReport, len(runs))
	taskSet := map[string]bool{}
	for i, r := range runs {
		reports[i] = map[string]taskReport{}
		for _, report := range buildReports(r.Outcomes) {
			reports[i][report.Task] = report
			taskSet[report.Task] = true
		}
	}
	tasks := sortedKeys(taskSet)

	out := armsReport{Baseline: order[0], Tasks: tasks, Arms: []arm{}, Missing: []missingTask{}, Notes: []string{}}
	for _, label := range order {
		idx := byLabel[label]
		a := arm{Label: label, Runs: len(idx)}
		profiles := map[string]bool{}
		for _, i := range idx {
			for _, report := range reports[i] {
				profiles[report.Profile] = true
			}
		}
		a.Profiles = sortedKeys(profiles)
		for _, task := range tasks {
			var present []taskReport
			var missing []int
			for rep, i := range idx {
				if report, ok := reports[i][task]; ok {
					present = append(present, report)
				} else {
					missing = append(missing, rep+1)
				}
			}
			if len(missing) > 0 {
				out.Missing = append(out.Missing, missingTask{Task: task, Arm: label, InRuns: missing})
			}
			a.Tasks = append(a.Tasks, aggregateTask(task, present, missing))
		}
		a.Total = totalOf(a.Tasks)
		out.Arms = append(out.Arms, a)
	}
	if len(order) < 2 {
		out.Notes = append(out.Notes, "only one arm: there is nothing to compare, only the repetitions of that arm")
	}
	for _, a := range out.Arms {
		if a.Runs < 3 {
			out.Notes = append(out.Notes, fmt.Sprintf("arm %s has %d run(s): fewer than 3 repetitions, so differences cannot be told from noise", a.Label, a.Runs))
		}
	}
	return out
}

func aggregateTask(task string, present []taskReport, missing []int) armTask {
	t := armTask{Task: task, Runs: len(present), MissingInRuns: missing}
	var score, cost, wall, turns, tokens, patch, tools, subCalls []float64
	for _, r := range present {
		if r.Score != nil {
			score = append(score, float64(*r.Score))
		}
		if r.MaxScore > t.MaxScore {
			t.MaxScore = r.MaxScore
		}
		if r.Scoring != "" {
			t.Scoring = r.Scoring
		}
		if r.OracleValid {
			t.ValidRuns++
		}
		cost = append(cost, r.AgentStats.CostUSD)
		turns = append(turns, float64(r.AgentStats.Turns))
		tokens = append(tokens, float64(r.Tokens.Total))
		if r.Timing.WallClockMs != nil {
			wall = append(wall, *r.Timing.WallClockMs/1000)
		}
		patch = append(patch, float64(r.PatchStats.Additions+r.PatchStats.Deletions))
		if n := len(r.PatchStats.TestFilesEdited); n > t.TestFilesEditedMax {
			t.TestFilesEditedMax = n
		}
		tools = append(tools, float64(r.ToolUsage.Total))
		calls := r.ToolUsage.ByTool["subagent"].Calls
		subCalls = append(subCalls, float64(calls))
		if calls > 0 {
			t.RunsWithSubagentCall++
		}
		if hasChildAgent(r.ProcessStats) {
			t.RunsDelegated++
		}
		if r.TimedOut {
			t.TimedOutRuns++
		}
	}
	t.Score, t.CostUSD, t.WallClockSec, t.Turns = stats(score), stats(cost), stats(wall), stats(turns)
	t.Tokens, t.PatchLines = stats(tokens), stats(patch)
	t.ToolCalls, t.SubagentCalls = stats(tools), stats(subCalls)
	return t
}

func medianOf(s numStats) float64 {
	if s.Median == nil {
		return 0
	}
	return *s.Median
}

func totalOf(tasks []armTask) armTotal {
	var t armTotal
	anyDelegation := false
	for _, task := range tasks {
		if task.Runs == 0 {
			continue
		}
		t.Tasks++
		t.TaskRuns += task.Runs
		t.ScoreMedianSum += medianOf(task.Score)
		t.MaxScoreSum += task.MaxScore
		t.ValidRuns += task.ValidRuns
		t.CostUSDMedianSum += medianOf(task.CostUSD)
		t.WallClockSecMedianSum += medianOf(task.WallClockSec)
		t.TurnsMedianSum += medianOf(task.Turns)
		t.TokensMedianSum += medianOf(task.Tokens)
		t.ToolCallsMedianSum += medianOf(task.ToolCalls)
		t.SubagentCallsMedianSum += medianOf(task.SubagentCalls)
		t.PatchLinesMedianSum += medianOf(task.PatchLines)
		t.TimedOutRuns += task.TimedOutRuns
		t.RunsDelegated += task.RunsDelegated
		if task.RunsDelegated > 0 || task.RunsWithSubagentCall > 0 {
			anyDelegation = true
		}
	}
	t.NeverDelegated = !anyDelegation
	return t
}

const armsFilePath = "/tmp/daggervals-arms.json"

func runReportPath(label string, rep int) string {
	return fmt.Sprintf("/tmp/daggervals-run-%s-%d.json", label, rep)
}

// PreparedRuns is everything CompareRuns gives the judge.
type PreparedRuns struct {
	// Files maps path to content: the arms file and one report file per run.
	Files  map[string]string
	Prompt string
}

// PrepareCompareRuns aggregates the runs and builds the files and the prompt. It calls no model.
func PrepareCompareRuns(runs []RunInput) (*PreparedRuns, error) {
	arms := aggregateArms(runs)
	pretty, err := json.MarshalIndent(arms, "", "  ")
	if err != nil {
		return nil, err
	}
	files := map[string]string{armsFilePath: string(pretty)}
	repetition := map[string]int{}
	var runPaths []string
	for _, r := range runs {
		repetition[r.Label]++
		path := runReportPath(r.Label, repetition[r.Label])
		if files[path], err = reportsJSON(r.Outcomes); err != nil {
			return nil, err
		}
		runPaths = append(runPaths, path)
	}
	prompt := COMPARE_RUNS_PROMPT + "\n" + armsFilePath + "\nPer-run reports (read them only to check a claim):\n" + strings.Join(runPaths, "\n") + "\n"
	return &PreparedRuns{Files: files, Prompt: prompt}, nil
}

func (j *Judge) CompareRuns(ctx context.Context, runs []RunInput, container *dagger.Container) ([]string, error) {
	prepared, err := PrepareCompareRuns(runs)
	if err != nil {
		return nil, err
	}
	return j.ask(ctx, container, prepared.Files, prepared.Prompt)
}

// DryRunLines is what a dry run returns: the aggregate and the full prompt.
func (p *PreparedRuns) DryRunLines() []string {
	lines := []string{"=== " + armsFilePath + " ==="}
	lines = append(lines, strings.Split(p.Files[armsFilePath], "\n")...)
	lines = append(lines, "", "=== run report files (not shown) ===")
	paths := make([]string, 0, len(p.Files))
	for path := range p.Files {
		if path != armsFilePath {
			paths = append(paths, fmt.Sprintf("%s (%d bytes)", path, len(p.Files[path])))
		}
	}
	sort.Strings(paths)
	lines = append(lines, paths...)
	lines = append(lines, "", "=== prompt ===")
	return append(lines, strings.Split(p.Prompt, "\n")...)
}

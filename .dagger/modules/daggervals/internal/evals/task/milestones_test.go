package task

import (
	"reflect"
	"strings"
	"testing"
)

func results(passed ...bool) []MilestoneResult {
	out := make([]MilestoneResult, len(passed))
	for i, p := range passed {
		out[i] = MilestoneResult{ID: "m" + string(rune('1'+i)), Passed: p}
	}
	return out
}

func TestScoreMilestones(t *testing.T) {
	tests := []struct {
		name    string
		scoring string
		passed  []bool
		want    int
	}{
		{"prefix stops at the first failure", ScoringPrefix, []bool{true, true, false, true}, 2},
		{"prefix all", ScoringPrefix, []bool{true, true}, 2},
		{"prefix first fails", ScoringPrefix, []bool{false, true, true}, 0},
		{"count ignores order", ScoringCount, []bool{true, true, false, true}, 3},
		{"count none", ScoringCount, []bool{false, false}, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			score, maxScore, err := ScoreMilestones(tt.scoring, results(tt.passed...))
			if err != nil || score != tt.want || maxScore != len(tt.passed) {
				t.Fatalf("got %d/%d, %v; want %d/%d", score, maxScore, err, tt.want, len(tt.passed))
			}
		})
	}
	if _, _, err := ScoreMilestones("sum", results(true)); err == nil {
		t.Fatal("unknown scoring must be an error")
	}
}

func TestMilestoneFromCheck(t *testing.T) {
	m := Milestone{ID: "m2-glob", Title: "Glob"}

	r, err := MilestoneFromCheck(m, 0, "PASS: m2-glob (3 expected tests)\n")
	if err != nil || !r.Passed || r.Detail != "" || r.ID != "m2-glob" || r.Title != "Glob" {
		t.Fatalf("exit 0: %+v, %v", r, err)
	}

	out := "FAIL\tfivemods/glob\t0.4s\nREJECT: glob_test.go mentions go test flags (\"-test.run=^$\")\nPASS: ignored\n"
	r, err = MilestoneFromCheck(m, 1, out)
	if err != nil || r.Passed || r.Detail != `REJECT: glob_test.go mentions go test flags ("-test.run=^$")` {
		t.Fatalf("exit 1 with REJECT: %+v, %v", r, err)
	}

	r, err = MilestoneFromCheck(m, 1, "go: build failed\n\n")
	if err != nil || r.Passed || r.Detail != "go: build failed" {
		t.Fatalf("exit 1 without REJECT uses the last line: %+v, %v", r, err)
	}

	r, err = MilestoneFromCheck(m, 124, "")
	if err != nil || r.Passed || r.Detail != "timed out" {
		t.Fatalf("exit 124: %+v, %v", r, err)
	}

	r, err = MilestoneFromCheck(m, 137, "")
	if err != nil || r.Passed || r.Detail != "timed out" {
		t.Fatalf("exit 137 (timeout -k): %+v, %v", r, err)
	}

	for _, code := range []int{2, 127} {
		if _, err := MilestoneFromCheck(m, code, "usage"); err == nil {
			t.Fatalf("exit %d must be a harness error, not a failing milestone", code)
		}
	}

	r, _ = MilestoneFromCheck(m, 1, "REJECT: "+strings.Repeat("x", 1000))
	if len(r.Detail) > maxDetailBytes+3 {
		t.Fatalf("detail not bounded: %d bytes", len(r.Detail))
	}
}

func TestFormatMilestoneLines(t *testing.T) {
	rs := []MilestoneResult{
		{ID: "m1-a", Passed: true},
		{ID: "m2-b", Detail: "REJECT: m2-b: command exited 1"},
	}
	lines, err := FormatMilestoneLines(ScoringPrefix, rs)
	want := []string{"m1-a PASS", "m2-b FAIL: REJECT: m2-b: command exited 1", "score 1/2 (prefix)"}
	if err != nil || !reflect.DeepEqual(lines, want) {
		t.Fatalf("got %q, %v", lines, err)
	}
}

func TestPrecheckDetail(t *testing.T) {
	tests := []struct {
		code     int
		output   string
		want     string
		rejected bool
		wantErr  bool
	}{
		{0, "", "", false, false},
		{precheckSymlinks, "./zz_test.go ./a/link \n", "workspace contains symlinks: ./zz_test.go ./a/link", true, false},
		{precheckSpecial, "./pipe_test.go \n", "workspace contains special files: ./pipe_test.go", true, false},
		{precheckControl, "$'zz\\nmain_test.go' \n", "workspace contains file names with control characters: $'zz\\nmain_test.go'", true, false},
		{precheckTooLarge, "300000\n", "workspace is too large: 300000 KiB (limit 204800 KiB)", true, false},
		{99, "cd failed", "", false, true},
		{127, "", "", false, true},
	}
	for _, tt := range tests {
		detail, rejected, err := PrecheckDetail(tt.code, tt.output)
		if detail != tt.want || rejected != tt.rejected || (err != nil) != tt.wantErr {
			t.Errorf("exit %d: got %q, %v, %v", tt.code, detail, rejected, err)
		}
	}
}

func TestRejectAllFailsEveryMilestone(t *testing.T) {
	o := RepoTaskOracle{Spec: RepoTaskSpec{Milestones: []Milestone{{ID: "m1-a", Title: "A"}, {ID: "m2-b", Title: "B"}}}}
	results := o.rejectAll("workspace contains symlinks: ./zz_test.go")
	if len(results) != 2 {
		t.Fatalf("results: %+v", results)
	}
	for i, r := range results {
		if r.Passed || r.Detail != "workspace contains symlinks: ./zz_test.go" || r.ID != o.Spec.Milestones[i].ID || r.Title != o.Spec.Milestones[i].Title {
			t.Fatalf("milestone %d: %+v", i, r)
		}
	}
	score, maxScore, err := ScoreMilestones(ScoringCount, results)
	if err != nil || score != 0 || maxScore != 2 {
		t.Fatalf("score %d/%d, %v", score, maxScore, err)
	}
}

func TestPrecheckScriptListsTheThreeChecks(t *testing.T) {
	for _, want := range []string{"find . -type l", "! -type d ! -type f", "du -sk", "exit 10", "exit 11", "exit 12", "exit 13", "[[:cntrl:]]", "204800"} {
		if !strings.Contains(precheckScript, want) {
			t.Errorf("precheck script lacks %q:\n%s", want, precheckScript)
		}
	}
}

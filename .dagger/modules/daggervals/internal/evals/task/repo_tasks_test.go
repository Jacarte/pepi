package task

import (
	"fmt"
	"sort"
	"strings"
	"testing"
)

// fakeSource is a tasks repo checkout held in a map of path -> contents.
type fakeSource map[string]string

func (f fakeSource) ReadFile(path string) (string, error) {
	text, ok := f[path]
	if !ok {
		return "", fmt.Errorf("no such file %s", path)
	}
	return text, nil
}

func (f fakeSource) ListDir(path string) ([]string, error) {
	seen := map[string]bool{}
	for file := range f {
		rest, ok := strings.CutPrefix(file, path+"/")
		if !ok {
			continue
		}
		if dir, _, nested := strings.Cut(rest, "/"); nested {
			seen[dir+"/"] = true
		} else {
			seen[rest] = true
		}
	}
	if len(seen) == 0 {
		return nil, fmt.Errorf("no such directory %s", path)
	}
	var entries []string
	for entry := range seen {
		entries = append(entries, entry)
	}
	sort.Strings(entries)
	return entries, nil
}

const validTaskYAML = `name: demo
kind: parallel
language: go
go_version: 1.22.5
sessions: 1
session_timeout_minutes: 30
budget_usd: 2
deadline_minutes: 15
scoring: count             # milestones passed, in any order
owned_dirs: [acceptance]
restore_seed_tests: true
`

const validMilestonesYAML = `milestones:
  - id: m1-first
    title: First thing
    command: go test ./a/... -run '^TestM1'
  - id: m2-second
    title: "Second: thing"
    command: go test ./b/... -run '^TestM2'
`

// fakeTask builds the files of one task directory.
func fakeTask(name, taskYAML, milestonesYAML string, prompts ...string) fakeSource {
	files := fakeSource{
		"tasks/" + name + "/task.yaml":        taskYAML,
		"tasks/" + name + "/milestones.yaml":  milestonesYAML,
		"tasks/" + name + "/seed/go.mod":      "module demo\n",
		"tasks/" + name + "/hidden/a_test.go": "package a\n",
	}
	for i, prompt := range prompts {
		files[fmt.Sprintf("tasks/%s/prompts/%02d.md", name, i+1)] = prompt
	}
	return files
}

func TestLoadRepoTaskValid(t *testing.T) {
	src := fakeTask("demo", validTaskYAML, validMilestonesYAML, "Do the thing.\n")
	spec, err := LoadRepoTask(src, "demo")
	if err != nil {
		t.Fatal(err)
	}
	if spec.Name != "demo" || spec.Kind != "parallel" || spec.GoVersion != "1.22.5" || spec.Sessions != 1 ||
		spec.SessionTimeoutMinutes != 30 || spec.DeadlineMinutes != 15 || spec.Scoring != ScoringCount {
		t.Fatalf("unexpected spec: %+v", spec)
	}
	if len(spec.Milestones) != 2 || spec.Milestones[1].Title != "Second: thing" || spec.Milestones[0].Command != "go test ./a/... -run '^TestM1'" {
		t.Fatalf("milestones: %+v", spec.Milestones)
	}
	if spec.ProblemStatement() != "Do the thing.\n" {
		t.Fatalf("problem statement: %q", spec.ProblemStatement())
	}
}

func TestLoadRepoTaskNullDeadline(t *testing.T) {
	taskYAML := strings.NewReplacer("kind: parallel", "kind: long", "deadline_minutes: 15", "deadline_minutes: null",
		"owned_dirs: [acceptance]\n", "", "restore_seed_tests: true\n", "").Replace(validTaskYAML)
	spec, err := LoadRepoTask(fakeTask("demo", taskYAML, validMilestonesYAML, "p"), "demo")
	if err != nil || spec.DeadlineMinutes != 0 {
		t.Fatalf("unexpected spec %+v, %v", spec, err)
	}
}

func TestLoadRepoTaskRejectsBadTasks(t *testing.T) {
	replace := func(old, new string) string {
		if !strings.Contains(validTaskYAML, old) {
			t.Fatalf("test bug: %q not in task.yaml", old)
		}
		return strings.Replace(validTaskYAML, old, new, 1)
	}
	tests := []struct {
		name     string
		taskYAML string
		prompts  []string
		wantErr  string
	}{
		{"bad kind", replace("kind: parallel", "kind: sprint"), []string{"p"}, "kind must be"},
		{"not go", replace("language: go", "language: rust"), []string{"p"}, "language must be go"},
		{"name mismatch", replace("name: demo", "name: other"), []string{"p"}, "does not match directory"},
		{"missing name", replace("name: demo\n", ""), []string{"p"}, "name missing"},
		{"missing sessions", replace("sessions: 1\n", ""), []string{"p"}, "sessions must be"},
		{"zero sessions", replace("sessions: 1", "sessions: 0"), []string{"p"}, "sessions must be"},
		{"missing timeout", replace("session_timeout_minutes: 30\n", ""), []string{"p"}, "session_timeout_minutes"},
		{"go_version injection", replace("go_version: 1.22.5", `go_version: "1.22.5; rm -rf /"`), []string{"p"}, "go_version"},
		{"bad scoring", replace("scoring: count", "scoring: sum"), []string{"p"}, "scoring must be"},
		{"parallel without deadline", replace("deadline_minutes: 15", "deadline_minutes: null"), []string{"p"}, "deadline_minutes required"},
		{"absolute owned dir", replace("[acceptance]", "[/etc]"), []string{"p"}, "owned_dirs"},
		{"escaping owned dir", replace("[acceptance]", "[../x]"), []string{"p"}, "owned_dirs"},
		{"unknown field", validTaskYAML + "sesions: 3\n", []string{"p"}, "sesions"},
		{"prompts fewer than sessions", replace("sessions: 1", "sessions: 2"), []string{"p"}, "prompts/ must hold exactly 01.md, 02.md"},
		{"prompts more than sessions", validTaskYAML, []string{"p", "q"}, "prompts/ must hold exactly 01.md"},
		{"empty prompt", validTaskYAML, []string{"  \n"}, "is empty"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := LoadRepoTask(fakeTask("demo", tt.taskYAML, validMilestonesYAML, tt.prompts...), "demo")
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("want error containing %q, got %v", tt.wantErr, err)
			}
		})
	}
}

func TestParseMilestonesRejectsBad(t *testing.T) {
	tests := []struct{ name, yaml, wantErr string }{
		{"empty", "milestones: []\n", "no milestones"},
		{"bad id", "milestones:\n  - {id: first, title: t, command: go test ./...}\n", "must start with m<number>"},
		{"duplicate", "milestones:\n  - {id: m1, title: t, command: go test ./...}\n  - {id: m1, title: u, command: go test ./...}\n", "duplicate"},
		{"missing title", "milestones:\n  - {id: m1, command: go test ./...}\n", "title missing"},
		{"not go test", "milestones:\n  - {id: m1, title: t, command: make test}\n", "go test"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := ParseMilestonesYAML(tt.yaml)
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("want error containing %q, got %v", tt.wantErr, err)
			}
		})
	}
}

func TestLoadRepoTasksSkipsMultiSession(t *testing.T) {
	src := fakeTask("demo", validTaskYAML, validMilestonesYAML, "p")
	long := strings.NewReplacer("name: demo", "name: long-one", "kind: parallel", "kind: long", "sessions: 1", "sessions: 3", "deadline_minutes: 15", "deadline_minutes: null").Replace(validTaskYAML)
	for path, text := range fakeTask("long-one", long, validMilestonesYAML, "a", "b", "c") {
		src[path] = text
	}
	src["tasks/.gitkeep"] = ""

	runnable, skipped, err := LoadRepoTasks(src, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(runnable) != 1 || runnable[0].Name != "demo" {
		t.Fatalf("runnable: %+v", runnable)
	}
	if len(skipped) != 1 || skipped[0].Name != "long-one" || skipped[0].Reason != "multi-session not supported yet" {
		t.Fatalf("skipped: %+v", skipped)
	}

	// Naming a multi-session task still skips it instead of failing.
	runnable, skipped, err = LoadRepoTasks(src, []string{"long-one"})
	if err != nil || len(runnable) != 0 || len(skipped) != 1 {
		t.Fatalf("named multi-session task: runnable %v skipped %v err %v", runnable, skipped, err)
	}

	if _, _, err := LoadRepoTasks(src, []string{"nope"}); err == nil || !strings.Contains(err.Error(), `unknown task "nope"`) {
		t.Fatalf("unknown task: %v", err)
	}
}

func TestLoadRepoTasksMalformedTaskIsAnError(t *testing.T) {
	src := fakeTask("demo", strings.Replace(validTaskYAML, "kind: parallel", "kind: nope", 1), validMilestonesYAML, "p")
	if _, _, err := LoadRepoTasks(src, nil); err == nil || !strings.Contains(err.Error(), "task demo") {
		t.Fatalf("want error naming the task, got %v", err)
	}
}

func TestParseTaskNames(t *testing.T) {
	names, err := ParseTaskNames(" five-modules, legacy-monolith,,five-modules ")
	if err != nil || strings.Join(names, "|") != "five-modules|legacy-monolith" {
		t.Fatalf("got %v, %v", names, err)
	}
	if names, err := ParseTaskNames(""); err != nil || len(names) != 0 {
		t.Fatalf("empty: %v, %v", names, err)
	}
	if _, err := ParseTaskNames("ok,../etc"); err == nil {
		t.Fatal("expected an error for a path-like task name")
	}
}

func TestTasksRunIDSuffix(t *testing.T) {
	const sha = "0e17b0fe2524ffa22d5975d910a054d5ac1572de"
	got, err := TasksRunIDSuffix(sha, nil)
	if err != nil || got != "@tasks@0e17b0fe2524" {
		t.Fatalf("got %q, %v", got, err)
	}
	got, err = TasksRunIDSuffix(sha, []string{"five-modules", "legacy-monolith"})
	if err != nil || got != "@tasks@0e17b0fe2524@only=five-modules,legacy-monolith" {
		t.Fatalf("got %q, %v", got, err)
	}
	for _, bad := range []string{"", "main", "0e17b0f", "0E17B0FE2524XX"} {
		if _, err := TasksRunIDSuffix(bad, nil); err == nil {
			t.Fatalf("sha %q should be rejected", bad)
		}
	}
}

func TestValidateTasksSuiteArgs(t *testing.T) {
	tests := []struct {
		name                                  string
		suite, tasksRepo, tasksRef, taskNames string
		wantErr                               string
	}{
		{"default suite", "", "", "", "", ""},
		{"tasks with ref", "tasks", "", "main", "", ""},
		{"tasks with everything", "tasks", "https://example.com/x.git", "abc", "a,b", ""},
		{"tasks without ref", "tasks", "", "", "", "requires tasksRef"},
		{"tasks with blank ref", "tasks", "", "  ", "", "requires tasksRef"},
		{"tasks and go", "tasks,go", "", "main", "", "cannot be combined"},
		{"all and tasks", "all,tasks", "", "main", "", "cannot be combined"},
		{"tasks arguments with go suite", "go", "", "main", "", "only valid with suite=tasks"},
		{"task names with default suite", "", "", "", "a", "only valid with suite=tasks"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := ValidateTasksSuiteArgs(tt.suite, tt.tasksRepo, tt.tasksRef, tt.taskNames)
			if tt.wantErr == "" && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if tt.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tt.wantErr)) {
				t.Fatalf("want error containing %q, got %v", tt.wantErr, err)
			}
		})
	}
}

func TestTimeoutSeconds(t *testing.T) {
	tests := []struct {
		name string
		spec RepoTaskSpec
		want int
	}{
		{"parallel uses the deadline", RepoTaskSpec{Kind: "parallel", SessionTimeoutMinutes: 30, DeadlineMinutes: 15}, 900},
		{"long uses the session timeout", RepoTaskSpec{Kind: "long", SessionTimeoutMinutes: 30}, 1800},
		{"long ignores a deadline", RepoTaskSpec{Kind: "long", SessionTimeoutMinutes: 20, DeadlineMinutes: 5}, 1200},
		{"parallel without deadline", RepoTaskSpec{Kind: "parallel", SessionTimeoutMinutes: 10}, 600},
		{"capped", RepoTaskSpec{Kind: "long", SessionTimeoutMinutes: 120}, 3600},
	}
	for _, tt := range tests {
		if got := tt.spec.TimeoutSeconds(); got != tt.want {
			t.Errorf("%s: got %d, want %d", tt.name, got, tt.want)
		}
	}
}

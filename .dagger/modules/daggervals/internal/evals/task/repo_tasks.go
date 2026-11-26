package task

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path"
	"regexp"
	"slices"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// Tasks are read from the pepi-eval-tasks layout
// tasks/<name>/{task.yaml, milestones.yaml, prompts/NN.md, seed/, hidden/, memory/fixture.json}
// through a TaskSource, so the parsing is tested without a Dagger engine.

const (
	// DefaultTaskTimeoutSeconds is the agent time limit when FixTask.TimeoutSeconds is 0.
	DefaultTaskTimeoutSeconds = 1800
	// MaxTaskTimeoutSeconds is also the cap in network/capture.sh.
	MaxTaskTimeoutSeconds = 3600
)

const (
	ScoringPrefix = "prefix"
	ScoringCount  = "count"
)

// TaskSource is the read-only view of a tasks repo checkout.
type TaskSource interface {
	ReadFile(path string) (string, error)
	// ListDir returns the entry names of a directory. Subdirectories end with "/".
	ListDir(path string) ([]string, error)
}

// Milestone is one entry of milestones.yaml.
type Milestone struct {
	ID      string `yaml:"id"`
	Title   string `yaml:"title"`
	Command string `yaml:"command"`
}

// RepoTaskSpec is a validated task directory: task.yaml, milestones.yaml and the prompts.
type RepoTaskSpec struct {
	Name                  string
	Kind                  string
	GoVersion             string
	Sessions              int
	SessionTimeoutMinutes int
	DeadlineMinutes       int // 0 when null
	Scoring               string
	Milestones            []Milestone
	// Prompts holds prompts/01.md .. prompts/NN.md, one per session.
	Prompts []string
	// MemoryFixture holds the texts of the optional memory/fixture.json. Only the harness
	// reads it, to seed the memory server; it never reaches the agent's container.
	MemoryFixture []string
}

// taskYAML mirrors task.yaml, including the fields only the tasks repo scripts read.
// Pointers tell a missing field from zero.
type taskYAML struct {
	Name                  string   `yaml:"name"`
	Kind                  string   `yaml:"kind"`
	Language              string   `yaml:"language"`
	GoVersion             string   `yaml:"go_version"`
	Sessions              *int     `yaml:"sessions"`
	SessionTimeoutMinutes *int     `yaml:"session_timeout_minutes"`
	BudgetUSD             *float64 `yaml:"budget_usd"`
	DeadlineMinutes       *int     `yaml:"deadline_minutes"`
	Scoring               string   `yaml:"scoring"`
	OwnedDirs             []string `yaml:"owned_dirs"`
	RestoreSeedTests      bool     `yaml:"restore_seed_tests"`
}

type milestonesYAML struct {
	Milestones []Milestone `yaml:"milestones"`
}

var (
	goVersionPattern  = regexp.MustCompile(`^[0-9]+\.[0-9]+(\.[0-9]+)?$`)
	milestoneIDFormat = regexp.MustCompile(`^m[0-9]+($|[^0-9])`)
	taskNamePattern   = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*$`)
)

// decodeStrict rejects unknown keys, so a typo in a task file does not fall back to a default.
func decodeStrict(data string, out any) error {
	decoder := yaml.NewDecoder(strings.NewReader(data))
	decoder.KnownFields(true)
	if err := decoder.Decode(out); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	return nil
}

// ParseTaskYAML validates task.yaml; dirName must match the name field. Milestones and
// prompts are filled by LoadRepoTask.
func ParseTaskYAML(dirName, data string) (RepoTaskSpec, error) {
	var raw taskYAML
	if err := decodeStrict(data, &raw); err != nil {
		return RepoTaskSpec{}, fmt.Errorf("task.yaml: %w", err)
	}
	var problems []string
	bad := func(format string, args ...any) { problems = append(problems, fmt.Sprintf(format, args...)) }

	if raw.Name == "" {
		bad("name missing")
	} else if raw.Name != dirName {
		bad("name %q does not match directory %q", raw.Name, dirName)
	}
	switch raw.Kind {
	case "long", "needs-subagents", "parallel", "memory":
	default:
		bad("kind must be long|needs-subagents|parallel|memory (got %q)", raw.Kind)
	}
	if raw.Language != "go" {
		bad("language must be go (got %q)", raw.Language)
	}
	// go_version ends up in a download URL: only plain version numbers are accepted.
	if !goVersionPattern.MatchString(raw.GoVersion) {
		bad("go_version must look like 1.22.5 (got %q)", raw.GoVersion)
	}
	if raw.Sessions == nil || *raw.Sessions < 1 {
		bad("sessions must be an integer >= 1")
	}
	if raw.SessionTimeoutMinutes == nil || *raw.SessionTimeoutMinutes < 1 {
		bad("session_timeout_minutes must be a positive integer")
	}
	if raw.BudgetUSD == nil || *raw.BudgetUSD < 0 {
		bad("budget_usd must be a number >= 0")
	}
	if raw.Scoring != ScoringPrefix && raw.Scoring != ScoringCount {
		bad("scoring must be prefix|count (got %q)", raw.Scoring)
	}
	if raw.DeadlineMinutes != nil && *raw.DeadlineMinutes < 1 {
		bad("deadline_minutes must be a positive integer or null")
	}
	if raw.Kind == "parallel" && raw.DeadlineMinutes == nil {
		bad("deadline_minutes required for kind=parallel")
	}
	for _, dir := range raw.OwnedDirs {
		if dir == "" || path.IsAbs(dir) || path.Clean(dir) == "." || slices.Contains(strings.Split(dir, "/"), "..") {
			bad("owned_dirs entry %q must be a relative path inside the workspace", dir)
		}
	}
	if len(problems) > 0 {
		return RepoTaskSpec{}, fmt.Errorf("task.yaml: %s", strings.Join(problems, "; "))
	}

	spec := RepoTaskSpec{
		Name:                  raw.Name,
		Kind:                  raw.Kind,
		GoVersion:             raw.GoVersion,
		Sessions:              *raw.Sessions,
		SessionTimeoutMinutes: *raw.SessionTimeoutMinutes,
		Scoring:               raw.Scoring,
	}
	if raw.DeadlineMinutes != nil {
		spec.DeadlineMinutes = *raw.DeadlineMinutes
	}
	return spec, nil
}

// ParseMilestonesYAML validates milestones.yaml.
func ParseMilestonesYAML(data string) ([]Milestone, error) {
	var raw milestonesYAML
	if err := decodeStrict(data, &raw); err != nil {
		return nil, fmt.Errorf("milestones.yaml: %w", err)
	}
	if len(raw.Milestones) == 0 {
		return nil, errors.New("milestones.yaml: no milestones")
	}
	seen := map[string]bool{}
	for i, m := range raw.Milestones {
		_, isSkill, skillErr := ParseSkillCheck(m.Command)
		switch {
		case m.ID == "":
			return nil, fmt.Errorf("milestones.yaml: milestone #%d: id missing", i+1)
		case !milestoneIDFormat.MatchString(m.ID):
			return nil, fmt.Errorf("milestones.yaml: id %q must start with m<number>", m.ID)
		case seen[m.ID]:
			return nil, fmt.Errorf("milestones.yaml: duplicate id %q", m.ID)
		case m.Title == "":
			return nil, fmt.Errorf("milestones.yaml: milestone %s: title missing", m.ID)
		case skillErr != nil:
			return nil, fmt.Errorf("milestones.yaml: milestone %s: %w", m.ID, skillErr)
		case !isSkill && !strings.HasPrefix(m.Command, "go test"):
			return nil, fmt.Errorf("milestones.yaml: milestone %s: command must start with 'go test', skill_loaded: or skill_not_loaded:", m.ID)
		}
		seen[m.ID] = true
	}
	return raw.Milestones, nil
}

// ParseMemoryFixture validates memory/fixture.json: {"memories":[{"text":"..."}]}.
func ParseMemoryFixture(data string) ([]string, error) {
	var raw struct {
		Memories []struct {
			Text string `json:"text"`
		} `json:"memories"`
	}
	decoder := json.NewDecoder(strings.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&raw); err != nil {
		return nil, fmt.Errorf("memory/fixture.json: %w", err)
	}
	if len(raw.Memories) == 0 {
		return nil, errors.New("memory/fixture.json: no memories")
	}
	texts := make([]string, len(raw.Memories))
	for i, m := range raw.Memories {
		if strings.TrimSpace(m.Text) == "" {
			return nil, fmt.Errorf("memory/fixture.json: memory #%d: text missing", i+1)
		}
		texts[i] = m.Text
	}
	return texts, nil
}

// LoadRepoTask reads and validates tasks/<name>, which must hold one prompt per session
// (prompts/01.md .. NN.md) and may hold memory/fixture.json.
func LoadRepoTask(src TaskSource, name string) (RepoTaskSpec, error) {
	dir := "tasks/" + name
	taskYAMLText, err := src.ReadFile(dir + "/task.yaml")
	if err != nil {
		return RepoTaskSpec{}, fmt.Errorf("task %s: %w", name, err)
	}
	spec, err := ParseTaskYAML(name, taskYAMLText)
	if err != nil {
		return RepoTaskSpec{}, fmt.Errorf("task %s: %w", name, err)
	}
	milestonesText, err := src.ReadFile(dir + "/milestones.yaml")
	if err != nil {
		return RepoTaskSpec{}, fmt.Errorf("task %s: %w", name, err)
	}
	if spec.Milestones, err = ParseMilestonesYAML(milestonesText); err != nil {
		return RepoTaskSpec{}, fmt.Errorf("task %s: %w", name, err)
	}

	entries, err := src.ListDir(dir + "/prompts")
	if err != nil {
		return RepoTaskSpec{}, fmt.Errorf("task %s: prompts: %w", name, err)
	}
	var want []string
	for n := 1; n <= spec.Sessions; n++ {
		want = append(want, fmt.Sprintf("%02d.md", n))
	}
	sort.Strings(entries)
	if !slices.Equal(entries, want) {
		return RepoTaskSpec{}, fmt.Errorf("task %s: prompts/ must hold exactly %s for sessions: %d (found: %s)",
			name, strings.Join(want, ", "), spec.Sessions, strings.Join(entries, ", "))
	}
	for _, file := range want {
		text, err := src.ReadFile(dir + "/prompts/" + file)
		if err != nil {
			return RepoTaskSpec{}, fmt.Errorf("task %s: %w", name, err)
		}
		if strings.TrimSpace(text) == "" {
			return RepoTaskSpec{}, fmt.Errorf("task %s: prompts/%s is empty", name, file)
		}
		spec.Prompts = append(spec.Prompts, text)
	}

	taskEntries, err := src.ListDir(dir)
	if err != nil {
		return RepoTaskSpec{}, fmt.Errorf("task %s: %w", name, err)
	}
	if slices.Contains(taskEntries, "memory/") {
		fixture, err := src.ReadFile(dir + "/memory/fixture.json")
		if err != nil {
			return RepoTaskSpec{}, fmt.Errorf("task %s: %w", name, err)
		}
		if spec.MemoryFixture, err = ParseMemoryFixture(fixture); err != nil {
			return RepoTaskSpec{}, fmt.Errorf("task %s: %w", name, err)
		}
	}
	return spec, nil
}

// LoadRepoTasks loads the named tasks, or every task directory when names is empty. A
// malformed task is an error.
func LoadRepoTasks(src TaskSource, names []string) ([]RepoTaskSpec, error) {
	entries, err := src.ListDir("tasks")
	if err != nil {
		return nil, fmt.Errorf("list tasks: %w", err)
	}
	var available []string
	for _, entry := range entries {
		if dir, ok := strings.CutSuffix(entry, "/"); ok {
			available = append(available, dir)
		}
	}
	sort.Strings(available)

	selected := available
	if len(names) > 0 {
		selected = nil
		for _, name := range names {
			if !slices.Contains(available, name) {
				return nil, fmt.Errorf("unknown task %q: available tasks: %s", name, strings.Join(available, ", "))
			}
			selected = append(selected, name)
		}
	}
	var specs []RepoTaskSpec
	for _, name := range selected {
		spec, err := LoadRepoTask(src, name)
		if err != nil {
			return nil, err
		}
		specs = append(specs, spec)
	}
	return specs, nil
}

// ParseTaskNames splits the comma-separated taskNames argument: trimmed, without
// empty entries or duplicates, sorted so the run id does not depend on the order.
func ParseTaskNames(csv string) ([]string, error) {
	var names []string
	for _, name := range strings.Split(csv, ",") {
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		if !taskNamePattern.MatchString(name) {
			return nil, fmt.Errorf("invalid task name %q", name)
		}
		if !slices.Contains(names, name) {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names, nil
}

// TasksRunIDSuffix is appended to the run id of a tasks-suite run: the first 12 hex
// digits of the resolved commit, plus the task filter when one is set.
func TasksRunIDSuffix(commitSHA string, names []string) (string, error) {
	if len(commitSHA) < 12 || strings.Trim(commitSHA, "0123456789abcdef") != "" {
		return "", fmt.Errorf("tasks commit %q is not a hex sha", commitSHA)
	}
	suffix := "@tasks@" + commitSHA[:12]
	if len(names) > 0 {
		suffix += "@only=" + strings.Join(names, ",")
	}
	return suffix, nil
}

// TimeoutSeconds is the agent time limit of each session: the deadline for parallel tasks
// that have one, else the session timeout, capped at MaxTaskTimeoutSeconds.
func (s RepoTaskSpec) TimeoutSeconds() int {
	minutes := s.SessionTimeoutMinutes
	if s.Kind == "parallel" && s.DeadlineMinutes > 0 {
		minutes = s.DeadlineMinutes
	}
	return min(minutes*60, MaxTaskTimeoutSeconds)
}

// ProblemStatement is the prompt of a single-session task; for more sessions, every prompt
// under a "Session N prompt:" heading, for the judges (the agent gets one prompt per session).
func (s RepoTaskSpec) ProblemStatement() string {
	if len(s.Prompts) == 1 {
		return s.Prompts[0]
	}
	parts := make([]string, len(s.Prompts))
	for i, prompt := range s.Prompts {
		parts[i] = fmt.Sprintf("Session %d prompt:\n%s", i+1, prompt)
	}
	return strings.Join(parts, "\n\n")
}

// DefaultTasksRepo is the tasks repo used when tasksRepo is empty.
const DefaultTasksRepo = "https://github.com/Jacarte/pepi-eval-tasks.git"

// ValidateTasksSuiteArgs checks the tasks-suite arguments of Evals: suite=tasks is only
// valid alone and needs tasksRef, and the other suites reject the tasks arguments.
func ValidateTasksSuiteArgs(suite, tasksRepo, tasksRef, taskNames string) error {
	if suite != "tasks" {
		if strings.Contains(suite, "tasks") {
			return fmt.Errorf("suite %q: tasks cannot be combined with other suites, use suite=tasks alone", suite)
		}
		if tasksRepo != "" || tasksRef != "" || taskNames != "" {
			return errors.New("tasksRepo, tasksRef and taskNames are only valid with suite=tasks")
		}
		return nil
	}
	if strings.TrimSpace(tasksRef) == "" {
		return errors.New("suite=tasks requires tasksRef (a branch or commit sha of the tasks repo)")
	}
	return nil
}

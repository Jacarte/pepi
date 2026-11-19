// Package ci holds the logic of the CI eval workflow: the tiers and their shards, the
// merging and comparison of the shards' result files, the PR comment bodies and the
// baseline committed after a merge. It calls no model and no container.
package ci

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"dagger/daggervals/internal/evals/task"
)

// Shard is one parallel eval job of a tier.
type Shard struct {
	Name string
	// Suite is the Evals suite: rust, go or tasks.
	Suite string
	// TaskNames filters the tasks suite, sorted as task.ParseTaskNames sorts them so the
	// run id suffix @only=... matches. Evals accepts no filter for rust and go: they run whole.
	TaskNames []string
}

// Tier is what a PR label selects: its shards, each run Runs times.
type Tier struct {
	Name   string
	Runs   int
	Shards []Shard
}

var (
	rustShard   = Shard{Name: "rust", Suite: "rust"}
	goShard     = Shard{Name: "go", Suite: "go"}
	memoryShard = Shard{Name: "tasks-memory", Suite: "tasks", TaskNames: []string{
		"memory-convention", "memory-key", "memory-oncall", "memory-recall", "memory-slowtests", "memory-stale-doc",
	}}
	longShard = Shard{Name: "tasks-long", Suite: "tasks", TaskNames: []string{
		"five-modules", "kv-database", "legacy-monolith", "memory-carryover", "twelve-modules",
	}}
)

// Tiers are the eval tiers, in the order of the PR labels eval and eval-full.
var Tiers = []Tier{
	{Name: "fast", Runs: 1, Shards: []Shard{rustShard, goShard, memoryShard}},
	{Name: "full", Runs: 3, Shards: []Shard{rustShard, goShard, memoryShard, longShard}},
}

// LookupTier returns the tier called name.
func LookupTier(name string) (Tier, error) {
	for _, t := range Tiers {
		if t.Name == name {
			return t, nil
		}
	}
	return Tier{}, fmt.Errorf("unknown tier %q: want fast or full", name)
}

// Shard returns the shard of t called name.
func (t Tier) Shard(name string) (Shard, error) {
	var names []string
	for _, s := range t.Shards {
		if s.Name == name {
			return s, nil
		}
		names = append(names, s.Name)
	}
	return Shard{}, fmt.Errorf("tier %s has no shard %q: want one of %s", t.Name, name, strings.Join(names, ", "))
}

// FixedName is the Evals --fixed name of a shard: run i of it is cached as
// <FixedName>[-<i>]<suite suffix><memory suffix>, so every file names its tier, shard and run.
func (t Tier) FixedName(s Shard) string {
	return "ci-" + t.Name + "-" + s.Name
}

// TaskCount is how many task outcomes a run of s has.
func (s Shard) TaskCount() int {
	switch s.Suite {
	case "rust":
		return len(task.RustTasks)
	case "go":
		return len(task.GoTasks)
	default:
		return len(s.TaskNames)
	}
}

// suiteSuffix is the run id suffix Evals gives the suite of s (see Evals and
// task.TasksRunIDSuffix).
func suiteSuffix(s Shard, tasksSha string) string {
	switch s.Suite {
	case "rust":
		return ""
	case "tasks":
		return "@tasks@" + shortSha(tasksSha) + "@only=" + strings.Join(s.TaskNames, ",")
	default:
		return "@" + s.Suite
	}
}

// memorySuffixPattern is memory.Config.RunIDSuffix with memory on and the fixture seeded,
// the only memory arm of the CI.
var memorySuffixPattern = regexp.MustCompile(`^@hippo=[A-Za-z0-9_][A-Za-z0-9._-]*-[0-9a-f]{12}$`)

// judgeFileSuffix ends the judge report that Evals writes next to each cache file.
const judgeFileSuffix = ".eval.md"

// ResultFile is a file of ci-eval-run, by its name.
type ResultFile struct {
	Shard Shard
	// Run counts from 1.
	Run int
	// Judge is the .eval.md judge report of the run, otherwise the []TaskOutcome cache file.
	Judge bool
	// Memory is the memory arm: @hippo=<tag>-<digest12>.
	Memory string
}

// ParseResultName reads the tier's file name: tasksSha is the tasks repo commit the tasks
// shards ran with.
func (t Tier) ParseResultName(name, tasksSha string) (ResultFile, error) {
	stem, judge := strings.CutSuffix(name, judgeFileSuffix)
	for _, s := range t.Shards {
		rest, ok := strings.CutPrefix(stem, t.FixedName(s))
		if !ok || (rest != "" && rest[0] != '-' && rest[0] != '@') {
			continue
		}
		run := 1
		if t.Runs > 1 {
			digits, after, _ := strings.Cut(strings.TrimPrefix(rest, "-"), "@")
			n, err := strconv.Atoi(digits)
			if !strings.HasPrefix(rest, "-") || err != nil || n < 1 || n > t.Runs || strconv.Itoa(n) != digits {
				return ResultFile{}, fmt.Errorf("file %q: want run 1 to %d after %s", name, t.Runs, t.FixedName(s))
			}
			run, rest = n, "@"+after
			if after == "" {
				rest = ""
			}
		}
		memory, ok := strings.CutPrefix(rest, suiteSuffix(s, tasksSha))
		if !ok || !memorySuffixPattern.MatchString(memory) {
			return ResultFile{}, fmt.Errorf("file %q: want %s%s followed by @hippo=<tag>-<digest12>", name, t.FixedName(s), suiteSuffix(s, tasksSha))
		}
		return ResultFile{Shard: s, Run: run, Judge: judge, Memory: memory}, nil
	}
	return ResultFile{}, fmt.Errorf("file %q is not a result file of tier %s", name, t.Name)
}

var shaPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)

// ValidateSha accepts a full lowercase git sha.
func ValidateSha(what, sha string) error {
	if !shaPattern.MatchString(sha) {
		return fmt.Errorf("%s %q is not a full 40-hex git sha", what, sha)
	}
	return nil
}

// ValidatePR accepts a PR number.
func ValidatePR(pr int) error {
	if pr <= 0 {
		return fmt.Errorf("prNumber must be a PR number, got %d", pr)
	}
	return nil
}

func shortSha(sha string) string {
	if len(sha) < 12 {
		return sha
	}
	return sha[:12]
}

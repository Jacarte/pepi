package ci

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"dagger/daggervals/internal/evals/engine"
	"dagger/daggervals/internal/evals/task"
)

const (
	testTasksSha = "0123456789abcdef0123456789abcdef01234567"
	testMemory   = "@hippo=c8f33b2-aaaaaaaaaaaa"
)

func TestTiers(t *testing.T) {
	shardNames := func(tier Tier) (names []string) {
		for _, s := range tier.Shards {
			names = append(names, s.Name+":"+s.Suite)
		}
		return names
	}
	fast, err := LookupTier("fast")
	if err != nil || fast.Runs != 1 || !reflect.DeepEqual(shardNames(fast), []string{"rust:rust", "go:go", "tasks-memory:tasks"}) {
		t.Fatalf("fast: %+v, %v", fast, err)
	}
	full, err := LookupTier("full")
	if err != nil || full.Runs != 3 || !reflect.DeepEqual(shardNames(full), []string{"rust:rust", "go:go", "tasks-memory:tasks", "tasks-long:tasks"}) {
		t.Fatalf("full: %+v, %v", full, err)
	}
	memory, _ := fast.Shard("tasks-memory")
	if want := []string{"memory-convention", "memory-key", "memory-oncall", "memory-recall", "memory-slowtests", "memory-stale-doc"}; !reflect.DeepEqual(memory.TaskNames, want) {
		t.Fatalf("tasks-memory: %v", memory.TaskNames)
	}
	long, _ := full.Shard("tasks-long")
	if want := []string{"five-modules", "kv-database", "legacy-monolith", "memory-carryover", "twelve-modules"}; !reflect.DeepEqual(long.TaskNames, want) {
		t.Fatalf("tasks-long: %v", long.TaskNames)
	}
	// The rust and go shards run their whole suite.
	if len(task.RustTasks) != 8 || len(task.GoTasks) != 5 {
		t.Fatalf("suites: %d rust, %d go tasks", len(task.RustTasks), len(task.GoTasks))
	}
	for _, s := range full.Shards {
		if s.Suite != "tasks" {
			if s.TaskNames != nil {
				t.Fatalf("%s: Evals takes no task names for suite %s", s.Name, s.Suite)
			}
			continue
		}
		// The names are what Evals accepts, in its order, so the run id suffix is the same.
		parsed, err := task.ParseTaskNames(strings.Join(s.TaskNames, ","))
		if err != nil || !reflect.DeepEqual(parsed, s.TaskNames) {
			t.Fatalf("%s: %v, %v", s.Name, parsed, err)
		}
		want, _ := task.TasksRunIDSuffix(testTasksSha, parsed)
		if got := suiteSuffix(s, testTasksSha); got != want {
			t.Fatalf("%s suffix %q, Evals %q", s.Name, got, want)
		}
	}
	if _, err := LookupTier("medium"); err == nil {
		t.Fatal("unknown tier")
	}
	if _, err := fast.Shard("tasks-long"); err == nil || !strings.Contains(err.Error(), "rust, go, tasks-memory") {
		t.Fatalf("fast has no tasks-long: %v", err)
	}
}

// resultName is the name Evals gives run i of shard s (see Evals and engine.RunID).
func resultName(tier Tier, s Shard, run int) string {
	return engine.RunID(tier.FixedName(s), run, tier.Runs, suiteSuffix(s, testTasksSha)+testMemory)
}

func TestResultNames(t *testing.T) {
	fast, _ := LookupTier("fast")
	full, _ := LookupTier("full")
	want := map[string]string{
		resultName(fast, fast.Shards[0], 1): "ci-fast-rust@hippo=c8f33b2-aaaaaaaaaaaa",
		resultName(fast, fast.Shards[1], 1): "ci-fast-go@go@hippo=c8f33b2-aaaaaaaaaaaa",
		resultName(fast, fast.Shards[2], 1): "ci-fast-tasks-memory@tasks@0123456789ab@only=memory-convention,memory-key,memory-oncall,memory-recall,memory-slowtests,memory-stale-doc@hippo=c8f33b2-aaaaaaaaaaaa",
		resultName(full, full.Shards[0], 2): "ci-full-rust-2@hippo=c8f33b2-aaaaaaaaaaaa",
		resultName(full, full.Shards[3], 3): "ci-full-tasks-long-3@tasks@0123456789ab@only=five-modules,kv-database,legacy-monolith,memory-carryover,twelve-modules@hippo=c8f33b2-aaaaaaaaaaaa",
	}
	for got, name := range want {
		if got != name {
			t.Errorf("got %s, want %s", got, name)
		}
	}
	// Every shard and run of a tier parses back, and no two collide.
	for _, tier := range Tiers {
		seen := map[string]bool{}
		for _, s := range tier.Shards {
			for run := 1; run <= tier.Runs; run++ {
				name := resultName(tier, s, run)
				for _, file := range []string{name, name + ".eval.md"} {
					f, err := tier.ParseResultName(file, testTasksSha)
					if err != nil || f.Shard.Name != s.Name || f.Run != run || f.Judge != strings.HasSuffix(file, ".eval.md") || f.Memory != testMemory {
						t.Fatalf("%s: %+v, %v", file, f, err)
					}
					if seen[file] {
						t.Fatalf("%s collides", file)
					}
					seen[file] = true
				}
			}
		}
	}
	bad := []string{
		"ci-fast-rust",       // no memory arm
		"ci-fast-rust@nomem", // memory off
		"ci-fast-rust-1@hippo=c8f33b2-aaaaaaaaaaaa", // fast has one run, no index
		"ci-full-rust@hippo=c8f33b2-aaaaaaaaaaaa",   // full needs the index
		"ci-full-rust-4@hippo=c8f33b2-aaaaaaaaaaaa", // run 4 of 3
		"ci-full-rust-01@hippo=c8f33b2-aaaaaaaaaaaa",
		"ci-fast-go@hippo=c8f33b2-aaaaaaaaaaaa",                                              // go without @go
		"ci-fast-tasks-memory@tasks@ffffffffffff@only=memory-key@hippo=c8f33b2-aaaaaaaaaaaa", // other commit and filter
		"ci-fast-tasks-long@tasks@0123456789ab@only=five-modules@hippo=c8f33b2-aaaaaaaaaaaa", // not a fast shard
		"evals-1@hippo=c8f33b2-aaaaaaaaaaaa",
		"../ci-fast-rust@hippo=c8f33b2-aaaaaaaaaaaa",
	}
	for _, name := range bad {
		tier := fast
		if strings.HasPrefix(name, "ci-full") {
			tier = full
		}
		if f, err := tier.ParseResultName(name, testTasksSha); err == nil {
			t.Errorf("%s parsed: %+v", name, f)
		}
	}
}

// outcome is a synthetic task outcome of profile plain.
func outcome(name string, score, maxScore int, cost float64) engine.TaskOutcome {
	start, end := "2026-01-01T00:00:00Z", "2026-01-01T00:02:00Z"
	return engine.TaskOutcome{
		Task: name, Score: score, MaxScore: maxScore, Valid: maxScore == 0 || score == maxScore, Profile: "plain",
		AgentStats:   engine.AgentStats{CostUSD: cost},
		ProcessStats: engine.ProcessStats{Groups: []engine.ProcessGroup{{Role: "parent", Instances: []engine.ProcessInstance{{StartedAt: &start, EndedAt: &end}}}}},
	}
}

// tierFiles builds the result files of every shard and run, the outcomes of a shard coming
// from shardOutcomes (the same for every run).
func tierFiles(t *testing.T, tier Tier, shardOutcomes map[string][]engine.TaskOutcome) map[string]string {
	t.Helper()
	files := map[string]string{}
	for _, s := range tier.Shards {
		for run := 1; run <= tier.Runs; run++ {
			content, err := json.Marshal(shardOutcomes[s.Name])
			if err != nil {
				t.Fatal(err)
			}
			name := resultName(tier, s, run)
			files[name] = string(content)
			files[name+".eval.md"] = "### " + s.Name + " run judged\nVERDICT: fine\n"
		}
	}
	return files
}

// shardOutcomes are one synthetic outcome per task of every shard, the costs counting up.
func shardOutcomes() map[string][]engine.TaskOutcome {
	out := map[string][]engine.TaskOutcome{}
	cost := 0.0
	add := func(shard, name string, score, maxScore int) {
		cost += 0.25
		out[shard] = append(out[shard], outcome(name, score, maxScore, cost))
	}
	for _, rust := range task.RustTasks {
		add("rust", rust.Env.GetRepo(), 0, 0)
	}
	for _, golang := range task.GoTasks {
		add("go", golang.Env.GetRepo(), 0, 0)
	}
	for _, s := range []Shard{memoryShard, longShard} {
		for _, name := range s.TaskNames {
			add(s.Name, "tasks/"+name, 1, 2)
		}
	}
	return out
}

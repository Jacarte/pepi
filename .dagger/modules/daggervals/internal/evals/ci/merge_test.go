package ci

import (
	"reflect"
	"strings"
	"testing"
)

func TestMergeResultsPerRunIndex(t *testing.T) {
	full, _ := LookupTier("full")
	files := tierFiles(t, full, shardOutcomes())
	r, err := MergeResults(full, testTasksSha, "plain", files)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Problems) != 0 || len(r.Runs) != 3 || r.Memory != testMemory || len(r.Files) != len(files) {
		t.Fatalf("results: %+v", r.Problems)
	}
	// One run per index with every shard, in tier order, so compare-runs sees 3 runs of every
	// task; tasks of the same repo are numbered by their order in the suite.
	names := r.TaskNames()
	if len(names) != 8+5+6+5 || names[0] != "https://github.com/tokio-rs/axum.git" || names[3] != "https://github.com/tokio-rs/tokio.git #2" ||
		names[4] != "https://github.com/tokio-rs/tokio.git #3" || names[7] != "https://github.com/sharkdp/bat.git #2" ||
		names[9] != "https://github.com/hashicorp/terraform.git #2" || names[13] != "tasks/memory-convention" || names[23] != "tasks/twelve-modules" {
		t.Fatalf("tasks: %v", names)
	}
	for i, run := range r.Runs {
		if !reflect.DeepEqual(taskNames(run), names) {
			t.Fatalf("run %d: %v", i+1, taskNames(run))
		}
	}
	if len(r.Judges) != 12 || r.Judges[0].Run != 1 || r.Judges[0].Shard != "rust" || r.Judges[4].Run != 2 || r.Judges[4].Shard != "rust" {
		t.Fatalf("judges: %+v", r.Judges)
	}
	inputs := r.RunInputs("pr")
	if len(inputs) != 3 || inputs[2].Label != "pr" || len(inputs[2].Outcomes) != 24 {
		t.Fatalf("run inputs: %+v", inputs)
	}
}

func TestMergeResultsProblems(t *testing.T) {
	fast, _ := LookupTier("fast")
	outcomes := shardOutcomes()
	outcomes["tasks-memory"] = outcomes["tasks-memory"][1:]
	for i := range outcomes["go"] {
		outcomes["go"][i].AgentExitCode = 1
	}
	outcomes["rust"][0].AgentExitCode = 1
	files := tierFiles(t, fast, outcomes)
	delete(files, resultName(fast, fast.Shards[2], 1)+".eval.md")
	r, err := MergeResults(fast, testTasksSha, "other", files)
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(r.Problems, "\n")
	for _, want := range []string{"shard tasks-memory run 1: 5 task outcomes, want 6", "shard go run 1: every agent exited with an error",
		"shard tasks-memory run 1: no judge report", `ran with profile "plain", not "other"`} {
		if !strings.Contains(joined, want) {
			t.Errorf("no problem %q in:\n%s", want, joined)
		}
	}
	// One agent failing is the agent's outcome, not the harness's.
	if strings.Contains(joined, "shard rust run 1: every agent") {
		t.Errorf("rust: %s", joined)
	}
	files[resultName(fast, fast.Shards[0], 1)] = "[]"
	delete(files, resultName(fast, fast.Shards[1], 1))
	r, _ = MergeResults(fast, testTasksSha, "plain", files)
	if joined := strings.Join(r.Problems, "\n"); !strings.Contains(joined, "shard rust run 1: 0 task outcomes, want 8") || !strings.Contains(joined, "shard go run 1: no result file") {
		t.Errorf("problems:\n%s", joined)
	}
	// No files at all: every shard is missing.
	r, err = MergeResults(fast, testTasksSha, "plain", map[string]string{})
	if err != nil || len(r.Problems) != 3 {
		t.Fatalf("empty: %+v, %v", r, err)
	}
}

func TestMergeResultsErrors(t *testing.T) {
	fast, _ := LookupTier("fast")
	cases := map[string]func(map[string]string){
		"not a result file of tier fast": func(f map[string]string) { f["notes.txt"] = "x" },
		"not an eval cache file":         func(f map[string]string) { f[resultName(fast, fast.Shards[0], 1)] = "{" },
		"two memory arms": func(f map[string]string) {
			name := resultName(fast, fast.Shards[0], 1)
			delete(f, name)
			f[strings.Replace(name, testMemory, "@hippo=c8f33b2-bbbbbbbbbbbb", 1)] = "[]"
		},
	}
	for want, mutate := range cases {
		files := tierFiles(t, fast, shardOutcomes())
		mutate(files)
		if _, err := MergeResults(fast, testTasksSha, "plain", files); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("want %q, got %v", want, err)
		}
	}
}

package ci

import (
	"reflect"
	"strings"
	"testing"

	"dagger/daggervals/internal/evals/engine"
)

const (
	testSetupHash = "1111111111111111111111111111111111111111111111111111111111111111"
	testRepo      = "https://github.com/Jacarte/pepi-eval-tasks.git"
)

var testFacts = EvalFacts{TasksRepo: testRepo, TasksSha: testTasksSha, Profile: "plain"}

// comparableFixture is a fast PR eval and a baseline of the same setup.
func comparableFixture(t *testing.T) (*Results, Track, map[string]string) {
	t.Helper()
	fast, _ := LookupTier("fast")
	pr, err := MergeResults(fast, testTasksSha, "plain", tierFiles(t, fast, shardOutcomes()))
	if err != nil {
		t.Fatal(err)
	}
	baselineFiles := tierFiles(t, fast, shardOutcomes())
	base, _ := MergeResults(fast, testTasksSha, "plain", baselineFiles)
	track := Track{"fast": {SetupHash: testSetupHash, TasksRepo: testRepo, TasksSha: testTasksSha, Profile: "plain", Runs: 1, Tasks: base.TaskNames(), Files: base.Files, PR: 1}}
	return pr, track, baselineFiles
}

func trackText(t *testing.T, track Track) string {
	t.Helper()
	text, err := track.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	return text
}

func TestCompareComparable(t *testing.T) {
	pr, track, baselineFiles := comparableFixture(t)
	got := Compare(pr, testFacts, trackText(t, track), baselineFiles, testSetupHash)
	if !got.Comparable || got.Reason != "" || got.Baseline == nil || len(got.Baseline.Runs) != 1 {
		t.Fatalf("%+v", got)
	}
}

// Every reason for no comparison, one change at a time.
func TestCompareReasons(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(pr *Results, facts *EvalFacts, track Track, files map[string]string, baseHash *string)
		want   string
	}{
		{"incomplete eval", func(pr *Results, _ *EvalFacts, _ Track, _ map[string]string, _ *string) {
			pr.Problems = []string{"shard go run 1: no result file"}
		}, "the eval is incomplete: shard go run 1: no result file"},
		{"no baseline", func(_ *Results, _ *EvalFacts, track Track, _ map[string]string, _ *string) {
			delete(track, "fast")
		}, "there is no fast baseline on the base branch yet"},
		{"files differ from the track", func(_ *Results, _ *EvalFacts, _ Track, files map[string]string, _ *string) {
			for name := range files {
				delete(files, name)
				break
			}
		}, "eval-baseline/fast does not hold the files .eval.track lists"},
		{"unreadable baseline", func(_ *Results, _ *EvalFacts, track Track, files map[string]string, _ *string) {
			name := track["fast"].Files[0]
			if !strings.HasSuffix(name, ".eval.md") {
				files[name] = "{"
			} else {
				files[track["fast"].Files[1]] = "{"
			}
		}, "the baseline cannot be read"},
		{"incomplete baseline", func(_ *Results, _ *EvalFacts, track Track, files map[string]string, _ *string) {
			entry := track["fast"]
			entry.Profile = "bedrock"
			track["fast"] = entry
		}, "the baseline is incomplete"},
		{"runs", func(_ *Results, _ *EvalFacts, track Track, _ map[string]string, _ *string) {
			entry := track["fast"]
			entry.Runs = 3
			track["fast"] = entry
		}, "the baseline has 3 runs, this eval 1"},
		{"tasks sha", func(_ *Results, facts *EvalFacts, _ Track, _ map[string]string, _ *string) {
			facts.TasksSha = "ffffffffffffffffffffffffffffffffffffffff"
		}, "the baseline ran tasks " + testRepo + "@0123456789ab, this eval " + testRepo + "@ffffffffffff"},
		{"tasks repo", func(_ *Results, facts *EvalFacts, _ Track, _ map[string]string, _ *string) {
			facts.TasksRepo = "https://example.com/fork.git"
		}, "this eval https://example.com/fork.git@0123456789ab"},
		{"profile", func(_ *Results, facts *EvalFacts, _ Track, _ map[string]string, _ *string) {
			facts.Profile = "bedrock"
		}, "the baseline ran profile plain, this eval bedrock"},
		{"memory arm", func(pr *Results, _ *EvalFacts, _ Track, _ map[string]string, _ *string) {
			pr.Memory = "@hippo=c8f33b2-bbbbbbbbbbbb"
		}, "the baseline ran memory @hippo=c8f33b2-aaaaaaaaaaaa, this eval @hippo=c8f33b2-bbbbbbbbbbbb"},
		{"task list", func(pr *Results, _ *EvalFacts, _ Track, _ map[string]string, _ *string) {
			pr.Runs[0] = append(pr.Runs[0], engine.TaskOutcome{Task: "tasks/new"})
		}, "the baseline ran other tasks"},
		{"setup hash", func(_ *Results, _ *EvalFacts, _ Track, _ map[string]string, baseHash *string) {
			*baseHash = "2222222222222222222222222222222222222222222222222222222222222222"
		}, "the baseline is for setup 111111111111, the base is at 222222222222"},
	}
	for _, tc := range cases {
		pr, track, files := comparableFixture(t)
		facts, baseHash := testFacts, testSetupHash
		tc.mutate(pr, &facts, track, files, &baseHash)
		got := Compare(pr, facts, trackText(t, track), files, baseHash)
		if got.Comparable || got.Baseline != nil || !strings.Contains(got.Reason, tc.want) {
			t.Errorf("%s: want %q, got %+v", tc.name, tc.want, got)
		}
	}
	pr, track, files := comparableFixture(t)
	if got := Compare(pr, testFacts, "fast: [", files, testSetupHash); got.Comparable || !strings.HasPrefix(got.Reason, ".eval.track: ") {
		t.Errorf("broken track: %+v", got)
	}
	// Several differences are all named, on one line.
	got := Compare(pr, EvalFacts{TasksRepo: testRepo, TasksSha: testTasksSha, Profile: "bedrock"}, trackText(t, track), files, strings.Repeat("3", 64))
	if !strings.Contains(got.Reason, "profile plain, this eval bedrock; the baseline is for setup") || strings.Contains(got.Reason, "\n") {
		t.Fatalf("reasons: %q", got.Reason)
	}
}

func TestTrackRoundTrip(t *testing.T) {
	track := Track{
		"full": {SetupHash: testSetupHash, TasksRepo: testRepo, TasksSha: testTasksSha, Profile: "plain", Runs: 3,
			Tasks: []string{"a", "a #2"}, Files: []string{"ci-full-rust-1@x"}, PR: 7, EvaluatedSha: testTasksSha, MergeSha: testTasksSha, Date: "2026-05-01"},
	}
	text, err := track.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(text, "# Written by") || !strings.Contains(text, "full:\n  setup_hash: \""+testSetupHash+"\"\n") && !strings.Contains(text, "full:\n  setup_hash: "+testSetupHash+"\n") {
		t.Fatalf("yaml:\n%s", text)
	}
	back, err := ParseTrack(text)
	if err != nil || !reflect.DeepEqual(back, track) {
		t.Fatalf("round trip: %+v, %v", back, err)
	}
	if empty, err := ParseTrack(""); err != nil || len(empty) != 0 {
		t.Fatalf("empty: %v, %v", empty, err)
	}
	if _, err := ParseTrack("fast:\n  setup_hsh: x\n"); err == nil {
		t.Fatal("an unknown key must be an error")
	}
}

// The task lists of every run are compared, not only run 1's.
func TestCompareTaskListOfEveryRun(t *testing.T) {
	full, _ := LookupTier("full")
	files := tierFiles(t, full, shardOutcomes())
	pr, _ := MergeResults(full, testTasksSha, "plain", files)
	base, _ := MergeResults(full, testTasksSha, "plain", files)
	track := Track{"full": {SetupHash: testSetupHash, TasksRepo: testRepo, TasksSha: testTasksSha, Profile: "plain", Runs: 3, Files: base.Files}}
	if got := Compare(pr, testFacts, trackText(t, track), files, testSetupHash); !got.Comparable {
		t.Fatalf("same tasks: %+v", got.Reason)
	}
	pr.Runs[2][0].Task = "https://github.com/tokio-rs/tokio.git"
	if got := Compare(pr, testFacts, trackText(t, track), files, testSetupHash); got.Comparable || got.Reason != "the baseline ran other tasks" {
		t.Fatalf("run 3 differs: %+v", got)
	}
}

package ci

import (
	"encoding/json"
	"fmt"
	"slices"
	"sort"

	"dagger/daggervals/internal/evals/engine"
	judge "dagger/daggervals/internal/evals/llm-judge"
)

// Results are the files of one tier's eval, all shards, merged per run index.
type Results struct {
	Tier Tier
	// Runs[i] are the outcomes of run i+1 of every shard, in shard order: compare-runs
	// reads one file per run, and the order fixes the " #n" names of tasks of the same repo.
	Runs [][]engine.TaskOutcome
	// Judges are the .eval.md reports, by run then shard.
	Judges []JudgeReport
	// Memory is the memory arm of the files, @hippo=<tag>-<digest12>.
	Memory string
	// Problems are the infrastructure failures: expected files that are missing (a shard
	// that failed or was aborted), runs with another number of tasks than the shard has,
	// runs whose agents all failed, outcomes of another profile.
	Problems []string
	// Files are every file name, sorted.
	Files []string
}

// JudgeReport is the .eval.md of one shard's run.
type JudgeReport struct {
	Run   int
	Shard string
	Text  string
}

// MergeResults reads the result files of tier t (name to content). tasksSha and profile
// are what the eval was run with. A file that is not one of the tier's is an error; as the
// memory arm must be the same, a name maps to one shard and run.
func MergeResults(t Tier, tasksSha, profile string, files map[string]string) (*Results, error) {
	type key struct {
		shard string
		run   int
		judge bool
	}
	parsed := map[key]string{}
	r := &Results{Tier: t}
	for name := range files {
		f, err := t.ParseResultName(name, tasksSha)
		if err != nil {
			return nil, err
		}
		if r.Memory != "" && r.Memory != f.Memory {
			return nil, fmt.Errorf("files of two memory arms: %s and %s", r.Memory, f.Memory)
		}
		r.Memory = f.Memory
		parsed[key{f.Shard.Name, f.Run, f.Judge}] = name
		r.Files = append(r.Files, name)
	}
	sort.Strings(r.Files)
	for run := 1; run <= t.Runs; run++ {
		var outcomes []engine.TaskOutcome
		for _, s := range t.Shards {
			name, ok := parsed[key{s.Name, run, false}]
			if !ok {
				r.Problems = append(r.Problems, fmt.Sprintf("shard %s run %d: no result file", s.Name, run))
				continue
			}
			var shardOutcomes []engine.TaskOutcome
			if err := json.Unmarshal([]byte(files[name]), &shardOutcomes); err != nil {
				return nil, fmt.Errorf("file %s: not an eval cache file: %w", name, err)
			}
			if len(shardOutcomes) != s.TaskCount() {
				r.Problems = append(r.Problems, fmt.Sprintf("shard %s run %d: %d task outcomes, want %d", s.Name, run, len(shardOutcomes), s.TaskCount()))
			}
			agentsFailed := 0
			for _, o := range shardOutcomes {
				if o.Profile != profile {
					r.Problems = append(r.Problems, fmt.Sprintf("shard %s run %d: task %s ran with profile %q, not %q", s.Name, run, o.Task, o.Profile, profile))
				}
				if o.AgentExitCode != 0 {
					agentsFailed++
				}
			}
			// Every agent failing points at the harness (auth, quota), not at the agent.
			if len(shardOutcomes) > 0 && agentsFailed == len(shardOutcomes) {
				r.Problems = append(r.Problems, fmt.Sprintf("shard %s run %d: every agent exited with an error", s.Name, run))
			}
			outcomes = append(outcomes, shardOutcomes...)
			if judgeName, ok := parsed[key{s.Name, run, true}]; ok {
				r.Judges = append(r.Judges, JudgeReport{Run: run, Shard: s.Name, Text: files[judgeName]})
			} else {
				r.Problems = append(r.Problems, fmt.Sprintf("shard %s run %d: no judge report", s.Name, run))
			}
		}
		r.Runs = append(r.Runs, outcomes)
	}
	return r, nil
}

// sameTasks says whether every run of r has the tasks of the same run of o.
func (r *Results) sameTasks(o *Results) bool {
	if len(r.Runs) != len(o.Runs) {
		return false
	}
	for i := range r.Runs {
		if !slices.Equal(taskNames(r.Runs[i]), taskNames(o.Runs[i])) {
			return false
		}
	}
	return true
}

// TaskNames are the tasks of run 1 as compare-runs names them.
func (r *Results) TaskNames() []string {
	if len(r.Runs) == 0 {
		return nil
	}
	return taskNames(r.Runs[0])
}

func taskNames(outcomes []engine.TaskOutcome) []string {
	names := make([]string, len(outcomes))
	for i, o := range outcomes {
		names[i] = o.Task
	}
	return judge.UniqueTaskNames(names)
}

// RunInputs are the runs as compare-runs arms under label.
func (r *Results) RunInputs(label string) []judge.RunInput {
	runs := make([]judge.RunInput, len(r.Runs))
	for i, outcomes := range r.Runs {
		runs[i] = judge.RunInput{Label: label, Outcomes: outcomes}
	}
	return runs
}

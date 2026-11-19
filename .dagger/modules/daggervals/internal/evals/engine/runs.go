package engine

import "fmt"

// MaxRuns bounds the runs argument of Evals.
const MaxRuns = 20

// ValidateRuns accepts 1..MaxRuns.
func ValidateRuns(runs int) error {
	if runs < 1 || runs > MaxRuns {
		return fmt.Errorf("runs must be between 1 and %d, got %d", MaxRuns, runs)
	}
	return nil
}

// RunID is the cache id of run i (1-based) out of runs. One run keeps the base id; several
// runs insert -<i> right after the base, before the suffix (@suite, @tasks@sha, ...).
func RunID(base string, i, runs int, suffix string) string {
	if runs == 1 {
		return base + suffix
	}
	return fmt.Sprintf("%s-%d%s", base, i, suffix)
}

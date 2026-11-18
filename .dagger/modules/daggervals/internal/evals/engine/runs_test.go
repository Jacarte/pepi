package engine

import (
	"strings"
	"testing"
)

func TestValidateRuns(t *testing.T) {
	for _, runs := range []int{1, 3, MaxRuns} {
		if err := ValidateRuns(runs); err != nil {
			t.Errorf("runs=%d: %v", runs, err)
		}
	}
	for _, runs := range []int{-1, 0, MaxRuns + 1} {
		if err := ValidateRuns(runs); err == nil || !strings.Contains(err.Error(), "between 1 and 20") {
			t.Errorf("runs=%d: want a range error, got %v", runs, err)
		}
	}
}

func TestRunID(t *testing.T) {
	const tasksSuffix = "@tasks@cfcae7eba7c5@only=twelve-modules"
	tests := []struct {
		name         string
		base         string
		i, runs      int
		suffix, want string
	}{
		{"one run keeps the id", "tm-plain", 1, 1, "", "tm-plain"},
		{"one run keeps the suite suffix", "abc123", 1, 1, "@go", "abc123@go"},
		{"one run keeps the tasks suffix", "tm-plain", 1, 1, tasksSuffix, "tm-plain" + tasksSuffix},
		{"several runs, fixed id, tasks suffix", "tm-plain", 2, 3, tasksSuffix, "tm-plain-2" + tasksSuffix},
		{"several runs, hash id, suite suffix", "abc123", 3, 3, "@all", "abc123-3@all"},
		{"several runs, no suffix", "tm-plain", 1, 2, "", "tm-plain-1"},
	}
	for _, tt := range tests {
		if got := RunID(tt.base, tt.i, tt.runs, tt.suffix); got != tt.want {
			t.Errorf("%s: got %q, want %q", tt.name, got, tt.want)
		}
	}
}

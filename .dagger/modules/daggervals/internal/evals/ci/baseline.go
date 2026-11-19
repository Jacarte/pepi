package ci

import (
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"
	"time"
)

// Size caps of a baseline: the files are committed to a public repo on every merge.
const (
	MaxBaselineFileBytes = 2 << 20
	MaxBaselineBytes     = 5 << 20
)

// Entry is a directory entry of the results, as listed before reading them.
type Entry struct {
	Name string
	// Regular is false for a directory, a symlink or anything else.
	Regular bool
	Size    int
}

// baselineNamePattern allows the characters of run ids, with ',' for @only=a,b.
var baselineNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9@._=,-]*$`)

// CheckBaselineEntries accepts only regular files with safe names, within the size caps.
func CheckBaselineEntries(entries []Entry) error {
	total := 0
	for _, e := range entries {
		switch {
		case !baselineNamePattern.MatchString(e.Name) || strings.Contains(e.Name, ".."):
			return fmt.Errorf("result file name %q is not allowed", e.Name)
		case !e.Regular:
			return fmt.Errorf("result %q is not a regular file (directory or symlink)", e.Name)
		case e.Size > MaxBaselineFileBytes:
			return fmt.Errorf("result file %q has %d bytes, more than %d", e.Name, e.Size, MaxBaselineFileBytes)
		}
		total += e.Size
	}
	if total > MaxBaselineBytes {
		return fmt.Errorf("the results have %d bytes, more than %d", total, MaxBaselineBytes)
	}
	return nil
}

var setupHashPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// ParseStatus reads the status.json of ci-eval-report.
func ParseStatus(text string) (Status, error) {
	var status Status
	decoder := json.NewDecoder(strings.NewReader(text))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&status); err != nil {
		return Status{}, fmt.Errorf("status.json: %w", err)
	}
	return status, nil
}

// Baseline is what ci-baseline-prepare checks and writes.
type Baseline struct {
	Tier Tier
	// Status is the status.json of the PR's eval report.
	Status    Status
	PR        int
	PRHeadSha string
	MergeSha  string
	Date      string
	// Files are the eval's result files, as the eval report saw them.
	Files map[string]string
	// Track is the current .eval.track, empty when there is none.
	Track string
}

// Prepare checks the baseline and returns the files to write, by path from the repo root:
// eval-baseline/<tier>/* and .eval.track with this tier's entry replaced.
func (b Baseline) Prepare() (map[string]string, error) {
	s := b.Status
	if err := ValidatePR(b.PR); err != nil {
		return nil, err
	}
	if err := ValidateSha("prHeadSha", b.PRHeadSha); err != nil {
		return nil, err
	}
	if err := ValidateSha("mergeSha", b.MergeSha); err != nil {
		return nil, err
	}
	if _, err := time.Parse(time.DateOnly, b.Date); err != nil {
		// RFC 3339, as `date -u -Iseconds` prints it.
		if _, err := time.Parse(time.RFC3339, b.Date); err != nil {
			return nil, fmt.Errorf("date %q: want YYYY-MM-DD or RFC 3339", b.Date)
		}
	}
	switch {
	case s.Tier != b.Tier.Name:
		return nil, fmt.Errorf("status.json is for tier %q, not %s", s.Tier, b.Tier.Name)
	case s.PR != b.PR:
		return nil, fmt.Errorf("status.json is for PR %d, not %d", s.PR, b.PR)
	case s.EvaluatedSha != b.PRHeadSha:
		return nil, fmt.Errorf("evaluated head != merged head: baseline not committed (evaluated %s, merged head %s)", shortSha(s.EvaluatedSha), shortSha(b.PRHeadSha))
	case !s.InfraOK:
		return nil, fmt.Errorf("the eval was incomplete: baseline not committed")
	case s.Runs != b.Tier.Runs:
		return nil, fmt.Errorf("status.json has %d runs, tier %s %d", s.Runs, b.Tier.Name, b.Tier.Runs)
	}
	if !setupHashPattern.MatchString(s.SetupHash) {
		return nil, fmt.Errorf("status.json has no setup hash")
	}
	if err := ValidateSha("status.json tasks_sha", s.TasksSha); err != nil {
		return nil, err
	}
	names := make([]string, 0, len(b.Files))
	for name := range b.Files {
		names = append(names, name)
	}
	sort.Strings(names)
	if !slices.Equal(names, s.Files) {
		return nil, fmt.Errorf("the result files are not the ones the eval report read")
	}
	results, err := MergeResults(b.Tier, s.TasksSha, s.Profile, b.Files)
	if err != nil {
		return nil, err
	}
	if len(results.Problems) > 0 {
		return nil, fmt.Errorf("the results are incomplete: %s", strings.Join(results.Problems, "; "))
	}
	track, err := ParseTrack(b.Track)
	if err != nil {
		return nil, err
	}
	track[b.Tier.Name] = TrackEntry{
		SetupHash: s.SetupHash, TasksRepo: s.TasksRepo, TasksSha: s.TasksSha, Profile: s.Profile,
		Runs: s.Runs, Tasks: results.TaskNames(), Files: names,
		PR: b.PR, EvaluatedSha: s.EvaluatedSha, MergeSha: b.MergeSha, Date: b.Date,
	}
	trackText, err := track.Marshal()
	if err != nil {
		return nil, err
	}
	out := map[string]string{TrackFile: trackText}
	for name, content := range b.Files {
		out[BaselineDir(b.Tier.Name)+"/"+name] = content
	}
	if err := ScanSecrets(out, nil); err != nil {
		return nil, fmt.Errorf("%w: baseline not committed", err)
	}
	return out, nil
}

package ci

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"

	"gopkg.in/yaml.v3"
)

// TrackFile is the record of the committed baselines, at the repo root.
const TrackFile = ".eval.track"

// BaselineDir is the folder of a tier's baseline files, relative to the repo root.
func BaselineDir(tier string) string { return "eval-baseline/" + tier }

// TrackEntry is the baseline of one tier. SetupHash is the eval id: the setup id of the
// evaluated head (setup.PiSetup.GetSetupId), which the CI files never change. It is a public
// sha256 over the agent env file content too, so rotating a key invalidates the baselines.
type TrackEntry struct {
	SetupHash    string   `yaml:"setup_hash"`
	TasksRepo    string   `yaml:"tasks_repo"`
	TasksSha     string   `yaml:"tasks_sha"`
	Profile      string   `yaml:"profile"`
	Runs         int      `yaml:"runs"`
	Tasks        []string `yaml:"tasks"`
	Files        []string `yaml:"files"`
	PR           int      `yaml:"pr"`
	EvaluatedSha string   `yaml:"evaluated_sha"`
	MergeSha     string   `yaml:"merge_sha"`
	Date         string   `yaml:"date"`
}

// Track is .eval.track: the baseline of each tier, by tier name.
type Track map[string]TrackEntry

// ParseTrack reads .eval.track; empty text is an empty track. Unknown keys are errors so a
// typo does not silently drop a field.
func ParseTrack(text string) (Track, error) {
	track := Track{}
	decoder := yaml.NewDecoder(strings.NewReader(text))
	decoder.KnownFields(true)
	if err := decoder.Decode(&track); err != nil && !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("%s: %w", TrackFile, err)
	}
	return track, nil
}

const trackHeader = "# Written by `dagger call daggervals ci-baseline-prepare` after a merge: the eval baseline of each tier.\n" +
	"# setup_hash is a published sha256 of the setup files, the agent env file content and the profile:\n" +
	"# rotating a key of the agent env changes it and makes every baseline incomparable.\n"

// Marshal is the YAML of .eval.track, with sorted keys.
func (t Track) Marshal() (string, error) {
	var out bytes.Buffer
	out.WriteString(trackHeader)
	encoder := yaml.NewEncoder(&out)
	encoder.SetIndent(2)
	if err := encoder.Encode(map[string]TrackEntry(t)); err != nil {
		return "", err
	}
	return out.String(), encoder.Close()
}

// EvalFacts are what a PR eval ran with.
type EvalFacts struct {
	TasksRepo string
	TasksSha  string
	Profile   string
}

// Comparison says whether the PR eval and its tier's baseline can be compared.
type Comparison struct {
	Comparable bool
	// Reason says why not, one line; empty when comparable.
	Reason string
	// Baseline is the merged baseline when comparable.
	Baseline *Results
}

// Compare decides whether pr can be compared with the tier's baseline at the PR's base:
// trackText (.eval.track) and baselineFiles (eval-baseline/<tier>) come from the base checkout and
// baseSetupHash is the setup id of the base. They are comparable only when the eval is
// complete and the baseline ran the same runs, tasks commit, profile, memory arm and task
// list on the setup the base has.
func Compare(pr *Results, facts EvalFacts, trackText string, baselineFiles map[string]string, baseSetupHash string) Comparison {
	no := func(reason string) Comparison { return Comparison{Reason: reason} }
	tier := pr.Tier
	if len(pr.Problems) > 0 {
		return no("the eval is incomplete: " + strings.Join(pr.Problems, "; "))
	}
	track, err := ParseTrack(trackText)
	if err != nil {
		return no(err.Error())
	}
	entry, ok := track[tier.Name]
	if !ok {
		return no(fmt.Sprintf("there is no %s baseline on the base branch yet", tier.Name))
	}
	names := make([]string, 0, len(baselineFiles))
	for name := range baselineFiles {
		names = append(names, name)
	}
	slices.Sort(names)
	if !slices.Equal(names, entry.Files) {
		return no(fmt.Sprintf("%s does not hold the files %s lists", BaselineDir(tier.Name), TrackFile))
	}
	baseline, err := MergeResults(tier, entry.TasksSha, entry.Profile, baselineFiles)
	if err != nil {
		return no("the baseline cannot be read: " + err.Error())
	}
	if len(baseline.Problems) > 0 {
		return no("the baseline is incomplete: " + strings.Join(baseline.Problems, "; "))
	}
	var reasons []string
	if entry.Runs != tier.Runs {
		reasons = append(reasons, fmt.Sprintf("the baseline has %d runs, this eval %d", entry.Runs, tier.Runs))
	}
	if entry.TasksRepo != facts.TasksRepo || entry.TasksSha != facts.TasksSha {
		reasons = append(reasons, fmt.Sprintf("the baseline ran tasks %s@%s, this eval %s@%s", entry.TasksRepo, shortSha(entry.TasksSha), facts.TasksRepo, shortSha(facts.TasksSha)))
	}
	if entry.Profile != facts.Profile {
		reasons = append(reasons, fmt.Sprintf("the baseline ran profile %s, this eval %s", entry.Profile, facts.Profile))
	}
	if baseline.Memory != pr.Memory {
		reasons = append(reasons, fmt.Sprintf("the baseline ran memory %s, this eval %s", baseline.Memory, pr.Memory))
	}
	if !baseline.sameTasks(pr) {
		reasons = append(reasons, "the baseline ran other tasks")
	}
	if entry.SetupHash != baseSetupHash {
		reasons = append(reasons, fmt.Sprintf("the baseline is for setup %s, the base is at %s", shortSha(entry.SetupHash), shortSha(baseSetupHash)))
	}
	if len(reasons) > 0 {
		return no(strings.Join(reasons, "; "))
	}
	return Comparison{Comparable: true, Baseline: baseline}
}

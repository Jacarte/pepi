package ci

import (
	"encoding/json"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	"dagger/daggervals/internal/evals/engine"
	judge "dagger/daggervals/internal/evals/llm-judge"
)

// The sticky PR comments are found by these markers.
const (
	JudgeMarker   = "<!-- pepi-eval:judge -->"
	CompareMarker = "<!-- pepi-eval:compare -->"
)

// CommentLimit bounds a comment body in bytes, so in characters too, below GitHub's 65536.
const CommentLimit = 60000

// SummaryLimit bounds the job summary in bytes, below GitHub's 1 MiB.
const SummaryLimit = 900000

const zeroWidthSpace = "\u200b"

var (
	// @user, @org/team and #123, owner/repo#123, GH-123 would notify or cross-reference.
	mentionPattern   = regexp.MustCompile(`([@#])([A-Za-z0-9])`)
	ghRefPattern     = regexp.MustCompile(`(?i)\b(gh)-([0-9])`)
	issueLinkPattern = regexp.MustCompile(`(/(?:issues|pull|discussions)/)([0-9])`)
)

// Neutralize makes untrusted text (judge reports, agent output, task names) safe to render
// as Markdown in a comment: '<' and '&' are escaped, so no HTML, entity, comment marker or
// </details> gets through, and '[' becomes &#91;, so no link or image either (code blocks then
// show them escaped); a zero-width space breaks mentions and issue references; a code fence
// left open is closed, so it cannot swallow the rest of the body; invalid UTF-8 is dropped.
func Neutralize(s string) string {
	s = strings.ToValidUTF8(s, "")
	s = strings.NewReplacer("&", "&amp;", "<", "&lt;").Replace(s)
	s = mentionPattern.ReplaceAllString(s, "${1}"+zeroWidthSpace+"${2}")
	s = ghRefPattern.ReplaceAllString(s, "${1}"+zeroWidthSpace+"-${2}")
	s = issueLinkPattern.ReplaceAllString(s, "${1}"+zeroWidthSpace+"${2}")
	// After the mentions, whose '#' rule would break the entity.
	s = strings.ReplaceAll(s, "[", "&#91;")
	return closeOpenFence(s)
}

var fencePattern = regexp.MustCompile("^ {0,3}(`{3,}|~{3,})")

// closeOpenFence appends the closing line of a fenced code block that s leaves open.
func closeOpenFence(s string) string {
	open := ""
	for _, line := range strings.Split(s, "\n") {
		m := fencePattern.FindStringSubmatch(line)
		switch {
		case m == nil:
		// A backtick in the info string makes it an inline code span, not a fence.
		case open == "" && m[1][0] == '`' && strings.Contains(line[len(m[0]):], "`"):
		case open == "":
			open = m[1]
		case m[1][0] == open[0] && len(m[1]) >= len(open) && strings.TrimSpace(strings.TrimLeft(line, " "+open[:1])) == "":
			open = ""
		}
	}
	if open == "" {
		return s
	}
	return s + "\n" + open
}

// part is a piece of a body: open and close are our own markup, text is neutralized and may
// be cut to fit.
type part struct {
	open, text, close string
}

func (p part) String() string { return p.open + p.text + p.close }

const cutNotice = "\n\n*[cut]*"

// fit joins parts within limit bytes. When they do not fit, the first part that does not is
// cut at a line, the rest are left out and note ends the body.
func fit(parts []part, limit int, note string) string {
	var out strings.Builder
	for _, p := range parts {
		out.WriteString(p.String())
	}
	if out.Len() <= limit {
		return out.String()
	}
	out.Reset()
	for _, p := range parts {
		if out.Len()+len(p.String())+len(note) <= limit {
			out.WriteString(p.String())
			continue
		}
		room := limit - out.Len() - len(note) - len(p.open) - len(p.close) - len(cutNotice)
		// Closing a fence the cut left open adds a line: cut again by that much.
		for try := 0; try < 2 && room > 0; try++ {
			cut := closeOpenFence(cutAtLine(p.text, room)) + cutNotice
			over := out.Len() + len(p.open) + len(cut) + len(p.close) + len(note) - limit
			if over <= 0 {
				out.WriteString(p.open + cut + p.close)
				break
			}
			room -= over
		}
		break
	}
	out.WriteString(note)
	return out.String()
}

// cutAtLine keeps at most max bytes of s: up to the last line end when there is one, else
// up to a rune boundary.
func cutAtLine(s string, max int) string {
	if len(s) <= max {
		return s
	}
	s = s[:max]
	if i := strings.LastIndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	last := len(s) - 1
	for last > 0 && !utf8.RuneStart(s[last]) {
		last--
	}
	if last >= 0 && !utf8.FullRuneInString(s[last:]) {
		s = s[:last]
	}
	return s
}

// Report is everything ci-eval-report renders.
type Report struct {
	PR           int
	EvaluatedSha string
	Facts        EvalFacts
	Results      *Results
	Comparison   Comparison
	// CompareLines is the compare-runs answer; nil without a comparison.
	CompareLines []string
	// SetupHash is the setup id of the evaluated head, BaseSetupHash the base's.
	SetupHash     string
	BaseSetupHash string
	// RunURL is the workflow run, linked from the truncation note; may be empty.
	RunURL string
	// Inputs are the result files by name and SecretValues the values of the eval secrets,
	// for the secret scan.
	Inputs       map[string]string
	SecretValues []string
}

// Status is status.json: what the workflow and ci-baseline-prepare read.
type Status struct {
	Tier          string `json:"tier"`
	PR            int    `json:"pr"`
	EvaluatedSha  string `json:"evaluated_sha"`
	TasksRepo     string `json:"tasks_repo"`
	TasksSha      string `json:"tasks_sha"`
	Profile       string `json:"profile"`
	Runs          int    `json:"runs"`
	SetupHash     string `json:"setup_hash"`
	BaseSetupHash string `json:"base_setup_hash"`
	Comparable    bool   `json:"comparable"`
	Reason        string `json:"reason"`
	// Verdict is WIN, REGRESSION, NO CHANGE or INCONCLUSIVE from compare-runs; empty without
	// a comparison or when the answer has no verdict line.
	Verdict    string `json:"verdict"`
	Confidence string `json:"confidence"`
	// InfraOK is false when a shard's files are missing or wrong: the only failure of the check.
	InfraOK  bool     `json:"infra_ok"`
	Problems []string `json:"problems"`
	Tasks    []string `json:"tasks"`
	Files    []string `json:"files"`
}

var (
	verdictPattern    = regexp.MustCompile(`(?m)^[\s*_>]*VERDICT pr vs base:\s*(WIN|REGRESSION|NO CHANGE|INCONCLUSIVE)\b`)
	confidencePattern = regexp.MustCompile(`(?m)^[\s*_>]*CONFIDENCE pr:\s*(high|medium|low)\b`)
)

// Render returns the files of ci-eval-report: judge.md and compare.md (comment bodies),
// summary.md (the job summary) and status.json. When the inputs, the compare-runs answer or
// the bodies hold a possible secret, the bodies are withheld and infra_ok is false.
func (r Report) Render() (map[string]string, error) {
	tier := r.Results.Tier
	note := "\n\n---\n**Truncated** to fit a GitHub comment: the full text is in the job summary and the run artifacts" + r.runLink() + ".\n"
	judgeParts := r.judgeParts()
	compareParts := r.compareParts()
	compareText := strings.Join(r.CompareLines, "\n")
	summaryNote := "\n\n---\n**Truncated** to fit the job summary: the full files are in the run artifacts" + r.runLink() + ".\n"
	bodies := map[string]string{
		"judge.md":   fit(judgeParts, CommentLimit, note),
		"compare.md": fit(compareParts, CommentLimit, note),
		"summary.md": fit(slices.Concat(judgeParts, []part{{text: "\n\n---\n\n"}}, compareParts), SummaryLimit, summaryNote),
	}
	scanned := map[string]string{"compare-runs answer": compareText}
	maps.Copy(scanned, r.Inputs)
	maps.Copy(scanned, bodies)
	problems := slices.Clone(r.Results.Problems)
	if hits := SecretHits(scanned, r.SecretValues); len(hits) > 0 {
		for _, hit := range hits {
			problems = append(problems, "secret scan: "+hit)
		}
		withheld := r.heading("withheld") + "\nWithheld: the secret scan found possible secrets; see the problems in status.json.\n"
		bodies = map[string]string{
			"judge.md":   JudgeMarker + "\n" + withheld,
			"compare.md": CompareMarker + "\n" + withheld,
			"summary.md": withheld,
		}
	}
	status := Status{
		Tier: tier.Name, PR: r.PR, EvaluatedSha: r.EvaluatedSha,
		TasksRepo: r.Facts.TasksRepo, TasksSha: r.Facts.TasksSha, Profile: r.Facts.Profile, Runs: tier.Runs,
		SetupHash: r.SetupHash, BaseSetupHash: r.BaseSetupHash,
		Comparable: r.Comparison.Comparable, Reason: r.Comparison.Reason,
		InfraOK: len(problems) == 0, Problems: nonNil(problems),
		Tasks: nonNil(r.Results.TaskNames()), Files: nonNil(r.Results.Files),
	}
	if m := verdictPattern.FindStringSubmatch(compareText); m != nil {
		status.Verdict = m[1]
	}
	if m := confidencePattern.FindStringSubmatch(compareText); m != nil {
		status.Confidence = m[1]
	}
	statusJSON, err := json.MarshalIndent(status, "", "  ")
	if err != nil {
		return nil, err
	}
	bodies["status.json"] = string(statusJSON) + "\n"
	return bodies, nil
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func (r Report) runLink() string {
	if r.RunURL == "" {
		return ""
	}
	return " ([workflow run](" + r.RunURL + "))"
}

func (r Report) heading(what string) string {
	tier := r.Results.Tier
	return fmt.Sprintf("### pepi eval `%s`: %s\nHead `%s` · runs %d · profile `%s` · tasks `%s`\n",
		tier.Name, what, shortSha(r.EvaluatedSha), tier.Runs, codeSpan(r.Facts.Profile), shortSha(r.Facts.TasksSha))
}

func (r Report) judgeParts() []part {
	head := JudgeMarker + "\n" + r.heading("judge")
	if len(r.Results.Problems) > 0 {
		head += "\n**Infrastructure problem**, the eval is incomplete:\n"
		for _, p := range r.Results.Problems {
			head += "- " + Neutralize(p) + "\n"
		}
	}
	parts := []part{{text: head + "\n" + r.table()}}
	if len(r.Results.Judges) > 0 {
		parts = append(parts, part{text: "\n#### Judge reports\n"})
	}
	for _, j := range r.Results.Judges {
		parts = append(parts, part{
			open:  fmt.Sprintf("\n<details><summary>run %d · %s</summary>\n\n", j.Run, j.Shard),
			text:  Neutralize(j.Text),
			close: "\n\n</details>\n",
		})
	}
	return parts
}

func (r Report) compareParts() []part {
	head := CompareMarker + "\n" + r.heading("comparison with the baseline") + "\n"
	if !r.Comparison.Comparable {
		return []part{{text: head + "No comparison: " + Neutralize(r.Comparison.Reason) + "\n"}}
	}
	return []part{{text: head}, {text: Neutralize(strings.Join(r.CompareLines, "\n")) + "\n"}}
}

// table has one row per task (in run order) and one value per run in each cell.
func (r Report) table() string {
	type row struct {
		name     string
		outcomes []*engine.TaskOutcome
	}
	var rows []*row
	byName := map[string]*row{}
	for run, outcomes := range r.Results.Runs {
		for i, name := range taskNames(outcomes) {
			if byName[name] == nil {
				byName[name] = &row{name: name, outcomes: make([]*engine.TaskOutcome, len(r.Results.Runs))}
				rows = append(rows, byName[name])
			}
			byName[name].outcomes[run] = &outcomes[i]
		}
	}
	var b strings.Builder
	b.WriteString("| Task | Score | Cost USD | Wall clock s | Valid | Timed out | Agent failed |\n|---|---|---|---|---|---|---|\n")
	for _, row := range rows {
		var score, cost, wall []string
		var valid, timedOut, failed, ran int
		for _, o := range row.outcomes {
			if o == nil {
				score, cost, wall = append(score, "–"), append(cost, "–"), append(wall, "–")
				continue
			}
			ran++
			switch {
			case o.MaxScore > 0:
				score = append(score, fmt.Sprintf("%d/%d", o.Score, o.MaxScore))
			case o.Valid:
				score = append(score, "pass")
			default:
				score = append(score, "fail")
			}
			cost = append(cost, fmt.Sprintf("%.2f", o.AgentStats.CostUSD))
			if sec := judge.WallClockSec(o.ProcessStats); sec != nil {
				wall = append(wall, fmt.Sprintf("%.0f", *sec))
			} else {
				wall = append(wall, "?")
			}
			if o.Valid {
				valid++
			}
			if o.TimedOut {
				timedOut++
			}
			if o.AgentExitCode != 0 {
				failed++
			}
		}
		fmt.Fprintf(&b, "| `%s` | %s | %s | %s | %d/%d | %d/%d | %d/%d |\n", codeSpan(row.name),
			strings.Join(score, ", "), strings.Join(cost, ", "), strings.Join(wall, ", "), valid, ran, timedOut, ran, failed, ran)
	}
	return b.String()
}

// codeSpan makes untrusted text a safe code span body in a table cell: no backtick, pipe or
// line break. Mentions and HTML are inert in a code span.
func codeSpan(s string) string {
	return strings.NewReplacer("`", "'", "|", "/", "\n", " ", "\r", " ").Replace(s)
}

package ci

import (
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"
)

var update = flag.Bool("update", false, "rewrite the golden files in testdata")

func TestNeutralize(t *testing.T) {
	cases := map[string]string{
		"<!-- pepi-eval:compare -->":        "&lt;!-- pepi-eval:compare -->",
		"</details><img src=x onerror=a()>": "&lt;/details>&lt;img src=x onerror=a()>",
		"ping @octocat and @org/team":       "ping @\u200boctocat and @\u200borg/team",
		"fixes #12, closes owner/repo#3":    "fixes #\u200b12, closes owner/repo#\u200b3",
		"see GH-42 and /pull/7":             "see GH\u200b-42 and /pull/\u200b7",
		"&#64;octocat &commat;x":            "&amp;#\u200b64;octocat &amp;commat;x",
		"## heading\n- item":                "## heading\n- item",
		"```go\nunclosed":                   "```go\nunclosed\n```",
		"````\n```\nstill open":             "````\n```\nstill open\n````",
		"~~~\nclosed\n~~~\nafter":           "~~~\nclosed\n~~~\nafter",
		"```a`b not a fence\nafter":         "```a`b not a fence\nafter",
		"bad \xff byte":                     "bad  byte",
	}
	for in, want := range cases {
		if got := Neutralize(in); got != want {
			t.Errorf("Neutralize(%q) = %q, want %q", in, got, want)
		}
	}
}

// No link, image or reference link survives, even after a backslash.
func TestNeutralizeLinks(t *testing.T) {
	got := Neutralize("![b](https://x/beacon) [ok](https://phish) \\[x](y) [r][1]\n[1]: https://phish")
	if strings.Contains(got, "[") || strings.Count(got, "&#91;") != 6 {
		t.Fatalf("%q", got)
	}
}

func TestFitTruncates(t *testing.T) {
	// Multi-byte text and an open fence: the result fits in bytes, is valid UTF-8, closes
	// the fence and the <details>, and keeps the note.
	long := "```\n" + strings.Repeat("héllo wörld ✓ 🙂\n", 10000)
	parts := []part{
		{text: JudgeMarker + "\nhead\n"},
		{open: "\n<details><summary>run 1 · rust</summary>\n\n", text: Neutralize("small"), close: "\n\n</details>\n"},
		{open: "\n<details><summary>run 1 · go</summary>\n\n", text: Neutralize(long), close: "\n\n</details>\n"},
		{open: "\n<details><summary>run 1 · tasks-memory</summary>\n\n", text: "left out", close: "\n\n</details>\n"},
	}
	note := "\n---\n**Truncated** note\n"
	got := fit(parts, CommentLimit, note)
	if len(got) > CommentLimit || !utf8.ValidString(got) || !strings.HasPrefix(got, JudgeMarker) || !strings.HasSuffix(got, note) {
		t.Fatalf("len %d, valid %v", len(got), utf8.ValidString(got))
	}
	if strings.Count(got, "<details>") != 2 || strings.Count(got, "</details>") != 2 || strings.Contains(got, "left out") || !strings.Contains(got, "*[cut]*") {
		t.Fatalf("structure: %q", got[len(got)-300:])
	}
	if !strings.Contains(got, "\n```\n\n*[cut]*") {
		t.Fatalf("the fence is not closed before the cut notice")
	}
	if short := fit(parts[:2], CommentLimit, note); strings.Contains(short, "Truncated") {
		t.Fatal("a body that fits has no note")
	}
	// A cut without line ends stays on a rune boundary.
	if got := cutAtLine("ééé", 3); got != "é" {
		t.Fatalf("cutAtLine: %q", got)
	}
}

// reportFixture is a fast eval of synthetic files compared with a baseline, with an agent
// trying to break the comment in its judge report.
func reportFixture(t *testing.T, comparable bool) Report {
	pr, track, baselineFiles := comparableFixture(t)
	pr.Judges[0].Text = "### tokio\n@octocat fixed #1 <!-- pepi-eval:compare --> </details>\n```\nunclosed"
	comparison := Compare(pr, testFacts, trackText(t, track), baselineFiles, testSetupHash)
	report := Report{PR: 5, EvaluatedSha: "abcdefabcdefabcdefabcdefabcdefabcdefabcd", Facts: testFacts, Results: pr, Comparison: comparison,
		SetupHash: testSetupHash, BaseSetupHash: testSetupHash, RunURL: "https://github.com/Jacarte/pepi/actions/runs/1"}
	if comparable {
		report.CompareLines = []string{"## Overall", "| x | y |", "VERDICT pr vs base: NO CHANGE", "CONFIDENCE pr: low"}
	} else {
		report.Comparison = Compare(pr, testFacts, "", nil, testSetupHash)
	}
	return report
}

func TestRenderGolden(t *testing.T) {
	for _, comparable := range []bool{true, false} {
		files, err := reportFixture(t, comparable).Render()
		if err != nil {
			t.Fatal(err)
		}
		dir := filepath.Join("testdata", map[bool]string{true: "comparable", false: "no-baseline"}[comparable])
		for name, content := range files {
			path := filepath.Join(dir, name)
			if *update {
				if err := os.MkdirAll(dir, 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
					t.Fatal(err)
				}
				continue
			}
			want, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("%v (run go test -update)", err)
			}
			if content != string(want) {
				t.Errorf("%s differs from the golden file; got:\n%s", path, content)
			}
		}
		// The markers appear once, where they belong, whatever the agent wrote.
		if strings.Count(files["judge.md"], "<!--") != 1 || !strings.HasPrefix(files["judge.md"], JudgeMarker) || strings.Contains(files["judge.md"], CompareMarker) {
			t.Errorf("judge.md markers")
		}
		if strings.Count(files["compare.md"], "<!--") != 1 || !strings.HasPrefix(files["compare.md"], CompareMarker) {
			t.Errorf("compare.md markers")
		}
		if strings.Contains(files["judge.md"], "@octocat") || strings.Contains(files["judge.md"], "#1 ") {
			t.Errorf("a mention or reference got through")
		}
	}
}

func TestRenderStatus(t *testing.T) {
	files, err := reportFixture(t, true).Render()
	if err != nil {
		t.Fatal(err)
	}
	status, err := ParseStatus(files["status.json"])
	if err != nil {
		t.Fatal(err)
	}
	if !status.Comparable || status.Verdict != "NO CHANGE" || status.Confidence != "low" || !status.InfraOK || status.SetupHash != testSetupHash || len(status.Files) != 6 || len(status.Tasks) != 19 {
		t.Fatalf("%+v", status)
	}
	report := reportFixture(t, false)
	report.Results.Problems = []string{"shard go run 1: no result file"}
	files, _ = report.Render()
	status, _ = ParseStatus(files["status.json"])
	if status.Comparable || status.Verdict != "" || status.InfraOK || status.Reason == "" || len(status.Problems) != 1 {
		t.Fatalf("%+v", status)
	}
	if !strings.Contains(files["judge.md"], "**Infrastructure problem**") || !strings.Contains(files["compare.md"], "No comparison: there is no fast baseline") {
		t.Fatalf("bodies:\n%s\n%s", files["judge.md"], files["compare.md"])
	}
}

// A secret in the results or in the compare-runs answer withholds every body.
func TestRenderWithholdsSecrets(t *testing.T) {
	key := "typesafe-key-value"
	for name, mutate := range map[string]func(r *Report){
		"result file": func(r *Report) { r.Inputs = map[string]string{"ci-fast-rust@x": `{"Output":"` + key + `"}`} },
		"compare":     func(r *Report) { r.CompareLines = append(r.CompareLines, "the key is "+key) },
		"shape":       func(r *Report) { r.Results.Judges[1].Text = "ghp_" + strings.Repeat("Ab3", 12) },
	} {
		report := reportFixture(t, true)
		report.SecretValues = []string{key}
		mutate(&report)
		files, err := report.Render()
		if err != nil {
			t.Fatal(err)
		}
		status, _ := ParseStatus(files["status.json"])
		joined := strings.Join(status.Problems, "\n")
		if status.InfraOK || !strings.Contains(joined, "secret scan: ") || strings.Contains(joined, key) || strings.Contains(files["status.json"], "ghp_") {
			t.Errorf("%s: %+v", name, status)
		}
		for _, body := range []string{"judge.md", "compare.md", "summary.md"} {
			if !strings.Contains(files[body], "Withheld: the secret scan") || strings.Contains(files[body], key) || strings.Contains(files[body], "VERDICT") {
				t.Errorf("%s: %s not withheld:\n%s", name, body, files[body])
			}
		}
		if !strings.HasPrefix(files["judge.md"], JudgeMarker) || !strings.HasPrefix(files["compare.md"], CompareMarker) {
			t.Errorf("%s: markers", name)
		}
	}
	files, _ := reportFixture(t, true).Render()
	if status, _ := ParseStatus(files["status.json"]); !status.InfraOK {
		t.Fatalf("clean: %+v", status.Problems)
	}
}

// A full tier with long judge reports still fits in a comment; the summary keeps it all.
func TestRenderTruncatesJudgeComment(t *testing.T) {
	report := reportFixture(t, true)
	for i := range report.Results.Judges {
		report.Results.Judges[i].Text = strings.Repeat("ünïcödé judge line 🙂\n", 2000)
	}
	files, err := report.Render()
	if err != nil {
		t.Fatal(err)
	}
	judgeBody := files["judge.md"]
	if len(judgeBody) > CommentLimit || utf8.RuneCountInString(judgeBody) > CommentLimit || !strings.Contains(judgeBody, "**Truncated**") || !strings.Contains(judgeBody, "([workflow run](https://github.com/Jacarte/pepi/actions/runs/1))") {
		t.Fatalf("judge.md: %d bytes", len(judgeBody))
	}
	if strings.Count(judgeBody, "<details>") != strings.Count(judgeBody, "</details>") {
		t.Fatal("unbalanced details")
	}
	if summary := files["summary.md"]; strings.Contains(summary, "Truncated") || len(summary) < 3*len(report.Results.Judges[0].Text) {
		t.Fatalf("summary.md: %d bytes", len(summary))
	}
}

package ci

import (
	"slices"
	"strings"
	"testing"
)

func TestCheckBaselineEntries(t *testing.T) {
	ok := []Entry{{Name: "ci-fast-tasks-memory@tasks@0123456789ab@only=memory-key,memory-recall@hippo=c8f33b2-aaaaaaaaaaaa.eval.md", Regular: true, Size: 10}}
	if err := CheckBaselineEntries(ok); err != nil {
		t.Fatal(err)
	}
	bad := map[string][]Entry{
		"not allowed":         {{Name: "../x", Regular: true}},
		"not allowed ":        {{Name: ".hidden", Regular: true}},
		"not allowed  ":       {{Name: "a b", Regular: true}},
		"not allowed   ":      {{Name: "a/b", Regular: true}},
		"not allowed    ":     {{Name: "ci-fast..rust", Regular: true}},
		"not a regular file":  {{Name: "link", Regular: false}},
		"more than 2097152":   {{Name: "big", Regular: true, Size: MaxBaselineFileBytes + 1}},
		"bytes, more than 52": {{Name: "a", Regular: true, Size: MaxBaselineFileBytes}, {Name: "b", Regular: true, Size: MaxBaselineFileBytes}, {Name: "c", Regular: true, Size: MaxBaselineFileBytes}},
	}
	for want, entries := range bad {
		if err := CheckBaselineEntries(entries); err == nil || !strings.Contains(err.Error(), strings.TrimSpace(want)) {
			t.Errorf("%v: want %q, got %v", entries, want, err)
		}
	}
}

func TestScanSecrets(t *testing.T) {
	// Assembled at run time so this file itself holds no secret shape.
	secrets := map[string]string{
		"sk- API key":                 "sk-" + strings.Repeat("a1B2", 8),
		"GitHub token":                "ghp_" + strings.Repeat("Ab3", 12),
		"GitHub fine-grained token":   "github_pat_" + strings.Repeat("Ab3_", 10),
		"AWS access key id":           "AKIA" + "ABCDEFGHIJ234567",
		"Bearer or Basic credentials": "Authorization: Bearer " + strings.Repeat("x9", 15),
		"credential assignment":       `{"password":"` + strings.Repeat("Pw9", 8) + `"}`,
		"private key":                 "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE",
		"Slack token":                 "xoxp-" + "1234567890-abcdef",
		"GitLab token":                "glpat-" + strings.Repeat("Ab3_", 6),
		"Google API key":              "AIza" + strings.Repeat("Ab3_-", 7),
		"Hugging Face token":          "hf_" + strings.Repeat("Ab3", 11),
		"npm token":                   "npm_" + strings.Repeat("Ab3", 12),
		"JWT":                         "eyJ" + "hbGciOiJIUzI1NiJ9.eyJ" + "zdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlMTIz",
	}
	for rule, secret := range secrets {
		err := ScanSecrets(map[string]string{"file.json": "before " + secret + " after"}, nil)
		if err == nil || !strings.Contains(err.Error(), "file.json: "+rule) {
			t.Errorf("%s: %v", rule, err)
			continue
		}
		if strings.Contains(err.Error(), secret) || strings.Contains(err.Error(), "before") {
			t.Errorf("%s: the error shows the matching text: %v", rule, err)
		}
	}
	// Values of the secret env, as written and JSON-escaped; short values are ignored.
	value := "v4lue&<with>specials"
	for _, content := range []string{"x " + value, `{"Output":"v4lue\u0026\u003cwith\u003especials"}`} {
		err := ScanSecrets(map[string]string{"f": content}, []string{value, "short"})
		if err == nil || !strings.Contains(err.Error(), "f: a value of the eval secrets") || strings.Contains(err.Error(), value) {
			t.Errorf("%q: %v", content, err)
		}
	}
	clean := map[string]string{
		"f": `{"Task":"tasks/memory-key","Output":"disk-usage task-runner sk-short password: hunter2 Bearer ${token} short"}`,
	}
	if err := ScanSecrets(clean, []string{"short"}); err != nil {
		t.Fatalf("false positive: %v", err)
	}
}

func TestSecretValues(t *testing.T) {
	env := []EnvVar{{"TYPESAFE_API_KEY", "key-value-1"}, {"JEV_MODEL", "bedrock-claude"}, {"LLM_TOKEN", "tok"}, {"OPENAI_BASE_URL", "https://llm"}}
	auth := `{"anthropic":{"type":"oauth","access":"access-token-1","refresh":"refresh-token-1","expires":1},"list":["in-a-list-1"]}`
	got := SecretValues(env, auth)
	slices.Sort(got)
	// Model names and base URLs also appear in the agent's output: only secret-named variables.
	if want := []string{"access-token-1", "in-a-list-1", "key-value-1", "oauth", "refresh-token-1", "tok"}; !slices.Equal(got, want) {
		t.Fatalf("got %v", got)
	}
	if got := SecretValues(nil, " not json token \n"); !slices.Equal(got, []string{"not json token"}) {
		t.Fatalf("not JSON: %v", got)
	}
	// The short values are kept but not searched for.
	if hits := SecretHits(map[string]string{"f": "oauth tok"}, got); len(hits) != 0 {
		t.Fatalf("hits: %v", hits)
	}
}

const (
	testHead  = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
	testMerge = "9999999999999999999999999999999999999999"
)

// baselineFixture is the baseline of a complete fast eval of PR 5 after its merge.
func baselineFixture(t *testing.T) Baseline {
	t.Helper()
	fast, _ := LookupTier("fast")
	files := tierFiles(t, fast, shardOutcomes())
	report := reportFixture(t, true)
	rendered, err := report.Render()
	if err != nil {
		t.Fatal(err)
	}
	status, err := ParseStatus(rendered["status.json"])
	if err != nil {
		t.Fatal(err)
	}
	return Baseline{Tier: fast, Status: status, PR: 5, PRHeadSha: testHead, MergeSha: testMerge, Date: "2026-05-02", Files: files}
}

func TestPrepareBaseline(t *testing.T) {
	b := baselineFixture(t)
	other := Track{"full": {SetupHash: strings.Repeat("f", 64), Runs: 3, PR: 2, Files: []string{"ci-full-rust-1@x"}}}
	b.Track, _ = other.Marshal()
	out, err := b.Prepare()
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != len(b.Files)+1 {
		t.Fatalf("files: %d", len(out))
	}
	for name, content := range b.Files {
		if out["eval-baseline/fast/"+name] != content {
			t.Fatalf("%s not copied", name)
		}
	}
	track, err := ParseTrack(out[".eval.track"])
	if err != nil {
		t.Fatal(err)
	}
	fast := track["fast"]
	if fast.SetupHash != testSetupHash || fast.TasksSha != testTasksSha || fast.TasksRepo != testRepo || fast.Profile != "plain" || fast.Runs != 1 ||
		fast.PR != 5 || fast.EvaluatedSha != testHead || fast.MergeSha != testMerge || fast.Date != "2026-05-02" || len(fast.Files) != 6 || len(fast.Tasks) != 19 {
		t.Fatalf("fast: %+v", fast)
	}
	// The other tier's entry is kept as it was.
	if full := track["full"]; full.PR != 2 || full.Runs != 3 || full.SetupHash != strings.Repeat("f", 64) {
		t.Fatalf("full: %+v", full)
	}
	// A baseline of the same tier replaces the old one.
	b.Track = out[".eval.track"]
	b.Date = "2026-05-03"
	again, err := b.Prepare()
	if err != nil {
		t.Fatal(err)
	}
	track, _ = ParseTrack(again[".eval.track"])
	if track["fast"].Date != "2026-05-03" || track["full"].PR != 2 {
		t.Fatalf("replaced: %+v", track)
	}
}

func TestPrepareBaselineRefuses(t *testing.T) {
	cases := map[string]func(b *Baseline){
		"evaluated head != merged head: baseline not committed": func(b *Baseline) { b.PRHeadSha = testMerge },
		"is not a full 40-hex git sha":                          func(b *Baseline) { b.MergeSha = "main" },
		"date":                                                  func(b *Baseline) { b.Date = "yesterday" },
		"for tier":                                              func(b *Baseline) { b.Status.Tier = "full" },
		"for PR 4":                                              func(b *Baseline) { b.Status.PR = 4 },
		"the eval was incomplete":                               func(b *Baseline) { b.Status.InfraOK = false },
		"no setup hash":                                         func(b *Baseline) { b.Status.SetupHash = "" },
		"not the ones the eval report read":                     func(b *Baseline) { b.Files["ci-fast-extra"] = "[]" },
		"secret scan: possible secrets in eval-baseline/fast/ci-fast-go@go@hippo=c8f33b2-aaaaaaaaaaaa.eval.md: GitHub token: baseline not committed": func(b *Baseline) {
			b.Files["ci-fast-go@go@hippo=c8f33b2-aaaaaaaaaaaa.eval.md"] = "ghp_" + strings.Repeat("Ab3", 12)
		},
		"prNumber must be a PR number": func(b *Baseline) { b.PR = 0 },
		"status.json has 3 runs":       func(b *Baseline) { b.Status.Runs = 3 },
		"status.json tasks_sha":        func(b *Baseline) { b.Status.TasksSha = "main" },
		"the results are incomplete: shard go run 1: no judge report": func(b *Baseline) {
			name := "ci-fast-go@go@hippo=c8f33b2-aaaaaaaaaaaa.eval.md"
			delete(b.Files, name)
			b.Status.Files = slices.DeleteFunc(b.Status.Files, func(f string) bool { return f == name })
		},
		".eval.track": func(b *Baseline) { b.Track = "fast: [" },
	}
	for want, mutate := range cases {
		b := baselineFixture(t)
		mutate(&b)
		if _, err := b.Prepare(); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("want %q, got %v", want, err)
		}
	}
	b := baselineFixture(t)
	b.Date = "2026-05-02T10:00:00Z"
	if _, err := b.Prepare(); err != nil {
		t.Fatalf("an RFC 3339 date: %v", err)
	}
}

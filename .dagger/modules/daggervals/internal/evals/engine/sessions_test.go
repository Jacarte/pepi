package engine

import (
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// The reset runs only between sessions: never before the first (which runs TestEnv) and,
// since it starts a session, never after the last.
func TestSessionSetup(t *testing.T) {
	if script, reset := sessionSetup(0, "setup"); script != "setup" || reset {
		t.Fatalf("session 1: %q %v", script, reset)
	}
	for i := 1; i < 3; i++ {
		if script, reset := sessionSetup(i, "setup"); script != "" || !reset {
			t.Fatalf("session %d: %q %v", i+1, script, reset)
		}
	}
}

func writeFiles(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for name, contents := range files {
		file := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(file), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(file, []byte(contents), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func listFiles(t *testing.T, root string) []string {
	t.Helper()
	var files []string
	_ = filepath.Walk(root, func(file string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			rel, _ := filepath.Rel(root, file)
			contents, _ := os.ReadFile(file)
			files = append(files, rel+"="+string(contents))
		}
		return nil
	})
	slices.Sort(files)
	return files
}

// resetScript on a fake agent dir, /tmp and HOME, with a fake go that records its call.
func TestResetScript(t *testing.T) {
	// Resolved, so $PWD (physical) and the paths agree where the temp dir is a symlink.
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	agentDir, snapshot, tmp, home, bin := filepath.Join(root, "agent"), filepath.Join(root, "snapshot"), filepath.Join(root, "tmp"), filepath.Join(root, "home"), filepath.Join(root, "bin")
	writeFiles(t, agentDir, map[string]string{
		"settings.json": "changed by pi", "sessions/--testbed--/s1.jsonl": "transcript", "logs/prompts.jsonl": "prompts",
		"run-history.jsonl": "history", "auth.json": "secret", "npm/pkg/index.js": "pkg", "hippocampus-outbox/pepi-1.json": "pending",
	})
	writeFiles(t, snapshot, map[string]string{"settings.json": "before", "extensions/a.ts": "ext"})
	base := filepath.Join(tmp, "daggervals-base-tree")
	writeFiles(t, tmp, map[string]string{"daggervals-base-tree": "tree", "scratch.go": "s1", "daggervals-notes": "planted", "go-build123/x": "o", "repo/main.go": "workspace"})
	writeFiles(t, home, map[string]string{".cache/go-build/x": "obj", ".bashrc": "keep"})
	writeFiles(t, bin, map[string]string{"go": "#!/bin/sh\necho \"$@\" > " + filepath.Join(root, "go-called") + "\n"})
	if err := os.Chmod(filepath.Join(bin, "go"), 0o755); err != nil {
		t.Fatal(err)
	}

	cmd := exec.Command("bash", "-c", resetScript, "reset", agentDir, snapshot, tmp, base)
	cmd.Dir = filepath.Join(tmp, "repo")
	cmd.Env = []string{"PATH=" + bin + ":/usr/bin:/bin", "HOME=" + home}
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%v: %s", err, out)
	}
	if got := listFiles(t, agentDir); !slices.Equal(got, []string{"auth.json=secret", "extensions/a.ts=ext", "hippocampus-outbox/pepi-1.json=pending", "npm/pkg/index.js=pkg", "settings.json=before"}) {
		t.Errorf("agent dir: %v", got)
	}
	if got := listFiles(t, tmp); !slices.Equal(got, []string{"daggervals-base-tree=tree", "repo/main.go=workspace"}) {
		t.Errorf("tmp: %v", got)
	}
	if got := listFiles(t, home); !slices.Equal(got, []string{".bashrc=keep"}) {
		t.Errorf("home: %v", got)
	}
	if _, err := os.Stat(snapshot); err == nil {
		t.Error("the snapshot must be removed")
	}
	if called, _ := os.ReadFile(filepath.Join(root, "go-called")); strings.TrimSpace(string(called)) != "clean -cache" {
		t.Errorf("go clean: %q", called)
	}
}

func TestParseRecalls(t *testing.T) {
	jsonl := `{"type":"session","id":"x"}
{"type":"message","message":{"role":"user","content":"hi"}}
{"type":"custom_message","customType":"hippocampus-recall","content":"Untrusted memory evidence: [{\"memory\":\"CRC is Castagnoli\"}]","display":false}
{"type":"custom_message","customType":"other","content":"no"}
not json
{"type":"custom_message","customType":"hippocampus-recall","content":"` + strings.Repeat("é", 3000) + `","display":false}
`
	got := parseRecalls(jsonl)
	if len(got) != 2 || got[0] != `Untrusted memory evidence: [{"memory":"CRC is Castagnoli"}]` || len(got[1]) > maxRecalled+3 || !strings.HasSuffix(got[1], "é...") {
		t.Fatalf("recalls: %d %q", len(got), got[0])
	}
}

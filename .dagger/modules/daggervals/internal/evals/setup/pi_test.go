package setup

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"dagger/daggervals/internal/evals/memory"
)

// Without memory flags and env file the container's settings.json is left as the profile has it.
func TestMemorySettingsCommand(t *testing.T) {
	if command := memorySettingsCommand(memory.Untouched); command != nil {
		t.Fatalf("untouched must not rewrite settings: %v", command)
	}
	for _, mode := range []memory.Mode{memory.On, memory.Off} {
		command := memorySettingsCommand(mode)
		if len(command) != 6 || command[0] != "node" || !slices.Equal(command[3:], []string{string(mode), "http://hippocampus:8000", "eval"}) {
			t.Fatalf("%s: %v", mode, command)
		}
	}
}

// runMemorySettings runs the rewrite on settings in a temporary file instead of SettingsPath.
func runMemorySettings(t *testing.T, mode memory.Mode, settings string) (map[string]any, error) {
	t.Helper()
	if _, err := exec.LookPath("node"); err != nil {
		t.Skip("node is not installed")
	}
	file := filepath.Join(t.TempDir(), "settings.json")
	if err := os.WriteFile(file, []byte(settings), 0o600); err != nil {
		t.Fatal(err)
	}
	command := memorySettingsCommand(mode)
	command[2] = strings.Replace(command[2], memory.SettingsPath, file, 1)
	if out, err := exec.Command(command[0], command[1:]...).CombinedOutput(); err != nil {
		return nil, fmt.Errorf("%w: %s", err, out)
	}
	data, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal(err)
	}
	if mode == memory.On {
		// The seeding user must be the one the extension derives from the rewritten settings.
		if id, err := memory.UserID(string(data)); err != nil || id != "default-user:project:4bdd6907e23c2e39e6a512fe" {
			t.Fatalf("user id of the rewritten settings: %q, %v", id, err)
		}
	}
	return result, nil
}

func TestMemorySettingsScript(t *testing.T) {
	profile := `{"extensions":["-builtin:mcp"],"hippocampus":{"enabled":true,"baseUrl":"http://localhost:8000","userId":"default-user","recallLimit":5,"recallLegacyCwd":true}}`

	on, err := runMemorySettings(t, memory.On, profile)
	if err != nil {
		t.Fatal(err)
	}
	hippocampus, _ := json.Marshal(on["hippocampus"])
	if string(hippocampus) != `{"baseUrl":"http://hippocampus:8000","enabled":true,"legacyUserIds":[],"projectId":"eval","recallLegacyCwd":false,"recallLimit":5,"requestTimeoutMs":30000,"userId":"default-user"}` {
		t.Errorf("on: %s", hippocampus)
	}
	if extensions, _ := json.Marshal(on["extensions"]); string(extensions) != `["-builtin:mcp"]` {
		t.Errorf("on must not change extensions: %s", extensions)
	}

	if _, err := runMemorySettings(t, memory.On, `{"pi-memory-mem0":{"mode":"self-hosted"}}`); err == nil {
		t.Error("on without a hippocampus section must fail")
	}

	off, err := runMemorySettings(t, memory.Off, profile)
	if err != nil {
		t.Fatal(err)
	}
	if enabled := off["hippocampus"].(map[string]any)["enabled"]; enabled != false {
		t.Errorf("off: enabled %v", enabled)
	}
	if extensions, _ := json.Marshal(off["extensions"]); string(extensions) != `["-builtin:mcp","-extensions/hippocampus.ts"]` {
		t.Errorf("off must exclude the parent entry point: %s", extensions)
	}
}

// The setup id must not move when the CI commits a baseline (.github, eval-baseline,
// .eval.track) or another profile changes, and must move with AGENTS.md and the profile.
func TestSetupFilesHashScript(t *testing.T) {
	for _, tool := range []string{"bash", "sha256sum"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skip(tool + " is not installed")
		}
	}
	dir := t.TempDir()
	write := func(path, content string) {
		t.Helper()
		full := filepath.Join(dir, path)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	hash := func() string {
		t.Helper()
		cmd := exec.Command("bash", "-c", setupFilesHashScript, "_", "plain")
		cmd.Dir = dir
		out, err := cmd.Output()
		if err != nil {
			t.Fatal(err)
		}
		return strings.TrimSpace(string(out))
	}
	write("AGENTS.md", "rules")
	write("profiles/plain/settings.json", "{}")
	write(".dagger/modules/daggervals/main.go", "package main")
	base := hash()
	if len(base) != 64 {
		t.Fatalf("not a sha256: %q", base)
	}
	for _, path := range []string{".github/workflows/eval.yml", "eval-baseline/fast/ci-fast-rust", ".eval.track", "profiles/other/settings.json", "README.md", "evals_cache/x"} {
		write(path, "new")
		if got := hash(); got != base {
			t.Fatalf("%s changed the setup hash", path)
		}
	}
	for _, path := range []string{"AGENTS.md", "profiles/plain/settings.json", "profiles/plain/new.md", ".dagger/modules/daggervals/main.go"} {
		write(path, "changed "+path)
		got := hash()
		if got == base {
			t.Fatalf("%s did not change the setup hash", path)
		}
		base = got
	}
}

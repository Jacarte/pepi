package setup

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"dagger/daggervals/internal/dagger"
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

// The ablation tests read the root AGENTS.md from the checkout. The
// test package is six directories below the repository root.
func readRepoFile(t *testing.T, rel string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "..", "..", rel))
	if os.IsNotExist(err) {
		t.Skip(rel + " is not in the checkout")
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestAblationRunIDSuffix(t *testing.T) {
	if got := AblationRunIDSuffix(false); got != "" {
		t.Fatalf("flag off: %q, want empty", got)
	}
	if got := AblationRunIDSuffix(true); got != "@forced" {
		t.Fatalf("flag on: %q, want @forced", got)
	}
}

// With the flag off the setup id is the sha256 of the same bytes as before the flag existed, and
// the flag has its own id.
func TestSetupIdWithoutAblationIsUnchanged(t *testing.T) {
	filesHash, envContent, profile := "0123abcd", "KEY=value\n", "bedrock"
	before := sha256.Sum256([]byte(filesHash + envContent + profile))
	if got := sha256.Sum256([]byte(setupIdInput(filesHash, envContent, profile, false))); got != before {
		t.Fatal("setup id with the flag off changed")
	}
	if forced := sha256.Sum256([]byte(setupIdInput(filesHash, envContent, profile, true))); forced == before {
		t.Fatal("setup id with the flag on is the id without it")
	}
}

// --force-delegation appends the section after a blank line.
func TestAppendForcedDelegation(t *testing.T) {
	if !strings.HasPrefix(forcedDelegationSection, "## Mandatory delegation (evaluation ablation)\n") {
		t.Fatal("the section heading changed")
	}
	for input, want := range map[string]string{
		"# Rules\n": "# Rules\n\n" + forcedDelegationSection,
		"# Rules":   "# Rules\n\n" + forcedDelegationSection,
		"":          forcedDelegationSection,
	} {
		if got := appendForcedDelegation(input); got != want {
			t.Fatalf("input %q: %q, want %q", input, got, want)
		}
	}
}

// The real root AGENTS.md with the delegation section appended: the file text is kept and the
// section comes last.
func TestForcedDelegationOnRepositoryAgents(t *testing.T) {
	agents := readRepoFile(t, "AGENTS.md")
	forced := appendForcedDelegation(agents)
	if !strings.HasPrefix(forced, agents) || !strings.HasSuffix(forced, "\n\n"+forcedDelegationSection) {
		t.Fatal("forced AGENTS.md is not the root file with the delegation section last")
	}
}

// --force-delegation is eval-only: a baked image with the flag fails at the first check, before
// any dagger call (Workspace is nil in these tests), and a baked image without it is accepted.
func TestBakeRejectsAblations(t *testing.T) {
	c := &PiSetup{Bake: true, Profile: "bedrock", ForceDelegation: true}
	if err := c.validateAblations(); err == nil {
		t.Error("validateAblations must fail on a baked image")
	}
	if err := c.Resolve(context.Background()); err == nil {
		t.Error("Resolve must fail on a baked image")
	}
	func() {
		defer func() {
			if recover() == nil {
				t.Error("Setup must panic on a baked image")
			}
		}()
		c.Setup()
	}()
	for _, c := range []*PiSetup{
		{Bake: true, Profile: "bedrock"},
		{Profile: "bedrock", ForceDelegation: true},
	} {
		if err := c.validateAblations(); err != nil {
			t.Errorf("bake=%v force=%v: %v", c.Bake, c.ForceDelegation, err)
		}
	}
	if setupFunc := (&PiSetup{Bake: true, Profile: "bedrock"}).Setup(); setupFunc == nil {
		t.Fatal("a baked image without ablations must set up")
	}
}

// The judge is not the agent under test: its copy of the setup has the flag off and no resolved
// AGENTS.md, so its container chain takes no ablation step, while the agent's keeps them.
// The sentinel container is never used: applyAblations returns it unchanged for the judge.
func TestJudgeSetupHasNoAblations(t *testing.T) {
	rules := "# Rules\n"
	agent := &PiSetup{Profile: "bedrock", ForceDelegation: true, ablationAgents: &rules}
	judge := agent.WithoutAblations()
	if judge.ForceDelegation || judge.ablationAgents != nil {
		t.Fatalf("judge keeps the ablation: force=%v resolved=%v", judge.ForceDelegation, judge.ablationAgents != nil)
	}
	if judge.Profile != agent.Profile {
		t.Fatalf("judge profile %q, want %q", judge.Profile, agent.Profile)
	}
	if !agent.ForceDelegation || agent.ablationAgents == nil {
		t.Fatal("WithoutAblations changed the agent setup")
	}
	sentinel := new(dagger.Container)
	if got := judge.applyAblations(sentinel); got != sentinel {
		t.Fatal("the judge container chain takes an ablation step")
	}
	if err := judge.validateAblations(); err != nil {
		t.Fatal(err)
	}
}

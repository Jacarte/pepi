package engine

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"path"
	"strings"
	"unicode/utf8"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/memory"
	tasks "dagger/daggervals/internal/evals/task"
)

// piAgentDir is Pi's agent dir in the agent container.
var piAgentDir = path.Dir(memory.SettingsPath)

// agentDirSnapshotPath is where the agent dir before the first session is put back from.
const agentDirSnapshotPath = "/tmp/daggervals-agent-dir"

// resetScript runs between two sessions, outside the measured exec, so a session sees only
// the workspace and the memory service of the previous one: Pi's transcripts (sessions/),
// logs and anything else written into the agent dir ($1) are replaced by its state before
// the first session ($2), and /tmp ($3) and the build caches are emptied. Kept: the Pi auth
// mount, installed packages, the Hippocampus outbox (a capture still to deliver), the base
// tree of the final diff ($4) and the workspace when it is under /tmp. The workspace's git
// history is not touched.
const resetScript = `set -eu
find "$1" -mindepth 1 -maxdepth 1 ! -name auth.json ! -name npm ! -name hippocampus-outbox -exec rm -rf {} +
cp -a "$2"/. "$1"/
rm -rf "$2"
find "$3" -mindepth 1 -maxdepth 1 ! -path "$4" ! -path "$PWD" -exec rm -rf {} +
rm -rf "${HOME:-/root}/.cache"
if command -v go >/dev/null; then go clean -cache; fi
`

// sessionSetup is the setup of session i (from 0): TestEnv runs once, before the first
// session (for repo tasks it records the base tree of the final diff), and every later
// session starts with a reset.
func sessionSetup(i int, testEnvScript string) (setupScript string, reset bool) {
	if i == 0 {
		return testEnvScript, false
	}
	return "", true
}

// resetSession applies resetScript to container. agentDirBefore is the agent dir before the
// first session, without the auth secret.
func resetSession(container *dagger.Container, agentDirBefore *dagger.Directory) *dagger.Container {
	return container.
		WithDirectory(agentDirSnapshotPath, agentDirBefore).
		WithExec([]string{"bash", "-c", resetScript, "reset", piAgentDir, agentDirSnapshotPath, "/tmp", tasks.BaseTreeFile})
}

// maxRecalled bounds each text of SessionOutcome.Recalled.
const maxRecalled = 4096

// recalled reads the Hippocampus recall messages of the session from Pi's transcripts, the
// parent's and its children's, before the reset removes them.
func recalled(ctx context.Context, container *dagger.Container) ([]string, error) {
	dir := container.Directory(piAgentDir)
	exists, err := dir.Exists(ctx, "sessions")
	if err != nil || !exists {
		return nil, err
	}
	files, err := dir.Glob(ctx, "sessions/**/*.jsonl")
	if err != nil {
		return nil, fmt.Errorf("list Pi transcripts: %w", err)
	}
	var texts []string
	for _, file := range files {
		contents, err := dir.File(file).Contents(ctx)
		if err != nil {
			return nil, fmt.Errorf("read Pi transcript %s: %w", file, err)
		}
		texts = append(texts, parseRecalls(contents)...)
	}
	return texts, nil
}

// parseRecalls returns the content of the hippocampus-recall custom messages of a Pi
// transcript, cut to maxRecalled bytes. Pi keeps them although they are not displayed.
func parseRecalls(jsonl string) []string {
	var texts []string
	scanner := bufio.NewScanner(strings.NewReader(jsonl))
	scanner.Buffer(nil, 64<<20)
	for scanner.Scan() {
		var entry struct {
			Type       string `json:"type"`
			CustomType string `json:"customType"`
			Content    any    `json:"content"`
		}
		if json.Unmarshal(scanner.Bytes(), &entry) != nil || entry.Type != "custom_message" || entry.CustomType != "hippocampus-recall" {
			continue
		}
		text, ok := entry.Content.(string)
		if !ok {
			raw, _ := json.Marshal(entry.Content)
			text = string(raw)
		}
		if len(text) > maxRecalled {
			cut := maxRecalled
			for cut > 0 && !utf8.RuneStart(text[cut]) {
				cut--
			}
			text = text[:cut] + "..."
		}
		texts = append(texts, text)
	}
	return texts
}

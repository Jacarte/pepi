package setup

import (
	"context"
	"crypto/sha256"
	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/memory"
	"encoding/hex"
	"strings"
)

type PiSetup struct {
	Workspace *dagger.Directory
	Auth      *dagger.Secret
	Env       *dagger.EnvFile
	Profile   string
	// Bake copies the workspace into the image filesystem (instead of mounting it)
	// and skips the auth mount; Auth and Env may then be nil.
	Bake bool
	// Memory rewrites the hippocampus section (and, off, the extensions list) in the
	// container's settings.json only; the profiles on disk are not touched.
	Memory memory.Mode
}

// memorySettingsScript is run by node with the mode, the base URL and the project id. On needs
// the profile's hippocampus section. The maximum timeout keeps a slow server LLM from cutting
// the capture pi waits for before exiting, or the recall; legacy scopes are empty on a fresh
// server, and one failed scope fails the whole recall. Off, enabled:false stops recall and
// capture but leaves the parent's tool registered, so its auto-discovered entry point is
// excluded; children keep the read-only tool, which pi-subagents requires.
const memorySettingsScript = `
const fs = require('fs');
const [mode, baseUrl, projectId] = process.argv.slice(1);
const file = '` + memory.SettingsPath + `';
const settings = JSON.parse(fs.readFileSync(file, 'utf8'));
const hippocampus = settings.hippocampus;
if (mode === 'on') {
  if (!hippocampus) {
    console.error(file + ': no hippocampus section to use the memory service');
    process.exit(1);
  }
  settings.hippocampus = { ...hippocampus, enabled: true, baseUrl, projectId, requestTimeoutMs: 30000, recallLegacyCwd: false, legacyUserIds: [] };
} else {
  settings.hippocampus = { ...hippocampus, enabled: false };
  settings.extensions = [...(settings.extensions ?? []), '-extensions/hippocampus.ts'];
}
fs.writeFileSync(file, JSON.stringify(settings, null, 2) + '\n');
`

// memorySettingsCommand is the settings rewrite of mode, nil for memory.Untouched.
func memorySettingsCommand(mode memory.Mode) []string {
	if mode == memory.Untouched {
		return nil
	}
	return []string{"node", "-e", memorySettingsScript, string(mode), memory.BaseURL, memory.ProjectID}
}

// Copy needed files, setup
// the needed for pi and return a new Container setup function
func (c *PiSetup) Setup() dagger.WithContainerFunc {

	return func(container *dagger.Container) *dagger.Container {

		// install pi
		container = container.WithExec([]string{"npm", "install", "-g", "--ignore-scripts", "@earendil-works/pi-coding-agent"})
		// install codegraph
		container = container.WithExec([]string{"npm", "install", "-g", "@colbymchenry/codegraph"})

		if c.Bake {
			container = container.WithDirectory("/root/.pi/agent", c.Workspace).
				WithExec([]string{"/root/.pi/agent/profile.sh", c.Profile})
		} else {
			// Copy the workspace to the container
			container = container.WithMountedDirectory("/root/.pi/agent", c.Workspace).
				WithMountedSecret("/root/.pi/agent/auth.json", c.Auth).
				WithExec([]string{"/root/.pi/agent/profile.sh", c.Profile})
			// TODO install ketch
			// TODO install herdr
		}
		// FIXME
		// remove the current /Users/<user/.pi in the settings.json fixed extension paths
		// in favor of /root/.pi after copying settings.json to the root folder
		container = container.WithExec([]string{"bash", "-c", `
					sed -i 's|/Users/[^/]*/.pi|/root/.pi|g' /root/.pi/agent/settings.json
			`})
		if command := memorySettingsCommand(c.Memory); command != nil {
			container = container.WithExec(command)
		}

		if c.Env != nil {
			container = container.WithEnvFileVariables(c.Env)
		}

		// TODO Set some MCPs as services

		// Return the container with the setup done
		return container
	}
}

func (c *PiSetup) GetSetupId(ctx context.Context, container *dagger.Container) (string, error) {

	withFilesContainer := container.WithDirectory("/root/.pi/agent", c.Workspace)

	// Hash only the files that define the agent setup. Volatile workspace files
	// (budget history, caches, run history, ...) must not change the run ID.
	// Root settings.json/mcp.json/models-router.json are overlaid by the profile,
	// so the profile directory is hashed instead.
	shaFolder, err := withFilesContainer.WithExec([]string{"bash", "-c", `
cd /root/.pi/agent || exit 1
paths=()
for p in AGENTS.md profile.sh extensions skills prompts workflows "profiles/$1" .dagger; do
  [ -e "$p" ] && paths+=("$p")
done
find "${paths[@]}" -type f -not -path '*/node_modules/*' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
`, "_", c.Profile}).Stdout(ctx)
	if err != nil {
		return "", err
	}

	envContent, err := c.Env.AsFile().Contents(ctx)
	if err != nil {
		return "", err
	}
	// The final id is sha256(workspace sha + env content + profile name), hex, no newline.
	sum := sha256.Sum256([]byte(strings.TrimSpace(shaFolder) + envContent + c.Profile))
	return hex.EncodeToString(sum[:]), nil

}

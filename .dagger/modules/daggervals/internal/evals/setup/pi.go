package setup

import (
	"context"
	"crypto/sha256"
	"dagger/daggervals/internal/dagger"
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
				// TODO read the profile from an ENV passed here
				WithExec([]string{"/root/.pi/agent/profile.sh", c.Profile})
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
for p in AGENTS.md profile.sh extensions skills prompts workflows "profiles/$1"; do
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

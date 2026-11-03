package setup

import "dagger/daggervals/internal/dagger"

type PiSetup struct {
	Workspace *dagger.Directory
	Auth      *dagger.Secret
	Env       *dagger.EnvFile
}

// Copy needed files, setup
// the needed for pi and return a new Container setup function
func (c *PiSetup) Setup() dagger.WithContainerFunc {

	return func(container *dagger.Container) *dagger.Container {

		// install pi
		container = container.WithExec([]string{"npm", "install", "-g", "--ignore-scripts", "@earendil-works/pi-coding-agent"})

		// Copy the workspace to the container
		container = container.WithMountedDirectory("/root/.pi/agent", c.Workspace).
			WithMountedSecret("/root/.pi/agent/auth.json", c.Auth).
			// TODO read the profile from an ENV passed here
			WithExec([]string{"/root/.pi/agent/profile.sh", "codex"}).
			WithEnvFileVariables(c.Env)

		// TODO Set some MCPs as services

		// Return the container with the setup done
		return container
	}
}

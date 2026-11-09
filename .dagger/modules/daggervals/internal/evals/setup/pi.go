package setup

import "dagger/daggervals/internal/dagger"

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

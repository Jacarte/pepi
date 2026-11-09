package environment

import "dagger/daggervals/internal/dagger"

type RustEnvironment struct{}

func (e *RustEnvironment) PrepareEnv() dagger.WithContainerFunc {

	return func(container *dagger.Container) *dagger.Container {
		// TODO install cargo, etc
		// Depends on the env

		// Return the container with the setup done
		return container
	}
}

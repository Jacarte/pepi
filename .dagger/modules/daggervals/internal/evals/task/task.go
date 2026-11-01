package task

import (
	"context"
	"dagger/daggervals/internal/dagger"
)

type Baseline struct {
	Repo       string
	BaseCommit string
	Version    string
}

// This preinstall all that is needed
// for the repo of the task to be in its home
// cargo, etc
type Environment interface {
	PrepareEnv(ctx context.Context) dagger.WithContainerFunc
}

// Executes the test after the agent finishes
type Oracle interface {
	// We report if the agent said solved while in
	// practice did not
	ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error)
}

type FixTask struct {
	// Expected patch
	GoldPatch string

	ProblemStatement string
	// The command the agent needs to execute to
	// test the fix is correct
	HowToTest string

	Baseline Baseline

	Env Environment

	Oracle Oracle
}

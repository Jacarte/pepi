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

// TestEnvironment supplies bash setup (checkout, patch, failing tests) to run
// inside the measured exec, before the agent. Its cwd and exports carry over.
// Script must only construct the script; provisioning belongs to Environment.
type TestEnvironment interface {
	Script(ctx context.Context) (string, error)
}

// Executes the test after the agent finishes
type Oracle interface {
	// We report if the agent said solved while in
	// practice did not
	ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error)
}

// The flow is
// 1. PrepareEnv
// 2. PrepareTestEnv
// 3. Run the agent to fix the problem
// 4. Run the Oracle to validate the final answer
// 5. Run the original tests with the fix (this is the oracle) to validate the fix is correct

type FixTask struct {
	// Expected patch
	GoldPatch string

	ProblemStatement string
	// The command the agent needs to execute to
	// test the fix is correct
	HowToTest string

	Baseline Baseline

	Env Environment

	TestEnv TestEnvironment

	Oracle Oracle
}

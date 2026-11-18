package task

import (
	"context"
	"dagger/daggervals/internal/dagger"
)

// This preinstall all that is needed
// for the repo of the task to be in its home
// cargo, etc
type Environment interface {
	PrepareEnv(ctx context.Context) dagger.WithContainerFunc
	PrepareEnvForTesting(ctx context.Context) dagger.WithContainerFunc
	GetRepo() string
	GetFinalDiff(ctx context.Context, container *dagger.Container) string
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

// MilestoneResult is the verdict of one hidden milestone.
type MilestoneResult struct {
	ID     string
	Title  string
	Passed bool
	// Detail is the failure line when the milestone did not pass.
	Detail string
}

// MilestoneOracle is an Oracle that grades milestones instead of one pass/fail. The
// engine prefers it: Score counts the passed milestones according to Scoring (prefix or
// count) and the task is valid when all pass. Grading must happen in a fresh container
// that is built from the agent's files only, never in the agent's container.
type MilestoneOracle interface {
	Oracle
	Scoring() string
	GradeMilestones(ctx context.Context, container *dagger.Container) ([]MilestoneResult, error)
}

// The flow is
// 1. PrepareEnv
// 2. PrepareTestEnv
// 3. Run the agent to fix the problem
// 4. Run the Oracle to validate the final answer
// 5. Run the original tests with the fix (this is the oracle) to validate the fix is correct

type FixTask struct {
	GoldenPatch string

	ProblemStatement string
	// The command the agent needs to execute to
	// test the fix is correct
	EvalTest string

	Env Environment

	TestEnv TestEnvironment

	Oracle Oracle

	// TimeoutSeconds is the agent's time limit, of each session; 0 means
	// DefaultTaskTimeoutSeconds.
	TimeoutSeconds int

	// SessionPrompts, when set, makes a multi-session task: one fresh `pi -p` per prompt, in
	// order, in the same container, so a session sees the workspace and the agent dir (and
	// the memory service) the previous one left but not its conversation. TestEnv runs only
	// before the first session. ProblemStatement then holds every prompt, for the judges.
	SessionPrompts []string

	// MemoryFixture are memories seeded into the task's memory server before the agent starts.
	MemoryFixture []string
}

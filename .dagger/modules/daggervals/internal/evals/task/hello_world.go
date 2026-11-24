package task

import (
	"context"
	"dagger/daggervals/internal/dagger"
	"fmt"
	"strings"
)

type HelloWorldEnvironment struct{}

func (HelloWorldEnvironment) PrepareEnv(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return container
	}
}

func (HelloWorldEnvironment) PrepareEnvForTesting(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return container
	}
}

func (HelloWorldEnvironment) GetRepo() string   { return "hello-world" }
func (HelloWorldEnvironment) GetCommit() string { return "" }

type HelloWorldOracle struct{}

func (HelloWorldEnvironment) GetFinalDiff(ctx context.Context, container *dagger.Container) string {
	return "" // no diff to check for hello world
}

func (HelloWorldOracle) ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error) {
	// Get the stdout and log it
	stdout, err := container.Stdout(ctx)
	if err != nil {
		return false, err
	}
	fmt.Println("HelloWorldOracle: stdout:", stdout)
	return checkHelloWorld(stdout)
}

// checkHelloWorld is the pure part of the oracle: stdout must say hello world.
func checkHelloWorld(stdout string) (bool, error) {
	if !strings.Contains(strings.ToLower(stdout), "hello world") {
		return false, fmt.Errorf("HelloWorldOracle: stdout does not contain 'hello world': %s", stdout)
	}
	return true, nil
}

// The report lines of steps 1-7 are only read by people; the oracle checks "hello world".
var HelloWorldTask = FixTask{
	ProblemStatement: "Do these steps in order and do not skip any, even when a step fails; report a failure instead of stopping.\n" +
		"1. Print one line starting with \"TOOLS:\" listing the name of every tool you can call.\n" +
		"2. Print one line starting with \"SUBAGENT_TOOL:\" with \"available\" or \"missing\", depending on whether you have a tool named subagent.\n" +
		"3. If it is available, call it with action \"list\" and print one line starting with \"SUBAGENTS:\" with the agent names it returns. Otherwise print \"SUBAGENTS: none\".\n" +
		"4. If it is available, call it once with agent \"scout\" and the task \"Reply with the single word ready\", then print one line starting with \"SCOUT_CALL:\" saying ok or failed and why. Otherwise print \"SCOUT_CALL: skipped\".\n" +
		"5. Run bash: ls /root/.pi/agent/npm/node_modules 2>&1 | head -40. Print one line starting with \"PACKAGES:\" with the names, or the error.\n" +
		"6. Print one line starting with \"EXTENSIONS:\" with the file names in /root/.pi/agent/extensions (use bash ls).\n" +
		"7. Print one line starting with \"AGENTS_MD:\" with yes if your context contains the global AGENTS.md rules (for example the rule that the parent orchestrates and workers implement), otherwise no.\n" +
		"Finally reply with the exact words hello world on the final line.",
	Env:    HelloWorldEnvironment{},
	Oracle: HelloWorldOracle{},
}

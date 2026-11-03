package task

// here we will have const as tasks, and the agent will be able to run them

import (
	"context"
	"dagger/daggervals/internal/dagger"
	"fmt"
	"strings"
)

type WasmtimeTask struct{}

func (WasmtimeTask) PrepareEnv(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		// Install Rust v1.97
		container = container.WithExec([]string{
			"sh", "-c",
			"curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.97.0",
		})

		return container
	}
}

type WasmtimeTaskOracle struct{}

func (WasmtimeTaskOracle) ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error) {
	// Get the stdout and log it
	stdout, err := container.Stdout(ctx)
	if err != nil {
		return false, err
	}
	fmt.Println("HelloWorldOracle: stdout:", stdout)
	// Check if the stdout contains "hello world"
	lowerStdout := strings.ToLower(stdout)
	if !strings.Contains(lowerStdout, "hello world") {
		return false, fmt.Errorf("HelloWorldOracle: stdout does not contain 'hello world': %s", stdout)
	}
	return true, nil
}

// An array of tasks
var WasmtimeTasks = []FixTask{
	FixTask{
		ProblemStatement: "say 'hello world' using wasmtime",
		Env:              WasmtimeTask{},
		Oracle:           WasmtimeTaskOracle{},
	},
}

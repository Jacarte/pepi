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

type HelloWorldOracle struct{}

func (HelloWorldOracle) ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error) {
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

var HelloWorldTask = FixTask{
	ProblemStatement: "say 'hello world'",
	Env:              HelloWorldEnvironment{},
	Oracle:           HelloWorldOracle{},
}

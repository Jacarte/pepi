package llm

import (
	"dagger/daggervals/internal/evals/task"
	"reflect"
	"testing"
)

func TestTaskCommandPreservesPromptAndCommand(t *testing.T) {
	agent := NewPiAgent()
	prompt := "say 'hello'; $(do-not-execute)\n\"quotes\""
	args := agent.TaskCommand(task.FixTask{ProblemStatement: prompt})
	if !reflect.DeepEqual(args, []string{"pi", "-p", prompt}) {
		t.Fatalf("unexpected args: %q", args)
	}
	args[0] = "mutated"
	if agent.TaskCommand(task.FixTask{})[0] != "pi" {
		t.Fatal("command aliasing")
	}
}

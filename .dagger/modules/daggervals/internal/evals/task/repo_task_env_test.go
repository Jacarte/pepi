package task

import (
	"context"
	"strings"
	"testing"
)

// Only seedPath(name) reaches the agent's container, as /testbed.
func TestAgentOnlyGetsSeed(t *testing.T) {
	if got := seedPath("five-modules"); got != "tasks/five-modules/seed" {
		t.Fatalf("seed path: %q", got)
	}
	for _, p := range []string{seedPath("x"), goWorkdir} {
		if strings.Contains(p, "hidden") || strings.Contains(p, "milestones") || strings.Contains(p, "task.yaml") || strings.Contains(p, "memory") {
			t.Fatalf("agent-visible path %q names grading files", p)
		}
	}

	script, err := RepoTestEnv{}.Script(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	want := "cd /testbed\ngit add -A\ngit write-tree > /tmp/daggervals-base-tree\n"
	if script != want {
		t.Fatalf("test env script:\n%s\nwant:\n%s", script, want)
	}
	for _, forbidden := range []string{"hidden", "milestones", "/tasks", "git apply", "memory"} {
		if strings.Contains(script, forbidden) || strings.Contains(seedGitScript, forbidden) {
			t.Fatalf("agent-side scripts must not mention %q", forbidden)
		}
	}
}

func TestRepoTaskReusesGoTask(t *testing.T) {
	env := RepoTask{GoTask: GoTask{Repo: taskPath("demo"), GoVersion: "1.22.5"}}
	if env.GetRepo() != "tasks/demo" {
		t.Fatalf("repo: %q", env.GetRepo())
	}
	if !strings.Contains(env.installGoScript(), "go1.22.5.linux-") {
		t.Fatalf("install script does not use the task's Go: %s", env.installGoScript())
	}
}

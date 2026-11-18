package task

import (
	"context"
	"strings"
	"testing"
)

var _ Environment = RepoTask{}
var _ TestEnvironment = RepoTestEnv{}
var _ MilestoneOracle = RepoTaskOracle{}

// What reaches the agent's container: only seedPath(name), mounted as /testbed. The
// engine-level listing of those files is printed by `dagger call daggervals
// task-agent-files`.
func TestAgentOnlyGetsSeed(t *testing.T) {
	if got := seedPath("five-modules"); got != "tasks/five-modules/seed" {
		t.Fatalf("seed path: %q", got)
	}
	for _, p := range []string{seedPath("x"), goWorkdir} {
		if strings.Contains(p, "hidden") || strings.Contains(p, "milestones") || strings.Contains(p, "task.yaml") {
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
	for _, forbidden := range []string{"hidden", "milestones", "/tasks", "git apply"} {
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

func TestOracleScoringComesFromTheTask(t *testing.T) {
	o := RepoTaskOracle{Spec: RepoTaskSpec{Scoring: ScoringPrefix}}
	if o.Scoring() != "prefix" {
		t.Fatalf("scoring: %q", o.Scoring())
	}
}

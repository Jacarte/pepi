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

// splitSkillResults keeps the skill milestones out of the graded results, in order.
func TestSplitSkillResults(t *testing.T) {
	milestones := []Milestone{
		{ID: "m1", Command: "go test ./a"},
		{ID: "m2", Command: "skill_loaded:demo"},
		{ID: "m3", Command: "go test ./b"},
		{ID: "m4", Command: "skill_not_loaded:other"},
	}
	results := []MilestoneResult{{ID: "m1", Passed: true}, {ID: "m2"}, {ID: "m3"}, {ID: "m4", Passed: true}}
	graded, skill, err := splitSkillResults(milestones, results)
	if err != nil || len(graded) != 2 || graded[0].ID != "m1" || graded[1].ID != "m3" {
		t.Fatalf("graded: %+v %v", graded, err)
	}
	if len(skill) != 2 || skill[0].ID != "m2" || skill[1].ID != "m4" || !skill[1].Passed {
		t.Fatalf("skill: %+v", skill)
	}

	if _, _, err := splitSkillResults([]Milestone{{ID: "m5", Command: "skill_loaded:Bad"}}, nil); err == nil {
		t.Fatal("an invalid skill milestone must be an error")
	}
}

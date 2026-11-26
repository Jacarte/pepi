package task

import (
	"strings"
	"testing"
)

func TestParseSkillCheck(t *testing.T) {
	for command, want := range map[string]SkillCheck{
		"skill_loaded:grill-me":    {Skill: "grill-me", Loaded: true},
		"skill_not_loaded:handoff": {Skill: "handoff"},
	} {
		got, isSkill, err := ParseSkillCheck(command)
		if err != nil || !isSkill || got != want {
			t.Fatalf("%q: %+v, %v, %v; want %+v", command, got, isSkill, err, want)
		}
	}
	for _, command := range []string{"go test ./...", "skill_loaded:", "skill_loaded:../x", "skill_loaded:A"} {
		_, isSkill, err := ParseSkillCheck(command)
		if command == "go test ./..." && (isSkill || err != nil) {
			t.Fatalf("go test is not a skill check: %v %v", isSkill, err)
		}
		if command != "go test ./..." && (!isSkill || err == nil) {
			t.Fatalf("%q must be an invalid skill check: %v %v", command, isSkill, err)
		}
	}
}

// A skill milestone passes when the skill's loaded state matches the check.
func TestSkillCheckGrade(t *testing.T) {
	m := Milestone{ID: "m3-skill", Title: "Skill"}
	loaded := []string{"grill-me"}
	if r := (SkillCheck{Skill: "grill-me", Loaded: true}).Grade(m, loaded); !r.Passed || r.ID != "m3-skill" || r.Title != "Skill" {
		t.Fatalf("loaded: %+v", r)
	}
	if r := (SkillCheck{Skill: "handoff", Loaded: true}).Grade(m, loaded); r.Passed || r.Detail != "skill not loaded: handoff" {
		t.Fatalf("not loaded: %+v", r)
	}
	if r := (SkillCheck{Skill: "grill-me"}).Grade(m, loaded); r.Passed || r.Detail != "skill loaded: grill-me" {
		t.Fatalf("must not be loaded: %+v", r)
	}
	if r := (SkillCheck{Skill: "handoff"}).Grade(m, nil); !r.Passed {
		t.Fatalf("not loaded, as required: %+v", r)
	}
}

// milestones.yaml takes skill checks next to go test commands, and rejects other commands.
func TestParseMilestonesYAMLSkillChecks(t *testing.T) {
	milestones, err := ParseMilestonesYAML(`milestones:
  - id: m1-build
    title: Build
    command: go test ./...
  - id: m2-skill
    title: Skill
    command: skill_loaded:grill-me
`)
	if err != nil || len(milestones) != 2 || milestones[1].Command != "skill_loaded:grill-me" {
		t.Fatalf("valid skill check: %+v, %v", milestones, err)
	}
	for _, command := range []string{"skill_loaded:Bad_Name", "make test"} {
		_, err := ParseMilestonesYAML("milestones:\n  - id: m1\n    title: T\n    command: " + command + "\n")
		if err == nil || !strings.Contains(err.Error(), "milestone m1") {
			t.Fatalf("%q: %v", command, err)
		}
	}
}

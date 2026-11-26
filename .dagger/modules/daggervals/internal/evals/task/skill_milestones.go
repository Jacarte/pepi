package task

import (
	"fmt"
	"regexp"
	"slices"
	"strings"
)

// A skill milestone is not a go test: its command is skill_loaded:<name> (the parent read
// <name>/SKILL.md) or skill_not_loaded:<name>. It is graded from the skills the parent loaded,
// never from the workspace, so it is deterministic and needs no LLM.
const (
	skillLoadedPrefix    = "skill_loaded:"
	skillNotLoadedPrefix = "skill_not_loaded:"
)

// skillNamePattern matches the folder names of skills, as taskNamePattern does for tasks.
var skillNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*$`)

// SkillNotGradedDetail is what GradeTask reports for a skill milestone: it grades no agent run, so
// it cannot know the skills loaded, and the milestone is left out of the score.
const SkillNotGradedDetail = "graded only in evals"

// SplitSkillMilestones splits milestones into the ones GradeTask grades and the skill milestones
// it leaves out (see SkillNotGradedDetail). Order is kept within each list.
func SplitSkillMilestones(milestones []Milestone) (graded, skill []Milestone, err error) {
	for _, m := range milestones {
		_, isSkill, err := ParseSkillCheck(m.Command)
		if err != nil {
			return nil, nil, fmt.Errorf("milestone %s: %w", m.ID, err)
		}
		if isSkill {
			skill = append(skill, m)
		} else {
			graded = append(graded, m)
		}
	}
	return graded, skill, nil
}

// SkillCheck is a skill milestone: Loaded true requires the skill to be loaded, false that it is not.
type SkillCheck struct {
	Skill  string
	Loaded bool
}

// ParseSkillCheck reports whether command is a skill milestone and parses it. A command with a
// skill prefix and an invalid name is an error.
func ParseSkillCheck(command string) (check SkillCheck, isSkill bool, err error) {
	var name string
	switch {
	case strings.HasPrefix(command, skillLoadedPrefix):
		name, check.Loaded = strings.TrimPrefix(command, skillLoadedPrefix), true
	case strings.HasPrefix(command, skillNotLoadedPrefix):
		name = strings.TrimPrefix(command, skillNotLoadedPrefix)
	default:
		return SkillCheck{}, false, nil
	}
	if !skillNamePattern.MatchString(name) {
		return SkillCheck{}, true, fmt.Errorf("skill name %q must match %s", name, skillNamePattern)
	}
	check.Skill = name
	return check, true, nil
}

// Grade returns the verdict of the check for the skills the parent loaded.
func (c SkillCheck) Grade(m Milestone, skillsLoaded []string) MilestoneResult {
	result := MilestoneResult{ID: m.ID, Title: m.Title, Passed: slices.Contains(skillsLoaded, c.Skill) == c.Loaded}
	if !result.Passed {
		if c.Loaded {
			result.Detail = "skill not loaded: " + c.Skill
		} else {
			result.Detail = "skill loaded: " + c.Skill
		}
	}
	return result
}

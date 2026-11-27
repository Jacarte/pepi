package setup

import (
	"context"
	"fmt"
	"regexp"
	"strings"

	"dagger/daggervals/internal/dagger"
)

// The ablation flags of Evals are off by default; with them off the setup, the run id and the
// setup id are the ones without the flags.
//
//   - --force-delegation appends the mandatory delegation section to AGENTS.md.
//   - --global-skills stages skills at ~/.agents/skills in the eval image (one folder per skill).
//   - --without-skill removes one of those skills from the image (the skill ablation arm).

// piAgentDir is the agent directory in the eval container.
const piAgentDir = "/root/.pi/agent"

// globalSkillsDir is where the global skills of the agent live in the eval container.
const globalSkillsDir = "/root/.agents/skills"

// forcedDelegationSection is appended to AGENTS.md by --force-delegation.
const forcedDelegationSection = "## Mandatory delegation (evaluation ablation)\n\n" +
	"For any task that edits code, the parent must delegate the implementation to a `worker` subagent " +
	"and have a `reviewer` subagent check the result before finishing. The final report must say that both happened.\n"

// skillNamePattern matches a skill folder name: a single path element, never a path.
var skillNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*$`)

// AblationRunIDSuffix is appended to the run id: @forced with forceDelegation; empty when it is off.
func AblationRunIDSuffix(forceDelegation bool) string {
	if forceDelegation {
		return "@forced"
	}
	return ""
}

// SkillRunIDSuffix is appended to the run id for the skill flags: @skills when skills are staged,
// and @noskill-<name> too when withoutSkill names the skill left out. Empty without skills.
func SkillRunIDSuffix(staged bool, withoutSkill string) string {
	switch {
	case !staged:
		return ""
	case withoutSkill != "":
		return "@skills@noskill-" + withoutSkill
	default:
		return "@skills"
	}
}

// setupIdInput is what GetSetupId hashes: the workspace files hash (with the staged skills
// digest), the env content, the profile and the ablation suffixes. The suffixes are empty with
// the flags off, so the input is unchanged.
func setupIdInput(filesHash, envContent, profile, ablations string) string {
	return filesHash + envContent + profile + ablations
}

// checkAblationFlags rejects the flags that a baked agent image (PiSetup.Bake) cannot carry, and
// a skill to remove that is not staged or not a plain name.
func checkAblationFlags(forceDelegation, staged bool, withoutSkill string, bake bool) error {
	if bake && forceDelegation {
		return fmt.Errorf("--force-delegation is eval-only: a baked agent image (PiSetup.Bake) cannot carry it")
	}
	if bake && (staged || withoutSkill != "") {
		return fmt.Errorf("--global-skills and --without-skill are eval-only: a baked agent image (PiSetup.Bake) cannot carry them")
	}
	if withoutSkill != "" && !staged {
		return fmt.Errorf("--without-skill=%s needs --global-skills: there is no skill to remove", withoutSkill)
	}
	if withoutSkill != "" && !skillNamePattern.MatchString(withoutSkill) {
		return fmt.Errorf("--without-skill=%q must be a skill folder name", withoutSkill)
	}
	return nil
}

// validateAblations checks the flags of the setup (see checkAblationFlags).
func (c *PiSetup) validateAblations() error {
	return checkAblationFlags(c.ForceDelegation, c.GlobalSkills != nil, c.WithoutSkill, c.Bake)
}

// ablationSuffix is the run id suffix of the flags: the ablation and the skill suffixes.
func (c *PiSetup) ablationSuffix() string {
	return AblationRunIDSuffix(c.ForceDelegation) + SkillRunIDSuffix(c.GlobalSkills != nil, c.WithoutSkill)
}

// WithoutAblations returns a copy of the setup with the flags off and no resolved AGENTS.md. The
// judge uses it: it grades the agent under test and must not run with its ablation.
func (c *PiSetup) WithoutAblations() *PiSetup {
	judge := *c
	judge.ForceDelegation = false
	judge.ablationAgents = nil
	judge.GlobalSkills = nil
	judge.WithoutSkill = ""
	return &judge
}

// Resolve checks the flags and reads AGENTS.md with the profile applied to compute the text
// --force-delegation writes into it. It must run before Setup; Setup fails otherwise.
func (c *PiSetup) Resolve(ctx context.Context) error {
	if err := c.validateAblations(); err != nil {
		return err
	}
	if c.WithoutSkill != "" {
		// A missing skill would be removed as nothing, and the arm would not be an ablation.
		found, err := c.GlobalSkills.Exists(ctx, c.WithoutSkill+"/SKILL.md", dagger.DirectoryExistsOpts{ExpectedType: dagger.ExistsTypeRegularType})
		if err != nil {
			return err
		}
		if !found {
			return fmt.Errorf("--without-skill=%s: --global-skills has no %s/SKILL.md", c.WithoutSkill, c.WithoutSkill)
		}
	}
	if !c.ForceDelegation {
		return nil
	}
	agents, err := c.effectiveFile(ctx, "AGENTS.md")
	if err != nil {
		return err
	}
	forced := appendForcedDelegation(agents)
	c.ablationAgents = &forced
	return nil
}

// applyAblations writes the AGENTS.md Resolve computed into the container of an eval, after
// profile.sh has applied the profile.
func (c *PiSetup) applyAblations(container *dagger.Container) *dagger.Container {
	if !c.ForceDelegation {
		return container
	}
	if c.ablationAgents == nil {
		panic("PiSetup: Resolve must run before Setup when an ablation flag is set")
	}
	return container.WithNewFile(piAgentDir+"/AGENTS.md", *c.ablationAgents)
}

// applySkills stages the global skills at ~/.agents/skills, without the skill --without-skill names.
func (c *PiSetup) applySkills(container *dagger.Container) *dagger.Container {
	if c.GlobalSkills == nil {
		return container
	}
	skills := c.GlobalSkills
	if c.WithoutSkill != "" {
		skills = skills.WithoutDirectory(c.WithoutSkill)
	}
	return container.WithDirectory(globalSkillsDir, skills)
}

// appendForcedDelegation appends forcedDelegationSection to AGENTS.md text after a blank line.
func appendForcedDelegation(text string) string {
	base := text
	if base != "" && !strings.HasSuffix(base, "\n") {
		base += "\n"
	}
	if base != "" {
		base += "\n"
	}
	return base + forcedDelegationSection
}

// effectiveFile is the text of an agent file as profile.sh leaves it: the profile's file when the
// profile has one, else the workspace root file.
func (c *PiSetup) effectiveFile(ctx context.Context, name string) (string, error) {
	if c.Profile == "" || strings.Contains(c.Profile, "/") || strings.HasPrefix(c.Profile, ".") {
		return "", fmt.Errorf("invalid profile name %q", c.Profile)
	}
	path := "profiles/" + c.Profile + "/" + name
	overlaid, err := c.Workspace.Exists(ctx, path, dagger.DirectoryExistsOpts{ExpectedType: dagger.ExistsTypeRegularType})
	if err != nil {
		return "", err
	}
	if !overlaid {
		path = name
	}
	return c.Workspace.File(path).Contents(ctx)
}

package setup

import (
	"context"
	"fmt"
	"strings"

	"dagger/daggervals/internal/dagger"
)

// The ablation flags of Evals. --force-delegation is the only one and is off by default; with it
// off the setup, the run id and the setup id are the ones without the flag.
//
//   - --force-delegation appends the mandatory delegation section to AGENTS.md.

// piAgentDir is the agent directory in the eval container.
const piAgentDir = "/root/.pi/agent"

// forcedDelegationSection is appended to AGENTS.md by --force-delegation.
const forcedDelegationSection = "## Mandatory delegation (evaluation ablation)\n\n" +
	"For any task that edits code, the parent must delegate the implementation to a `worker` subagent " +
	"and have a `reviewer` subagent check the result before finishing. The final report must say that both happened.\n"

// AblationRunIDSuffix is appended to the run id: @forced with forceDelegation; empty when it is off.
func AblationRunIDSuffix(forceDelegation bool) string {
	if forceDelegation {
		return "@forced"
	}
	return ""
}

// setupIdInput is what GetSetupId hashes: the workspace files hash, the env content, the profile
// and the ablation suffix. The suffix is empty with the flag off, so the input is unchanged.
func setupIdInput(filesHash, envContent, profile string, forceDelegation bool) string {
	return filesHash + envContent + profile + AblationRunIDSuffix(forceDelegation)
}

// checkAblationFlags rejects a baked image with an ablation: it carries none.
func checkAblationFlags(forceDelegation, bake bool) error {
	if bake && forceDelegation {
		return fmt.Errorf("--force-delegation is eval-only: a baked agent image (PiSetup.Bake) cannot carry it")
	}
	return nil
}

// validateAblations checks the flags of the setup (see checkAblationFlags).
func (c *PiSetup) validateAblations() error {
	return checkAblationFlags(c.ForceDelegation, c.Bake)
}

// WithoutAblations returns a copy of the setup with the flags off and no resolved AGENTS.md. The
// judge uses it: it grades the agent under test and must not run with its ablation.
func (c *PiSetup) WithoutAblations() *PiSetup {
	judge := *c
	judge.ForceDelegation = false
	judge.ablationAgents = nil
	return &judge
}

// Resolve reads AGENTS.md with the profile applied and computes the text --force-delegation
// writes into it. It must run before Setup when the flag is set; Setup fails otherwise.
func (c *PiSetup) Resolve(ctx context.Context) error {
	if err := c.validateAblations(); err != nil {
		return err
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

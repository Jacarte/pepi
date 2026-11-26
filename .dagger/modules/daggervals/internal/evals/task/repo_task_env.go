package task

import (
	"context"
	"fmt"
	"strings"

	"dagger/daggervals/internal/dagger"
)

// seedGitScript commits the seed so GetFinalDiff can diff against it.
const seedGitScript = `git init -q && git config user.name seed && git config user.email seed@example.invalid && git add -A && git commit -q -m seed`

// tasksMountPath is where the tasks repo checkout is mounted in the grading container.
const tasksMountPath = "/tasks"

// RepoTask is the Environment of a task from the tasks repo. Only the task's seed/ goes
// into /testbed: the agent never gets hidden/, task.yaml, milestones.yaml or the repo.
type RepoTask struct {
	GoTask
	Seed *dagger.Directory
}

// PrepareEnvForTesting installs the task's Go, puts the seed at /testbed and commits it.
func (t RepoTask) PrepareEnvForTesting(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return t.withGoToolchain(container).
			WithDirectory(goWorkdir, t.Seed).
			WithWorkdir(goWorkdir).
			WithExec([]string{"bash", "-c", seedGitScript})
	}
}

// RepoTestEnv only records the base tree; the hidden tests are used only at grading.
type RepoTestEnv struct{}

func (RepoTestEnv) Script(ctx context.Context) (string, error) {
	return fmt.Sprintf("cd %s\ngit add -A\ngit write-tree > %s\n", goWorkdir, BaseTreeFile), nil
}

func seedPath(name string) string { return "tasks/" + name + "/seed" }

func taskPath(name string) string { return "tasks/" + name }

// NewRepoFixTask builds the FixTask of a task. tasksTree is the whole tasks repo checkout;
// base is the plain image the grader starts from.
func NewRepoFixTask(spec RepoTaskSpec, tasksTree *dagger.Directory, base *dagger.Container) FixTask {
	fixTask := FixTask{
		ProblemStatement: spec.ProblemStatement(),
		Env: RepoTask{
			GoTask: GoTask{Repo: taskPath(spec.Name), GoVersion: spec.GoVersion},
			Seed:   tasksTree.Directory(seedPath(spec.Name)),
		},
		TestEnv: RepoTestEnv{},
		Oracle: RepoTaskOracle{
			Spec:      spec,
			TasksRepo: tasksTree,
			Base:      base,
		},
		TimeoutSeconds: spec.TimeoutSeconds(),
		MemoryFixture:  spec.MemoryFixture,
	}
	if len(spec.Prompts) > 1 {
		fixTask.SessionPrompts = spec.Prompts
	}
	return fixTask
}

// RepoTaskOracle grades the hidden milestones in a fresh container.
type RepoTaskOracle struct {
	Spec RepoTaskSpec
	// TasksRepo is mounted only in the grading container.
	TasksRepo *dagger.Directory
	// Base is the plain image: no agent, no secrets.
	Base *dagger.Container
}

func (o RepoTaskOracle) Scoring() string { return o.Spec.Scoring }

// ValidateFinalAnswer is true when every milestone passes.
func (o RepoTaskOracle) ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error) {
	// No agent run is recorded here: skill_loaded milestones fail and skill_not_loaded ones pass.
	// The engine grades with the skills its agent loaded.
	results, err := o.GradeMilestones(ctx, container, nil)
	if err != nil {
		return false, err
	}
	for _, r := range results {
		if !r.Passed {
			return false, nil
		}
	}
	return true, nil
}

// GradeMilestones grades the agent's /testbed (without .git) and the skill milestones from skillsLoaded.
func (o RepoTaskOracle) GradeMilestones(ctx context.Context, container *dagger.Container, skillsLoaded []string) ([]MilestoneResult, error) {
	return o.GradeWorkspace(ctx, container.Directory(goWorkdir).WithoutDirectory(".git"), skillsLoaded)
}

const (
	// gradeCheckTimeoutSeconds bounds one milestone check; go test itself times out at 10
	// minutes per package. gradeWarmupTimeoutSeconds bounds the build-cache warm-up.
	// gradeKillAfterSeconds is the grace period before SIGKILL, so code that ignores SIGTERM
	// cannot hang the grader.
	gradeCheckTimeoutSeconds  = 900
	gradeWarmupTimeoutSeconds = 600
	gradeKillAfterSeconds     = 30
	// gradeMaxWorkspaceKiB is the largest agent workspace that is graded.
	gradeMaxWorkspaceKiB = 200 * 1024
	// gradeGoCache is the build cache handed to the unprivileged user that runs the agent's
	// code; the warm-up fills it as that user.
	gradeGoCache = "/var/lib/grade-gocache"
)

// Exit codes of precheckScript.
const (
	precheckSymlinks = 10
	precheckSpecial  = 11
	precheckTooLarge = 12
	precheckControl  = 13
)

// precheckScript looks at the copied workspace before anything else touches it: symlinks and
// special files (a symlinked test file is compiled by go but invisible to the scripts' find
// of regular files), names with control characters (a newline splits a name in two in the
// scripts' lists, so the cheat scan would miss the file) and size. It runs no agent code.
var precheckScript = fmt.Sprintf(`cd /work || exit 99
links=$(find . -type l -print | sort | head -n 20 | tr '\n' ' ')
if [ -n "$links" ]; then echo "$links"; exit %d; fi
special=$(find . ! -type d ! -type f -print | sort | head -n 20 | tr '\n' ' ')
if [ -n "$special" ]; then echo "$special"; exit %d; fi
control=$(find . -name '*[[:cntrl:]]*' -exec bash -c 'printf "%%q\n" "$@"' _ {} + | head -n 20 | tr '\n' ' ')
if [ -n "$control" ]; then echo "$control"; exit %d; fi
kib=$(du -sk . | cut -f1)
if [ "$kib" -gt %d ]; then echo "$kib"; exit %d; fi
`, precheckSymlinks, precheckSpecial, precheckControl, gradeMaxWorkspaceKiB, precheckTooLarge)

// unprivileged runs a command as the user that agent code runs as in the grading container.
var unprivileged = []string{"setpriv", "--no-new-privs", "--reuid=65534", "--regid=65534", "--clear-groups"}

// gradingBase is the grading container before the workspace is copied in. Nothing of the
// agent's container is in it.
func (o RepoTaskOracle) gradingBase() *dagger.Container {
	goTask := GoTask{GoVersion: o.Spec.GoVersion}
	return o.Base.
		WithExec([]string{"env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "build-essential", "gawk", "util-linux"}).
		With(goTask.withGoToolchain).
		// The scripts run the agent's code as uid 65534 with setpriv: fail here if it is missing.
		WithExec(append(append([]string{}, unprivileged...), "sh", "-c", `[ "$(id -u)" = 65534 ]`)).
		WithEnvVariable("GRADE_GOCACHE", gradeGoCache).
		WithMountedDirectory(tasksMountPath, o.TasksRepo)
}

// warmUp compiles the standard library and the workspace once, as the unprivileged user, so
// the milestone execs reuse the Go build cache. It never fails the grading.
func warmUp(container *dagger.Container) *dagger.Container {
	script := fmt.Sprintf(`mkdir -p %[1]s /tmp/grade-home /tmp/grade-gopath && chown -R 65534:65534 %[1]s /tmp/grade-home /tmp/grade-gopath
timeout -k %[2]d %[3]d %[4]s env HOME=/tmp/grade-home GOPATH=/tmp/grade-gopath GOCACHE=%[1]s bash -c 'cd /work && go build std; go build ./...' || true
`, gradeGoCache, gradeKillAfterSeconds, gradeWarmupTimeoutSeconds, strings.Join(unprivileged, " "))
	return container.WithExec([]string{"bash", "-c", script})
}

// rejectAll fails every milestone with the same detail: the workspace was refused before
// any milestone ran.
func (o RepoTaskOracle) rejectAll(detail string) []MilestoneResult {
	results := make([]MilestoneResult, len(o.Spec.Milestones))
	for i, m := range o.Spec.Milestones {
		results[i] = MilestoneResult{ID: m.ID, Title: m.Title, Detail: detail}
	}
	return results
}

// GradeWorkspace runs every go test milestone with scripts/check-milestone.sh, each in its own
// exec from the same prepared container, so a milestone cannot affect another; a skill milestone
// is graded from skillsLoaded without an exec. A refused workspace fails every milestone.
// Errors are harness problems, never an agent failure.
func (o RepoTaskOracle) GradeWorkspace(ctx context.Context, workspace *dagger.Directory, skillsLoaded []string) ([]MilestoneResult, error) {
	taskDir := tasksMountPath + "/" + taskPath(o.Spec.Name)
	withWork := o.gradingBase().WithDirectory("/work", workspace).WithWorkdir("/work")

	pre := withWork.WithExec([]string{"bash", "-c", precheckScript}, dagger.ContainerWithExecOpts{Expect: dagger.ReturnTypeAny})
	preCode, err := pre.ExitCode(ctx)
	if err != nil {
		return nil, fmt.Errorf("precheck workspace for %s: %w", o.Spec.Name, err)
	}
	preOut, err := pre.Stdout(ctx)
	if err != nil {
		return nil, fmt.Errorf("precheck workspace for %s: %w", o.Spec.Name, err)
	}
	detail, rejected, err := PrecheckDetail(preCode, preOut)
	if err != nil {
		return nil, fmt.Errorf("precheck workspace for %s: %w", o.Spec.Name, err)
	}
	if rejected {
		return o.rejectAll(detail), nil
	}

	prep := withWork.WithExec([]string{"bash", tasksMountPath + "/scripts/prepare-workspace.sh", taskDir, "/work"},
		dagger.ContainerWithExecOpts{Expect: dagger.ReturnTypeAny})
	prepCode, err := prep.ExitCode(ctx)
	if err != nil {
		return nil, fmt.Errorf("prepare grading workspace for %s: %w", o.Spec.Name, err)
	}
	prepOut, err := prep.Stdout(ctx)
	if err != nil {
		return nil, fmt.Errorf("prepare grading workspace for %s: %w", o.Spec.Name, err)
	}
	switch prepCode {
	case 0:
	case 1: // prepare-workspace.sh refused the workspace (REJECT line)
		return o.rejectAll(failureLine(prepOut)), nil
	default:
		return nil, fmt.Errorf("prepare grading workspace for %s: prepare-workspace.sh exit %d: %s", o.Spec.Name, prepCode, failureLine(prepOut))
	}
	prepared, err := warmUp(prep).Sync(ctx)
	if err != nil {
		return nil, fmt.Errorf("prepare grading workspace for %s: %w", o.Spec.Name, err)
	}

	results := make([]MilestoneResult, 0, len(o.Spec.Milestones))
	for _, m := range o.Spec.Milestones {
		if skill, isSkill, err := ParseSkillCheck(m.Command); isSkill {
			if err != nil {
				return nil, fmt.Errorf("grade %s %s: %w", o.Spec.Name, m.ID, err)
			}
			results = append(results, skill.Grade(m, skillsLoaded))
			continue
		}
		check := prepared.WithExec(
			[]string{"sh", "-c", fmt.Sprintf(`timeout -k %d %d bash "$@" 2>&1`, gradeKillAfterSeconds, gradeCheckTimeoutSeconds), "check-milestone",
				tasksMountPath + "/scripts/check-milestone.sh", taskDir, "/work", m.ID},
			dagger.ContainerWithExecOpts{Expect: dagger.ReturnTypeAny})
		exitCode, err := check.ExitCode(ctx)
		if err != nil {
			return nil, fmt.Errorf("grade %s %s: %w", o.Spec.Name, m.ID, err)
		}
		output, err := check.Stdout(ctx)
		if err != nil {
			return nil, fmt.Errorf("grade %s %s: %w", o.Spec.Name, m.ID, err)
		}
		result, err := MilestoneFromCheck(m, exitCode, output)
		if err != nil {
			return nil, err
		}
		results = append(results, result)
	}
	return results, nil
}

// AgentFiles lists the files of the task that go into /testbed.
func (t RepoTask) AgentFiles(ctx context.Context) ([]string, error) {
	return t.Seed.Glob(ctx, "**/*")
}

// DirSource reads a tasks repo checkout held as a Dagger directory.
type DirSource struct {
	Ctx context.Context
	Dir *dagger.Directory
}

func (d DirSource) ReadFile(path string) (string, error) {
	return d.Dir.File(path).Contents(d.Ctx)
}

func (d DirSource) ListDir(path string) ([]string, error) {
	return d.Dir.Entries(d.Ctx, dagger.DirectoryEntriesOpts{Path: path})
}

var _ MilestoneOracle = RepoTaskOracle{}

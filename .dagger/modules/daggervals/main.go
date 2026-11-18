package main

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"dagger/daggervals/internal/dagger"
	cache "dagger/daggervals/internal/evals/cache"
	"dagger/daggervals/internal/evals/engine"
	"dagger/daggervals/internal/evals/llm"
	judge "dagger/daggervals/internal/evals/llm-judge"
	"dagger/daggervals/internal/evals/memory"

	"dagger/daggervals/internal/evals/setup"
	"dagger/daggervals/internal/evals/task"
)

type Daggervals struct {
	Source *dagger.Directory
	Auth   *dagger.Secret
	// HippocampusEnvDir holds the workspace's .hippocampus.env, if there is one. It is never in
	// Source: only the memory server gets it.
	// +private
	HippocampusEnvDir *dagger.Directory
}

// hippocampusEnvFile is the default env file of the memory server, at the workspace root.
const hippocampusEnvFile = ".hippocampus.env"

func New(
	// The current workspace, auto-populated by Dagger.
	ws *dagger.Workspace,
) *Daggervals {
	return &Daggervals{
		HippocampusEnvDir: ws.Directory("/", dagger.WorkspaceDirectoryOpts{Include: []string{hippocampusEnvFile}}),
		Source: ws.Directory("/", dagger.WorkspaceDirectoryOpts{
			Exclude: []string{
				"logs",
				"tmp",
				".jj",
				".git",
				"npm",
				"missions",
				"sessions",
				"web-search-cache",
				"bin",
				"auth.json",
				"evals_cache",
				hippocampusEnvFile,
			},
		}),
	}
}

type EvalResult struct {
	Log   []string
	Cache *dagger.Directory
}

func (m *Daggervals) Hello(
	ctx context.Context,
	auth *dagger.Secret,
	agentEnv *dagger.File,
	agentProfile string,
	// Env file of the Hippocampus memory server (LLM gateway credentials); only that server gets it. Default: the workspace's .hippocampus.env when it exists, else no memory service.
	// +optional
	hippocampusEnv *dagger.File,
	// Run with the memory disabled in the agent (no mem0 tool, no automatic recall or capture) and no memory service.
	// +optional
	noHippocampus bool,
	// Image tag of jacarte/mem0server. Default: the pinned c8f33b2.
	// +optional
	hippocampusRef string,
) (string, error) {
	memoryConfig, err := m.memoryConfig(ctx, hippocampusEnv, noHippocampus, hippocampusRef, true)
	if err != nil {
		return "", err
	}
	rootContainer, err := rootImage().Sync(ctx)
	if err != nil {
		return "", err
	}

	evalEngine := engine.NewEngine(
		llm.NewPiAgent(&setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile, Memory: memoryConfig.Mode()}),
		[]task.FixTask{task.HelloWorldTask},
		nil,
	)
	evalEngine.Memory = memoryConfig
	evalEngine.AgentEnv = agentEnv.AsEnvFile()
	outcomes, err := evalEngine.Run(ctx, rootContainer, "noul", agentProfile)
	if err != nil {
		return "", err
	}
	return outcomes[0].Output, nil
}

func (m *Daggervals) Evals(
	ctx context.Context,
	auth *dagger.Secret,
	agentEnv *dagger.File,
	agentProfile string,
	cacheDir *dagger.Directory,
	// +optional
	fixed string,
	// Task suite to run: rust (default), go, all or tasks. tasks cannot be combined with the others.
	// +optional
	suite string,
	// Git repo of the tasks suite (suite=tasks). Default: https://github.com/Jacarte/pepi-eval-tasks.git
	// +optional
	tasksRepo string,
	// Branch or commit sha of the tasks repo; required with suite=tasks. The resolved sha is part of the run id.
	// +optional
	tasksRef string,
	// Comma-separated names of the tasks to run (suite=tasks). Empty means every supported task.
	// +optional
	taskNames string,
	// Token to fetch a private tasks repo (suite=tasks). Unset means an anonymous clone. Only the git fetch uses it.
	// +optional
	tasksToken *dagger.Secret,
	// How many times to run the whole eval, one after the other (1 to 20). Run i is cached as <id>-<i><suffixes>, so the runs plug into compare-runs.
	// +optional
	// +default=1
	runs int,
	// Env file of the Hippocampus memory server (LLM gateway credentials); only that server gets it. Default: the workspace's .hippocampus.env when it exists, else no memory service.
	// +optional
	hippocampusEnv *dagger.File,
	// Run with the memory disabled in the agent (no mem0 tool, no automatic recall or capture) and no memory service. The run id gets @nomem.
	// +optional
	noHippocampus bool,
	// Image tag of jacarte/mem0server. Default: the pinned c8f33b2. The tag and the image digest are part of the run id.
	// +optional
	hippocampusRef string,
	// Seed the task's memory/fixture.json into the memory server before the agent starts. false adds @nofx to the run id.
	// +optional
	// +default=true
	memoryFixture bool,
) (*EvalResult, error) {
	if err := engine.ValidateRuns(runs); err != nil {
		return nil, err
	}
	if err := task.ValidateTasksSuiteArgs(suite, tasksRepo, tasksRef, taskNames); err != nil {
		return nil, err
	}
	memoryConfig, err := m.memoryConfig(ctx, hippocampusEnv, noHippocampus, hippocampusRef, memoryFixture)
	if err != nil {
		return nil, err
	}
	var tasks []task.FixTask
	if suite != "tasks" {
		var err error
		if tasks, err = evalSuiteTasks(suite); err != nil {
			return nil, err
		}
	}
	rootContainer, err := rootImage().Sync(ctx)
	if err != nil {
		return nil, err
	}
	var repoSuite *tasksSuite
	if suite == "tasks" {
		repoSuite, err = loadTasksSuite(ctx, tasksRepo, tasksRef, taskNames, tasksToken, rootContainer)
		if err != nil {
			return nil, err
		}
		if len(repoSuite.Tasks) == 0 {
			return nil, fmt.Errorf("no task in the tasks repo")
		}
		tasks = repoSuite.Tasks
	}
	piSetup := setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile, Memory: memoryConfig.Mode()}
	piAgent := llm.NewPiAgent(&piSetup)

	baseID := fixed
	if baseID == "" {
		// A hash of the workspace files and the profile.
		baseID, err = piSetup.GetSetupId(ctx, rootContainer)
		if err != nil {
			return nil, err
		}
	}
	idSuffix := ""
	if repoSuite != nil {
		idSuffix = repoSuite.RunIDSuffix
	} else if suite != "" && suite != "rust" {
		idSuffix = "@" + suite
	}
	idSuffix += memoryConfig.RunIDSuffix()

	result := &EvalResult{Cache: cacheDir}
	for i := 1; i <= runs; i++ {
		one, err := evalOnce(ctx, piAgent, piSetup.Env, memoryConfig, tasks, rootContainer, agentProfile, result.Cache, engine.RunID(baseID, i, runs, idSuffix))
		if err != nil {
			return nil, err
		}
		result.Cache = one.Cache
		if runs > 1 {
			result.Log = append(result.Log, fmt.Sprintf("## run %d/%d", i, runs))
		}
		result.Log = append(result.Log, one.Log...)
	}
	return result, nil
}

// evalOnce runs the eval and the judge once under runId. The returned cache directory is
// cacheDir plus this run's entry and eval report.
func evalOnce(ctx context.Context, piAgent *llm.PiAgent, agentEnv *dagger.EnvFile, memoryConfig *memory.Config, tasks []task.FixTask, rootContainer *dagger.Container, agentProfile string, cacheDir *dagger.Directory, runId string) (*EvalResult, error) {
	evalCache := cache.NewGHCache(cacheDir, ctx)
	evalEngine := engine.NewEngine(piAgent, tasks, evalCache)
	evalEngine.Memory = memoryConfig
	evalEngine.AgentEnv = agentEnv

	outcomes, err := evalEngine.Run(ctx, rootContainer, runId, agentProfile)
	if err != nil {
		return nil, err
	}
	judge := &judge.Judge{Agent: piAgent}
	judgedEvals, err := judge.Judge(ctx, outcomes, rootContainer)
	if err != nil {
		return nil, err
	}

	joinedEvals := strings.Join(judgedEvals, "\n")
	return &EvalResult{
		Cache: evalCache.GetDirectory().WithNewFile(runId+".eval.md", joinedEvals),
		Log:   judgedEvals,
	}, nil
}

// memoryConfig resolves the memory flags of Evals and Hello. Without hippocampusEnv the
// workspace's .hippocampus.env is used when it exists.
func (m *Daggervals) memoryConfig(ctx context.Context, hippocampusEnv *dagger.File, noHippocampus bool, hippocampusRef string, memoryFixture bool) (*memory.Config, error) {
	if hippocampusEnv != nil && noHippocampus {
		return nil, fmt.Errorf("hippocampusEnv and noHippocampus are mutually exclusive")
	}
	if hippocampusEnv == nil && !noHippocampus {
		exists, err := m.HippocampusEnvDir.Exists(ctx, hippocampusEnvFile)
		if err != nil {
			return nil, err
		}
		if exists {
			hippocampusEnv = m.HippocampusEnvDir.File(hippocampusEnvFile)
		}
	}
	return memory.Resolve(ctx, hippocampusEnv, noHippocampus, hippocampusRef, memoryFixture)
}

// evalSuiteTasks maps the suite argument of Evals to its tasks. Empty means rust.
func evalSuiteTasks(suite string) ([]task.FixTask, error) {
	switch suite {
	case "", "rust":
		return task.RustTasks, nil
	case "go":
		return task.GoTasks, nil
	case "all":
		return append(append([]task.FixTask{}, task.RustTasks...), task.GoTasks...), nil
	default:
		return nil, fmt.Errorf("unknown suite %q: want rust, go, all or tasks", suite)
	}
}

// tasksSuite is the result of loading the tasks repo at a resolved commit.
type tasksSuite struct {
	Tasks       []task.FixTask
	RunIDSuffix string
	// Tree is the whole checkout of the repo, without .git.
	Tree *dagger.Directory
}

// checkoutTasksRepo resolves tasksRef (a branch or a sha) to a commit and returns that
// commit's tree, so everything read afterwards belongs to the sha recorded in the run id.
// The token, if any, is only given to the git fetch: it never reaches a container, the run
// id or the cache.
func checkoutTasksRepo(ctx context.Context, tasksRepo, tasksRef string, tasksToken *dagger.Secret) (sha string, tree *dagger.Directory, err error) {
	if tasksRepo == "" {
		tasksRepo = task.DefaultTasksRepo
	}
	if strings.TrimSpace(tasksRef) == "" {
		return "", nil, fmt.Errorf("tasksRef is required")
	}
	var gitOpts []dagger.GitOpts
	if tasksToken != nil {
		gitOpts = append(gitOpts, dagger.GitOpts{HTTPAuthUsername: "x-access-token", HTTPAuthToken: tasksToken})
	}
	repo := dag.Git(tasksRepo, gitOpts...)
	// NoLock: do not pin the branch in dagger.lock.
	sha, err = repo.Ref(tasksRef, dagger.GitRepositoryRefOpts{NoLock: true}).CommitSHA(ctx)
	if err != nil {
		return "", nil, fmt.Errorf("resolve %s of %s: %w", tasksRef, tasksRepo, err)
	}
	return sha, repo.Commit(sha).Tree(dagger.GitCommitTreeOpts{DiscardGitDir: true}), nil
}

// tasksTree is the tasks repo checkout of GradeTask and TaskAgentFiles: tasksDir when given,
// otherwise the clone of tasksRepo at tasksRef. The first result names the commit.
func tasksTree(ctx context.Context, tasksRepo, tasksRef string, tasksToken *dagger.Secret, tasksDir *dagger.Directory) (commit string, tree *dagger.Directory, err error) {
	if tasksDir != nil {
		return "local checkout (tasksDir)", tasksDir.WithoutDirectory(".git"), nil
	}
	return checkoutTasksRepo(ctx, tasksRepo, tasksRef, tasksToken)
}

// loadTasksSuite loads the tasks of the repo at tasksRef. base is the plain image
// the grading container is built from.
func loadTasksSuite(ctx context.Context, tasksRepo, tasksRef, taskNames string, tasksToken *dagger.Secret, base *dagger.Container) (*tasksSuite, error) {
	names, err := task.ParseTaskNames(taskNames)
	if err != nil {
		return nil, err
	}
	sha, tree, err := checkoutTasksRepo(ctx, tasksRepo, tasksRef, tasksToken)
	if err != nil {
		return nil, err
	}
	suffix, err := task.TasksRunIDSuffix(sha, names)
	if err != nil {
		return nil, err
	}
	specs, err := task.LoadRepoTasks(task.DirSource{Ctx: ctx, Dir: tree}, names)
	if err != nil {
		return nil, err
	}
	suite := &tasksSuite{RunIDSuffix: suffix, Tree: tree}
	for _, spec := range specs {
		suite.Tasks = append(suite.Tasks, task.NewRepoFixTask(spec, tree, base))
	}
	return suite, nil
}

// GradeTask grades a workspace with the hidden milestones of one task, without an agent
// or an LLM, using the grader of the tasks suite. testbed plays the role of the agent's
// /testbed. It returns one line per milestone ("m1-x PASS" or "m1-x FAIL: detail"), a score
// line and the tasks commit.
func (m *Daggervals) GradeTask(
	ctx context.Context,
	taskName string,
	// The files the agent leaves in /testbed. Not called workspace: dagger reserves that flag.
	testbed *dagger.Directory,
	// +optional
	tasksRepo string,
	// Branch or commit sha of the tasks repo. Required unless tasksDir is set.
	// +optional
	tasksRef string,
	// Token for a private tasks repo; unset means an anonymous clone.
	// +optional
	tasksToken *dagger.Secret,
	// A local checkout of the tasks repo to use instead of cloning, for developing tasks before they are pushed.
	// +optional
	tasksDir *dagger.Directory,
) ([]string, error) {
	sha, tree, err := tasksTree(ctx, tasksRepo, tasksRef, tasksToken, tasksDir)
	if err != nil {
		return nil, err
	}
	spec, err := task.LoadRepoTask(task.DirSource{Ctx: ctx, Dir: tree}, taskName)
	if err != nil {
		return nil, err
	}
	oracle := task.RepoTaskOracle{Spec: spec, TasksRepo: tree, Base: rootImage()}
	results, err := oracle.GradeWorkspace(ctx, testbed.WithoutDirectory(".git"))
	if err != nil {
		return nil, err
	}
	lines, err := task.FormatMilestoneLines(spec.Scoring, results)
	if err != nil {
		return nil, err
	}
	return append(lines, "tasks commit "+sha), nil
}

// TaskAgentFiles lists the files a task puts in the agent's /testbed (the seed), to check
// that hidden/, task.yaml and milestones.yaml never reach the agent.
func (m *Daggervals) TaskAgentFiles(
	ctx context.Context,
	taskName string,
	// +optional
	tasksRepo string,
	// Branch or commit sha of the tasks repo. Required unless tasksDir is set.
	// +optional
	tasksRef string,
	// Token for a private tasks repo; unset means an anonymous clone.
	// +optional
	tasksToken *dagger.Secret,
	// A local checkout of the tasks repo to use instead of cloning.
	// +optional
	tasksDir *dagger.Directory,
) ([]string, error) {
	_, tree, err := tasksTree(ctx, tasksRepo, tasksRef, tasksToken, tasksDir)
	if err != nil {
		return nil, err
	}
	spec, err := task.LoadRepoTask(task.DirSource{Ctx: ctx, Dir: tree}, taskName)
	if err != nil {
		return nil, err
	}
	fixTask := task.NewRepoFixTask(spec, tree, rootImage())
	return fixTask.Env.(task.RepoTask).AgentFiles(ctx)
}

func (m *Daggervals) TestCacheWrite(ctx context.Context, cacheDir *dagger.Directory) *dagger.Directory {
	cache := cache.NewGHCache(cacheDir, ctx)
	// Test the cache by writing and reading a sample outcome
	sampleOutcome := []engine.TaskOutcome{
		{
			Task:             "test-task",
			ProblemStatement: "Test problem statement",
			Valid:            true,
			ValidationError:  "",
			GoldenPatch:      "Sample golden patch",
			FinalDiff:        "Sample final diff",
		},
	}

	runId := "test-run-id"

	err := cache.Set(runId, sampleOutcome)
	if err != nil {
		return nil
	}

	return cache.GetDirectory()
}

func (m *Daggervals) CompareEvals(ctx context.Context, auth *dagger.Secret, agentEnv *dagger.File, agentProfile string, before *dagger.File, after *dagger.File) ([]string, error) {
	rootImage, err := rootImage().Sync(ctx)
	if err != nil {
		return nil, err
	}
	judge := &judge.Judge{Agent: llm.NewPiAgent(&setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile})}
	beforeOutcomes, err := readOutcomes(ctx, before)
	if err != nil {
		return nil, err
	}
	afterOutcomes, err := readOutcomes(ctx, after)
	if err != nil {
		return nil, err
	}
	return judge.Compare2Evals(ctx, beforeOutcomes, afterOutcomes, rootImage)
}

// readOutcomes parses an eval cache file.
func readOutcomes(ctx context.Context, file *dagger.File) ([]engine.TaskOutcome, error) {
	contents, err := file.Contents(ctx)
	if err != nil {
		return nil, err
	}
	var outcomes []engine.TaskOutcome
	if err := json.Unmarshal([]byte(contents), &outcomes); err != nil {
		return nil, fmt.Errorf("not an eval cache file ([]TaskOutcome JSON): %w", err)
	}
	return outcomes, nil
}

// CompareRuns compares two or more eval cache files, as CompareEvals does for two. labels
// names the arm of each file, comma-separated, one per file; files with the same label are
// repetitions of one arm, and the first label is the baseline. Empty labels name the files
// run1, run2, ... The aggregation is done in Go (see judge.PrepareCompareRuns). With dryRun
// the model is not called: the result is the aggregate and the full prompt, and auth,
// agentEnv and agentProfile are not needed.
func (m *Daggervals) CompareRuns(
	ctx context.Context,
	// +optional
	auth *dagger.Secret,
	// +optional
	agentEnv *dagger.File,
	// +optional
	agentProfile string,
	files []*dagger.File,
	// +optional
	labels string,
	// +optional
	dryRun bool,
) ([]string, error) {
	names, err := judge.ParseLabels(labels, len(files))
	if err != nil {
		return nil, err
	}
	runs := make([]judge.RunInput, len(files))
	for i, file := range files {
		outcomes, err := readOutcomes(ctx, file)
		if err != nil {
			return nil, fmt.Errorf("file %d (%s): %w", i+1, names[i], err)
		}
		runs[i] = judge.RunInput{Label: names[i], Outcomes: outcomes}
	}
	if dryRun {
		prepared, err := judge.PrepareCompareRuns(runs)
		if err != nil {
			return nil, err
		}
		return prepared.DryRunLines(), nil
	}
	if auth == nil || agentEnv == nil || agentProfile == "" {
		return nil, fmt.Errorf("compare-runs needs auth, agentEnv and agentProfile unless dryRun is set")
	}
	rootImage, err := rootImage().Sync(ctx)
	if err != nil {
		return nil, err
	}
	judge := &judge.Judge{Agent: llm.NewPiAgent(&setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile})}
	return judge.CompareRuns(ctx, runs, rootImage)
}

// GenerateImage returns a container with pi and the selected profile baked in.
// It never receives auth.json: mount it at run time. Export it with
// `export --path image.tar` or push it with `publish --address ...`.
func (m *Daggervals) GenerateImage(ctx context.Context, agentProfile string) (*dagger.Container, error) {
	rootContainer, err := rootImage("gh").Sync(ctx)
	if err != nil {
		return nil, err
	}

	piSetup := &setup.PiSetup{Workspace: m.Source, Profile: agentProfile, Bake: true}
	return rootContainer.
		With(piSetup.Setup()).
		WithEntrypoint([]string{"pi"}), nil
}

// rootImage is the base image shared by every function: ubuntu with git, curl, the network
// capture tools, extraPackages and Node.js 24.
func rootImage(extraPackages ...string) *dagger.Container {
	packages := append([]string{"git", "curl", "tcpdump", "tshark"}, extraPackages...)
	return dag.Container().
		From("ubuntu:24.04").
		WithExec([]string{"apt-get", "update", "--fix-missing"}).
		WithExec(append([]string{"env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y"}, packages...)).
		WithExec([]string{"curl", "-fsSL", "https://deb.nodesource.com/setup_24.x", "-o", "nodesource_setup.sh"}).
		WithExec([]string{"bash", "nodesource_setup.sh"}).
		WithExec([]string{"apt-get", "install", "-y", "nodejs"})
}

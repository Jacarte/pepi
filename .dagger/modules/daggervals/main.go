package main

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"dagger/daggervals/internal/dagger"
	cache "dagger/daggervals/internal/evals/cache"
	"dagger/daggervals/internal/evals/ci"
	"dagger/daggervals/internal/evals/engine"
	"dagger/daggervals/internal/evals/llm"
	judge "dagger/daggervals/internal/evals/llm-judge"
	"dagger/daggervals/internal/evals/memory"
	"dagger/daggervals/internal/evals/recall"

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

// workspaceExcludes are left out of Source; other checkouts get the same filter so their
// setup id is computed from the same files.
var workspaceExcludes = []string{
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
}

func New(
	// The current workspace, auto-populated by Dagger.
	ws *dagger.Workspace,
) *Daggervals {
	return &Daggervals{
		HippocampusEnvDir: ws.Directory("/", dagger.WorkspaceDirectoryOpts{Include: []string{hippocampusEnvFile}}),
		Source:            ws.Directory("/", dagger.WorkspaceDirectoryOpts{Exclude: workspaceExcludes}),
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

// RecallInjection checks a Pi session transcript for the context cost of Hippocampus recall:
// no memory injected twice, recall only on the turns recallEveryNTurns allows, and the size of
// what was injected. It runs offline on the transcript; it does not start an agent.
func (m *Daggervals) RecallInjection(
	ctx context.Context,
	// Pi session transcript (JSONL) of the parent session.
	transcript *dagger.File,
	// Recall gate of the run: recall on turns 1, N+1, 2N+1, ... (1 to 50).
	// +default=1
	recallEveryNTurns int,
) (string, error) {
	text, err := transcript.Contents(ctx)
	if err != nil {
		return "", fmt.Errorf("read transcript: %w", err)
	}
	report, err := recall.Analyze(text, recallEveryNTurns)
	if err != nil {
		return "", err
	}
	out, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return "", err
	}
	return string(out), nil
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
	// Append the mandatory delegation section to the agent's AGENTS.md (implementation by a worker, review by a reviewer). The run id gets @forced.
	// +optional
	forceDelegation bool,
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
	piSetup := setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile, Memory: memoryConfig.Mode(), ForceDelegation: forceDelegation}
	if err := piSetup.Resolve(ctx); err != nil {
		return nil, err
	}
	piAgent := llm.NewPiAgent(&piSetup)
	// The judge grades the agent under test; it never runs with the ablation flags.
	judgeAgent := llm.NewPiAgent(piSetup.WithoutAblations())

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
	idSuffix += setup.AblationRunIDSuffix(forceDelegation)
	idSuffix += memoryConfig.RunIDSuffix()

	result := &EvalResult{Cache: cacheDir}
	for i := 1; i <= runs; i++ {
		one, err := evalOnce(ctx, piAgent, piSetup.Env, memoryConfig, tasks, rootContainer, agentProfile, result.Cache, engine.RunID(baseID, i, runs, idSuffix), judgeAgent)
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

// evalOnce runs the agent under test (piAgent) and the judge (judgeAgent) once under runId. The
// returned cache directory is cacheDir plus this run's entry and eval report.
func evalOnce(ctx context.Context, piAgent *llm.PiAgent, agentEnv *dagger.EnvFile, memoryConfig *memory.Config, tasks []task.FixTask, rootContainer *dagger.Container, agentProfile string, cacheDir *dagger.Directory, runId string, judgeAgent *llm.PiAgent) (*EvalResult, error) {
	evalCache := cache.NewGHCache(cacheDir, ctx)
	evalEngine := engine.NewEngine(piAgent, tasks, evalCache)
	evalEngine.Memory = memoryConfig
	evalEngine.AgentEnv = agentEnv

	outcomes, err := evalEngine.Run(ctx, rootContainer, runId, agentProfile)
	if err != nil {
		return nil, err
	}
	judge := &judge.Judge{Agent: judgeAgent}
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

// CiEvalRun runs one shard of a CI eval tier (fast: rust, go, tasks-memory; full: the same
// plus tasks-long) from an empty cache, with memory on, as often as the tier says (fast 1,
// full 3). It returns the shard's result files, named <ci-tier-shard>[-<run>]<suffixes>
// plus a .eval.md judge report each, for ci-eval-report. It fails instead when a file holds
// a secret shape or a value of auth or of a secret-named variable of agentEnv or
// hippocampusEnv (the error names the file and the rule only).
func (m *Daggervals) CiEvalRun(
	ctx context.Context,
	auth *dagger.Secret,
	agentEnv *dagger.File,
	agentProfile string,
	// Env file of the Hippocampus memory server: every CI shard runs with memory on.
	hippocampusEnv *dagger.File,
	// fast or full.
	tier string,
	// rust, go, tasks-memory or (full only) tasks-long.
	shard string,
	// Full commit sha of the tasks repo, required by the tasks-* shards; rust and go ignore it.
	// +optional
	tasksRef string,
	// Git repo of the tasks suite. Default: https://github.com/Jacarte/pepi-eval-tasks.git
	// +optional
	tasksRepo string,
) (*dagger.Directory, error) {
	t, err := ci.LookupTier(tier)
	if err != nil {
		return nil, err
	}
	s, err := t.Shard(shard)
	if err != nil {
		return nil, err
	}
	var repo, ref, names string
	if s.Suite == "tasks" {
		if err := ci.ValidateSha("tasksRef", tasksRef); err != nil {
			return nil, err
		}
		repo, ref, names = tasksRepo, tasksRef, strings.Join(s.TaskNames, ",")
	}
	result, err := m.Evals(ctx, auth, agentEnv, agentProfile, dag.Directory(), t.FixedName(s), s.Suite, repo, ref, names, nil, t.Runs, hippocampusEnv, false, "", true, false)
	if err != nil {
		return nil, err
	}
	files, err := readFlatDir(ctx, result.Cache)
	if err != nil {
		return nil, err
	}
	// A change of the run id scheme fails here, before the report has to read the files.
	for name := range files {
		if _, err := t.ParseResultName(name, ref); err != nil {
			return nil, err
		}
	}
	secretValues, err := evalSecretValues(ctx, auth, agentEnv, hippocampusEnv)
	if err != nil {
		return nil, err
	}
	if err := ci.ScanSecrets(files, secretValues); err != nil {
		return nil, fmt.Errorf("%w: shard results not published", err)
	}
	return result.Cache, nil
}

// CiEvalReport merges the result files of every shard of a tier per run, compares them with
// the tier's baseline of the PR's base when they are comparable, and returns judge.md and
// compare.md (the sticky PR comment bodies), summary.md (the job summary) and status.json.
// The results, the compare-runs answer and the bodies are scanned for secrets (shapes and the
// values of auth and agentEnv): on a hit the bodies are withheld and infra_ok is false. With
// dryRun the comparison calls no model and auth is not needed.
func (m *Daggervals) CiEvalReport(
	ctx context.Context,
	// +optional
	auth *dagger.Secret,
	// The agent env file of the shards: part of the setup hashes.
	agentEnv *dagger.File,
	agentProfile string,
	// The files of every ci-eval-run shard, in one directory.
	results *dagger.Directory,
	// Checkout of the PR's base: its setup hash, .eval.track and eval-baseline/<tier>/.
	baseSource *dagger.Directory,
	tier string,
	prNumber int,
	// The PR head sha the shards evaluated; the workspace must be that checkout.
	evaluatedSha string,
	// Full commit sha of the tasks repo the shards ran with.
	tasksRef string,
	// Git repo of the tasks suite. Default: https://github.com/Jacarte/pepi-eval-tasks.git
	// +optional
	tasksRepo string,
	// URL of the workflow run, linked from the truncation notes.
	// +optional
	runUrl string,
	// +optional
	dryRun bool,
) (*dagger.Directory, error) {
	t, err := ci.LookupTier(tier)
	if err != nil {
		return nil, err
	}
	if err := ci.ValidatePR(prNumber); err != nil {
		return nil, err
	}
	if err := ci.ValidateSha("evaluatedSha", evaluatedSha); err != nil {
		return nil, err
	}
	if err := ci.ValidateSha("tasksRef", tasksRef); err != nil {
		return nil, err
	}
	if runUrl != "" && !runURLPattern.MatchString(runUrl) {
		return nil, fmt.Errorf("runUrl %q is not a GitHub Actions run URL", runUrl)
	}
	if tasksRepo == "" {
		tasksRepo = task.DefaultTasksRepo
	}
	files, err := readFlatDir(ctx, results)
	if err != nil {
		return nil, err
	}
	pr, err := ci.MergeResults(t, tasksRef, agentProfile, files)
	if err != nil {
		return nil, err
	}
	rootContainer, err := rootImage().Sync(ctx)
	if err != nil {
		return nil, err
	}
	headHash, err := setupHash(ctx, m.Source, agentEnv, agentProfile, rootContainer)
	if err != nil {
		return nil, err
	}
	base := baseSource.Filter(dagger.DirectoryFilterOpts{Exclude: workspaceExcludes})
	baseHash, err := setupHash(ctx, base, agentEnv, agentProfile, rootContainer)
	if err != nil {
		return nil, err
	}
	trackText, err := optionalContents(ctx, base, ci.TrackFile)
	if err != nil {
		return nil, err
	}
	baselineFiles := map[string]string{}
	if exists, err := base.Exists(ctx, ci.BaselineDir(t.Name), dagger.DirectoryExistsOpts{ExpectedType: dagger.ExistsTypeDirectoryType}); err != nil {
		return nil, err
	} else if exists {
		if baselineFiles, err = readFlatDir(ctx, base.Directory(ci.BaselineDir(t.Name))); err != nil {
			return nil, err
		}
	}
	facts := ci.EvalFacts{TasksRepo: tasksRepo, TasksSha: tasksRef, Profile: agentProfile}
	report := ci.Report{
		PR: prNumber, EvaluatedSha: evaluatedSha, Facts: facts, Results: pr,
		Comparison: ci.Compare(pr, facts, trackText, baselineFiles, baseHash),
		SetupHash:  headHash, BaseSetupHash: baseHash, RunURL: runUrl, Inputs: files,
	}
	if report.SecretValues, err = evalSecretValues(ctx, auth, agentEnv, nil); err != nil {
		return nil, err
	}
	if report.Comparison.Comparable {
		runs := append(report.Comparison.Baseline.RunInputs("base"), pr.RunInputs("pr")...)
		if dryRun {
			prepared, err := judge.PrepareCompareRuns(runs)
			if err != nil {
				return nil, err
			}
			report.CompareLines = prepared.DryRunLines()
		} else {
			if auth == nil {
				return nil, fmt.Errorf("ci-eval-report needs auth unless dryRun is set")
			}
			judge := &judge.Judge{Agent: llm.NewPiAgent(&setup.PiSetup{Workspace: m.Source, Auth: auth, Env: agentEnv.AsEnvFile(), Profile: agentProfile})}
			if report.CompareLines, err = judge.CompareRuns(ctx, runs, rootContainer); err != nil {
				return nil, err
			}
		}
	}
	rendered, err := report.Render()
	if err != nil {
		return nil, err
	}
	return directoryOf(rendered), nil
}

var runURLPattern = regexp.MustCompile(`^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+(/attempts/[0-9]+)?$`)

// CiSetupHash is the setup id that Evals uses as run id without --fixed, of source (default:
// the workspace) with the same files left out. .github, eval-baseline and .eval.track are not
// part of it; the content of agentEnv is.
func (m *Daggervals) CiSetupHash(
	ctx context.Context,
	agentEnv *dagger.File,
	agentProfile string,
	// +optional
	source *dagger.Directory,
) (string, error) {
	if source == nil {
		source = m.Source
	} else {
		source = source.Filter(dagger.DirectoryFilterOpts{Exclude: workspaceExcludes})
	}
	rootContainer, err := rootImage().Sync(ctx)
	if err != nil {
		return "", err
	}
	return setupHash(ctx, source, agentEnv, agentProfile, rootContainer)
}

// CiBaselinePrepare checks the results of a merged PR's eval and returns them laid out for
// the repo root: eval-baseline/<tier>/<files> and .eval.track with this tier's entry
// replaced. It takes no secret: the exact secret values are searched by ci-eval-run and
// ci-eval-report, here only the secret shapes. results and status must come from the
// approved eval run of the PR head: status.json is not signed. It refuses when the evaluated
// head is not the PR head at merge time, the eval was incomplete, a file name, type or size
// is not allowed, or a file looks like it holds a secret (the error names the file and the
// rule only).
func (m *Daggervals) CiBaselinePrepare(
	ctx context.Context,
	// The files of every ci-eval-run shard, in one directory, as the report read them.
	results *dagger.Directory,
	// status.json of ci-eval-report: setup hash, tasks commit, profile and evaluated head.
	status *dagger.File,
	tier string,
	prNumber int,
	// The PR head sha at merge time.
	prHeadSha string,
	mergeSha string,
	// The baseline date, YYYY-MM-DD.
	date string,
	// The current .eval.track. Default: the workspace's, when it exists.
	// +optional
	track *dagger.File,
) (*dagger.Directory, error) {
	t, err := ci.LookupTier(tier)
	if err != nil {
		return nil, err
	}
	entries, err := listFlatDir(ctx, results)
	if err != nil {
		return nil, err
	}
	if err := ci.CheckBaselineEntries(entries); err != nil {
		return nil, err
	}
	files, err := readFiles(ctx, results, entries)
	if err != nil {
		return nil, err
	}
	statusText, err := status.Contents(ctx)
	if err != nil {
		return nil, err
	}
	parsedStatus, err := ci.ParseStatus(statusText)
	if err != nil {
		return nil, err
	}
	trackText := ""
	if track != nil {
		trackText, err = track.Contents(ctx)
	} else {
		trackText, err = optionalContents(ctx, m.Source, ci.TrackFile)
	}
	if err != nil {
		return nil, err
	}
	out, err := ci.Baseline{
		Tier: t, Status: parsedStatus, PR: prNumber, PRHeadSha: prHeadSha, MergeSha: mergeSha, Date: date,
		Files: files, Track: trackText,
	}.Prepare()
	if err != nil {
		return nil, err
	}
	return directoryOf(out), nil
}

// evalSecretValues are the values the secret scan searches for: auth.json and the
// secret-named variables of the env files (see ci.SecretValues). Any argument may be nil.
func evalSecretValues(ctx context.Context, auth *dagger.Secret, envFiles ...*dagger.File) ([]string, error) {
	var vars []ci.EnvVar
	for _, file := range envFiles {
		if file == nil {
			continue
		}
		fileVars, err := file.AsEnvFile().Variables(ctx)
		if err != nil {
			return nil, fmt.Errorf("read env file: %w", err)
		}
		for _, v := range fileVars {
			name, err := v.Name(ctx)
			if err != nil {
				return nil, fmt.Errorf("read env file: %w", err)
			}
			value, err := v.Value(ctx)
			if err != nil {
				return nil, fmt.Errorf("read env file: %w", err)
			}
			vars = append(vars, ci.EnvVar{Name: name, Value: value})
		}
	}
	authText := ""
	if auth != nil {
		var err error
		if authText, err = auth.Plaintext(ctx); err != nil {
			return nil, fmt.Errorf("read auth: %w", err)
		}
	}
	return ci.SecretValues(vars, authText), nil
}

// setupHash is the setup id of Evals for source.
func setupHash(ctx context.Context, source *dagger.Directory, agentEnv *dagger.File, agentProfile string, rootContainer *dagger.Container) (string, error) {
	piSetup := &setup.PiSetup{Workspace: source, Env: agentEnv.AsEnvFile(), Profile: agentProfile}
	return piSetup.GetSetupId(ctx, rootContainer)
}

// listFlatDir lists dir without following symlinks; the result directories are flat.
func listFlatDir(ctx context.Context, dir *dagger.Directory) ([]ci.Entry, error) {
	names, err := dir.Entries(ctx)
	if err != nil {
		return nil, err
	}
	entries := make([]ci.Entry, 0, len(names))
	for _, name := range names {
		stat, err := dir.Stat(ctx, name, dagger.DirectoryStatOpts{DoNotFollowSymlinks: true})
		if err != nil {
			return nil, err
		}
		fileType, err := stat.FileType(ctx)
		if err != nil {
			return nil, err
		}
		entry := ci.Entry{Name: strings.TrimSuffix(name, "/"), Regular: fileType == dagger.FileTypeRegular || fileType == dagger.FileTypeRegularType}
		if entry.Regular {
			if entry.Size, err = dir.File(name).Size(ctx); err != nil {
				return nil, err
			}
		}
		entries = append(entries, entry)
	}
	return entries, nil
}

// readFlatDir reads every file of dir by name; a directory or symlink in it is an error.
func readFlatDir(ctx context.Context, dir *dagger.Directory) (map[string]string, error) {
	entries, err := listFlatDir(ctx, dir)
	if err != nil {
		return nil, err
	}
	return readFiles(ctx, dir, entries)
}

// readFiles reads the listed entries of dir, which must be regular files.
func readFiles(ctx context.Context, dir *dagger.Directory, entries []ci.Entry) (map[string]string, error) {
	var err error
	files := make(map[string]string, len(entries))
	for _, e := range entries {
		if !e.Regular {
			return nil, fmt.Errorf("result %q is not a regular file (directory or symlink)", e.Name)
		}
		if files[e.Name], err = dir.File(e.Name).Contents(ctx); err != nil {
			return nil, err
		}
	}
	return files, nil
}

// optionalContents is the content of the file at path in dir, empty when there is none.
func optionalContents(ctx context.Context, dir *dagger.Directory, path string) (string, error) {
	exists, err := dir.Exists(ctx, path, dagger.DirectoryExistsOpts{ExpectedType: dagger.ExistsTypeRegularType})
	if err != nil || !exists {
		return "", err
	}
	return dir.File(path).Contents(ctx)
}

// directoryOf builds a directory of files by path.
func directoryOf(files map[string]string) *dagger.Directory {
	dir := dag.Directory()
	for path, content := range files {
		dir = dir.WithNewFile(path, content)
	}
	return dir
}

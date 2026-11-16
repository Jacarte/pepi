package task

// here we will have const as tasks, and the agent will be able to run them

import (
	"context"
	"dagger/daggervals/internal/dagger"
	"embed"
	"fmt"
	"regexp"
	"strings"
)

// rustWorkdir is on the rootfs, so it survives the measured exec (unlike the mounted workspace).
const rustWorkdir = "/testbed"

// rustBaseTreeFile holds the tree of the repo right after the harness applied the test
// patch. The final diff is taken against it, so it contains only the agent's changes.
const rustBaseTreeFile = "/tmp/daggervals-base-tree"

// RustTask is the Environment of a Rust repo pinned at a commit.
type RustTask struct {
	Repo   string
	Commit string
}

func (c RustTask) GetRepo() string   { return c.Repo }
func (c RustTask) GetCommit() string { return c.Commit }

func (RustTask) PrepareEnv(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return container.
			WithExec([]string{"env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "build-essential", "pkg-config"}).
			WithExec([]string{
				"sh", "-c",
				"curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.97.0",
			}).
			WithEnvVariable("PATH", "/root/.cargo/bin:$PATH", dagger.ContainerWithEnvVariableOpts{Expand: true}).
			WithEnvVariable("CARGO_BUILD_JOBS", "2").
			WithEnvVariable("CARGO_PROFILE_DEV_DEBUG", "0").
			WithEnvVariable("CARGO_PROFILE_TEST_DEBUG", "0").
			WithEnvVariable("CARGO_INCREMENTAL", "0")
	}
}

// GetFinalDiff returns what the agent changed, including new files, and excludes the
// test patch that the harness applied before the agent started.
func (c RustTask) GetFinalDiff(ctx context.Context, container *dagger.Container) string {
	diff, err := container.
		WithWorkdir(rustWorkdir).
		WithExec([]string{"bash", "-c", "git add -A && git diff --cached \"$(cat " + rustBaseTreeFile + ")\""}).
		Stdout(ctx)
	if err != nil {
		return fmt.Sprintf("Error getting final diff: %v", err)
	}
	return diff
}

// PrepareEnvForTesting clones the repo at the task commit. The failing tests
// are applied later by RustTestEnv, inside the measured exec.
func (c RustTask) PrepareEnvForTesting(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return container.
			WithExec([]string{"git", "clone", c.Repo, rustWorkdir}).
			WithWorkdir(rustWorkdir).
			WithExec([]string{"git", "checkout", c.Commit, "--"})
	}
}

// RustTestEnv applies the test patch (the failing tests) before the agent runs.
type RustTestEnv struct {
	TestPatch string
}

func (e RustTestEnv) Script(ctx context.Context) (string, error) {
	const eof = "EOF_RUST_TEST_PATCH"
	if strings.Contains(e.TestPatch, eof) {
		return "", fmt.Errorf("test patch contains heredoc delimiter %s", eof)
	}
	patch := strings.TrimRight(e.TestPatch, "\n")
	return fmt.Sprintf("cd %s\ngit apply -v - <<'%s'\n%s\n%s\ngit add -A\ngit write-tree > %s\n",
		rustWorkdir, eof, patch, eof, rustBaseTreeFile), nil
}

// RustTaskOracle re-runs the task's EvalTest script after the agent finished.
type RustTaskOracle struct {
	EvalTest string
}

func (o RustTaskOracle) ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error) {
	// The eval script does not fail on test failures, so the output decides.
	stdout, err := container.
		WithExec([]string{"bash", "-c", o.EvalTest}, dagger.ContainerWithExecOpts{Expect: dagger.ReturnTypeAny}).
		Stdout(ctx)
	if err != nil {
		return false, err
	}

	return checkRustTests(stdout)
}

var cargoTestFailed = regexp.MustCompile(`test result: FAILED|\.\.\. FAILED|error(\[E\d+\])?: could not compile`)

// checkRustTests is the pure part of the oracle: cargo must have run tests, and none failed.
func checkRustTests(stdout string) (bool, error) {
	if m := cargoTestFailed.FindString(stdout); m != "" {
		return false, fmt.Errorf("RustTaskOracle: cargo test failed (%q)", m)
	}
	if !strings.Contains(stdout, "test result: ok") {
		return false, fmt.Errorf("RustTaskOracle: no 'test result: ok' in cargo output: %s", stdout)
	}
	return true, nil
}

// An array of tasks
var RustTasks = []FixTask{
	{
		ProblemStatement: readOrError("rust/r1/problem.md"),
		EvalTest:         readOrError("rust/r1/eval.sh"),
		GoldenPatch:      readOrError("rust/r1/golden.patch"),
		Env: RustTask{
			Repo:   "https://github.com/tokio-rs/axum.git",
			Commit: "23808f72a2c00c314cedea40a75b73954402a148",
		},
		TestEnv: RustTestEnv{TestPatch: readOrError("rust/r1/test.patch")},
		Oracle:  RustTaskOracle{EvalTest: readOrError("rust/r1/eval.sh")},
	},
	{
		ProblemStatement: readOrError("rust/r2/problem.md"),
		EvalTest:         readOrError("rust/r2/eval.sh"),
		GoldenPatch:      readOrError("rust/r2/golden.patch"),
		/*nushell/nushell
		cc9f41e553333b1ad0aa4185a912f7a52355a238*/
		Env: RustTask{
			Repo:   "https://github.com/nushell/nushell.git",
			Commit: "cc9f41e553333b1ad0aa4185a912f7a52355a238",
		},
		TestEnv: RustTestEnv{TestPatch: readOrError("rust/r2/test.patch")},
		Oracle:  RustTaskOracle{EvalTest: readOrError("rust/r2/eval.sh")},
	},
}

//go:embed rust
var taskFiles embed.FS

func readOrError(file string) string {
	data, err := taskFiles.ReadFile(file)
	if err != nil {
		panic(err)
	}
	return string(data)
}

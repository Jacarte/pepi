package task

import (
	"context"
	"dagger/daggervals/internal/dagger"
	"embed"
	"fmt"
	"regexp"
	"strings"
)

const goWorkdir = "/testbed"

// GoTask is the Environment of a Go repo pinned at a commit.
type GoTask struct {
	Repo      string
	Commit    string
	GoVersion string
}

func (c GoTask) GetRepo() string   { return c.Repo }
func (c GoTask) GetCommit() string { return c.Commit }

// PrepareEnv only installs system packages: the engine builds it once from the first
// task and shares it, so the per-task Go toolchain is installed in PrepareEnvForTesting.
func (c GoTask) PrepareEnv(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return container.
			WithExec([]string{"env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "build-essential"})
	}
}

// installGoScript downloads the official Go tarball for the container arch.
func (c GoTask) installGoScript() string {
	return fmt.Sprintf(
		`case "$(uname -m)" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo "unsupported arch $(uname -m)" >&2; exit 1;; esac
curl -fsSL "https://go.dev/dl/go%s.linux-${arch}.tar.gz" | tar -C /usr/local -xz`,
		c.GoVersion)
}

// GetFinalDiff returns what the agent changed, including new files, and excludes the
// test patch that the harness applied before the agent started.
func (c GoTask) GetFinalDiff(ctx context.Context, container *dagger.Container) string {
	diff, err := container.
		WithWorkdir(goWorkdir).
		WithExec([]string{"bash", "-c", "git add -A && git diff --cached \"$(cat " + BaseTreeFile + ")\""}).
		Stdout(ctx)
	if err != nil {
		return fmt.Sprintf("Error getting final diff: %v", err)
	}
	return diff
}

// withGoToolchain installs this task's Go and the environment that makes go use exactly
// that version. GOTOOLCHAIN=local never downloads another one.
func (c GoTask) withGoToolchain(container *dagger.Container) *dagger.Container {
	return container.
		WithExec([]string{"sh", "-c", c.installGoScript()}).
		WithEnvVariable("GOROOT", "/usr/local/go").
		WithEnvVariable("GOPATH", "/root/go").
		WithEnvVariable("PATH", "/usr/local/go/bin:/root/go/bin:$PATH", dagger.ContainerWithEnvVariableOpts{Expand: true}).
		WithEnvVariable("GOTOOLCHAIN", "local").
		WithEnvVariable("GOFLAGS", "-p=2").
		WithEnvVariable("GOMAXPROCS", "2")
}

// PrepareEnvForTesting installs this task's Go and clones the repo at the task commit.
// The failing tests are applied later by GoTestEnv, inside the measured exec.
func (c GoTask) PrepareEnvForTesting(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return c.withGoToolchain(container).
			WithExec([]string{"git", "clone", c.Repo, goWorkdir}).
			WithWorkdir(goWorkdir).
			WithExec([]string{"git", "checkout", c.Commit, "--"})
	}
}

// GoTestEnv applies the test patch (the failing tests) before the agent runs.
type GoTestEnv struct {
	TestPatch string
}

func (e GoTestEnv) Script(ctx context.Context) (string, error) {
	const eof = "EOF_GO_TEST_PATCH"
	if strings.Contains(e.TestPatch, eof) {
		return "", fmt.Errorf("test patch contains heredoc delimiter %s", eof)
	}
	patch := strings.TrimRight(e.TestPatch, "\n")
	return fmt.Sprintf("cd %s\ngit apply -v - <<'%s'\n%s\n%s\ngit add -A\ngit write-tree > %s\n",
		goWorkdir, eof, patch, eof, BaseTreeFile), nil
}

// GoTaskOracle re-runs the task's go test command after the agent finished.
// Setup is the part of eval.sh that restores the test files, so the agent cannot
// pass by editing the tests.
type GoTaskOracle struct {
	Setup    string
	EvalTest string
}

const (
	goOutputStart = ">>>>> Start Go Test Output"
	goOutputEnd   = ">>>>> End Go Test Output"
)

func (o GoTaskOracle) script() string {
	return o.Setup + "echo '" + goOutputStart + "'\n" + o.EvalTest + "\necho '" + goOutputEnd + "'\n"
}

func (o GoTaskOracle) ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error) {
	// go test exits non-zero on failures, so the output decides.
	stdout, err := container.
		WithWorkdir(goWorkdir).
		WithExec([]string{"bash", "-c", o.script()}, dagger.ContainerWithExecOpts{Expect: dagger.ReturnTypeAny}).
		Stdout(ctx)
	if err != nil {
		return false, err
	}

	_, rest, found := strings.Cut(stdout, goOutputStart+"\n")
	output, _, ended := strings.Cut(rest, goOutputEnd)
	if !found || !ended {
		return false, fmt.Errorf("GoTaskOracle: test output markers missing: %s", stdout)
	}
	return checkGoTests(output)
}

var (
	goTestFailed = regexp.MustCompile(`(?m)^(FAIL\b|\s*--- FAIL:|panic: )|\[build failed\]|\[setup failed\]|\[no tests to run\]|testing: warning: no tests to run`)
	goPackageOK  = regexp.MustCompile(`(?m)^ok\s`)
	goTestPassed = regexp.MustCompile(`(?m)^\s*--- PASS:`)
)

// checkGoTests is the pure part of the oracle (stdout of go test -v): a package must
// have passed, at least one test must have run, and nothing failed.
func checkGoTests(stdout string) (bool, error) {
	if m := goTestFailed.FindString(stdout); m != "" {
		return false, fmt.Errorf("GoTaskOracle: go test failed (%q)", m)
	}
	if !goPackageOK.MatchString(stdout) {
		return false, fmt.Errorf("GoTaskOracle: no 'ok' package line in go test output: %s", stdout)
	}
	if !goTestPassed.MatchString(stdout) {
		return false, fmt.Errorf("GoTaskOracle: no '--- PASS:' line, no test ran: %s", stdout)
	}
	return true, nil
}

// GoTasks mirrors RustTasks. Go versions come from go.mod at the pinned commit,
// except where noted.
var GoTasks = []FixTask{
	goFixture("g1", "https://github.com/hashicorp/terraform.git", "24833a2b062126a71ef30a86a6d1fc3823afdfdb", "1.22.5"), // terraform-35543 import blocks in validate
	goFixture("g2", "https://github.com/hashicorp/terraform.git", "f2e4cb411ed6dfd36fd4f9960a74d8f29c7b8882", "1.22.0"), // terraform-34900 sensitive nested set
	// go.mod says go 1.12, which is only the minimum language version. The pinned dependencies
	// were built and tested (fails before, passes after the golden patch) with a modern
	// compiler, which is easier to install and download modules with than a 2019 toolchain.
	goFixture("g3", "https://github.com/gin-gonic/gin.git", "09a3650c97ca7ef3a542d428a9fb2c8da8c18002", "1.22.5"), // gin-1957 bind header
	// quic-go v0.36.1 only builds with go 1.19 and 1.20, so a newer compiler would not work here.
	goFixture("g4", "https://github.com/caddyserver/caddy.git", "0e2c7e1d35b287fc0e56d6db2951f791e09b5a37", "1.19.13"), // caddy-5626 nested import
	// go.mod has "go 1.22.0" with "toolchain go1.23.0"; with GOTOOLCHAIN=local we install the toolchain line.
	goFixture("g5", "https://github.com/prometheus/prometheus.git", "16bba78f1549cfd7909b61ebd7c55c822c86630b", "1.23.0"), // prometheus-15142 AppendHistogram race
}

const (
	goTestStartMarker = ": '>>>>> Start Test Output'\n"
	goTestEndMarker   = "\n: '>>>>> End Test Output'"
)

// goFixture builds a FixTask from golang/<dir>/{problem.md,eval.sh,golden.patch,test.patch}.
func goFixture(dir, repo, commit, goVersion string) FixTask {
	base := "golang/" + dir + "/"
	setup, evalTest := splitGoEvalScript(readGoOrError(base + "eval.sh"))
	return FixTask{
		ProblemStatement: readGoOrError(base + "problem.md"),
		EvalTest:         evalTest,
		GoldenPatch:      readGoOrError(base + "golden.patch"),
		Env:              GoTask{Repo: repo, Commit: commit, GoVersion: goVersion},
		TestEnv:          GoTestEnv{TestPatch: readGoOrError(base + "test.patch")},
		Oracle:           GoTaskOracle{Setup: setup, EvalTest: evalTest},
	}
}

// splitGoEvalScript returns the eval.sh part before the test output markers and the
// test command between them. From the first part it drops git status, show and diff,
// which only print the agent's diff.
func splitGoEvalScript(evalScript string) (setup, command string) {
	before, rest, found := strings.Cut(evalScript, goTestStartMarker)
	if !found {
		panic("eval.sh has no start test output marker")
	}
	command, _, found = strings.Cut(rest, goTestEndMarker)
	if !found {
		panic("eval.sh has no end test output marker")
	}
	var kept []string
	for _, line := range strings.SplitAfter(before, "\n") {
		trimmed := strings.TrimRight(line, "\n")
		if trimmed == "git status" || trimmed == "git show" || strings.HasPrefix(trimmed, "git -c core.fileMode=false diff") {
			continue
		}
		kept = append(kept, line)
	}
	return strings.Join(kept, ""), command
}

//go:embed golang
var goTaskFiles embed.FS

func readGoOrError(file string) string {
	data, err := goTaskFiles.ReadFile(file)
	if err != nil {
		panic(err)
	}
	return string(data)
}

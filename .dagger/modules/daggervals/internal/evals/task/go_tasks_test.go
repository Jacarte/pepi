package task

import (
	"context"
	"strings"
	"testing"
)

var goTaskCases = []struct {
	dir, repoSlug, commit, goVersion, testCommand string
}{
	{"g1", "hashicorp/terraform", "24833a2b062126a71ef30a86a6d1fc3823afdfdb", "1.22.5", `go test -v ./internal/terraform -run "^TestContext2Plan_import"`},
	{"g2", "hashicorp/terraform", "f2e4cb411ed6dfd36fd4f9960a74d8f29c7b8882", "1.22.0", `go test -v ./internal/terraform -run "(^TestContext2Apply|^TestContext2Plan).*[Ss]ensitive"`},
	{"g3", "gin-gonic/gin", "09a3650c97ca7ef3a542d428a9fb2c8da8c18002", "1.22.5", `go test -v . -run "^TestContext.*Bind"`},
	{"g4", "caddyserver/caddy", "0e2c7e1d35b287fc0e56d6db2951f791e09b5a37", "1.19.13", `go test -v ./caddyconfig/httpcaddyfile -run "Test.*Import"`},
	{"g5", "prometheus/prometheus", "16bba78f1549cfd7909b61ebd7c55c822c86630b", "1.23.0", `go test -v ./tsdb -run "^TestHead"`},
}

func TestGoFixtureFilesExistAndAreNotEmpty(t *testing.T) {
	for _, tc := range goTaskCases {
		for _, file := range []string{"eval.sh", "golden.patch", "problem.md", "repo.txt", "test.patch"} {
			data, err := goTaskFiles.ReadFile("golang/" + tc.dir + "/" + file)
			if err != nil || len(strings.TrimSpace(string(data))) == 0 {
				t.Errorf("%s/%s: err=%v len=%d", tc.dir, file, err, len(data))
			}
		}
	}
}

func TestGoTasksMatchFixtures(t *testing.T) {
	if len(GoTasks) != len(goTaskCases) {
		t.Fatalf("got %d tasks, want %d", len(GoTasks), len(goTaskCases))
	}
	for i, tc := range goTaskCases {
		repoTxt := strings.Fields(readGoOrError("golang/" + tc.dir + "/repo.txt"))
		if len(repoTxt) != 2 || repoTxt[0] != tc.repoSlug || repoTxt[1] != tc.commit {
			t.Fatalf("%s: repo.txt %q does not match %s@%s", tc.dir, repoTxt, tc.repoSlug, tc.commit)
		}
		task := GoTasks[i]
		env, ok := task.Env.(GoTask)
		if !ok {
			t.Fatalf("%s: Env is %T", tc.dir, task.Env)
		}
		if env.GetRepo() != "https://github.com/"+repoTxt[0]+".git" || env.GetCommit() != repoTxt[1] || env.GoVersion != tc.goVersion {
			t.Errorf("%s: env %+v", tc.dir, env)
		}
		if !strings.Contains(task.EvalTest, tc.testCommand) || strings.Contains(task.EvalTest, "Test Output") {
			t.Errorf("%s: EvalTest %q lacks %q or contains the markers", tc.dir, task.EvalTest, tc.testCommand)
		}
		oracle, ok := task.Oracle.(GoTaskOracle)
		if !ok || oracle.EvalTest != task.EvalTest {
			t.Fatalf("%s: oracle %+v", tc.dir, task.Oracle)
		}
		if !strings.Contains(oracle.Setup, "git checkout "+tc.commit+" ") || !strings.Contains(oracle.Setup, "git apply -v - <<'EOF_") {
			t.Errorf("%s: oracle setup does not restore the test files:\n%s", tc.dir, oracle.Setup)
		}
		for _, informational := range []string{"git status", "git show", "diff " + tc.commit} {
			if strings.Contains(oracle.Setup, informational+"\n") {
				t.Errorf("%s: oracle setup still prints %q", tc.dir, informational)
			}
		}
		testEnv, ok := task.TestEnv.(GoTestEnv)
		if !ok || testEnv.TestPatch != readGoOrError("golang/"+tc.dir+"/test.patch") {
			t.Errorf("%s: test env %+v", tc.dir, task.TestEnv)
		}
		if task.GoldenPatch != readGoOrError("golang/"+tc.dir+"/golden.patch") || task.ProblemStatement == "" {
			t.Errorf("%s: golden patch or problem statement mismatch", tc.dir)
		}
	}
}

func TestGoTestEnvScript(t *testing.T) {
	s, err := GoTestEnv{TestPatch: "diff --git a/x b/x\n"}.Script(context.Background())
	want := "cd /testbed\ngit apply -v - <<'EOF_GO_TEST_PATCH'\ndiff --git a/x b/x\nEOF_GO_TEST_PATCH\ngit add -A\ngit write-tree > /tmp/daggervals-base-tree\n"
	if err != nil || s != want {
		t.Fatalf("script %q err %v", s, err)
	}
}

func TestGoTestEnvScriptRefusesDelimiter(t *testing.T) {
	_, err := GoTestEnv{TestPatch: "+EOF_GO_TEST_PATCH\n"}.Script(context.Background())
	if err == nil || !strings.Contains(err.Error(), "EOF_GO_TEST_PATCH") {
		t.Fatalf("err %v", err)
	}
}

func TestSplitGoEvalScript(t *testing.T) {
	script := "cd /testbed\ngit status\ngit show\ngit -c core.fileMode=false diff abc\ngit checkout abc x_test.go\n: '>>>>> Start Test Output'\n(go test ./x) | cat\n: '>>>>> End Test Output'\ngit checkout x\n"
	setup, command := splitGoEvalScript(script)
	if setup != "cd /testbed\ngit checkout abc x_test.go\n" || command != "(go test ./x) | cat" {
		t.Fatalf("setup %q command %q", setup, command)
	}
}

func TestGoTaskOracleScriptPrintsMarkersAroundTheCommand(t *testing.T) {
	got := GoTaskOracle{Setup: "cd /testbed\n", EvalTest: "go test ./x"}.script()
	want := "cd /testbed\necho '" + goOutputStart + "'\ngo test ./x\necho '" + goOutputEnd + "'\n"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestGoTaskInstallsItsOwnGoVersion(t *testing.T) {
	for i, ts := range GoTasks {
		env := ts.Env.(GoTask)
		script := env.installGoScript()
		if !strings.Contains(script, "go"+env.GoVersion+".linux-${arch}.tar.gz") {
			t.Errorf("%s: install script lacks its version %s:\n%s", goTaskCases[i].dir, env.GoVersion, script)
		}
		for j, other := range GoTasks {
			otherVersion := other.Env.(GoTask).GoVersion
			if otherVersion != env.GoVersion && strings.Contains(script, "go"+otherVersion+".") {
				t.Errorf("%s: install script mentions %s of %s", goTaskCases[i].dir, otherVersion, goTaskCases[j].dir)
			}
		}
	}
}

func TestCheckGoTests(t *testing.T) {
	cases := []struct{ name, out, wantErr string }{
		{"pass", "=== RUN   TestA\n--- PASS: TestA (0.00s)\nPASS\nok  \tgithub.com/x/y\t0.5s\n", ""},
		{"pass with passing subtest only", "    --- PASS: TestA/sub (0.00s)\nok  \tgithub.com/x/y\t0.5s\n", ""},
		{"ok with indented logged panic", "=== RUN   TestA\n    a_test.go:5: panic: recovered as expected\n    a_test.go:6: --- FAIL: not a result\n--- PASS: TestA (0.00s)\nok  \tgithub.com/x/y\t0.5s\n", ""},
		{"test failure", "--- FAIL: TestA (0.00s)\n    a_test.go:1: boom\nFAIL\nFAIL\tgithub.com/x/y\t0.5s\n", "go test failed"},
		{"subtest failure", "    --- FAIL: TestA/sub (0.00s)\n--- PASS: TestB (0.00s)\nok  \tgithub.com/x/y\t0.5s\n", "go test failed"},
		{"build failure", "# github.com/x/y [github.com/x/y.test]\n./a_test.go:3:2: undefined: Foo\nFAIL\tgithub.com/x/y [build failed]\n", "go test failed"},
		{"panic", "panic: runtime error: index out of range\n\ngoroutine 1 [running]:\n", "go test failed"},
		{"setup failed", "FAIL\tgithub.com/x/y [setup failed]\n", "go test failed"},
		{"missing package", "cannot find package \"github.com/x/z\" in any of:\n", "no 'ok' package line"},
		{"no go files", "no Go files in /testbed\n", "no 'ok' package line"},
		{"empty output", "", "no 'ok' package line"},
		{"ok then FAIL", "--- PASS: TestA (0.00s)\nok  \tgithub.com/x/a\t0.5s\nFAIL\tgithub.com/x/b\t0.5s\n", "go test failed"},
		{"ok and FAIL subtest", "ok  \tgithub.com/x/a\t0.5s\n    --- FAIL: TestB/sub (0.00s)\n", "go test failed"},
		{"ok not at line start", "--- PASS: TestA (0.00s)\n  ok  \tgithub.com/x/a\t0.5s\n", "no 'ok' package line"},
		{"no test files only", "?   \tgithub.com/x/y\t[no test files]\n", "no 'ok' package line"},
		{"no tests to run", "testing: warning: no tests to run\nPASS\nok  \tgithub.com/x/y\t0.5s [no tests to run]\n", "go test failed"},
		{"ok without any PASS line", "PASS\nok  \tgithub.com/x/y\t0.5s\n", "no '--- PASS:' line"},
	}
	for _, tc := range cases {
		ok, err := checkGoTests(tc.out)
		if tc.wantErr == "" && (err != nil || !ok) || tc.wantErr != "" && (ok || err == nil || !strings.Contains(err.Error(), tc.wantErr)) {
			t.Errorf("%s: ok=%v err=%v", tc.name, ok, err)
		}
	}
}

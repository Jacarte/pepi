package task

import (
	"context"
	"strings"
	"testing"
)

func TestCheckHelloWorld(t *testing.T) {
	for _, tc := range []struct {
		name, stdout, wantErr string
	}{
		{"valid", "Done\nHello World\n", ""},
		{"lower case", "hello world", ""},
		{"stdout without hello world", "goodbye", "stdout does not contain"},
		{"empty stdout", "", "stdout does not contain"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ok, err := checkHelloWorld(tc.stdout)
			if tc.wantErr == "" {
				if err != nil || !ok {
					t.Fatalf("want success, got ok=%v err=%v", ok, err)
				}
				return
			}
			if ok || err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("want error containing %q, got ok=%v err=%v", tc.wantErr, ok, err)
			}
		})
	}
}

func TestCheckRustTests(t *testing.T) {
	cases := []struct{ name, out, wantErr string }{
		{"ok", "running 2 tests\ntest result: ok. 2 passed; 0 failed", ""},
		{"failed", "test a ... FAILED\ntest result: FAILED. 1 passed; 1 failed", "cargo test failed"},
		{"compile error", "error[E0425]: could not compile `axum`", "cargo test failed"},
		{"no tests", "", "no 'test result: ok'"},
	}
	for _, tc := range cases {
		ok, err := checkRustTests(tc.out)
		if tc.wantErr == "" && (err != nil || !ok) || tc.wantErr != "" && (ok || err == nil || !strings.Contains(err.Error(), tc.wantErr)) {
			t.Errorf("%s: ok=%v err=%v", tc.name, ok, err)
		}
	}
}

func TestRustTestEnvScript(t *testing.T) {
	s, err := RustTestEnv{TestPatch: "diff --git a/x b/x\n"}.Script(context.Background())
	if err != nil || !strings.Contains(s, "cd /testbed\ngit apply -v - <<'EOF_RUST_TEST_PATCH'\ndiff --git a/x b/x\nEOF_RUST_TEST_PATCH\ngit add -A\ngit write-tree > /tmp/daggervals-base-tree\n") {
		t.Fatalf("script %q err %v", s, err)
	}
}

func TestHelloWorldTaskKeepsOracleContract(t *testing.T) {
	for _, want := range []string{"hello world", "AGENTS_MD:"} {
		if !strings.Contains(HelloWorldTask.ProblemStatement, want) {
			t.Errorf("problem statement lacks %q", want)
		}
	}
}

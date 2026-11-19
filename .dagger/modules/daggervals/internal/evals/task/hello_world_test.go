package task

import (
	"context"
	"strings"
	"testing"
)

const helloWorldValidLog = `
======================
2026-11-12T10:00:00.000Z session=abc-123 action=recommend
{"model":"jev-latest","answers":{"lane":{"choice":"a"}}}

======================
{"lane":"CHEAP","provider":"amazon-bedrock","id":"some-model","action":"recommend","changed":false,"escalated":false,"reason":null,"role":"worker"}
Rename one variable in one file
`

func TestCheckHelloWorld(t *testing.T) {
	failedLog := "\n======================\n2026-11-12T10:00:00.000Z recommend FAILED: TypeSafe HTTP 500\nRename one variable\n"
	escalatedLog := strings.ReplaceAll(strings.ReplaceAll(helloWorldValidLog, `"escalated":false`, `"escalated":true`), `"reason":null`, `"reason":"low_confidence"`)
	switchOnly := strings.ReplaceAll(strings.ReplaceAll(helloWorldValidLog, "action=recommend", "action=switch_self"), `"action":"recommend"`, `"action":"switch_self"`)
	cases := []struct {
		name, stdout, log, wantErr string
	}{
		{"valid", "Done\nHello World\n", helloWorldValidLog, ""},
		{"lane local", "hello world", strings.ReplaceAll(helloWorldValidLog, "CHEAP", "LOCAL"), ""},
		{"stdout without hello world", "goodbye", helloWorldValidLog, "stdout does not contain"},
		{"empty log", "hello world", " \n", "log is empty"},
		{"failed call only", "hello world", failedLog, "no 'session=<id> action=recommend' header"},
		{"wrong action", "hello world", switchOnly, "no 'session=<id> action=recommend' header"},
		{"header without decision", "hello world", "x\n2026-11-12T10:00:00Z session=s action=recommend\n{\"answers\":{}}\n", "no decision JSON line"},
		{"bad lane", "hello world", strings.ReplaceAll(helloWorldValidLog, "CHEAP", "MEDIUM"), "invalid lane"},
		{"missing provider", "hello world", strings.ReplaceAll(helloWorldValidLog, `"provider":"amazon-bedrock",`, ""), "lacks provider"},
		{"decision before header", "hello world", `{"lane":"CHEAP","provider":"p","id":"i","action":"recommend","changed":false,"escalated":false,"reason":null}` + "\n", "no 'session=<id> action=recommend' header"},
		{"escalated with string reason", "hello world", escalatedLog, ""},
		{"missing escalated", "hello world", strings.ReplaceAll(helloWorldValidLog, `"escalated":false,`, ""), "lacks escalated or reason"},
		{"escalated without reason", "hello world", strings.ReplaceAll(helloWorldValidLog, `"escalated":false`, `"escalated":true`), "want escalated === (reason !== null)"},
		{"reason without escalated", "hello world", strings.ReplaceAll(helloWorldValidLog, `"reason":null`, `"reason":"critical"`), "want escalated === (reason !== null)"},
		{"non-string reason", "hello world", strings.ReplaceAll(helloWorldValidLog, `"reason":null`, `"reason":5`), "neither a string nor null"},
		{"multiple valid calls", "hello world", helloWorldValidLog + escalatedLog, ""},
		{"later invalid call", "hello world", helloWorldValidLog + strings.ReplaceAll(escalatedLog, `"escalated":true`, `"escalated":false`), "want escalated === (reason !== null)"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ok, err := checkHelloWorld(tc.stdout, tc.log)
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
	for _, want := range []string{"jev_route", `"recommend"`, "hello world"} {
		if !strings.Contains(HelloWorldTask.ProblemStatement, want) {
			t.Errorf("problem statement lacks %q", want)
		}
	}
}

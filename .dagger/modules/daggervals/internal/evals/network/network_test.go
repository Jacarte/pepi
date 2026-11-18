package network

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestMeasureRejectsEmptyCommand(t *testing.T) {
	if _, err := Measure(nil, "", nil, 60); err == nil {
		t.Fatal("empty command must be rejected")
	}
}

func writeScript(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte("#!/bin/bash\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
}

func captureBash(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "capture.sh")
	if err := os.WriteFile(path, []byte(captureScript), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestCaptureRejectsBadTimeoutVariable(t *testing.T) {
	script := captureBash(t)
	for _, value := range []string{"0", "-5", "abc", "1;ls", "1.5", "007"} {
		cmd := exec.Command("bash", script, "nonce", "/dev/null", "/dev/null", "/tmp/unused-report", "true")
		cmd.Env = append(os.Environ(), "DAGGERVALS_TASK_TIMEOUT_SECONDS="+value)
		out, err := cmd.CombinedOutput()
		if err == nil || !strings.Contains(string(out), "must be a positive integer") {
			t.Fatalf("value %q: want rejection, got err=%v out=%s", value, err, out)
		}
	}
}

// runCapture runs capture.sh with fake tcpdump and node, only where setsid and GNU timeout
// exist (Linux; the module runs it in ubuntu). It returns the output, the report path and
// the exec error.
func runCapture(t *testing.T, timeoutSeconds string, command ...string) (out []byte, report string, err error) {
	t.Helper()
	for _, tool := range []string{"setsid", "timeout"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("%s not available", tool)
		}
	}
	if _, err := os.Stat(TimeoutMarkerPath); err == nil {
		t.Skipf("%s exists: another run is using the fixed paths", TimeoutMarkerPath)
	}
	bin := t.TempDir()
	writeScript(t, filepath.Join(bin, "tcpdump"), `echo "tcpdump: listening on any" >&2
trap 'exit 0' INT TERM
while :; do sleep 0.1; done
`)
	writeScript(t, filepath.Join(bin, "node"), "echo '{}'\n")
	setup := filepath.Join(t.TempDir(), "test-env.sh")
	if err := os.WriteFile(setup, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	report = filepath.Join(t.TempDir(), "report.json")
	cmd := exec.Command("bash", append([]string{captureBash(t), "nonce", setup, "parser.mjs", report}, command...)...)
	cmd.Env = append(os.Environ(), "PATH="+bin+":"+os.Getenv("PATH"), "DAGGERVALS_TASK_TIMEOUT_SECONDS="+timeoutSeconds)
	t.Cleanup(func() { os.Remove(TimeoutMarkerPath); os.Remove("/tmp/daggervals-process.jsonl") })
	out, err = cmd.CombinedOutput()
	return out, report, err
}

// A task that exceeds its limit still produces the report and the marker, with exit 0.
func TestCaptureTimeoutIsData(t *testing.T) {
	out, report, err := runCapture(t, "1", "sleep", "30")
	if err != nil {
		t.Fatalf("a timed out task must still exit 0: %v\n%s", err, out)
	}
	if _, err := os.Stat(TimeoutMarkerPath); err != nil {
		t.Fatalf("timeout marker missing: %v\n%s", err, out)
	}
	if _, err := os.Stat(report); err != nil {
		t.Fatalf("report missing: %v\n%s", err, out)
	}
}

// A task that exits 124 on its own, well before the limit, is a failure, and a marker it
// wrote does not survive.
func TestCaptureIgnoresForgedTimeout(t *testing.T) {
	out, _, err := runCapture(t, "60", "bash", "-c", "echo 3600 > "+TimeoutMarkerPath+"; exit 124")
	if err == nil {
		t.Fatalf("an agent that exits 124 by itself must fail the exec\n%s", out)
	}
	if _, err := os.Stat(TimeoutMarkerPath); err == nil {
		t.Fatalf("forged timeout marker survived\n%s", out)
	}
}

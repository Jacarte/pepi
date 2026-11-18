package network

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const processReportPath = "/tmp/daggervals-process.jsonl"

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

// runCapture runs capture.sh with fake tcpdump and node and an empty TestEnv, only where
// setsid and GNU timeout exist (Linux; the module runs it in ubuntu). It returns the output,
// the report path and the exec error.
func runCapture(t *testing.T, timeoutSeconds string, command ...string) (out []byte, report string, err error) {
	return runCaptureWithTestEnv(t, "", timeoutSeconds, command...)
}

func runCaptureWithTestEnv(t *testing.T, testEnv, timeoutSeconds string, command ...string) (out []byte, report string, err error) {
	t.Helper()
	for _, tool := range []string{"setsid", "timeout"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("%s not available", tool)
		}
	}
	for _, marker := range []string{TimeoutMarkerPath, AgentExitMarkerPath} {
		if _, err := os.Stat(marker); err == nil {
			t.Skipf("%s exists: another run is using the fixed paths", marker)
		}
	}
	bin := t.TempDir()
	writeScript(t, filepath.Join(bin, "tcpdump"), `echo "tcpdump: listening on any" >&2
trap 'exit 0' INT TERM
while :; do sleep 0.1; done
`)
	writeScript(t, filepath.Join(bin, "node"), "echo '{}'\n")
	setup := filepath.Join(t.TempDir(), "test-env.sh")
	if err := os.WriteFile(setup, []byte(testEnv), 0o644); err != nil {
		t.Fatal(err)
	}
	report = filepath.Join(t.TempDir(), "report.json")
	cmd := exec.Command("bash", append([]string{captureBash(t), "nonce", setup, "parser.mjs", report}, command...)...)
	cmd.Env = append(os.Environ(), "PATH="+bin+":"+os.Getenv("PATH"), "DAGGERVALS_TASK_TIMEOUT_SECONDS="+timeoutSeconds)
	t.Cleanup(func() {
		os.Remove(TimeoutMarkerPath)
		os.Remove(AgentExitMarkerPath)
		os.Remove(processReportPath)
	})
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

// An agent that exits 124 on its own, well before the limit, is an agent failure, not a
// timeout, and a marker it wrote does not survive.
func TestCaptureIgnoresForgedTimeout(t *testing.T) {
	out, _, err := runCapture(t, "60", "bash", "-c", "echo 3600 > "+TimeoutMarkerPath+"; exit 124")
	if err != nil {
		t.Fatalf("an agent failure must exit 0: %v\n%s", err, out)
	}
	if _, err := os.Stat(TimeoutMarkerPath); err == nil {
		t.Fatalf("forged timeout marker survived\n%s", out)
	}
	if marker, err := os.ReadFile(AgentExitMarkerPath); err != nil || string(marker) != "124\n" {
		t.Fatalf("agent exit marker: %q, %v\n%s", marker, err, out)
	}
}

// An agent that exits non-zero by itself exits the wrapper 0 with its status in the marker.
// Whether that is data is decided in Go from the Pi process report (engine.agentSessionError):
// the wrapper only moves the report, so a fake Pi that answered once leaves one (in the shape
// llm.parsePiStats accepts, with a tool call), one that exits at once leaves none.
func TestCaptureAgentFailureIsData(t *testing.T) {
	const turn = `printf '%s\n' '{"type":"parent_start"}' ` +
		`'{"type":"agent","agentType":"parent","role":"parent","agentStats":{"turns":1},"instance":{"id":"parent","agent":"parent","resources":{"cpuTimeMs":1,"peakMemoryBytes":1},"startedAt":"2026-01-01T00:00:00Z","endedAt":"2026-01-01T00:00:01Z","durationMs":1000}}' ` +
		`'{"type":"tool_call","toolCall":{"id":"c1","tool":"bash","status":"ok","durationMs":12,"nested":false}}' '{"type":"complete"}' >"$DAGGERVALS_STATS_LOG"; `
	for name, script := range map[string]string{
		"after a turn": turn + "echo 'The model refused to complete the request' >&2; exit 1",
		"at once":      "echo 'The model refused to complete the request' >&2; exit 1",
	} {
		out, report, err := runCapture(t, "60", "bash", "-c", script)
		if err != nil {
			t.Fatalf("%s: an agent exit must exit 0: %v\n%s", name, err, out)
		}
		if marker, err := os.ReadFile(AgentExitMarkerPath); err != nil || string(marker) != "1\n" {
			t.Fatalf("%s: agent exit marker: %q, %v\n%s", name, marker, err, out)
		}
		if _, err := os.Stat(report); err != nil {
			t.Fatalf("%s: report missing: %v\n%s", name, err, out)
		}
		if tail := agentStderrTail(string(out), nil); tail != "The model refused to complete the request" {
			t.Fatalf("%s: stderr tail %q\n%s", name, tail, out)
		}
		_, err = os.Stat(processReportPath)
		if ranTurn := name == "after a turn"; ranTurn != (err == nil) {
			t.Fatalf("%s: process report present=%v\n%s", name, err == nil, out)
		}
		os.Remove(AgentExitMarkerPath)
		os.Remove(processReportPath)
	}

	out, _, err := runCapture(t, "60", "bash", "-c", "echo 1 > "+AgentExitMarkerPath)
	if err != nil {
		t.Fatalf("clean exit: %v\n%s", err, out)
	}
	if _, err := os.Stat(AgentExitMarkerPath); err == nil {
		t.Fatalf("forged agent exit marker survived\n%s", out)
	}
}

// A TestEnv failure (before the agent started) and an agent command that cannot run are
// infrastructure failures: the exec fails as before, with no marker.
func TestCaptureInfrastructureFailuresAreFatal(t *testing.T) {
	for name, run := range map[string]func() ([]byte, string, error){
		"TestEnv fails": func() ([]byte, string, error) {
			return runCaptureWithTestEnv(t, "exit 3\n", "60", "true")
		},
		"command not found": func() ([]byte, string, error) {
			return runCapture(t, "60", "/nonexistent/pi", "-p", "x")
		},
		"command not executable": func() ([]byte, string, error) {
			return runCapture(t, "60", "/etc/hostname")
		},
	} {
		out, _, err := run()
		if err == nil {
			t.Fatalf("%s: the exec must fail\n%s", name, out)
		}
		if _, err := os.Stat(AgentExitMarkerPath); err == nil {
			t.Fatalf("%s: no agent exit marker expected\n%s", name, out)
		}
	}
}

// The tail is the agent's part of stderr: its last lines, secrets replaced, then bounded.
func TestAgentStderrTail(t *testing.T) {
	stderr := "setup log\ndaggervals: agent started\n" + strings.Repeat("noise\n", 20) +
		"key sk-abcdefghijklmnop and Bearer abc.def\nvalue s3cr3t-value-123 and short ab\n" +
		"daggervals: agent exited with status 1\nmeasured stuff\n"
	tail := agentStderrTail(stderr, []string{"s3cr3t-value-123", "ab"})
	lines := strings.Split(tail, "\n")
	if len(lines) != agentErrorLines || lines[0] != "noise" || strings.Contains(tail, "setup log") || strings.Contains(tail, "daggervals:") {
		t.Fatalf("lines: %q", tail)
	}
	if !strings.HasSuffix(tail, "key [REDACTED] and Bearer [REDACTED]\nvalue [REDACTED] and short ab") {
		t.Fatalf("redaction: %q", tail)
	}
	// A multi-line secret across the 10-line cut leaks no part.
	spanning := agentStderrTail(agentStartedLine+"first-half-secret\nsecond-half-secret\n"+strings.Repeat("x\n", 9), []string{"first-half-secret\nsecond-half-secret"})
	if strings.Contains(spanning, "secret") {
		t.Fatalf("spanning secret: %q", spanning)
	}
	long := agentStderrTail(agentStartedLine+strings.Repeat("é", 1000), nil)
	if len(long) > agentErrorBytes+3 || !strings.HasPrefix(long, "...é") {
		t.Fatalf("byte cap: %d %q", len(long), long[:10])
	}
}

// The sessions of a task: counters summed, entries with the same key combined.
func TestMerge(t *testing.T) {
	a := Stats{IPPackets: 2, IPBytes: 20, LocalIPs: []string{"10.0.0.2"},
		IPSummaries: []IPSummary{{IP: "1.1.1.1", BytesIn: 5, Hits: 1, DNSCandidates: []string{"x"}}},
		DNSQueries:  []DNSQuery{{Name: "x", Type: 1, Count: 1}},
		Flows:       []Flow{{SourceIP: "10.0.0.2", DestinationIP: "1.1.1.1", Protocol: "tcp", SourcePort: 1, DestinationPort: 443, Packets: 2, Bytes: 20}}}
	b := Stats{IPPackets: 1, IPBytes: 10, LocalIPs: []string{"10.0.0.2"},
		IPSummaries: []IPSummary{{IP: "1.1.1.1", BytesIn: 3, Hits: 1, DNSCandidates: []string{"x", "y"}}, {IP: "2.2.2.2", Hits: 1}},
		DNSQueries:  []DNSQuery{{Name: "x", Type: 1, Count: 2}},
		Flows:       []Flow{{SourceIP: "10.0.0.2", DestinationIP: "1.1.1.1", Protocol: "tcp", SourcePort: 1, DestinationPort: 443, Packets: 1, Bytes: 10}}}
	m := Merge(a, b)
	if m.IPPackets != 3 || m.IPBytes != 30 || len(m.LocalIPs) != 1 || len(m.IPSummaries) != 2 || m.IPSummaries[0].BytesIn != 8 || m.IPSummaries[0].Hits != 2 ||
		strings.Join(m.IPSummaries[0].DNSCandidates, ",") != "x,y" || len(m.DNSQueries) != 1 || m.DNSQueries[0].Count != 3 || len(m.Flows) != 1 || m.Flows[0].Bytes != 30 {
		t.Fatalf("merged: %+v", m)
	}
	if strings.Join(a.IPSummaries[0].DNSCandidates, ",") != "x" {
		t.Fatal("Merge changed its input")
	}
	if empty, _ := json.Marshal(Merge()); string(empty) != `{"ipPackets":0,"ipBytes":0,"nonIPPackets":0,"localIPs":[],"ipSummaries":[],"dnsQueries":[],"flows":[]}` {
		t.Fatalf("empty: %s", empty)
	}
}

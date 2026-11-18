package memory

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"
)

// The expected ids come from the extension itself, with node:
// resolveIdentity(readConfig(dir), '/testbed') from extensions/hippocampus/core.ts.
func TestUserID(t *testing.T) {
	for settings, want := range map[string]string{
		`{"hippocampus":{"enabled":true,"userId":"default-user","projectId":"eval"}}`: "default-user:project:4bdd6907e23c2e39e6a512fe",
		`{"hippocampus":{"userId":" me ","projectId":" p1 "}}`:                        "me:project:ab23722c1bf07a34a42e6a92",
		`{"hippocampus":{"userId":7,"projectId":"eval"}}`:                             "default-user:project:4bdd6907e23c2e39e6a512fe",
	} {
		if got, err := UserID(settings); err != nil || got != want {
			t.Errorf("%s: %q, %v", settings, got, err)
		}
	}
	for name, settings := range map[string]string{
		"no section":    `{"pi-memory-mem0":{"userId":"default-user"}}`,
		"no projectId":  `{"hippocampus":{"userId":"default-user"}}`,
		"blank project": `{"hippocampus":{"projectId":" "}}`,
		"not json":      `{`,
	} {
		if _, err := UserID(settings); err == nil {
			t.Errorf("%s: want an error", name)
		}
	}
}

// Without an env file and without a flag nothing changes: no Config, no settings rewrite, no
// run id suffix.
func TestResolveWithoutMemoryKeepsTheOldBehavior(t *testing.T) {
	config, err := Resolve(context.Background(), nil, false, "", true)
	if err != nil || config != nil {
		t.Fatalf("want no config, got %+v, %v", config, err)
	}
	if config.Mode() != Untouched || config.RunIDSuffix() != "" {
		t.Fatalf("nil config: mode %q suffix %q", config.Mode(), config.RunIDSuffix())
	}
	if _, err := Resolve(context.Background(), nil, false, "main", true); err == nil {
		t.Fatal("hippocampusRef without memory must be an error")
	}
	if _, err := Resolve(context.Background(), nil, true, "main", true); err == nil {
		t.Fatal("hippocampusRef with noHippocampus must be an error")
	}
	for _, off := range []bool{false, true} {
		if _, err := Resolve(context.Background(), nil, off, "", false); err == nil {
			t.Fatalf("memoryFixture=false without memory (off=%v) must be an error", off)
		}
	}
}

func TestRunIDSuffix(t *testing.T) {
	off, err := Resolve(context.Background(), nil, true, "", true)
	if err != nil || off.Mode() != Off || off.RunIDSuffix() != "@nomem" {
		t.Fatalf("off: %+v %v", off, err)
	}
	digest := "3169412edb9947895950338516bedf56b2465cf226871b70ba59be55093af658"
	on := &Config{ref: "c8f33b2", digest: digest, fixture: true}
	if on.Mode() != On || on.RunIDSuffix() != "@hippo=c8f33b2-3169412edb99" {
		t.Fatalf("on: %q", on.RunIDSuffix())
	}
	on.fixture = false
	if on.RunIDSuffix() != "@hippo=c8f33b2-3169412edb99@nofx" {
		t.Fatalf("on without fixture: %q", on.RunIDSuffix())
	}
}

// Lines as the server prints them (from a run of jacarte/mem0server:c8f33b2).
const beforeMetrics = `# HELP http_requests_total Total HTTP requests received, partitioned by method, path, and response status code.
# TYPE http_requests_total counter
http_requests_total{method="POST",path="/memories",status_code="200"} 2.0
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{le="0.05",method="POST",path="/memories"} 0.0
http_request_duration_seconds_count{method="POST",path="/memories"} 2.0
http_request_duration_seconds_sum{method="POST",path="/memories"} 3.0
# TYPE memory_operations_total counter
memory_operations_total{agent_id="",operation="add",user_id="default-user:project:427298c01319"} 2.0
# TYPE memory_operations_created gauge
memory_operations_created{agent_id="",operation="add",user_id="default-user:project:427298c01319"} 1.7950419021585505e+09
# TYPE memory_operation_errors_total counter
`

const afterMetrics = `# TYPE http_requests_total counter
http_requests_total{method="POST",path="/memories",status_code="200"} 3.0
http_requests_total{method="GET",path="/metrics",status_code="200"} 1.0
http_requests_total{method="POST",path="/search",status_code="500"} 2.0
http_request_duration_seconds_count{method="POST",path="/memories"} 3.0
http_request_duration_seconds_sum{method="POST",path="/memories"} 4.0
http_request_duration_seconds_count{method="GET",path="/metrics"} 1.0
http_request_duration_seconds_sum{method="GET",path="/metrics"} 0.003
http_request_duration_seconds_count{method="POST",path="/search"} 2.0
http_request_duration_seconds_sum{method="POST",path="/search"} 0.2
# TYPE memory_operations_total counter
memory_operations_total{agent_id="",operation="add",user_id="default-user:project:427298c01319"} 3.0
memory_operations_total{agent_id="",operation="search",user_id="a \"quoted\", id"} 2.0
# TYPE memory_operation_errors_total counter
memory_operation_errors_total{operation="search"} 2.0
`

// The seeding before the agent and the harness's own /metrics reads are not counted.
func TestStatsDelta(t *testing.T) {
	before, err := parseMetrics(beforeMetrics)
	if err != nil {
		t.Fatal(err)
	}
	after, err := parseMetrics(afterMetrics)
	if err != nil {
		t.Fatal(err)
	}
	var s Stats
	s.setDelta(before, after)
	if len(s.Ops) != 2 || s.Ops["add"] != 1 || s.Ops["search"] != 2 {
		t.Fatalf("ops: %+v", s.Ops)
	}
	if *s.Errors != 2 {
		t.Fatalf("errors %v", *s.Errors)
	}
	// (4-3 + 0.2) s over 3 requests
	if s.AvgLatencyMs == nil || *s.AvgLatencyMs < 399.9 || *s.AvgLatencyMs > 400.1 {
		t.Fatalf("latency: %v", s.AvgLatencyMs)
	}

	var idle Stats
	idle.setDelta(after, after)
	if len(idle.Ops) != 0 || *idle.Errors != 0 || idle.AvgLatencyMs != nil {
		t.Fatalf("no traffic is zero ops and no latency: %+v", idle)
	}
}

func TestParseMetricsRejectsOtherServers(t *testing.T) {
	if _, err := parseMetrics("# TYPE up gauge\nup 1\n"); err == nil || !strings.Contains(err.Error(), "not a Hippocampus server") {
		t.Fatalf("want an error, got %v", err)
	}
	if _, err := parseMetrics(beforeMetrics + `memory_operations_total{operation="add} 1` + "\n"); err == nil {
		t.Fatal("unterminated label must be an error")
	}
}

// The sidecar is the only way into the server: pin what keeps it closed.
func TestSidecarConfig(t *testing.T) {
	for _, want := range []string{
		"admin off",
		"persist_config off",
		"(method('GET') && path('/health', '/memories')) || (method('POST') && path('/memories', '/search', '/retrieve')) || (method('GET', 'DELETE') && path_regexp('^/memories/[^/]+$'))",
		"max_size 128KB",
		"@failed status 4xx 5xx",
		"respond 403",
		":8001 {\n\t@harness header X-Harness-Token {$HARNESS_TOKEN}",
		"reverse_proxy 127.0.0.1:8002",
	} {
		if !strings.Contains(sidecarConfig, want) {
			t.Errorf("sidecar config lacks %q", want)
		}
	}
	if !slices.Contains(forcedEnv("n"), [2]string{"MEM0_HOST", "127.0.0.1"}) {
		t.Error("the server must listen on 127.0.0.1 only")
	}
}

// SeededStored and FixtureSize are only in the outcome when the fixture was seeded.
func TestSeededStoredJSON(t *testing.T) {
	without, _ := json.Marshal(Stats{Enabled: true})
	n := 1
	with, _ := json.Marshal(Stats{Enabled: true, Fixture: true, SeededStored: &n, FixtureSize: 2})
	if strings.Contains(string(without), "seededStored") || strings.Contains(string(without), "fixtureSize") ||
		!strings.Contains(string(with), `"seededStored":1,"fixtureSize":2`) {
		t.Fatalf("without: %s with: %s", without, with)
	}
}

// Without memory services (no Config, or the off arm) a session has no memory numbers.
func TestAfterSessionWithoutServices(t *testing.T) {
	if (*Task)(nil).AfterSession(context.Background(), 1) != nil || (&Task{}).AfterSession(context.Background(), 1) != nil {
		t.Fatal("want nil")
	}
}

// The admin listing of the server image (AdminMemoryListResponse): total_items and items[].content.
func TestParseAdminListing(t *testing.T) {
	text := `{"items":[{"memory_id":"a","scope":"user","scope_id":"u","content":"CRC is Castagnoli"},{"memory_id":"b","content":"` + strings.Repeat("é", 200) + `"}],"page":1,"page_size":20,"total_items":7,"total_pages":1}`
	n, texts, err := parseAdminListing(text)
	if err != nil || n != 7 || len(texts) != 2 || texts[0] != "CRC is Castagnoli" || len(texts[1]) > maxMemoryText+3 || !strings.HasSuffix(texts[1], "é...") {
		t.Fatalf("%d %q %v", n, texts, err)
	}
	if _, _, err := parseAdminListing(`{"items":[]}`); err == nil {
		t.Fatal("no total_items must be an error")
	}
}

package llm

import "testing"

func TestParsePiStatsToolCalls(t *testing.T) {
	log := `{"type":"parent_start"}
{"type":"agent","agentType":"parent","role":"parent","agentStats":{"turns":1},"instance":{"id":"parent","agent":"parent","resources":{"cpuTimeMs":1,"peakMemoryBytes":1},"startedAt":"2026-01-01T00:00:00Z","endedAt":"2026-01-01T00:00:01Z","durationMs":1000}}
{"type":"tool_call","toolCall":{"id":"c1","tool":"bash","status":"ok","durationMs":12,"nested":false}}
{"type":"complete"}
`
	_, ps, err := parsePiStats(log)
	if err != nil {
		t.Fatal(err)
	}
	if len(ps.ToolCalls) != 1 || ps.ToolCalls[0].Tool != "bash" {
		t.Fatalf("tool calls = %+v", ps.ToolCalls)
	}
}

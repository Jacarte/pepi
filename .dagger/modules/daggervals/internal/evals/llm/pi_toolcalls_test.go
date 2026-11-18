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

// The evidence engine.agentSessionError needs: a refusal after the model answered has tool
// calls, a Pi that exited before session_start has none (and null usage).
func TestParsePiStatsTurnsAsSessionEvidence(t *testing.T) {
	parent := func(usage string) string {
		return `{"type":"agent","agentType":"parent","role":"parent","agentStats":` + usage + `,"instance":{"id":"parent","agent":"parent","resources":{"cpuTimeMs":1,"peakMemoryBytes":1},"startedAt":"2026-01-01T00:00:00Z","endedAt":"2026-01-01T00:00:01Z","durationMs":1000}}`
	}
	refused := `{"type":"parent_start"}` + "\n" + parent(`{"turns":3,"input":10,"output":2,"cacheRead":0,"cacheWrite":0,"cost_usd":0.01}`) + "\n" +
		`{"type":"tool_call","toolCall":{"id":"c1","tool":"bash","status":"ok","durationMs":12,"nested":false}}` + "\n" + `{"type":"complete"}` + "\n"
	if stats, ps, err := parsePiStats(refused); err != nil || stats.Turns != 3 || len(ps.ToolCalls) != 1 {
		t.Fatalf("refusal after a tool call: %+v %+v, %v", stats, ps, err)
	}
	early := parent("null") + "\n" + `{"type":"warning","message":"Parent session unavailable; Pi exited before session_start."}` + "\n" + `{"type":"complete"}` + "\n"
	if stats, ps, err := parsePiStats(early); err != nil || stats.Turns != 0 || len(ps.ToolCalls) != 0 {
		t.Fatalf("exit before session_start: %+v %+v, %v", stats, ps, err)
	}
}

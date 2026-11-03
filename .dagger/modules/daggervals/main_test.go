package main

import (
	"dagger/daggervals/internal/evals/engine"
	"encoding/json"
	"errors"
	"testing"
)

func TestSerializeTaskOutcomesPreservesStatsOnValidationFailure(t *testing.T) {
	results, err := serializeTaskOutcomes([]engine.TaskOutcome{{
		Task: "repo", AgentStats: engine.AgentStats{Input: 3, CostUSD: 0.25},
		NetworkStats:    engine.NetworkStats{IPPackets: 1, IPBytes: 60},
		ValidationError: errors.New("oracle failed"),
	}})
	if err != nil {
		t.Fatal(err)
	}
	var got taskResult
	if err := json.Unmarshal([]byte(results[0]), &got); err != nil {
		t.Fatal(err)
	}
	if got.Valid || got.ValidationError != "oracle failed" || got.Stats.Input != 3 || got.Stats.CostUSD != 0.25 || got.NetworkStats.IPBytes != 60 {
		t.Fatalf("lost outcome data: %+v", got)
	}
}

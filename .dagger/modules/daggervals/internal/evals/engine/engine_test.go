package engine

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/dagger/core"
	"dagger/daggervals/internal/evals/task"

	"github.com/Khan/genqlient/graphql"
)

type syncClient struct {
	calls        int
	failAt       int
	err          error
	queries      []string
	networkError error
}

func (c *syncClient) MakeRequest(_ context.Context, request *graphql.Request, response *graphql.Response) error {
	c.queries = append(c.queries, request.Query)
	if strings.Contains(request.Query, "{contents}") {
		if c.networkError != nil {
			return c.networkError
		}
		return json.Unmarshal([]byte(`{"node":{"file":{"contents":"{\"ipPackets\":1,\"ipBytes\":60,\"dnsQueries\":[],\"flows\":[]}"}}}`), response.Data)
	}
	c.calls++
	if c.calls == c.failAt {
		return c.err
	}
	return json.Unmarshal([]byte(`{"container":{"sync":"result-id"},"node":{"sync":"result-id"}}`), response.Data)
}

type testSetup struct{}

func (testSetup) Setup() dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container { return container }
}

type testAgent struct {
	stats  AgentStats
	err    error
	events *[]string
}

func (a testAgent) CallPrompt(container *dagger.Container, _ string) *dagger.Container {
	return container
}

func (a testAgent) TaskCommand(_ task.FixTask) []string {
	*a.events = append(*a.events, "run")
	return []string{"pi", "-p", "hello world"}
}

func (a testAgent) GetAgentStats(context.Context, *dagger.Container) (AgentStats, error) {
	*a.events = append(*a.events, "stats")
	return a.stats, a.err
}

type testOracle struct {
	valid  bool
	err    error
	events *[]string
}

func (o testOracle) ValidateFinalAnswer(context.Context, *dagger.Container) (bool, error) {
	*o.events = append(*o.events, "validate")
	return o.valid, o.err
}

func TestRunReturnsStatsAndValidationOutcome(t *testing.T) {
	validationErr := errors.New("oracle failed")
	statsErr := errors.New("stats unavailable")
	for _, tc := range []struct {
		name          string
		valid         bool
		validationErr error
		statsErr      error
		wantCount     int
	}{
		{name: "success", valid: true, wantCount: 2},
		{name: "invalid", wantCount: 1},
		{name: "validation error", valid: true, validationErr: validationErr, wantCount: 1},
		{name: "stats error still validates", statsErr: statsErr, wantCount: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var events []string
			stats := AgentStats{Turns: 1, Input: 2, Output: 3, CacheRead: 4, CacheWrite: 5, CostUSD: 0.06}
			fixTask := task.FixTask{
				Baseline: task.Baseline{Repo: "repo"},
				Env:      task.HelloWorldEnvironment{},
				Oracle:   testOracle{valid: tc.valid, err: tc.validationErr, events: &events},
			}
			engine := NewEngine(testAgent{stats: stats, err: tc.statsErr, events: &events}, testSetup{}, []task.FixTask{fixTask, fixTask})
			client := &syncClient{}
			outcomes, err := engine.Run(context.Background(), core.NewQuery(client).Container())
			if !errors.Is(err, tc.statsErr) {
				t.Fatalf("Run error = %v, want %v", err, tc.statsErr)
			}
			if len(outcomes) != tc.wantCount {
				t.Fatalf("got %d outcomes, want %d", len(outcomes), tc.wantCount)
			}
			for _, outcome := range outcomes {
				if outcome.NetworkStats.IPPackets != 1 {
					t.Errorf("network stats lost: %+v", outcome.NetworkStats)
				}
				if outcome.Task != "repo" || outcome.AgentStats != stats || outcome.Valid != (tc.valid && tc.validationErr == nil) {
					t.Errorf("unexpected outcome: %+v", outcome)
				}
				if (!tc.valid || tc.validationErr != nil) != (outcome.ValidationError != nil) {
					t.Errorf("unexpected validation error: %v", outcome.ValidationError)
				}
				if tc.validationErr != nil && !errors.Is(outcome.ValidationError, tc.validationErr) {
					t.Errorf("validation error lost cause: %v", outcome.ValidationError)
				}
			}
			var wantEvents []string
			for range tc.wantCount {
				wantEvents = append(wantEvents, "run", "stats", "validate")
			}
			if !reflect.DeepEqual(events, wantEvents) {
				t.Errorf("events = %v, want %v", events, wantEvents)
			}
		})
	}
}

func TestRunReturnsInfrastructureErrors(t *testing.T) {
	for _, failAt := range []int{1, 2, 3} {
		var events []string
		syncErr := errors.New("sync failed")
		client := &syncClient{failAt: failAt, err: syncErr}
		engine := NewEngine(testAgent{events: &events}, testSetup{}, []task.FixTask{{
			Env: task.HelloWorldEnvironment{}, Oracle: testOracle{events: &events},
		}})
		outcomes, err := engine.Run(context.Background(), core.NewQuery(client).Container())
		if !errors.Is(err, syncErr) || len(outcomes) != 0 {
			t.Fatalf("sync %d: outcomes = %v, error = %v", failAt, outcomes, err)
		}
		wantEvents := 0
		if failAt == 3 {
			wantEvents = 1
		}
		if len(events) != wantEvents {
			t.Fatalf("sync %d: unexpected stats or validation: %v", failAt, events)
		}
	}
}

type scriptEnvironment struct {
	script string
	err    error
	client *syncClient
	t      *testing.T
}

func (e scriptEnvironment) Script(context.Context) (string, error) {
	if e.client.calls < 2 {
		e.t.Fatal("TestEnv constructed before PiSetup and Env sync")
	}
	return e.script, e.err
}

type markedSetup struct{}

func (markedSetup) Setup() dagger.WithContainerFunc {
	return func(c *dagger.Container) *dagger.Container { return c.WithExec([]string{"pi-setup-marker"}) }
}

func TestRunMeasurementBoundaryAndTaskIsolation(t *testing.T) {
	var events []string
	client := &syncClient{}
	first := task.FixTask{
		Env:     task.HelloWorldEnvironment{},
		TestEnv: scriptEnvironment{script: "echo first-test-env-marker", client: client, t: t},
		Oracle:  testOracle{valid: true, events: &events},
	}
	second := first
	second.TestEnv = scriptEnvironment{script: "echo second-test-env-marker", client: client, t: t}
	engine := NewEngine(testAgent{events: &events}, markedSetup{}, []task.FixTask{first, second})
	outcomes, err := engine.Run(context.Background(), core.NewQuery(client).Container())
	if err != nil || len(outcomes) != 2 {
		t.Fatalf("outcomes=%v err=%v", outcomes, err)
	}
	if len(client.queries) != 7 {
		t.Fatalf("got %d requests", len(client.queries))
	}
	if !strings.Contains(client.queries[0], "pi-setup-marker") {
		t.Fatal("PiSetup not synced first")
	}
	if !strings.Contains(client.queries[1], "rustup") {
		t.Fatal("Env not synced before measurement")
	}
	for i, marker := range map[int]string{2: "first-test-env-marker", 5: "second-test-env-marker"} {
		q := client.queries[i]
		if strings.Count(q, "withExec(args:") != 1 || !strings.Contains(q, marker) || strings.Contains(q, "pi-setup-marker") || strings.Contains(q, "rustup") || strings.Contains(q, "insecureRootCapabilities") {
			t.Fatalf("incorrect measured execution boundary at request %d", i)
		}
	}
	if strings.Contains(client.queries[5], "first-test-env-marker") {
		t.Fatal("previous task capture leaked into next task")
	}
}

func TestRunScriptAndNetworkErrors(t *testing.T) {
	for _, stage := range []string{"script", "network"} {
		t.Run(stage, func(t *testing.T) {
			var events []string
			failure := errors.New(stage + " failed")
			client := &syncClient{}
			fix := task.FixTask{Env: task.HelloWorldEnvironment{}, Oracle: testOracle{valid: true, events: &events}}
			if stage == "script" {
				fix.TestEnv = scriptEnvironment{err: failure, client: client, t: t}
			} else {
				client.networkError = failure
			}
			outcomes, err := NewEngine(testAgent{events: &events}, testSetup{}, []task.FixTask{fix}).Run(context.Background(), core.NewQuery(client).Container())
			if !errors.Is(err, failure) {
				t.Fatalf("error lost: %v", err)
			}
			if stage == "script" && (len(outcomes) != 0 || len(events) != 0) {
				t.Fatal("script failure ran agent")
			}
			if stage == "network" && (len(outcomes) != 1 || !reflect.DeepEqual(events, []string{"run", "stats", "validate"})) {
				t.Fatal("network error prevented stats/oracle")
			}
		})
	}
}

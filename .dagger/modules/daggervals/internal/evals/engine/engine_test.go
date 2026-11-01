package engine

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/dagger/core"
	"dagger/daggervals/internal/evals/task"

	"github.com/Khan/genqlient/graphql"
)

type syncClient struct {
	calls  int
	failAt int
	err    error
}

func (c *syncClient) MakeRequest(_ context.Context, _ *graphql.Request, response *graphql.Response) error {
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
	stats  Stats
	err    error
	events *[]string
}

func (a testAgent) CallPrompt(container *dagger.Container, _ string) *dagger.Container {
	return container
}

func (a testAgent) RunTask(container *dagger.Container, _ task.FixTask) *dagger.Container {
	*a.events = append(*a.events, "run")
	return container
}

func (a testAgent) GetStats(context.Context, *dagger.Container) (Stats, error) {
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
			stats := Stats{Turns: 1, Input: 2, Output: 3, CacheRead: 4, CacheWrite: 5, CostUSD: 0.06}
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
				if outcome.Task != "repo" || outcome.Stats != stats || outcome.Valid != (tc.valid && tc.validationErr == nil) {
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
	for _, failAt := range []int{1, 2} {
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
		if len(events) != failAt-1 {
			t.Fatalf("sync %d: unexpected stats or validation: %v", failAt, events)
		}
	}
}

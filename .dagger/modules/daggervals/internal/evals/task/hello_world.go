package task

import (
	"context"
	"dagger/daggervals/internal/dagger"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// The Jev router extension appends to <agent dir>/logs/router.log. That directory sits
// inside the mounted workspace, whose writes do not survive the measured exec, so
// HelloWorldTestEnv links logs to a rootfs directory and the oracle reads it from there.
const (
	helloWorldRouterLogDir  = "/tmp/daggervals-jev"
	helloWorldRouterLogPath = helloWorldRouterLogDir + "/router.log"
	agentRouterLogDir       = "/root/.pi/agent/logs"
)

type HelloWorldEnvironment struct{}

func (HelloWorldEnvironment) PrepareEnv(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return container
	}
}

func (HelloWorldEnvironment) PrepareEnvForTesting(ctx context.Context) dagger.WithContainerFunc {
	return func(container *dagger.Container) *dagger.Container {
		return container
	}
}

func (HelloWorldEnvironment) GetRepo() string   { return "hello-world" }
func (HelloWorldEnvironment) GetCommit() string { return "" }

// HelloWorldTestEnv runs inside the measured exec before the agent (see TestEnvironment).
type HelloWorldTestEnv struct{}

func (HelloWorldTestEnv) Script(ctx context.Context) (string, error) {
	return fmt.Sprintf(`mkdir -p %[1]s
rm -rf %[2]s
ln -s %[1]s %[2]s
`, helloWorldRouterLogDir, agentRouterLogDir), nil
}

type HelloWorldOracle struct{}

func (HelloWorldEnvironment) GetFinalDiff(ctx context.Context, container *dagger.Container) string {
	return "" // no diff to check for hello world
}

func (HelloWorldOracle) ValidateFinalAnswer(ctx context.Context, container *dagger.Container) (bool, error) {
	// Get the stdout and log it
	stdout, err := container.Stdout(ctx)
	if err != nil {
		return false, err
	}
	fmt.Println("HelloWorldOracle: stdout:", stdout)
	routerLog, err := container.File(helloWorldRouterLogPath).Contents(ctx)
	if err != nil {
		return false, fmt.Errorf("HelloWorldOracle: Jev router log %s is missing, so jev_route was never called (is TYPESAFE_API_KEY set?): %w", helloWorldRouterLogPath, err)
	}
	return checkHelloWorld(stdout, routerLog)
}

// checkHelloWorld is the pure part of the oracle: stdout must say hello world and the
// router log must prove that jev_route ran with action recommend.
func checkHelloWorld(stdout, routerLog string) (bool, error) {
	if !strings.Contains(strings.ToLower(stdout), "hello world") {
		return false, fmt.Errorf("HelloWorldOracle: stdout does not contain 'hello world': %s", stdout)
	}
	if err := checkJevRecommendLog(routerLog); err != nil {
		return false, err
	}
	return true, nil
}

var routerHeaderRecommend = regexp.MustCompile(`^\S+ session=\S+ action=recommend$`)

// checkJevRecommendLog expects, per router call, a header "<ISO> session=<id> action=<action>",
// the raw TypeSafe JSON, and later a JSON decision line with lane, provider, id, action,
// changed, escalated and reason (a string or null, with escalated === (reason !== null)).
// Every recommend decision after a recommend header must be valid, and at least one must exist.
func checkJevRecommendLog(routerLog string) error {
	if strings.TrimSpace(routerLog) == "" {
		return fmt.Errorf("HelloWorldOracle: Jev router log is empty: jev_route was never called")
	}
	headerSeen := false
	decisions := 0
	for _, line := range strings.Split(routerLog, "\n") {
		line = strings.TrimSpace(line)
		if routerHeaderRecommend.MatchString(line) {
			headerSeen = true
			continue
		}
		if !headerSeen || !strings.HasPrefix(line, "{") {
			continue
		}
		var decision struct {
			Lane      string          `json:"lane"`
			Provider  string          `json:"provider"`
			ID        string          `json:"id"`
			Action    string          `json:"action"`
			Changed   *bool           `json:"changed"`
			Escalated *bool           `json:"escalated"`
			Reason    json.RawMessage `json:"reason"`
		}
		if json.Unmarshal([]byte(line), &decision) != nil || decision.Action != "recommend" {
			continue
		}
		if err := checkJevDecision(decision.Lane, decision.Provider, decision.ID, decision.Changed, decision.Escalated, decision.Reason); err != nil {
			return fmt.Errorf("%w: %s", err, line)
		}
		decisions++
	}
	if !headerSeen {
		return fmt.Errorf("HelloWorldOracle: Jev router log has no 'session=<id> action=recommend' header line: jev_route was not called with action recommend")
	}
	if decisions == 0 {
		return fmt.Errorf("HelloWorldOracle: Jev router log has an action=recommend header but no decision JSON line (lane, provider, id, action, changed, escalated, reason)")
	}
	return nil
}

func checkJevDecision(lane, provider, id string, changed, escalated *bool, rawReason json.RawMessage) error {
	switch lane {
	case "LOCAL", "CHEAP", "FRONTIER":
	default:
		return fmt.Errorf("HelloWorldOracle: Jev decision has invalid lane %q", lane)
	}
	if provider == "" || id == "" || changed == nil {
		return fmt.Errorf("HelloWorldOracle: Jev decision lacks provider, id or changed")
	}
	if escalated == nil || rawReason == nil {
		return fmt.Errorf("HelloWorldOracle: Jev decision lacks escalated or reason")
	}
	// A missing reason key leaves rawReason nil; an explicit null decodes to a nil *string.
	var reason *string
	if err := json.Unmarshal(rawReason, &reason); err != nil {
		return fmt.Errorf("HelloWorldOracle: Jev decision reason is neither a string nor null")
	}
	if *escalated != (reason != nil) {
		return fmt.Errorf("HelloWorldOracle: Jev decision has escalated=%v but reason=%s; want escalated === (reason !== null)", *escalated, rawReason)
	}
	return nil
}

var HelloWorldTask = FixTask{
	ProblemStatement: "Call the jev_route tool with action \"recommend\" and task \"Rename one variable in one file\" (role \"worker\" is optional). " +
		"Then reply with the exact words hello world on the final line.",
	Env:     HelloWorldEnvironment{},
	TestEnv: HelloWorldTestEnv{},
	Oracle:  HelloWorldOracle{},
}

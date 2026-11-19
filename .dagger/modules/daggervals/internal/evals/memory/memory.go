// Package memory runs a Hippocampus memory server (a mem0 wrapper) next to each eval task, so
// the agent's Hippocampus extension (extensions/hippocampus.ts) has a working memory. Every task gets a fresh Postgres
// and a server container in which Caddy is the only way in: a filtered port for the agent and
// a token-guarded port on which the harness seeds the fixture and reads the metrics.
package memory

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	"dagger/daggervals/internal/dagger"
)

const (
	// Image is the Hippocampus server. DefaultRef is a pinned short-sha tag of it.
	Image      = "jacarte/mem0server"
	DefaultRef = "c8f33b2"
	// Alias is the hostname of the server container for the agent and the harness.
	Alias = "hippocampus"
	// Caddy in the server container listens on agentPort (filtered, for the agent) and
	// harnessPort (everything, with the per-task token); the server itself listens on
	// 127.0.0.1:serverPort only.
	agentPort   = 8000
	harnessPort = 8001
	serverPort  = 8002

	postgresImage = "pgvector/pgvector:pg16"
	caddyImage    = "caddy:2"
	// The image is published for linux/amd64 only. Its arm64 build needs this const and the
	// Platform option in serverImage removed.
	serverPlatform = dagger.Platform("linux/amd64")
	// SettingsPath is the agent's effective settings.json (after profile.sh).
	SettingsPath = "/root/.pi/agent/settings.json"
	// ProjectID is the explicit hippocampus.projectId with memory on: the task repos have no Git
	// origin, and every task has its own server, so one id for all tasks mixes nothing.
	ProjectID = "eval"
)

var (
	// BaseURL is what the agent's settings point the extension at.
	BaseURL    = fmt.Sprintf("http://%s:%d", Alias, agentPort)
	harnessURL = fmt.Sprintf("http://%s:%d", Alias, harnessPort)
)

// Mode is what PiSetup does to the hippocampus settings of the agent container.
type Mode string

const (
	// Untouched leaves the profile's settings as they are: no env file and no flag.
	Untouched Mode = ""
	// On points the extension at the memory server.
	On Mode = "on"
	// Off disables the tool, automatic capture and automatic recall.
	Off Mode = "off"
)

// sidecarConfig is Caddy in the server container. The agent port forwards only the calls
// the extension makes (403 for /configure, /reset, /admin/*, /metrics, /query and the rest)
// and drops the body and headers of upstream errors, which can quote the gateway's error with
// part of its API key. The body cap fits the extension's largest write: a capture of up to
// 24000 characters, which JSON-encoded as UTF-8 can exceed 64KB. The harness port passes
// everything, with the token only.
// The Dagger service network is shared: another task's :8000 is reachable by IP (integrity
// only, the token and the keys stay per task).
var sidecarConfig = fmt.Sprintf(`{
	admin off
	persist_config off
}
:%[1]d {
	@allowed expression `+"`"+`(method('GET') && path('/health', '/memories')) || (method('POST') && path('/memories', '/search', '/retrieve')) || (method('GET', 'DELETE') && path_regexp('^/memories/[^/]+$'))`+"`"+`
	handle @allowed {
		request_body {
			max_size 128KB
		}
		reverse_proxy 127.0.0.1:%[3]d {
			@failed status 4xx 5xx
			handle_response @failed {
				respond "" {rp.status_code}
			}
		}
	}
	handle {
		respond 403
	}
}
:%[2]d {
	@harness header X-Harness-Token {$HARNESS_TOKEN}
	handle @harness {
		reverse_proxy 127.0.0.1:%[3]d
	}
	handle {
		respond 403
	}
}
`, agentPort, harnessPort, serverPort)

// sidecarCommand runs Caddy and the server; the container stops when either exits.
var sidecarCommand = []string{"bash", "-c", "caddy run --config /etc/caddy/Caddyfile --adapter caddyfile & python server.py & wait -n"}

// curlHarness reads the token from the client's env, so it is never a call argument.
const curlHarness = `curl -fsS -H "X-Harness-Token: $HARNESS_TOKEN"`

// Config is the memory arm of an eval. A nil *Config (no env file and no flag) is the
// behavior without this package: no services, settings untouched, no stats.
type Config struct {
	// env is the server's env file, as secrets so Dagger scrubs the values from its output.
	env []envSecret
	// ref is the image tag, empty when memory is off.
	ref      string
	imageRef string
	digest   string
	fixture  bool
}

type envSecret struct {
	name   string
	secret *dagger.Secret
}

var tagPattern = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$`)

// Resolve builds the memory arm from the eval flags. envFile is the --hippocampus-env file or
// the workspace's .hippocampus.env, nil when there is neither. It resolves the image digest.
func Resolve(ctx context.Context, envFile *dagger.File, off bool, ref string, fixture bool) (*Config, error) {
	on := !off && envFile != nil
	if ref != "" && !on {
		return nil, errors.New("hippocampusRef needs memory on: a hippocampus env file and no noHippocampus")
	}
	if !fixture && !on {
		return nil, errors.New("memoryFixture=false needs memory on: a hippocampus env file and no noHippocampus")
	}
	if off {
		return &Config{}, nil
	}
	if envFile == nil {
		fmt.Println("warning: no .hippocampus.env in the workspace and no --hippocampus-env: the memory service is not started (pass --no-hippocampus for an explicit off arm)")
		return nil, nil
	}
	if ref == "" {
		ref = DefaultRef
	}
	if !tagPattern.MatchString(ref) {
		return nil, fmt.Errorf("hippocampusRef %q is not an image tag", ref)
	}
	imageRef, err := serverImage(Image + ":" + ref).ImageRef(ctx)
	if err != nil {
		return nil, fmt.Errorf("resolve %s:%s: %w", Image, ref, err)
	}
	_, digest, ok := strings.Cut(imageRef, "@sha256:")
	if !ok || len(digest) < 12 {
		return nil, fmt.Errorf("resolve %s:%s: no digest in %q", Image, ref, imageRef)
	}
	vars, err := envFile.AsEnvFile().Variables(ctx)
	if err != nil {
		return nil, fmt.Errorf("read hippocampus env file: %w", err)
	}
	config := &Config{ref: ref, imageRef: imageRef, digest: digest, fixture: fixture}
	for _, v := range vars {
		name, err := v.Name(ctx)
		if err != nil {
			return nil, fmt.Errorf("read hippocampus env file: %w", err)
		}
		value, err := v.Value(ctx)
		if err != nil {
			return nil, fmt.Errorf("read hippocampus env file: %w", err)
		}
		config.env = append(config.env, envSecret{name, dagger.Connect().SetSecret("hippocampus-env-"+name, value)})
	}
	return config, nil
}

func serverImage(address string) *dagger.Container {
	return dagger.Connect().Container(dagger.ContainerOpts{Platform: serverPlatform}).
		From(address, dagger.ContainerFromOpts{NoLock: true})
}

// Mode is the settings rewrite of the agent container for this arm.
func (c *Config) Mode() Mode {
	switch {
	case c == nil:
		return Untouched
	case c.ref == "":
		return Off
	default:
		return On
	}
}

// RunIDSuffix is appended to the run id: @hippo=<tag>-<digest12>[@nofx] when memory is on,
// @nomem when it is off, nothing without a Config.
func (c *Config) RunIDSuffix() string {
	switch c.Mode() {
	case On:
		suffix := "@hippo=" + c.ref + "-" + c.digest[:12]
		if !c.fixture {
			suffix += "@nofx"
		}
		return suffix
	case Off:
		return "@nomem"
	default:
		return ""
	}
}

// Task is the memory of one task: nil without a Config, no services when memory is off.
type Task struct {
	config           *Config
	userID           string
	postgres, server *dagger.Service
	// client is the harness's curl container: it has the token, the agent does not.
	client *dagger.Container
	seeded bool
	// seededStored is the user's memory count right after seeding.
	seededStored *int
	fixtureSize  int
	before       metrics
	beforeErr    error
	// last are the metrics after the previous session of a multi-session task.
	last    metrics
	lastErr error
}

// StartTask starts the services of one task, waits for the server, seeds the task's fixture
// when the fixture is enabled, and records the server metrics before the agent starts. agent
// is the task's container before the agent runs: its settings give the user_id the extension
// will use. The caller must Stop the task.
func (c *Config) StartTask(ctx context.Context, agent *dagger.Container, fixture []string) (*Task, error) {
	switch c.Mode() {
	case Untouched:
		return nil, nil
	case Off:
		return &Task{config: c}, nil
	}
	t := &Task{config: c}
	settings, err := agent.File(SettingsPath).Contents(ctx)
	if err != nil {
		return nil, fmt.Errorf("read agent settings: %w", err)
	}
	if t.userID, err = UserID(settings); err != nil {
		return nil, err
	}

	// nonce makes the services of each task distinct, so Dagger neither shares them between
	// parallel tasks nor reuses cached execs bound to them. It is not a secret.
	nonce := rand.Text()
	secret := func(name string) *dagger.Secret {
		return dagger.Connect().SetSecret("memory-"+nonce+"-"+name, rand.Text())
	}
	password, token := secret("postgres-password"), secret("harness-token")
	t.postgres = dagger.Connect().Container().
		From(postgresImage, dagger.ContainerFromOpts{NoLock: true}).
		WithEnvVariable("MEMORY_TASK", nonce).
		WithSecretVariable("POSTGRES_PASSWORD", password).
		WithExposedPort(5432).
		AsService(dagger.ContainerAsServiceOpts{UseEntrypoint: true})
	forced := forcedEnv(nonce)
	server := serverImage(c.imageRef).
		WithFile("/usr/local/bin/caddy", dagger.Connect().Container(dagger.ContainerOpts{Platform: serverPlatform}).
			From(caddyImage, dagger.ContainerFromOpts{NoLock: true}).File("/usr/bin/caddy")).
		WithNewFile("/etc/caddy/Caddyfile", sidecarConfig)
	for _, v := range c.env {
		if !slices.ContainsFunc(forced, func(kv [2]string) bool { return kv[0] == v.name }) && v.name != "POSTGRES_PASSWORD" && v.name != "HARNESS_TOKEN" {
			server = server.WithSecretVariable(v.name, v.secret)
		}
	}
	for _, kv := range forced {
		server = server.WithEnvVariable(kv[0], kv[1])
	}
	t.server = server.
		WithSecretVariable("POSTGRES_PASSWORD", password).
		WithSecretVariable("HARNESS_TOKEN", token).
		WithServiceBinding("postgres", t.postgres).
		WithExposedPort(agentPort).
		WithExposedPort(harnessPort).
		AsService(dagger.ContainerAsServiceOpts{Args: sidecarCommand})
	for _, svc := range []*dagger.Service{t.postgres, t.server} {
		if _, err := svc.Start(ctx); err != nil {
			t.Stop(context.WithoutCancel(ctx))
			return nil, fmt.Errorf("start memory services: %w", err)
		}
	}
	// The server image has curl.
	t.client = serverImage(c.imageRef).
		WithSecretVariable("HARNESS_TOKEN", token).
		WithServiceBinding(Alias, t.server)
	// Caddy listens before the server is up.
	if _, err := harness(t.client, "health", `for i in $(seq 150); do `+curlHarness+` -o /dev/null `+harnessURL+`/health 2>/dev/null && exit 0; sleep 2; done; exit 1`).Sync(ctx); err != nil {
		t.Stop(context.WithoutCancel(ctx))
		return nil, fmt.Errorf("memory server not healthy: %w", err)
	}

	if c.fixture && len(fixture) > 0 {
		if err := t.seed(ctx, fixture); err != nil {
			t.Stop(context.WithoutCancel(ctx))
			return nil, err
		}
		t.seeded, t.fixtureSize = true, len(fixture)
		// infer=true may skip or merge a fixture memory with HTTP 200.
		if n, _, err := t.stored(ctx, "seeded", 1); err != nil {
			fmt.Printf("warning: memory fixture seeded but not counted: %v\n", err)
		} else {
			t.seededStored = &n
			if n < len(fixture) {
				fmt.Printf("warning: memory fixture has %d memories but the server stored %d after seeding\n", len(fixture), n)
			}
		}
	}
	t.before, t.beforeErr = t.metrics(ctx, "before")
	t.last, t.lastErr = t.before, t.beforeErr
	return t, nil
}

// forcedEnv is the server env the harness sets over the env file.
func forcedEnv(nonce string) [][2]string {
	return [][2]string{
		{"MEMORY_TASK", nonce},
		{"MEM0_VECTOR_PROVIDER", "pgvector"},
		{"POSTGRES_HOST", "postgres"},
		{"POSTGRES_PORT", "5432"},
		{"POSTGRES_DB", "postgres"},
		{"POSTGRES_USER", "postgres"},
		{"MEM0_HOST", "127.0.0.1"},
		{"MEM0_PORT", fmt.Sprint(serverPort)},
		// Prometheus counters are per process: one worker makes /metrics complete.
		{"MEM0_WORKERS", "1"},
	}
}

// harness runs script in the harness client. phase only keeps Dagger from reusing the result
// of another call with the same script.
func harness(client *dagger.Container, phase, script string) *dagger.Container {
	return client.WithEnvVariable("PHASE", phase).WithExec([]string{"sh", "-c", script})
}

// seed posts each fixture memory as one user message, with inference, under the agent's user.
func (t *Task) seed(ctx context.Context, fixture []string) error {
	client := t.client
	for i, text := range fixture {
		body, err := json.Marshal(map[string]any{
			"messages": []map[string]string{{"role": "user", "content": text}},
			"user_id":  t.userID,
			"infer":    true,
		})
		if err != nil {
			return err
		}
		client = client.WithNewFile(fmt.Sprintf("/seed/%03d.json", i), string(body))
	}
	_, err := harness(client, "seed", `for f in /seed/*.json; do
  `+curlHarness+` --max-time 300 -o /dev/null -H 'Content-Type: application/json' --data-binary @"$f" `+harnessURL+`/memories || exit 1
done`).Sync(ctx)
	if err != nil {
		return fmt.Errorf("seed memory fixture: %w", err)
	}
	return nil
}

// Bind gives the agent container the server container as hippocampus.
func (t *Task) Bind(container *dagger.Container) *dagger.Container {
	if t == nil || t.server == nil {
		return container
	}
	return container.WithServiceBinding(Alias, t.server)
}

// Stop stops the services; Dagger would stop them at the end of the session anyway.
func (t *Task) Stop(ctx context.Context) {
	if t == nil {
		return
	}
	for _, svc := range []*dagger.Service{t.server, t.postgres} {
		if svc == nil {
			continue
		}
		if _, err := svc.Stop(ctx); err != nil {
			fmt.Printf("stop memory service: %v\n", err)
		}
	}
}

// Stats are the memory numbers of one task. Null numbers are unavailable, never zero.
type Stats struct {
	Enabled bool `json:"enabled"`
	// Fixture is true when the task's fixture memories were seeded before the agent started.
	Fixture bool   `json:"fixture"`
	Image   string `json:"image,omitempty"`
	Digest  string `json:"digest,omitempty"`
	// Ops counts the server's memory operations while the agent ran, by operation (add,
	// search, get_all, delete, ...): the mem0_memory tool and the extension's automatic
	// capture (add) and recall (search) together. The seeding is not counted.
	Ops          map[string]float64 `json:"ops"`
	Errors       *float64           `json:"errors"`
	AvgLatencyMs *float64           `json:"avgLatencyMs"`
	// Stored is the number of memories of the agent's user at the end, seeded ones included.
	Stored *int `json:"stored"`
	// SeededStored is the number of memories right after seeding; below the fixture size the
	// server dropped or merged fixture memories.
	SeededStored *int `json:"seededStored,omitempty"`
	// FixtureSize is the number of fixture memories that were seeded.
	FixtureSize int `json:"fixtureSize,omitempty"`
	// Error says why numbers are missing.
	Error string `json:"error,omitempty"`
}

// SessionStats are the memory numbers of one session of a multi-session task.
type SessionStats struct {
	// Ops and Errors count what the server did during the session, like Stats.Ops.
	Ops    map[string]float64 `json:"ops"`
	Errors *float64           `json:"errors"`
	// Stored is the number of memories of the agent's user after the session, and Memories
	// the texts of up to 20 of them, each cut to maxMemoryText bytes: what capture and
	// extraction kept (the extension redacts the evidence before it is sent).
	Stored   *int     `json:"stored"`
	Memories []string `json:"memories,omitempty"`
	// Error says why numbers are missing.
	Error string `json:"error,omitempty"`
}

// AfterSession reads the metrics and the stored memories right after session n (1-based) of
// a multi-session task; nil without memory services. A failure is recorded in Error.
func (t *Task) AfterSession(ctx context.Context, n int) *SessionStats {
	if t == nil || t.server == nil {
		return nil
	}
	stats := &SessionStats{}
	var errs []error
	after, afterErr := t.metrics(ctx, fmt.Sprintf("after-session-%d", n))
	if err := errors.Join(t.lastErr, afterErr); err != nil {
		errs = append(errs, err)
	} else {
		var delta Stats
		delta.setDelta(t.last, after)
		stats.Ops, stats.Errors = delta.Ops, delta.Errors
	}
	t.last, t.lastErr = after, afterErr
	if stored, texts, err := t.stored(ctx, fmt.Sprintf("session-%d", n), 20); err != nil {
		errs = append(errs, err)
	} else {
		stats.Stored, stats.Memories = &stored, texts
	}
	if len(errs) > 0 {
		stats.Error = errors.Join(errs...).Error()
	}
	return stats
}

// Stats reads the metrics after the agent and the stored memories; nil without a Config. A
// failure is recorded in Error, never returned.
func (t *Task) Stats(ctx context.Context) *Stats {
	if t == nil {
		return nil
	}
	stats := &Stats{Enabled: t.server != nil}
	if t.server == nil {
		return stats
	}
	stats.Fixture, stats.Image, stats.Digest = t.seeded, Image+":"+t.config.ref, "sha256:"+t.config.digest
	stats.SeededStored, stats.FixtureSize = t.seededStored, t.fixtureSize
	var errs []error
	after, err := t.metrics(ctx, "after")
	if err = errors.Join(t.beforeErr, err); err != nil {
		errs = append(errs, err)
	} else {
		stats.setDelta(t.before, after)
	}
	if stored, _, err := t.stored(ctx, "end", 1); err != nil {
		errs = append(errs, err)
	} else {
		stats.Stored = &stored
	}
	if len(errs) > 0 {
		stats.Error = errors.Join(errs...).Error()
	}
	return stats
}

func (t *Task) metrics(ctx context.Context, phase string) (metrics, error) {
	text, err := harness(t.client, "metrics-"+phase, curlHarness+" --max-time 30 "+harnessURL+"/metrics").Stdout(ctx)
	if err != nil {
		return metrics{}, fmt.Errorf("read memory metrics (%s): %w", phase, err)
	}
	return parseMetrics(text)
}

// stored counts the user's memories with the admin listing (GET /memories returns at most 20)
// and returns the texts of the first pageSize. phase keeps Dagger from reusing an earlier count.
func (t *Task) stored(ctx context.Context, phase string, pageSize int) (int, []string, error) {
	query := url.Values{"scope": {"user"}, "scope_id": {t.userID}, "page_size": {fmt.Sprint(pageSize)}}
	text, err := harness(t.client, "stored-"+phase, curlHarness+" --max-time 60 '"+harnessURL+"/admin/memories?"+query.Encode()+"'").Stdout(ctx)
	if err != nil {
		return 0, nil, fmt.Errorf("count stored memories: %w", err)
	}
	return parseAdminListing(text)
}

// maxMemoryText bounds each text of SessionStats.Memories.
const maxMemoryText = 300

// parseAdminListing reads total_items and the items' content of GET /admin/memories.
func parseAdminListing(text string) (int, []string, error) {
	var page struct {
		TotalItems *int `json:"total_items"`
		Items      []struct {
			Content string `json:"content"`
		} `json:"items"`
	}
	if err := json.Unmarshal([]byte(text), &page); err != nil || page.TotalItems == nil {
		return 0, nil, fmt.Errorf("count stored memories: no total_items in the admin listing")
	}
	var texts []string
	for _, item := range page.Items {
		content := item.Content
		if len(content) > maxMemoryText {
			cut := maxMemoryText
			for cut > 0 && !utf8.RuneStart(content[cut]) {
				cut--
			}
			content = content[:cut] + "..."
		}
		texts = append(texts, content)
	}
	return *page.TotalItems, texts, nil
}

// UserID is the user_id the extension writes under and searches first, from the effective
// settings.json: readConfig and resolveIdentity in extensions/hippocampus/core.ts. Only an
// explicit projectId is accepted: the Git origin fallback depends on the agent's repo.
func UserID(settingsJSON string) (string, error) {
	var settings struct {
		Hippocampus *struct {
			UserID    any `json:"userId"`
			ProjectID any `json:"projectId"`
		} `json:"hippocampus"`
	}
	if err := json.Unmarshal([]byte(settingsJSON), &settings); err != nil {
		return "", fmt.Errorf("parse agent settings.json: %w", err)
	}
	if settings.Hippocampus == nil {
		return "", errors.New("agent settings.json has no hippocampus section")
	}
	// Like the extension, a non-string or blank value counts as unset.
	trimmed := func(v any) string {
		s, _ := v.(string)
		return strings.TrimSpace(s)
	}
	project := trimmed(settings.Hippocampus.ProjectID)
	if project == "" {
		return "", errors.New("agent settings.json: hippocampus.projectId must be set")
	}
	user := trimmed(settings.Hippocampus.UserID)
	if user == "" {
		user = "default-user"
	}
	sum := sha256.Sum256([]byte("explicit:" + project))
	return user + ":project:" + hex.EncodeToString(sum[:])[:24], nil
}

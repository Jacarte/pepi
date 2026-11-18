// Package network measures TestEnv and agent traffic in the same exec namespace.
package network

import (
	"context"
	"crypto/rand"
	_ "embed"
	"encoding/json"
	"fmt"
	"path"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"dagger/daggervals/internal/dagger"
)

const reportPath = "/tmp/daggervals-network.json"

// TimeoutMarkerPath exists in the result container when the measured TestEnv/agent hit
// its time limit; capture.sh still emits the stats then.
const TimeoutMarkerPath = "/tmp/daggervals-task-timeout"

// AgentExitMarkerPath holds the agent's exit status when the agent itself exited non-zero
// (not a timeout, not TestEnv); capture.sh still emits the stats then.
const AgentExitMarkerPath = "/tmp/daggervals-agent-exit"

// agentStartedLine and agentExitedLine are what capture.sh prints to stderr around the agent.
const (
	agentStartedLine = "daggervals: agent started\n"
	agentExitedLine  = "daggervals: agent exited with status "
)

// AgentError keeps at most this much of the agent's stderr.
const (
	agentErrorLines = 10
	agentErrorBytes = 600
)

//go:embed capture.sh
var captureScript string

//go:embed report.mjs
var reportScript string

// Stats counts observed IP packets, not HTTP requests. Bytes include IP headers
// and retransmissions, but exclude link-layer headers. Flows are directional.
// DNSCandidates are names seen in DNS responses during this capture, not proof
// of a connection's hostname (shared IPs, caches, encrypted DNS and TTLs apply).
type Stats struct {
	IPPackets    int64       `json:"ipPackets"`
	IPBytes      int64       `json:"ipBytes"`
	NonIPPackets int64       `json:"nonIPPackets"`
	LocalIPs     []string    `json:"localIPs"`
	IPSummaries  []IPSummary `json:"ipSummaries"`
	DNSQueries   []DNSQuery  `json:"dnsQueries"`
	Flows        []Flow      `json:"flows"`
}

// IPSummary describes a peer of a known local endpoint. LocalIPs are read from
// interfaces in the capture's network namespace at report time. BytesIn/Out are
// relative to the container. Local-to-local traffic contributes to both sides;
// flows without a known local endpoint are excluded from summaries, not Flows.
// Hits counts distinct bidirectional IP/protocol/port 5-tuples, not HTTP requests
// or connections: replies, retransmissions and tuple reuse do not add hits.
// DNSCandidates remain heuristic; bytes are never attributed to domains.
type IPSummary struct {
	IP            string   `json:"ip"`
	BytesIn       int64    `json:"bytesIn"`
	BytesOut      int64    `json:"bytesOut"`
	Hits          int64    `json:"hits"`
	DNSCandidates []string `json:"dnsCandidates"`
}

type DNSQuery struct {
	Name  string `json:"name"`
	Type  int    `json:"type"`
	Count int64  `json:"count"`
}

type Flow struct {
	SourceIP                 string   `json:"sourceIP"`
	DestinationIP            string   `json:"destinationIP"`
	Protocol                 string   `json:"protocol"`
	SourcePort               int      `json:"sourcePort"`
	DestinationPort          int      `json:"destinationPort"`
	Packets                  int64    `json:"packets"`
	Bytes                    int64    `json:"bytes"`
	SourceDNSCandidates      []string `json:"sourceDNSCandidates"`
	DestinationDNSCandidates []string `json:"destinationDNSCandidates"`
}

// Measure deliberately uses one exec: background capture cannot survive a
// Dagger exec boundary. Tool installation, PiSetup and Env must already be synced.
// timeoutSeconds limits TestEnv plus the agent; capture.sh validates it. It is passed as
// an env var of that one process, not set in the container env.
func Measure(container *dagger.Container, testEnvScript string, command []string, timeoutSeconds int) (*dagger.Container, error) {
	if len(command) == 0 || command[0] == "" {
		return nil, fmt.Errorf("network measurement: empty agent command")
	}
	const dir = "/tmp/daggervals-measure"
	args := []string{"env", fmt.Sprintf("DAGGERVALS_TASK_TIMEOUT_SECONDS=%d", timeoutSeconds),
		"bash", dir + "/capture.sh", rand.Text(), dir + "/test-env.sh", dir + "/report.mjs", reportPath}
	args = append(args, command...)
	return container.
		WithNewFile(dir+"/capture.sh", captureScript).
		WithNewFile(dir+"/test-env.sh", testEnvScript).
		WithNewFile(dir+"/report.mjs", reportScript).
		WithExec(args), nil
}

func ReadStats(ctx context.Context, container *dagger.Container) (Stats, error) {
	contents, err := container.File(reportPath).Contents(ctx)
	if err != nil {
		return Stats{}, fmt.Errorf("read network stats: %w", err)
	}
	var stats Stats
	if err := json.Unmarshal([]byte(contents), &stats); err != nil {
		return Stats{}, fmt.Errorf("parse network stats: %w", err)
	}
	return stats, nil
}

// AgentExit is the agent's own non-zero exit status and the tail of its stderr, 0 and "" when
// it exited 0 or timed out. The tail has the agent env file's values replaced: they are plain
// env, not Dagger secrets. The memory server's env is never in this container.
func AgentExit(ctx context.Context, container *dagger.Container, agentEnv *dagger.EnvFile) (int, string, error) {
	exists, err := container.Directory("/tmp").Exists(ctx, path.Base(AgentExitMarkerPath))
	if err != nil {
		return 0, "", fmt.Errorf("read agent exit marker: %w", err)
	}
	if !exists {
		return 0, "", nil
	}
	contents, err := container.File(AgentExitMarkerPath).Contents(ctx)
	if err != nil {
		return 0, "", fmt.Errorf("read agent exit marker: %w", err)
	}
	code, err := strconv.Atoi(strings.TrimSpace(contents))
	if err != nil || code <= 0 {
		return 0, "", fmt.Errorf("agent exit marker holds %q", contents)
	}
	stderr, err := container.Stderr(ctx)
	if err != nil {
		return 0, "", fmt.Errorf("read agent stderr: %w", err)
	}
	var values []string
	if agentEnv != nil {
		vars, err := agentEnv.Variables(ctx)
		if err != nil {
			return 0, "", fmt.Errorf("read agent env: %w", err)
		}
		for _, v := range vars {
			value, err := v.Value(ctx)
			if err != nil {
				return 0, "", fmt.Errorf("read agent env: %w", err)
			}
			values = append(values, value)
		}
	}
	return code, agentStderrTail(stderr, values), nil
}

// agentStderrTail is the agent's part of the exec's stderr (between capture.sh's lines) with
// secrets replaced, then its last lines cut to agentErrorBytes. Redacting before cutting keeps
// a secret that spans the cut from leaking its tail.
func agentStderrTail(stderr string, secrets []string) string {
	if i := strings.LastIndex(stderr, agentStartedLine); i >= 0 {
		stderr = stderr[i+len(agentStartedLine):]
	}
	if i := strings.LastIndex(stderr, agentExitedLine); i >= 0 {
		stderr = stderr[:i]
	}
	lines := strings.Split(strings.TrimRight(redact(stderr, secrets), "\n"), "\n")
	if len(lines) > agentErrorLines {
		lines = lines[len(lines)-agentErrorLines:]
	}
	tail := strings.Join(lines, "\n")
	if len(tail) <= agentErrorBytes {
		return tail
	}
	cut := len(tail) - agentErrorBytes
	for cut < len(tail) && !utf8.RuneStart(tail[cut]) {
		cut++
	}
	return "..." + tail[cut:]
}

// Short env values (HOME=/root, flags) are not secrets and would garble the text.
const minSecretLength = 8

var (
	authPattern = regexp.MustCompile(`(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+`)
	keyPattern  = regexp.MustCompile(`\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})`)
)

// redact replaces each secret, longest first, and token-shaped strings.
func redact(text string, secrets []string) string {
	secrets = slices.Clone(secrets)
	slices.SortFunc(secrets, func(a, b string) int { return len(b) - len(a) })
	for _, secret := range secrets {
		if len(secret) >= minSecretLength {
			text = strings.ReplaceAll(text, secret, "[REDACTED]")
		}
	}
	text = authPattern.ReplaceAllString(text, "$1 [REDACTED]")
	return keyPattern.ReplaceAllString(text, "[REDACTED]")
}

// Merge is the traffic of several measurements of one task (the sessions of a multi-session
// task): counters are summed and entries with the same key are combined, so Hits counts
// distinct 5-tuples per session.
func Merge(all ...Stats) Stats {
	// Empty lists, not null, as in a report.
	merged := Stats{LocalIPs: []string{}, IPSummaries: []IPSummary{}, DNSQueries: []DNSQuery{}, Flows: []Flow{}}
	summaries, queries, flows := map[string]int{}, map[string]int{}, map[string]int{}
	for _, s := range all {
		merged.IPPackets += s.IPPackets
		merged.IPBytes += s.IPBytes
		merged.NonIPPackets += s.NonIPPackets
		merged.LocalIPs = union(merged.LocalIPs, s.LocalIPs)
		for _, ip := range s.IPSummaries {
			if i, ok := summaries[ip.IP]; ok {
				m := &merged.IPSummaries[i]
				m.BytesIn, m.BytesOut, m.Hits = m.BytesIn+ip.BytesIn, m.BytesOut+ip.BytesOut, m.Hits+ip.Hits
				m.DNSCandidates = union(m.DNSCandidates, ip.DNSCandidates)
				continue
			}
			summaries[ip.IP] = len(merged.IPSummaries)
			ip.DNSCandidates = slices.Clone(ip.DNSCandidates)
			merged.IPSummaries = append(merged.IPSummaries, ip)
		}
		for _, q := range s.DNSQueries {
			key := fmt.Sprintf("%s/%d", q.Name, q.Type)
			if i, ok := queries[key]; ok {
				merged.DNSQueries[i].Count += q.Count
				continue
			}
			queries[key] = len(merged.DNSQueries)
			merged.DNSQueries = append(merged.DNSQueries, q)
		}
		for _, f := range s.Flows {
			key := fmt.Sprintf("%s|%s|%s|%d|%d", f.SourceIP, f.DestinationIP, f.Protocol, f.SourcePort, f.DestinationPort)
			if i, ok := flows[key]; ok {
				m := &merged.Flows[i]
				m.Packets, m.Bytes = m.Packets+f.Packets, m.Bytes+f.Bytes
				m.SourceDNSCandidates = union(m.SourceDNSCandidates, f.SourceDNSCandidates)
				m.DestinationDNSCandidates = union(m.DestinationDNSCandidates, f.DestinationDNSCandidates)
				continue
			}
			flows[key] = len(merged.Flows)
			f.SourceDNSCandidates, f.DestinationDNSCandidates = slices.Clone(f.SourceDNSCandidates), slices.Clone(f.DestinationDNSCandidates)
			merged.Flows = append(merged.Flows, f)
		}
	}
	return merged
}

// union appends the values of b that a lacks, keeping the order of appearance.
func union(a, b []string) []string {
	for _, v := range b {
		if !slices.Contains(a, v) {
			a = append(a, v)
		}
	}
	return a
}

// Package network measures TestEnv and agent traffic in the same exec namespace.
package network

import (
	"context"
	"crypto/rand"
	_ "embed"
	"encoding/json"
	"fmt"

	"dagger/daggervals/internal/dagger"
)

const reportPath = "/tmp/daggervals-network.json"

// TimeoutMarkerPath exists in the result container when the measured TestEnv/agent hit
// its time limit; capture.sh still emits the stats then.
const TimeoutMarkerPath = "/tmp/daggervals-task-timeout"

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

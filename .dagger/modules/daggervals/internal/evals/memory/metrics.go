package memory

import (
	"fmt"
	"strconv"
	"strings"
)

// metrics are the Hippocampus counters behind Stats. The harness's own /metrics reads are
// left out of the latency.
type metrics struct {
	// ops is memory_operations_total by operation.
	ops          map[string]float64
	errors       float64
	latencySum   float64
	latencyCount float64
}

// parseMetrics reads the Prometheus text format of the server's /metrics.
func parseMetrics(text string) (metrics, error) {
	m := metrics{ops: map[string]float64{}}
	hippocampus := false
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "# TYPE memory_operations") {
			hippocampus = true
		}
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, labels, value, err := parseSample(line)
		if err != nil {
			return metrics{}, err
		}
		self := labels["path"] == "/metrics"
		switch {
		case name == "memory_operations_total":
			m.ops[labels["operation"]] += value
		case name == "memory_operation_errors_total":
			m.errors += value
		case name == "http_request_duration_seconds_sum" && !self:
			m.latencySum += value
		case name == "http_request_duration_seconds_count" && !self:
			m.latencyCount += value
		}
	}
	if !hippocampus {
		return metrics{}, fmt.Errorf("parse memory metrics: no memory_operations_total, not a Hippocampus server")
	}
	return m, nil
}

// parseSample splits `name{k="v",...} value [timestamp]`.
func parseSample(line string) (string, map[string]string, float64, error) {
	labels := map[string]string{}
	end := strings.IndexAny(line, "{ ")
	if end <= 0 {
		return "", nil, 0, fmt.Errorf("parse memory metrics: bad sample %q", line)
	}
	name, rest := line[:end], line[end:]
	if strings.HasPrefix(rest, "{") {
		rest = rest[1:]
		for {
			rest = strings.TrimLeft(rest, ", ")
			if strings.HasPrefix(rest, "}") {
				rest = rest[1:]
				break
			}
			key, after, ok := strings.Cut(rest, `="`)
			if !ok {
				return "", nil, 0, fmt.Errorf("parse memory metrics: bad labels in %q", line)
			}
			var value strings.Builder
			i := 0
			for ; i < len(after) && after[i] != '"'; i++ {
				if after[i] == '\\' && i+1 < len(after) {
					i++
					if after[i] == 'n' {
						value.WriteByte('\n')
						continue
					}
				}
				value.WriteByte(after[i])
			}
			if i == len(after) {
				return "", nil, 0, fmt.Errorf("parse memory metrics: unterminated label in %q", line)
			}
			labels[key] = value.String()
			rest = after[i+1:]
		}
	}
	fields := strings.Fields(rest)
	if len(fields) == 0 {
		return "", nil, 0, fmt.Errorf("parse memory metrics: no value in %q", line)
	}
	value, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return "", nil, 0, fmt.Errorf("parse memory metrics: bad value in %q", line)
	}
	return name, labels, value, nil
}

// setDelta fills the numbers of what happened between the two reads.
func (s *Stats) setDelta(before, after metrics) {
	s.Ops = map[string]float64{}
	for op, n := range after.ops {
		if d := n - before.ops[op]; d > 0 {
			s.Ops[op] = d
		}
	}
	errs := after.errors - before.errors
	s.Errors = &errs
	if count := after.latencyCount - before.latencyCount; count > 0 {
		avg := (after.latencySum - before.latencySum) / count * 1000
		s.AvgLatencyMs = &avg
	}
}

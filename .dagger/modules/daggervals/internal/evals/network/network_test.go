package network

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestReportParser(t *testing.T) {
	if _, err := exec.LookPath("node"); err != nil {
		t.Skip("node is required for the embedded packet report parser tests")
	}
	output, err := exec.Command("node", "--test", "report.test.mjs").CombinedOutput()
	if err != nil {
		t.Fatalf("report parser tests: %v\n%s", err, output)
	}
}

func TestStatsIPSummary(t *testing.T) {
	var stats Stats
	err := json.Unmarshal([]byte(`{
		"localIPs": ["10.0.0.2"],
		"ipSummaries": [{"ip":"192.0.2.1","bytesIn":70,"bytesOut":60,"hits":1,"dnsCandidates":["example.com"]}]
	}`), &stats)
	if err != nil {
		t.Fatal(err)
	}
	if len(stats.LocalIPs) != 1 || stats.LocalIPs[0] != "10.0.0.2" || len(stats.IPSummaries) != 1 {
		t.Fatalf("missing local endpoints or IP summaries: %+v", stats)
	}
	summary := stats.IPSummaries[0]
	if summary.IP != "192.0.2.1" || summary.BytesIn != 70 || summary.BytesOut != 60 || summary.Hits != 1 ||
		len(summary.DNSCandidates) != 1 || summary.DNSCandidates[0] != "example.com" {
		t.Fatalf("incorrect IP summary: %+v", summary)
	}
}

// Opt-in: requires a running Dagger engine and outbound DNS/HTTPS, not credentials.
func TestCaptureIntegration(t *testing.T) {
	if os.Getenv("DAGGERVALS_NETWORK_INTEGRATION") != "1" {
		t.Skip("set DAGGERVALS_NETWORK_INTEGRATION=1 to run the unprivileged capture integration")
	}
	dir := t.TempDir()
	files := map[string]string{
		"capture.sh": captureScript,
		"report.mjs": reportScript,
		"setup.sh":   "curl -fsSI https://example.com >/dev/null\necho setup-output\nexport TEST_ENV_READY=yes\n",
		"agent.sh":   "test \"$TEST_ENV_READY\" = yes\ncurl -fsSI https://example.org >/dev/null\nprintf 'hello world'\n",
		"fail.sh":    "exit 23\n",
		"empty.sh":   "",
		"slow.sh":    "touch /tmp/task-started\nsleep 300\n",
		"check.sh": `#!/bin/bash
set -euo pipefail
measure() { bash /probe/capture.sh "$RANDOM" /probe/"$1" /probe/report.mjs /tmp/daggervals-network.json bash /probe/"$2"; }
clean() {
    test -z "$(find /tmp -name '*.pcap' -o -name 'daggervals-net.*')"
    ! pgrep -x tcpdump
}
# Preparation traffic is intentionally outside the measurement.
curl -fsSI https://example.net >/dev/null
measure setup.sh agent.sh > /tmp/answer
[[ $(cat /tmp/answer) == 'hello world' ]]
rm /tmp/answer
clean
# Oracle traffic must not appear in the already finalized report.
curl -fsSI https://example.net >/dev/null
node --input-type=module - <<'JS'
import fs from 'node:fs';
import assert from 'node:assert/strict';
const s = JSON.parse(fs.readFileSync('/tmp/daggervals-network.json'));
assert.deepEqual([...new Set(s.dnsQueries.map(q => q.name))].sort(), ['example.com', 'example.org']);
const https = s.flows.find(f => f.destinationPort === 443 && s.localIPs.includes(f.sourceIP));
assert(https);
const peer = s.ipSummaries.find(summary => summary.ip === https.destinationIP);
assert(peer && peer.bytesIn > 0 && peer.bytesOut > 0 && peer.hits > 0);
assert(s.ipBytes > 0);
assert.equal(s.ipBytes, s.flows.reduce((sum, f) => sum + f.bytes, 0));
assert.equal(s.ipPackets, s.flows.reduce((sum, f) => sum + f.packets, 0));
console.log('DNS/HTTPS capture, setup boundary, oracle boundary, stdout and byte totals passed');
JS
for scenario in 'fail.sh agent.sh' 'empty.sh fail.sh'; do
    status=0
    measure $scenario || status=$?
    [[ $status == 23 ]]
    test ! -e /tmp/daggervals-network.json
    clean
done
# Cancellation must interrupt wait and reap the workload and capture.
bash /probe/capture.sh cancel /probe/empty.sh /probe/report.mjs /tmp/daggervals-network.json bash /probe/slow.sh &
pid=$!
for ((i=0; i<100; i++)); do [[ -e /tmp/task-started ]] && break; sleep 0.1; done
test -e /tmp/task-started
kill -TERM "$pid"
status=0
wait "$pid" || status=$?
[[ $status == 143 ]]
clean
test ! -e /tmp/daggervals-network.json
# Missing capture permission/tool must fail closed, not return empty stats.
mkdir /fake
printf '#!/bin/sh\nexit 1\n' > /fake/tcpdump
chmod +x /fake/tcpdump
status=0
PATH=/fake:$PATH measure empty.sh agent.sh || status=$?
[[ $status != 0 ]]
clean
test ! -e /tmp/daggervals-network.json
# Counter failures must survive raw-log cleanup without exposing other log data.
cat > /fake/tcpdump <<'JS'
#!/usr/bin/node
const fs = require('node:fs');
fs.writeFileSync(process.argv.at(-1), 'PRIVATE_CAPTURE_PAYLOAD');
process.on('SIGINT', () => {
    process.stderr.write('12 packets captured\n15 packets received by filter\n');
    if (process.env.TEST_DROPS !== 'missing') process.stderr.write('3 packets dropped by kernel\n');
    process.exit(0);
});
process.stderr.write('tcpdump: listening on test\nPRIVATE_LOG_VALUE\n');
setInterval(() => {}, 1000);
JS
for drops in missing 3; do
    status=0
    TEST_DROPS=$drops PATH=/fake:$PATH measure empty.sh empty.sh 2>/tmp/counter-error || status=$?
    [[ $status == 1 ]]
    if [[ $drops == missing ]]; then
        grep -F 'missing or unrecognized "packets dropped by kernel" counter' /tmp/counter-error
    else
        grep -F 'tcpdump reported 3 packets dropped by kernel' /tmp/counter-error
        grep -x '3 packets dropped by kernel' /tmp/counter-error
    fi
    grep -x '12 packets captured' /tmp/counter-error
    grep -x '15 packets received by filter' /tmp/counter-error
    ! grep -q 'PRIVATE_' /tmp/counter-error
    rm /tmp/counter-error
    test ! -e /tmp/daggervals-network.json
    clean
done
# tshark rejects a malformed pcap, rather than generating successful zero stats.
printf 'bad pcap' >/tmp/bad.pcap
printf '0 packets captured\n0 packets dropped by kernel\n' >/tmp/bad.log
if node /probe/report.mjs /tmp/bad.pcap /tmp/bad.log; then exit 1; fi
rm /tmp/bad.pcap /tmp/bad.log
clean
echo 'failure and cancellation cleanup passed'
`,
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, "dagger", "-m", "core", "api", "call", "container", "from", "--address=ubuntu:24.04",
		"with-exec", "--args=sh,-c,export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq tcpdump tshark nodejs curl >/dev/null",
		"with-directory", "--path=/probe", "--source="+dir,
		"with-exec", "--args=bash,/probe/check.sh", "stdout")
	cmd.Dir = os.TempDir()
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("live unprivileged capture: %v\n%s", err, output)
	}
	t.Log(string(output))
}

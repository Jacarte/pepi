import test from 'node:test';
import assert from 'node:assert/strict';
import {networkInterfaces} from 'node:os';
import {summarize as summarizeReport, summarizeCaptureLog} from './report.mjs';

const interfaces = {eth0: [{address: '10.0.0.2'}]};
const summarize = (lines, localInterfaces = interfaces) => summarizeReport(lines, localInterfaces);

const packet = (fields = {}) => JSON.stringify({layers: {
  frame_len: ['80'], frame_cap_len: ['80'], frame_protocols: ['sll:ip:udp:dns'],
  ip_src: ['10.0.0.2'], ip_dst: ['10.0.0.1'], ip_len: ['60'], ip_proto: ['17'],
  udp_srcport: ['1234'], udp_dstport: ['53'], ...fields,
}});

test('DNS queries are separate from flows; shared-IP candidates never duplicate bytes', async () => {
  const lines = [];
  for (const name of ['Alias.Example.', 'other.example']) {
    lines.push(packet({dns_flags_response: ['False'], dns_qry_name: [name], dns_qry_type: ['1']}));
    lines.push(packet({
      ip_src: ['10.0.0.1'], ip_dst: ['10.0.0.2'], udp_srcport: ['53'], udp_dstport: ['1234'],
      dns_flags_response: ['True'], dns_qry_name: [name], dns_qry_type: ['1'],
      dns_cname: ['cdn.example'], dns_a: ['192.0.2.1'],
    }));
  }
  lines.push(packet({
    frame_protocols: ['sll:ip:tcp:tls'], ip_dst: ['192.0.2.1'], ip_proto: ['6'],
    tcp_srcport: ['4321'], tcp_dstport: ['443'],
  }));
  const result = await summarize(lines);
  assert.deepEqual(result.dnsQueries, [
    {name: 'alias.example', type: 1, count: 1}, {name: 'other.example', type: 1, count: 1},
  ]);
  assert.equal(result.ipPackets, 5);
  assert.equal(result.ipBytes, 300);
  assert.equal(result.flows.reduce((sum, f) => sum + f.bytes, 0), 300);
  const https = result.flows.find(f => f.destinationPort === 443);
  assert.deepEqual(https.destinationDNSCandidates, ['alias.example', 'other.example']);
  assert.equal(https.packets, 1);
  assert.deepEqual(result.localIPs, ['10.0.0.2']);
  assert.deepEqual(result.ipSummaries, [
    {ip: '10.0.0.1', bytesIn: 120, bytesOut: 120, hits: 1, dnsCandidates: []},
    {ip: '192.0.2.1', bytesIn: 0, bytesOut: 60, hits: 1, dnsCandidates: ['alias.example', 'other.example']},
  ]);
});

test('IPv6, extension headers, repeat DNS queries, non-IP and unattributed traffic', async () => {
  const ipv6 = {
    frame_protocols: ['sll:ipv6:ipv6.hopopts:udp:dns'], ip_src: undefined, ip_dst: undefined,
    ipv6_src: ['2001:db8::1'], ipv6_dst: ['2001:db8::2'], ipv6_plen: ['20'], ipv6_nxt: ['0'],
    dns_flags_response: ['False'], dns_qry_name: ['v6.example'], dns_qry_type: ['28'],
  };
  const result = await summarize([
    '{"index":{}}', packet(ipv6), packet(ipv6),
    packet({...ipv6, dns_flags_response: ['True'], dns_aaaa: ['2001:db8::2']}),
    JSON.stringify({layers: {frame_len: ['30'], frame_cap_len: ['30'], frame_protocols: ['sll:arp']}}),
  ]);
  assert.equal(result.nonIPPackets, 1);
  assert.equal(result.ipBytes, 180);
  assert.deepEqual(result.dnsQueries, [{name: 'v6.example', type: 28, count: 2}]);
  assert.equal(result.flows[0].protocol, 'udp');
  assert.deepEqual(result.flows[0].sourceDNSCandidates, []);
  assert.deepEqual(result.flows[0].destinationDNSCandidates, ['v6.example']);
});

test('tunnels count the outer IP length only', async () => {
  const result = await summarize([packet({
    frame_protocols: ['sll:ipv6:ip:tcp'], ipv6_src: ['::1'], ipv6_dst: ['::2'],
    ipv6_plen: ['80'], ipv6_nxt: ['4'], tcp_srcport: ['123'], tcp_dstport: ['443'],
  })]);
  assert.equal(result.ipBytes, 120);
  assert.equal(result.flows[0].sourceIP, '::1');
  assert.equal(result.flows[0].protocol, 'ip:4');
  assert.equal(result.flows[0].destinationPort, 0);
});

test('reject malformed/truncated capture data', async () => {
  await assert.rejects(summarize(['not json']));
  await assert.rejects(summarize(['{}']), /missing packet/);
  await assert.rejects(summarize([packet({frame_cap_len: ['40']})]), /truncated/);
  await assert.rejects(summarize([packet({ip_len: ['NaN']})]), /invalid numeric/);
  assert.deepEqual(await summarize([], {}), {
    ipPackets: 0, ipBytes: 0, nonIPPackets: 0, localIPs: [], ipSummaries: [], dnsQueries: [], flows: [],
  });
});

test('capture counters accept zero drops, including singular and CRLF output', () => {
  assert.equal(summarizeCaptureLog('0 packets captured\n0 packets dropped by kernel'), 0);
  assert.equal(summarizeCaptureLog(' 1 packet captured\r\n2 packets received by filter\r\n0 packets dropped by kernel\r\n'), 1);
  assert.equal(summarizeCaptureLog('12 packets captured\n12 packets received by filter\n0 packets dropped by kernel\n'), 12);
});

test('capture counter errors distinguish missing counters from actual drops', () => {
  for (const {log, reason, details} of [
    {
      log: '',
      reason: 'missing or unrecognized "packets dropped by kernel" counter',
      details: '(none recognized)',
    },
    {
      log: '12 packets captured\n15 packets received by filter\n',
      reason: 'missing or unrecognized "packets dropped by kernel" counter',
      details: '12 packets captured\n15 packets received by filter',
    },
    {
      log: '0 packets dropped by kernel\n',
      reason: 'missing or unrecognized "packets captured" counter',
      details: '0 packets dropped by kernel',
    },
    {
      log: '12 packets captured\n15 packets received by filter\n3 packets dropped by kernel\n',
      reason: 'tcpdump reported 3 packets dropped by kernel',
      details: '12 packets captured\n15 packets received by filter\n3 packets dropped by kernel',
    },
    {
      log: '1 packet captured\n1 packet dropped by kernel\n',
      reason: 'tcpdump reported 1 packet dropped by kernel',
      details: '1 packet captured\n1 packet dropped by kernel',
    },
    {
      log: '0 packets dropped by kernel\n12 packets captured\n3 packets dropped by kernel\n',
      reason: 'tcpdump reported 3 packets dropped by kernel',
      details: '0 packets dropped by kernel\n12 packets captured\n3 packets dropped by kernel',
    },
  ]) {
    assert.throws(() => summarizeCaptureLog(log), {
      message: `incomplete capture: ${reason}\ntcpdump counters:\n${details}`,
    });
  }
});

test('capture counter diagnostics omit arbitrary logs and unrecognized lines', () => {
  const log = [
    'tcpdump: listening on secret-interface',
    'private-token=SECRET',
    '12 packets captured',
    '15 packets received by filter',
    '0 packets dropped by kernel private-token=SECRET',
    'kernel drops: unknown-format',
  ].join('\n');
  assert.throws(() => summarizeCaptureLog(log), {
    message: 'incomplete capture: missing or unrecognized "packets dropped by kernel" counter\ntcpdump counters:\n12 packets captured\n15 packets received by filter',
  });
});

test('IP summary hits deduplicate bidirectional tuples, not packets or HTTP requests', async () => {
  const outgoing = packet({ip_dst: ['192.0.2.1']});
  const incoming = packet({
    ip_src: ['192.0.2.1'], ip_dst: ['10.0.0.2'], udp_srcport: ['53'], udp_dstport: ['1234'], ip_len: ['70'],
  });
  const result = await summarize([
    outgoing, outgoing, incoming, incoming,
    packet({ip_dst: ['192.0.2.1'], udp_srcport: ['4321']}),
    packet({frame_protocols: ['sll:ip:tcp'], ip_proto: ['6'], ip_dst: ['192.0.2.1'], tcp_srcport: ['1234'], tcp_dstport: ['53']}),
    packet({ip_src: ['192.0.2.3'], ip_dst: ['192.0.2.4']}), // Neither endpoint is local.
  ]);
  assert.deepEqual(result.ipSummaries, [
    {ip: '192.0.2.1', bytesIn: 140, bytesOut: 240, hits: 3, dnsCandidates: []},
  ]);
  assert.equal(result.ipPackets, 7);
  assert.equal(result.ipBytes, 440);
  assert(result.flows.some(f => f.sourceIP === '192.0.2.3'));
});

test('local-to-local and loopback bytes count both container-relative directions', async () => {
  const result = await summarize([
    packet({ip_src: ['127.0.0.1'], ip_dst: ['127.0.0.1']}),
    packet({ip_dst: ['10.0.0.3']}),
  ], {lo: [{address: '127.0.0.1'}], eth0: [{address: '10.0.0.2'}, {address: '10.0.0.3'}], absent: undefined});
  assert.deepEqual(result.localIPs, ['10.0.0.2', '10.0.0.3', '127.0.0.1']);
  assert.deepEqual(result.ipSummaries, [
    {ip: '10.0.0.2', bytesIn: 60, bytesOut: 0, hits: 1, dnsCandidates: []},
    {ip: '10.0.0.3', bytesIn: 0, bytesOut: 60, hits: 1, dnsCandidates: []},
    {ip: '127.0.0.1', bytesIn: 60, bytesOut: 60, hits: 1, dnsCandidates: []},
  ]);
});

test('IPv6 summary matches canonical interface and DNS addresses while preserving raw flows', async () => {
  const ipv6 = {
    frame_protocols: ['sll:ipv6:udp'], ipv6_src: ['2001:db8::1'], ipv6_dst: ['2001:0db8:0:0:0:0:0:2'],
    ipv6_plen: ['20'], ipv6_nxt: ['17'], dns_flags_response: ['True'],
    dns_qry_name: ['V6.Example.'], dns_aaaa: ['2001:db8::2'],
  };
  const result = await summarize([
    packet(ipv6),
    packet({...ipv6, ipv6_src: ['2001:db8::2'], ipv6_dst: ['2001:db8::1'], udp_srcport: ['53'], udp_dstport: ['1234']}),
  ], {eth0: [{address: '2001:0DB8:0:0:0:0:0:1%eth0'}, {address: '2001:db8::1'}]});
  assert.deepEqual(result.localIPs, ['2001:db8::1']);
  assert.deepEqual(result.ipSummaries, [
    {ip: '2001:db8::2', bytesIn: 60, bytesOut: 60, hits: 1, dnsCandidates: ['v6.example']},
  ]);
  const outgoing = result.flows.find(f => f.sourceIP === '2001:db8::1');
  assert.equal(outgoing.destinationIP, '2001:0db8:0:0:0:0:0:2');
  assert.deepEqual(outgoing.destinationDNSCandidates, ['v6.example']);
});

test('default local addresses come from interfaces in the reporting process namespace', async () => {
  assert.deepEqual(await summarizeReport([]), await summarizeReport([], networkInterfaces()));
});

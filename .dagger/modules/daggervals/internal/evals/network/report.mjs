import {spawn} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {isIP} from 'node:net';
import {networkInterfaces} from 'node:os';
import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';

const fields = [
  'frame.len', 'frame.cap_len', 'frame.protocols', 'ip.src', 'ip.dst', 'ip.len', 'ip.proto',
  'ipv6.src', 'ipv6.dst', 'ipv6.plen', 'ipv6.nxt',
  'tcp.srcport', 'tcp.dstport', 'udp.srcport', 'udp.dstport',
  'dns.flags.response', 'dns.qry.name', 'dns.qry.type', 'dns.a', 'dns.aaaa',
];

export function summarizeCaptureLog(log) {
  // Only numeric counter lines are safe to return; never echo arbitrary logs.
  const counters = [...log.matchAll(/^[\t ]*(\d+)[\t ]+packets? (captured|received by filter|dropped by kernel|dropped by interface)[\t ]*\r?$/gm)];
  const fail = reason => {
    const details = counters.map(([line]) => line.trim()).join('\n') || '(none recognized)';
    throw new Error(`incomplete capture: ${reason}\ntcpdump counters:\n${details}`);
  };
  const drops = counters.filter(([, , kind]) => kind === 'dropped by kernel');
  if (drops.length === 0) fail('missing or unrecognized "packets dropped by kernel" counter');
  const loss = drops.find(([, count]) => Number(count) !== 0);
  if (loss) fail(`tcpdump reported ${loss[0].trim()}`);
  const captured = counters.find(([, , kind]) => kind === 'captured');
  if (!captured) fail('missing or unrecognized "packets captured" counter');
  return Number(captured[1]);
}

// Canonicalize IPv6 spelling for interface/DNS matching (scope IDs are not in
// captured IP headers). Keep the original addresses in the detailed flows.
const canonicalIP = ip => {
  const address = ip.split('%')[0];
  return isIP(address) === 6 ? new URL(`http://[${address}]`).hostname.slice(1, -1) : address;
};

export async function summarize(lines, interfaces = networkInterfaces()) {
  const localIPs = [...new Set(Object.values(interfaces).flatMap(addresses =>
    (addresses ?? []).map(({address}) => canonicalIP(address))))].sort();
  const local = new Set(localIPs);
  const flows = new Map();
  const queries = new Map();
  const namesByIP = new Map();
  const stats = {ipPackets: 0, ipBytes: 0, nonIPPackets: 0, localIPs, ipSummaries: [], dnsQueries: [], flows: []};
  const first = (packet, key) => packet[key]?.[0];
  const number = (value) => {
    const n = Number(value);
    if (value === undefined || !Number.isSafeInteger(n) || n < 0) {
      throw new Error(`invalid numeric packet field: ${value}`);
    }
    return n;
  };
  const normalize = (name) => name.toLowerCase().replace(/\.$/, '');
  for await (const line of lines) {
    if (!line.trim()) continue;
    const record = JSON.parse(line);
    if (record.index) continue; // tshark EK emits an index line per packet.
    const p = record.layers;
    if (!p) throw new Error('missing packet layers');
    if (number(first(p, 'frame_cap_len')) < number(first(p, 'frame_len'))) {
      throw new Error('truncated packet in capture');
    }
    // Count the outer IP packet once, including tunnels and retransmissions.
    const protocols = (first(p, 'frame_protocols') ?? '').split(':');
    const ipIndex = protocols.findIndex(protocol => protocol === 'ip' || protocol === 'ipv6');
    const ipv4 = protocols[ipIndex] === 'ip';
    const sourceIP = first(p, ipv4 ? 'ip_src' : 'ipv6_src');
    const destinationIP = first(p, ipv4 ? 'ip_dst' : 'ipv6_dst');
    if (!sourceIP || !destinationIP) {
      stats.nonIPPackets++;
      continue;
    }
    const bytes = number(first(p, ipv4 ? 'ip_len' : 'ipv6_plen')) + (ipv4 ? 0 : 40);
    const protocolNumber = number(first(p, ipv4 ? 'ip_proto' : 'ipv6_nxt'));
    const nextIP = protocols.findIndex((protocol, i) => i > ipIndex && (protocol === 'ip' || protocol === 'ipv6'));
    const ipLayers = protocols.slice(ipIndex + 1, nextIP < 0 ? undefined : nextIP);
    const transport = ipLayers.includes('tcp') ? 'tcp' : ipLayers.includes('udp') ? 'udp' : null;
    const protocol = transport ?? `ip:${protocolNumber}`;
    const sourcePort = transport && first(p, `${transport}_srcport`) !== undefined ? number(first(p, `${transport}_srcport`)) : 0;
    const destinationPort = transport && first(p, `${transport}_dstport`) !== undefined ? number(first(p, `${transport}_dstport`)) : 0;
    const key = JSON.stringify([sourceIP, destinationIP, protocol, sourcePort, destinationPort]);
    if (!flows.has(key)) flows.set(key, {sourceIP, destinationIP, protocol, sourcePort, destinationPort, packets: 0, bytes: 0});
    const flow = flows.get(key);
    flow.packets++;
    flow.bytes += bytes;
    stats.ipPackets++;
    stats.ipBytes += bytes;

    const names = (p.dns_qry_name ?? []).map(normalize);
    if (first(p, 'dns_flags_response') === 'False') {
      names.forEach((name, i) => {
        const type = number(p.dns_qry_type?.[i]);
        const key = JSON.stringify([name, type]);
        if (!queries.has(key)) queries.set(key, {name, type, count: 0});
        queries.get(key).count++;
      });
    } else if (first(p, 'dns_flags_response') === 'True') {
      // Candidates only: addresses in a DNS response (including CNAME targets
      // and additional records) may relate to its question names. Never assign
      // bytes to these names or claim these are actual visited hostnames.
      for (const address of [...(p.dns_a ?? []), ...(p.dns_aaaa ?? [])]) {
        const ip = canonicalIP(address);
        if (!namesByIP.has(ip)) namesByIP.set(ip, new Set());
        for (const name of names) namesByIP.get(ip).add(name);
      }
    }
  }
  const summaries = new Map();
  const candidates = ip => [...(namesByIP.get(ip) ?? [])].sort();
  for (const flow of flows.values()) {
    const source = canonicalIP(flow.sourceIP);
    const destination = canonicalIP(flow.destinationIP);
    // Direction-independent observed tuple: replies and retransmissions do not
    // create more hits. Reuse of the same tuple is not a new connection/hit.
    const endpoints = [[source, flow.sourcePort], [destination, flow.destinationPort]].sort();
    const hit = JSON.stringify([flow.protocol, endpoints]);
    const add = (ip, direction) => {
      if (!summaries.has(ip)) summaries.set(ip, {ip, bytesIn: 0, bytesOut: 0, hits: new Set()});
      const summary = summaries.get(ip);
      summary[direction] += flow.bytes;
      summary.hits.add(hit);
    };
    // Bytes are relative to the container, not to the peer. Local-to-local
    // traffic has both directions; transit/unknown-endpoint flows stay raw only.
    if (local.has(source)) add(destination, 'bytesOut');
    if (local.has(destination)) add(source, 'bytesIn');
  }
  stats.ipSummaries = [...summaries.values()].sort((a, b) => a.ip.localeCompare(b.ip)).map(summary => ({
    ...summary, hits: summary.hits.size, dnsCandidates: candidates(summary.ip),
  }));
  stats.dnsQueries = [...queries.values()].sort((a, b) => a.name.localeCompare(b.name) || a.type - b.type);
  stats.flows = [...flows.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, flow]) => ({
    ...flow,
    sourceDNSCandidates: candidates(canonicalIP(flow.sourceIP)),
    destinationDNSCandidates: candidates(canonicalIP(flow.destinationIP)),
  }));
  return stats;
}

async function report(pcap, captureLog) {
  const captured = summarizeCaptureLog(readFileSync(captureLog, 'utf8'));
  const child = spawn('tshark', ['-n', '-r', pcap, '-T', 'ek', ...fields.flatMap(f => ['-e', f])], {
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  const finished = new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('close', (code, signal) => code === 0 ? resolve() : reject(new Error(`tshark failed: ${code ?? signal}`)));
  });
  try {
    const [stats] = await Promise.all([
      summarize(createInterface({input: child.stdout, crlfDelay: Infinity})), finished,
    ]);
    if (stats.ipPackets + stats.nonIPPackets !== captured) throw new Error('capture packet count mismatch');
    process.stdout.write(JSON.stringify(stats));
  } finally {
    child.kill('SIGKILL');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await report(process.argv[2], process.argv[3]);
}

// Opt-in Daggervals metadata only; no prompts, outputs, arguments or credentials.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const identity = value => typeof value === 'string' && value.length > 0 && value.length <= 256 ? value : null;
const iso = value => number(value) !== null && value <= 8640000000000000 ? new Date(value).toISOString() : null;
const terminal = status => ['complete', 'completed', 'failed', 'stopped', 'rejected', 'partial'].includes(status);
const model = value => typeof value === 'string' && value.length > 0 && value.length <= 256 ? value : null;
const MAX_TOOL_CALLS = 2000;
const emptyUsage = () => ({ turns: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost_usd: 0 });

function sessionUsage(entries) {
  const usage = emptyUsage();
  for (const entry of entries) {
    if (entry.type !== 'message' || entry.message?.role !== 'assistant') continue;
    usage.turns++;
    for (const key of ['input', 'output', 'cacheRead', 'cacheWrite']) usage[key] += number(entry.message.usage?.[key]) ?? 0;
    usage.cost_usd += number(entry.message.usage?.cost?.total) ?? 0;
  }
  return usage;
}

function resultUsage(usage) {
  if (!usage) return null;
  return {
    turns: number(usage.turns), input: number(usage.input), output: number(usage.output),
    cacheRead: number(usage.cacheRead), cacheWrite: number(usage.cacheWrite), cost_usd: number(usage.cost),
  };
}

function parentResources() {
  const usage = process.resourceUsage();
  return {
    id: 'parent', pid: process.pid, cpuTimeMs: (usage.userCPUTime + usage.systemCPUTime) / 1000,
    peakMemoryBytes: usage.maxRSS * 1024, measurement: 'process-resourceUsage',
  };
}

// Linux /proc counters exclude descendants. startTicks prevents PID reuse from
// attaching another process's resource usage to an already-observed runner.
function readProcess(pid, ticksPerSecond) {
  const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
  const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
  const status = fs.readFileSync(`/proc/${pid}/status`, 'utf8');
  const highWater = /^VmHWM:\s+(\d+) kB$/m.exec(status);
  return {
    startTicks: fields[19],
    cpuTimeMs: (Number(fields[11]) + Number(fields[12])) * 1000 / ticksPerSecond,
    peakMemoryBytes: highWater ? Number(highWater[1]) * 1024 : null,
  };
}

class Collector {
  constructor() {
    this.pid = process.pid;
    this.ticks = null;
    this.children = new Map();
    this.background = new Map();
    this.warnings = new Set();
    this.toolCalls = [];
  }

  // Metadata only: never arguments or results. Capped so a runaway loop cannot bloat the log.
  toolCall(event) {
    if (!identity(event?.toolName) || !identity(event?.toolCallId)) return;
    if (this.toolCalls.length >= MAX_TOOL_CALLS) { this.warnings.add(`Tool calls beyond ${MAX_TOOL_CALLS} were not recorded.`); return; }
    this.toolCalls.push({
      id: event.toolCallId, tool: event.toolName, status: event.isError ? 'error' : 'ok',
      durationMs: number(event.durationMs), nested: identity(event.parentToolCallId) !== null,
    });
  }

  child(runId, index, agent, mode) {
    if (!identity(runId) || !identity(agent) || !Number.isInteger(index) || index < 0) return null;
    const id = `${runId}:${index}`;
    if (!this.children.has(id)) this.children.set(id, {
      id, agent, model: null, mode, status: 'unknown', startedAt: null, endedAt: null, durationMs: null,
      timingSource: 'unavailable', processId: mode === 'foreground' ? 'parent' : runId,
      resources: null, usage: null, usageSource: 'unavailable',
    });
    return this.children.get(id);
  }

  // pi-subagents 0.78.0 details use stable result.index, not result array position.
  details(details, observedAt = Date.now()) {
    if (!details || !Array.isArray(details.results)) return;
    if (identity(details.asyncId) && typeof details.asyncDir === 'string') this.asyncStart({ id: details.asyncId, asyncDir: details.asyncDir });
    for (const result of details.results) {
      if (!result || typeof result !== 'object') continue;
      const runId = result.runId ?? details.runId;
      if (this.background.has(runId)) continue; // status.json is authoritative for async steps
      const child = this.child(runId, result.index, result.agent, 'foreground');
      if (!child) continue;
      const progress = result.progress ?? result.progressSummary;
      child.model = model(result.model) ?? model(progress?.model) ?? child.model;
      const duration = number(result.progressSummary?.durationMs ?? progress?.durationMs);
      const status = result.detached ? 'detached' : progress?.status ?? (result.progressSummary ? (result.exitCode === 0 ? 'completed' : 'failed') : 'running');
      if (child.endedAt !== null && (!terminal(status) || (duration !== null && child.durationMs !== null && duration < child.durationMs))) continue;
      const usage = resultUsage(result.usage);
      if (usage) { child.usage = usage; child.usageSource = 'tool-result'; }
      if (child.timingSource === 'async-status') continue;
      if (child.timingSource.includes('completion-event')) {
        if (child.startedAt === null && duration !== null && child.endedAt !== null) {
          child.startedAt = child.endedAt - duration;
          child.durationMs = duration;
          child.timingSource = 'completion-event/reported-duration';
        }
        continue;
      }
      // Pending queue time is not child lifetime. Preserve the first terminal
      // observation so a later status receipt cannot move an earlier finish.
      if (status !== 'pending' && duration !== null) {
        if (child.startedAt === null) child.startedAt = observedAt - duration;
        child.durationMs = duration;
        child.timingSource = 'observed-progress/reported-duration';
      }
      if (terminal(status)) {
        child.endedAt = child.startedAt === null ? (child.endedAt ?? observedAt) : child.startedAt + (duration ?? 0);
      }
      if (!terminal(child.status)) child.status = status;
    }
  }

  foregroundComplete(event) {
    const child = this.child(event?.runId, event?.taskIndex, event?.agent, 'foreground');
    if (!child) return;
    const end = number(event.timestamp);
    if (end !== null) {
      child.endedAt = end;
      if (child.startedAt !== null) child.durationMs = Math.max(0, end - child.startedAt);
      child.timingSource = child.startedAt === null ? 'completion-event/start-unavailable' : 'observed-progress/completion-event';
    }
    child.status = terminal(event.state) ? event.state : event.success ? 'completed' : 'failed';
    // A detached completion has no usage payload. Only read its explicit session,
    // and exclude pre-run fork history; never search for a newest session file.
    if (child.startedAt !== null && typeof event.sessionFile === 'string') {
      try {
        const entries = fs.readFileSync(event.sessionFile, 'utf8').split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
        child.usage = sessionUsage(entries.filter(entry => {
          const time = Date.parse(entry.timestamp);
          return time >= child.startedAt && (end === null || time <= end);
        }));
        child.usageSource = 'child-session/time-window';
      } catch { this.warnings.add('Detached child session usage unavailable.'); }
    }
  }

  asyncStart(event) {
    if (!identity(event?.id) || typeof event.asyncDir !== 'string') return;
    if (!this.background.has(event.id)) this.background.set(event.id, {
      dir: event.asyncDir, startTicks: null,
      resource: { id: event.id, pid: Number.isInteger(event.pid) ? event.pid : 0, cpuTimeMs: null, peakMemoryBytes: null, measurement: 'unavailable' },
    });
    this.poll();
  }

  poll(final = false) {
    for (const [runId, run] of this.background) {
      let status;
      try { status = JSON.parse(fs.readFileSync(path.join(run.dir, 'status.json'), 'utf8')); }
      catch { if (final) this.warnings.add('An async status file was unavailable at capture end.'); continue; }
      if (status?.runId !== runId) { this.warnings.add('Async status identity mismatch.'); continue; }
      if (Number.isInteger(status.pid) && status.pid > 0 && !run.resource.pid) run.resource.pid = status.pid;
      const pid = run.resource.pid;
      if (pid && pid !== this.pid) {
        try {
          this.ticks ??= Number(execFileSync('getconf', ['CLK_TCK'], { encoding: 'utf8' }).trim());
          if (!(this.ticks > 0)) throw new Error('unavailable clock tick rate');
          const sample = readProcess(pid, this.ticks);
          if (run.startTicks !== null && run.startTicks !== sample.startTicks) {
            this.warnings.add('Background PID reused; retained only earlier samples.');
          } else {
            run.startTicks = sample.startTicks;
            run.resource.cpuTimeMs = Math.max(run.resource.cpuTimeMs ?? 0, sample.cpuTimeMs);
            if (sample.peakMemoryBytes !== null) run.resource.peakMemoryBytes = Math.max(run.resource.peakMemoryBytes ?? 0, sample.peakMemoryBytes);
            run.resource.measurement = 'sampled-lower-bound';
          }
        } catch { /* A runner may exit between status publication and /proc sampling. */ }
      }
      if (!Array.isArray(status.steps)) {
        this.warnings.add('Async status steps unavailable.');
        continue;
      }
      for (const [index, step] of status.steps.entries()) {
        if (!step || step.reused) continue;
        // Workflow references are reported under the child's own run identity.
        const childRunId = identity(step.runId) ?? runId;
        const ownRunner = step.async && childRunId !== runId;
        if (ownRunner && this.background.has(childRunId)) continue;
        const mode = pid === this.pid && !ownRunner ? 'foreground' : 'background';
        const child = this.child(childRunId, childRunId === runId ? index : 0, step.agent, mode);
        if (!child) continue;
        child.mode = mode;
        child.processId = ownRunner ? '' : pid === this.pid ? 'parent' : runId;
        if (ownRunner) this.warnings.add('An async workflow child process was not observed directly.');
        child.status = step.status;
        child.model = model(step.model) ?? child.model;
        child.startedAt = number(step.startedAt);
        child.endedAt = number(step.endedAt);
        child.durationMs = child.startedAt !== null && child.endedAt !== null ? Math.max(0, child.endedAt - child.startedAt) : number(step.durationMs);
        child.timingSource = 'async-status';
        if (!child.usage || child.usageSource === 'async-status/partial') {
          child.usage = {
            turns: number(step.turnCount), input: number(step.tokens?.input), output: number(step.tokens?.output),
            cacheRead: null, cacheWrite: null, cost_usd: number(step.totalCost?.costUsd),
          };
          child.usageSource = 'async-status/partial';
        }
      }
    }
  }

  records(usage, resource, startedAt, endedAt, exitCode, parentModel = null) {
    this.poll(true);
    const records = [{ type: 'agent', agentType: 'parent', role: 'parent', agentStats: usage, instance: {
      id: 'parent', agent: 'parent', model: parentModel, mode: 'parent', status: exitCode === 0 ? 'exited' : 'failed', startedAt: iso(startedAt), endedAt: iso(endedAt),
      durationMs: Math.max(0, endedAt - startedAt), timingSource: 'process-uptime/exit', processId: 'parent',
      resources: resource, usage, usageSource: usage ? 'parent-session' : 'unavailable',
    } }];
    for (const child of this.children.values()) {
      const agent = child.agent;
      records.push({ type: 'agent', agentType: agent, role: 'child', instance: {
        ...child, startedAt: iso(child.startedAt), endedAt: iso(child.endedAt),
      } });
    }
    for (const run of this.background.values()) {
      if (run.resource.pid !== this.pid) records.push({ type: 'background_process', process: run.resource });
    }
    for (const call of this.toolCalls) records.push({ type: 'tool_call', toolCall: call });
    for (const message of [
      'Tool calls are the parent session only; calls made inside subagent children are not captured.',
      'Foreground CPU/memory is included in the parent process, not attributable per child.',
      'Foreground start/end may be derived from observed progress and reported duration, not OS lifecycle timestamps.',
      'Null fields are unavailable; unfinished children have no end timestamp. Async cache usage is not recorded by status.json.',
      ...this.warnings,
    ]) records.push({ type: 'warning', message });
    records.push({ type: 'complete' });
    return records;
  }
}

export default function processStats(pi) {
  if (process.env.DAGGERVALS_PROCESS_STATS !== '1') return;
  const logPath = process.env.DAGGERVALS_STATS_LOG;
  if (!logPath || process.env.PI_SUBAGENT_CHILD === '1') return;
  if (process.env.DAGGERVALS_PARENT_PID && String(process.pid) !== process.env.DAGGERVALS_PARENT_PID) return;
  // In-process child sessions must not install a second parent collector.
  const key = Symbol.for('daggervals.process-stats');
  if (globalThis[key]) return;
  globalThis[key] = true;
  const collector = new Collector();
  const startedAt = Date.now() - process.uptime() * 1000;
  fs.appendFileSync(logPath, '', { mode: 0o600 });
  let manager;
  let sessionModel = null;
  pi.on('session_start', (_event, ctx) => {
    if (manager) return;
    manager = ctx.sessionManager;
    sessionModel = ctx.model?.provider && ctx.model?.id ? `${ctx.model.provider}/${ctx.model.id}` : model(ctx.model?.id);
    fs.appendFileSync(logPath, JSON.stringify({ type: 'parent_start', startedAt: iso(startedAt), resources: parentResources() }) + '\n', { mode: 0o600 });
  });
  pi.on('tool_execution_update', event => collector.details(event.partialResult?.details));
  pi.on('tool_execution_end', event => {
    collector.toolCall(event);
    collector.details(event.result?.details);
  });
  pi.events.on('subagent:foreground-complete', event => collector.foregroundComplete(event));
  pi.events.on('subagent:async-started', event => collector.asyncStart(event));
  pi.events.on('subagent:async-complete', () => collector.poll());
  const timer = setInterval(() => collector.poll(), 100);
  timer.unref();
  // session_shutdown precedes final cleanup; exit captures the complete parent
  // CPU lifetime and OS high-water RSS, including all in-process foreground work.
  process.once('exit', exitCode => {
    clearInterval(timer);
    if (!manager) collector.warnings.add('Parent session unavailable; Pi exited before session_start.');
    const usage = manager ? sessionUsage(manager.getEntries()) : null;
    // Prefer the model that actually answered; fall back to the session model.
    const last = manager?.getEntries().filter(e => e.type === 'message' && e.message?.role === 'assistant' && e.message.model).at(-1)?.message;
    const parentModel = last ? (last.provider ? `${last.provider}/${last.model}` : model(last.model)) : sessionModel;
    const records = collector.records(usage, parentResources(), startedAt, Date.now(), exitCode, parentModel);
    fs.appendFileSync(logPath, records.map(record => JSON.stringify(record)).join('\n') + '\n', { mode: 0o600 });
  });
}

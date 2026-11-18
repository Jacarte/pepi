import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync,
  renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { Capture } from "./core.ts";

export type WriteState = "saved" | "skipped" | "pending" | "held" | "failed";
export interface Delivery { captureId: string; state: WriteState; reason: string }
interface Entry extends Capture { version: 1; state: "pending" | "held" | "failed"; attempts: number; nextAttempt: number; lastAttempt?: number; reason: string }
const MAX_AUTO_ATTEMPTS = 5;
interface ProcessOwner { instanceId: string; activeTokens: Set<string> }
// Survives extension reloads in the same process, but not a restart reusing its PID.
const OWNER_KEY = Symbol.for("pepi.hippocampus.outbox-owner");
const ownerState: ProcessOwner = (globalThis as any)[OWNER_KEY] ??= { instanceId: randomUUID(), activeTokens: new Set<string>() };

export function canonicalDestination(value: string): string {
  const url = new URL(value);
  url.username = ""; url.password = ""; url.hash = "";
  url.pathname = url.pathname.replace(/\/+$/, "");
  return url.href;
}

function durableWrite(path: string, value: unknown) {
  const temp = `${path}.${randomUUID()}.tmp`;
  const fd = openSync(temp, "wx", 0o600);
  try { writeFileSync(fd, JSON.stringify(value)); fsyncSync(fd); } finally { closeSync(fd); }
  renameSync(temp, path);
}

function syncDirectory(directory: string) {
  const fd = openSync(directory, "r");
  try { fsyncSync(fd); } finally { closeSync(fd); }
}

/** Per-entry locks serialize multiple Pepi sessions. A dead process's lock is recoverable. */
interface LockLease { isOwner(): boolean; release(): void }
function lock(path: string): LockLease | undefined {
  if (existsSync(`${path}.recovery`)) return;
  const token = randomUUID();
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = openSync(path, "wx", 0o600);
      try { writeFileSync(fd, JSON.stringify({ pid: process.pid, token, instanceId: ownerState.instanceId, acquiredAt: Date.now() })); }
      finally { closeSync(fd); }
      ownerState.activeTokens.add(token);
      const isOwner = () => {
        try {
          const owner = JSON.parse(readFileSync(path, "utf8"));
          return owner.token === token && owner.instanceId === ownerState.instanceId;
        } catch { return false; }
      };
      return { isOwner, release() {
        try { if (isOwner()) unlinkSync(path); }
        catch { /* An external cleanup may already have removed it; never touch another path. */ }
        finally { ownerState.activeTokens.delete(token); }
      } };
    } catch (error: any) {
      if (error.code !== "EEXIST") throw error;
      try {
        const raw = readFileSync(path, "utf8");
        const owner = JSON.parse(raw);
        if (!Number.isInteger(owner.pid) || owner.pid <= 0) return;
        try { process.kill(owner.pid, 0); return; }
        catch (probe: any) { if (probe.code !== "ESRCH") return; }
        // Serialize stale-lock recovery too. Never remove an uncertain recovery lock:
        // a crash in these few synchronous operations needs local inspection, not unsafe takeover.
        const recoveryPath = `${path}.recovery`;
        let recovery: number;
        try { recovery = openSync(recoveryPath, "wx", 0o600); } catch { return; }
        try {
          if (readFileSync(path, "utf8") === raw) unlinkSync(path);
        } finally { closeSync(recovery); unlinkSync(recoveryPath); }
      } catch { return; }
    }
  }
}

function validEntry(value: any, captureId: string): value is Entry {
  if (value?.version !== 1 || value.captureId !== captureId || typeof value.body !== "string" ||
      !["pending", "held", "failed"].includes(value.state) || !Number.isInteger(value.attempts) ||
      !Number.isFinite(value.nextAttempt) || (value.lastAttempt !== undefined && !Number.isFinite(value.lastAttempt)) ||
      typeof value.reason !== "string" || !Number.isInteger(value.timeoutMs) || value.timeoutMs < 100 || value.timeoutMs > 30_000) return false;
  try {
    const url = new URL(value.destination);
    const body = JSON.parse(value.body);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password &&
      body.capture_id === captureId && typeof body.user_id === "string" && body.user_id.length > 0;
  } catch { return false; }
}

interface LockObservation { held: boolean; reason: string }
function observeLock(path: string, timeoutMs: number): LockObservation | undefined {
  if (existsSync(`${path}.recovery`)) return { held: true, reason: "Interrupted lock recovery; inspect local lock" };
  if (!existsSync(path)) return;
  // Other processes' nonce cannot be verified portably. Give a fresh live owner
  // time to finish its bounded HTTP request, then expose an uncertain hold. A
  // live PID is never grounds to steal a lock, nor proof it is the same process.
  const graceMs = timeoutMs + 5_000;
  let acquiredAt = 0;
  try { acquiredAt = statSync(path).mtimeMs; } catch { return; }
  try {
    const owner = JSON.parse(readFileSync(path, "utf8"));
    if (!Number.isInteger(owner.pid) || owner.pid <= 0 || typeof owner.token !== "string") throw new Error("invalid lock");
    if (owner.pid === process.pid) {
      if (owner.instanceId === ownerState.instanceId && ownerState.activeTokens.has(owner.token)) {
        return { held: false, reason: "waiting_for_active_writer" };
      }
      return { held: true, reason: "Lock belongs to a previous or unrecognized process instance; inspect local lock" };
    }
    try { process.kill(owner.pid, 0); }
    catch (error: any) { if (error.code === "ESRCH") return; } // Dead owners are recovered under the recovery guard.
    if (Number.isFinite(owner.acquiredAt)) acquiredAt = owner.acquiredAt;
    if (Date.now() - acquiredAt >= 0 && Date.now() - acquiredAt <= graceMs) {
      return { held: false, reason: "waiting_for_active_writer" };
    }
    return { held: true, reason: "Live lock owner cannot be verified after the delivery grace period; inspect local lock" };
  } catch { /* A half-written lock must not be taken from a potentially live owner. */ }
  return Date.now() - acquiredAt >= 0 && Date.now() - acquiredAt <= graceMs ? { held: false, reason: "waiting_for_writer_lock" }
    : { held: true, reason: "Invalid outbox lock; inspect local lock" };
}

export class Outbox {
  readonly directory: string;
  private fetcher: typeof fetch;
  private now: () => number;
  constructor(directory: string, fetcher = fetch, now = Date.now) {
    this.directory = directory; this.fetcher = fetcher; this.now = now;
  }
  private file(captureId: string) {
    if (!/^pepi-[a-f0-9-]{36}$/.test(captureId)) throw new Error("Invalid capture ID");
    return join(this.directory, `${captureId}.json`);
  }
  enqueue(capture: Capture) {
    mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    const file = this.file(capture.captureId);
    if (existsSync(file)) throw new Error("Capture already exists; replay its stored request");
    durableWrite(file, { ...capture, version: 1, state: "pending", attempts: 0, nextAttempt: this.now(), lastAttempt: 0, reason: "queued" });
    syncDirectory(this.directory); // The complete immutable request is durable before any HTTP call.
  }
  private files() {
    try { return readdirSync(this.directory).filter((name) => /^pepi-[a-f0-9-]{36}\.json$/.test(name)).sort(); }
    catch (error: any) { if (error.code === "ENOENT") return []; throw error; }
  }
  private read(file: string): Entry {
    const value = JSON.parse(readFileSync(join(this.directory, file), "utf8"));
    if (!validEntry(value, file.slice(0, -5))) throw new Error("Invalid outbox entry; retained for inspection");
    return value;
  }
  status() {
    return this.files().map((file) => {
      try {
        const entry = this.read(file);
        const lockFile = join(this.directory, `${file}.lock`);
        const observedLock = observeLock(lockFile, entry.timeoutMs);
        const blocked = observedLock?.held;
        const paused = this.paused(entry.destination);
        return { captureId: entry.captureId, state: blocked ? "held" : entry.state, attempts: entry.attempts,
          reason: observedLock?.reason ?? (paused && entry.state === "pending" ? "destination_paused_incompatible_contract" : entry.reason),
          lockFile: blocked ? lockFile : undefined,
          lockRecovery: blocked ? "Stop all Pepi writers sharing this outbox and verify the lock owner has ended. Remove only the named .lock file and its .recovery marker if present, preserving the .json request unchanged. Restart to replay its original capture ID." : undefined,
          automaticRetry: !blocked && !paused && entry.state === "pending" && entry.attempts < MAX_AUTO_ATTEMPTS };
      } catch { return { captureId: file.slice(0, -5), state: "held", reason: "Invalid outbox entry; inspect local file", automaticRetry: false }; }
    });
  }
  blockingCaptures(destination: string): string[] {
    return this.files().flatMap((file) => {
      try {
        const entry = this.read(file);
        return canonicalDestination(entry.destination) === canonicalDestination(destination) &&
          entry.reason === "missing_or_invalid_write_receipt" ? [entry.captureId] : [];
      } catch { return []; }
    });
  }
  paused(destination: string): boolean { return this.blockingCaptures(destination).length > 0; }
  nextDue(): number | undefined {
    let next: number | undefined;
    for (const file of this.files()) {
      try {
        const entry = this.read(file);
        if (entry.state === "pending" && entry.attempts < MAX_AUTO_ATTEMPTS && !this.paused(entry.destination) &&
            !observeLock(join(this.directory, `${file}.lock`), entry.timeoutMs)?.held) next = Math.min(next ?? Infinity, entry.nextAttempt);
      } catch { /* status() exposes malformed entries, never erase them. */ }
    }
    return next;
  }
  async drain(force = false, limit = 3): Promise<Delivery[]> {
    const deliveries: Delivery[] = [];
    // Oldest attempt first, including when explicit retry bypasses backoff. A
    // permanently unavailable first batch must not starve later pending work.
    const candidates = this.files().flatMap((file) => {
      try { const entry = this.read(file); return entry.state === "pending" ? [entry] : []; }
      catch { return []; } // Malformed entries are visible holds in status(), not retry candidates.
    }).sort((a, b) => (a.lastAttempt ?? 0) - (b.lastAttempt ?? 0) || a.captureId.localeCompare(b.captureId));
    for (const entry of candidates) {
      if (deliveries.length >= limit) break;
      const delivery = await this.deliver(entry.captureId, force);
      if (delivery) deliveries.push(delivery);
    }
    return deliveries;
  }
  async deliver(captureId: string, force = false): Promise<Delivery | undefined> {
    const file = this.file(captureId);
    const lease = lock(`${file}.lock`);
    if (!lease) return;
    try {
      if (!existsSync(file)) return;
      let entry: Entry;
      try { entry = this.read(`${captureId}.json`); }
      catch { return { captureId, state: "held", reason: "Invalid outbox entry; inspect local file" }; }
      if (this.paused(entry.destination)) return;
      if (entry.state !== "pending" || (!force && (entry.nextAttempt > this.now() || entry.attempts >= MAX_AUTO_ATTEMPTS))) return;
      let state: WriteState = "pending", reason = "transport_failure";
      try {
        const response = await this.fetcher(entry.destination, { method: "POST", body: entry.body,
          headers: { "content-type": "application/json" }, redirect: "error", signal: AbortSignal.timeout(entry.timeoutMs) });
        // A timeout/reset while receiving the body is a lost response, even after
        // successful headers. Only a completely received body can establish an
        // incompatible acknowledgement and pause this destination.
        const responseBody = await response.text();
        let payload: any;
        try { payload = JSON.parse(responseBody); } catch { /* A fully received malformed acknowledgement is not success. */ }
        if (response.ok && payload?.capture_id === captureId && ["saved", "skipped"].includes(payload?.write_status)) {
          state = payload.write_status;
          reason = state === "skipped" ? String(payload.skip_reason ?? "no_new_memories") : "acknowledged";
        } else if (response.ok) {
          state = "held"; reason = "missing_or_invalid_write_receipt";
        } else if (response.status === 503 && payload?.detail?.retryable === true) {
          reason = "retryable_backend_failure";
        } else if (response.status === 409 && payload?.detail?.code === "capture_conflict") {
          state = "failed"; reason = "capture_conflict";
        } else if (response.status === 409 && payload?.detail?.code === "capture_pending") {
          state = "held"; reason = `capture_${payload.detail.state === "unknown" ? "unknown" : "pending"}`;
        } else if (payload?.detail?.retryable === false || response.status >= 500) {
          state = "held"; reason = "backend_outcome_unresolved";
        } else {
          state = "failed"; reason = `http_${response.status}`;
        }
      } catch { /* Keep exact body and capture ID for a lost response or network failure. */ }
      if (!lease.isOwner()) return { captureId, state: "held", reason: "delivery_lock_ownership_lost" };
      if (state === "saved" || state === "skipped") {
        unlinkSync(file);
        syncDirectory(this.directory);
      } else {
        const attempts = entry.attempts + 1;
        durableWrite(file, { ...entry, state, reason, attempts,
          lastAttempt: Math.max(this.now(), (entry.lastAttempt ?? 0) + 1),
          nextAttempt: this.now() + Math.min(60_000, 1_000 * 2 ** Math.min(attempts - 1, 6)) });
        syncDirectory(this.directory);
      }
      return { captureId, state, reason };
    } finally { lease.release(); }
  }
}

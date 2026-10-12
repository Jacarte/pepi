/** Compact status for Codex ChatGPT subscription usage windows. */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const STATUS_ID = "codex-usage-bar";
const REFRESH_MS = 60_000;
const REQUEST_TIMEOUT_MS = 8_000;

type UsageWindow = { usedPercent: number; resetsAt?: number };
type Limits = { primary?: UsageWindow; secondary?: UsageWindow };

function asRecord(value: unknown): Record<string, unknown> | undefined {
	return value !== null && typeof value === "object" && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: undefined;
}

function parseWindow(value: unknown): UsageWindow | undefined {
	const row = asRecord(value);
	if (!row || typeof row.usedPercent !== "number" || !Number.isFinite(row.usedPercent)) return;
	return {
		usedPercent: Math.max(0, Math.min(100, row.usedPercent)),
		...(typeof row.resetsAt === "number" && Number.isFinite(row.resetsAt)
			? { resetsAt: row.resetsAt }
			: {}),
	};
}

function parseLimits(value: unknown): Limits | undefined {
	const root = asRecord(value);
	const limits = asRecord(root?.rateLimits) ?? root;
	if (!limits) return;
	const primary = parseWindow(limits.primary);
	const secondary = parseWindow(limits.secondary);
	return primary || secondary ? { primary, secondary } : undefined;
}

function resetLabel(timestamp: number | undefined): string {
	if (timestamp === undefined) return "";
	const date = new Date(timestamp * 1000);
	if (!Number.isFinite(date.getTime())) return "";
	return ` ↻${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

/** Run one app-server session; every JSON-RPC request is correlated and timed out. */
async function readUsage(onChild: (child: ChildProcessWithoutNullStreams | undefined) => void): Promise<Limits> {
	return new Promise((resolve, reject) => {
		let child: ChildProcessWithoutNullStreams;
		try {
			child = spawn("codex", ["app-server"], { stdio: ["pipe", "pipe", "ignore"] });
		} catch (error) {
			reject(error);
			return;
		}

		onChild(child);
		const lines = createInterface({ input: child.stdout });
		let nextId = 1;
		let settled = false;
		let timer: ReturnType<typeof setTimeout>;
		const pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
		const finish = (error?: Error, result?: Limits) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			lines.close();
			onChild(undefined);
			child.kill();
			for (const request of pending.values()) request.reject(error ?? new Error("Codex app-server closed"));
			pending.clear();
			if (error) reject(error);
			else if (result) resolve(result);
			else reject(new Error("Codex returned no usage windows"));
		};
		const send = (method: string, params: Record<string, unknown> = {}) => {
			const id = nextId++;
			return new Promise<unknown>((res, rej) => {
				pending.set(id, { resolve: res, reject: rej });
				child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`, (error) => {
					if (error) {
						pending.delete(id);
						rej(error);
					}
				});
			});
		};

		lines.on("line", (line) => {
			let parsed: unknown;
			try {
				parsed = JSON.parse(line);
			} catch {
				return; // Ignore non-JSON diagnostics defensively.
			}
			const message = asRecord(parsed);
			if (!message || typeof message.id !== "number") return;
			const request = pending.get(message.id);
			if (!request) return;
			pending.delete(message.id);
			if (message.error) request.reject(new Error("Codex app-server request failed"));
			else request.resolve(message.result);
		});
		child.once("error", (error) => finish(error));
		child.stdin.once("error", (error) => finish(error));
		child.once("exit", (code) => {
			if (!settled) finish(new Error(`Codex app-server exited (${code ?? "unknown"})`));
		});
		timer = setTimeout(() => finish(new Error("Codex app-server timed out")), REQUEST_TIMEOUT_MS);

		void (async () => {
			try {
				await send("initialize", {
					clientInfo: { name: "pi-codex-usage-bar", title: "Pi Codex usage bar", version: "1.0.0" },
				});
				if (settled) return;
				child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "initialized", params: {} })}\n`);
				const result = await send("account/rateLimits/read");
				const limits = parseLimits(result);
				if (!limits) throw new Error("Codex usage response schema unavailable");
				finish(undefined, limits);
			} catch (error) {
				finish(error instanceof Error ? error : new Error(String(error)));
			}
		})();
	});
}

function formatWindow(name: string, window: UsageWindow): string {
	const remaining = Math.round(100 - window.usedPercent);
	return `${name} ${remaining}%${resetLabel(window.resetsAt)}`;
}

export default function (pi: ExtensionAPI) {
	let timer: ReturnType<typeof setInterval> | undefined;
	let running = false;
	let stopped = false;
	let limits: Limits | undefined;
	let unavailable = false;
	let stale = false;
	let activeChild: ChildProcessWithoutNullStreams | undefined;

	function render(ctx: ExtensionContext) {
		if (!limits) {
			ctx.ui.setStatus(STATUS_ID, ctx.ui.theme.fg("dim", `Codex ${unavailable ? "usage unavailable" : "usage…"}`));
			return;
		}
		const text = [
			limits.primary && formatWindow("5h", limits.primary),
			limits.secondary && formatWindow("7d", limits.secondary),
		].filter(Boolean).join("  ");
		const staleLabel = stale ? " (stale)" : "";
		ctx.ui.setStatus(STATUS_ID, ctx.ui.theme.fg("dim", `Codex ${text}${staleLabel}`));
	}

	async function refresh(ctx: ExtensionContext) {
		if (running || stopped) return;
		running = true;
		try {
			limits = await readUsage((child) => { activeChild = child; });
			unavailable = false;
			stale = false;
		} catch {
			unavailable = true;
			stale = limits !== undefined;
		} finally {
			running = false;
			if (!stopped) render(ctx);
		}
	}

	pi.on("session_start", async (_event, ctx) => {
		stopped = false;
		void refresh(ctx);
		if (!timer) {
			timer = setInterval(() => void refresh(ctx), REFRESH_MS);
			timer.unref?.();
		}
	});
	pi.on("session_shutdown", async (_event, ctx) => {
		stopped = true;
		activeChild?.kill();
		if (timer) clearInterval(timer);
		timer = undefined;
		ctx.ui.setStatus(STATUS_ID, undefined);
	});
}

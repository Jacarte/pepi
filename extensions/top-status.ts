/**
 * Top Status Extension
 *
 * Moves the crowded extension-status line out of the bottom footer and into a
 * widget above the editor. Pi has no fixed top-of-screen slot in fullscreen
 * mode, so "above the editor" is the closest always-visible place.
 *
 *   widget (above editor)   all setStatus() texts, wrapped over several lines
 *   footer (bottom)         pwd + git branch, token/cost/context stats, model
 *
 * The footer replicates pi's built-in two lines minus the status line.
 *
 * Command:
 *   /topbar   Toggle between this layout and pi's built-in footer
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { homedir } from "node:os";

const WIDGET_ID = "top-status";
const MAX_STATUS_LINES = 4;
const SEPARATOR = " │ ";

type Totals = { input: number; output: number; cacheRead: number; cacheWrite: number; cost: number };
type Stats = { key: string; totals: Totals; cacheHit: number | undefined };

type StatusSource = { getExtensionStatuses(): ReadonlyMap<string, string> };

// ---------------------------------------------------------------------------
// Formatting helpers (mirror pi's built-in footer)
// ---------------------------------------------------------------------------

function formatTokens(n: number): string {
	if (n < 1000) return String(n);
	if (n < 10_000) return `${(n / 1000).toFixed(1)}k`;
	if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
	if (n < 10_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
	return `${Math.round(n / 1_000_000)}M`;
}

function sanitize(text: string): string {
	return text.replace(/[\r\n\t]/g, " ").replace(/ +/g, " ").trim();
}

function shortCwd(cwd: string): string {
	const home = homedir();
	if (cwd === home) return "~";
	return cwd.startsWith(`${home}/`) ? `~${cwd.slice(home.length)}` : cwd;
}

// ---------------------------------------------------------------------------
// Session usage totals (cached per leaf, since render runs every frame)
// ---------------------------------------------------------------------------

type UsageLike = {
	input?: number;
	output?: number;
	cacheRead?: number;
	cacheWrite?: number;
	cost?: { total?: number };
};

function computeStats(ctx: ExtensionContext): Totals & { cacheHit: number | undefined } {
	const totals: Totals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 };
	let cacheHit: number | undefined;
	const add = (u: UsageLike | undefined) => {
		if (!u) return;
		totals.input += u.input ?? 0;
		totals.output += u.output ?? 0;
		totals.cacheRead += u.cacheRead ?? 0;
		totals.cacheWrite += u.cacheWrite ?? 0;
		totals.cost += u.cost?.total ?? 0;
	};

	for (const entry of ctx.sessionManager.getEntries() as Array<Record<string, any>>) {
		if (entry.type === "usage") add(entry.usage);
		else if (entry.type === "message" && entry.message?.role === "assistant") {
			const u = entry.message.usage as UsageLike | undefined;
			add(u);
			const prompt = (u?.input ?? 0) + (u?.cacheRead ?? 0) + (u?.cacheWrite ?? 0);
			cacheHit = prompt > 0 ? ((u?.cacheRead ?? 0) / prompt) * 100 : undefined;
		} else if (entry.type === "message" && entry.message?.role === "toolResult") add(entry.message.usage);
		else if ((entry.type === "branch_summary" || entry.type === "compaction") && entry.usage) add(entry.usage);
	}
	return { ...totals, cacheHit };
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
	let enabled = true;
	let cache: Stats | undefined;

	function stats(ctx: ExtensionContext): Totals & { cacheHit: number | undefined } {
		const key = `${ctx.sessionManager.getSessionId()}:${ctx.sessionManager.getLeafId()}`;
		if (!cache || cache.key !== key) {
			const { cacheHit, ...totals } = computeStats(ctx);
			cache = { key, totals, cacheHit };
		}
		return { ...cache.totals, cacheHit: cache.cacheHit };
	}

	function install(ctx: ExtensionContext) {
		if (ctx.mode !== "tui") return;
		let source: StatusSource | undefined;
		let providerCount = () => 1;
		let branch = () => null as string | null;

		// Footer: built-in lines minus the status line. Captures footerData so the
		// widget below can read live statuses on every frame.
		ctx.ui.setFooter((tui, theme, footerData) => {
			source = footerData;
			providerCount = () => footerData.getAvailableProviderCount();
			branch = () => footerData.getGitBranch();
			const unsub = footerData.onBranchChange(() => tui.requestRender());

			return {
				dispose: unsub,
				invalidate() {},
				render(width: number): string[] {
					try {
						let pwd = shortCwd(ctx.sessionManager.getCwd());
						const b = branch();
						if (b) pwd += ` (${b})`;
						const name = ctx.sessionManager.getSessionName();
						if (name) pwd += ` • ${name}`;

						const s = stats(ctx);
						const parts: string[] = [];
						if (s.input) parts.push(`↑${formatTokens(s.input)}`);
						if (s.output) parts.push(`↓${formatTokens(s.output)}`);
						if (s.cacheRead) parts.push(`R${formatTokens(s.cacheRead)}`);
						if (s.cacheWrite) parts.push(`W${formatTokens(s.cacheWrite)}`);
						if ((s.cacheRead > 0 || s.cacheWrite > 0) && s.cacheHit !== undefined) {
							parts.push(`CH${s.cacheHit.toFixed(1)}%`);
						}
						if (s.cost) parts.push(`$${s.cost.toFixed(3)}`);

						const usage = ctx.getContextUsage();
						const window = usage?.contextWindow ?? ctx.model?.contextWindow ?? 0;
						const pct = usage?.percent ?? null;
						const ctxText = `${pct === null ? "?" : pct.toFixed(1)}%/${formatTokens(window)}`;
						parts.push(
							pct !== null && pct > 90
								? theme.fg("error", ctxText)
								: pct !== null && pct > 70
									? theme.fg("warning", ctxText)
									: ctxText,
						);

						let left = parts.join(" ");
						if (visibleWidth(left) > width) left = truncateToWidth(left, width, "...");

						const model = ctx.model;
						let right = model?.id ?? "no-model";
						if (model?.reasoning) {
							const level = pi.getThinkingLevel() || "off";
							right += level === "off" ? " • thinking off" : ` • ${level}`;
						}
						if (providerCount() > 1 && model) {
							const withProvider = `(${model.provider}) ${right}`;
							if (visibleWidth(left) + 2 + visibleWidth(withProvider) <= width) right = withProvider;
						}

						const room = width - visibleWidth(left) - 2;
						const rightFit = room > 0 ? truncateToWidth(right, room, "") : "";
						const pad = " ".repeat(Math.max(0, width - visibleWidth(left) - visibleWidth(rightFit)));

						return [
							truncateToWidth(theme.fg("dim", pwd), width, theme.fg("dim", "...")),
							theme.fg("dim", left) + theme.fg("dim", pad + rightFit),
						];
					} catch {
						return []; // stale ctx during session switch: skip a frame
					}
				},
			};
		});

		// Widget: every extension status, packed into lines of the terminal width.
		ctx.ui.setWidget(
			WIDGET_ID,
			(_tui, theme) => ({
				invalidate() {},
				render(width: number): string[] {
					const statuses = source?.getExtensionStatuses();
					if (!statuses || statuses.size === 0) return [];

					const items = [...statuses.entries()]
						.sort(([a], [b]) => a.localeCompare(b))
						.map(([, text]) => sanitize(text))
						.filter(Boolean);
					const sep = theme.fg("dim", SEPARATOR);
					const sepWidth = visibleWidth(sep);

					const lines: string[] = [];
					let line = "";
					for (const item of items) {
						const w = visibleWidth(item);
						if (line && visibleWidth(line) + sepWidth + w > width) {
							lines.push(line);
							line = item;
						} else {
							line = line ? line + sep + item : item;
						}
					}
					if (line) lines.push(line);

					return lines.slice(0, MAX_STATUS_LINES).map((l) => truncateToWidth(l, width, theme.fg("dim", "...")));
				},
			}),
			{ placement: "aboveEditor" },
		);
	}

	function uninstall(ctx: ExtensionContext) {
		ctx.ui.setWidget(WIDGET_ID, undefined);
		ctx.ui.setFooter(undefined);
	}

	pi.on("session_start", async (_event, ctx) => {
		cache = undefined;
		if (enabled) install(ctx);
	});

	pi.registerCommand("topbar", {
		description: "Toggle statuses above the editor vs pi's built-in footer",
		handler: async (_args, ctx) => {
			enabled = !enabled;
			if (enabled) install(ctx);
			else uninstall(ctx);
			ctx.ui.notify(enabled ? "Statuses moved above the editor" : "Built-in footer restored", "info");
		},
	});
}

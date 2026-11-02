/**
 * LiteLLM Budget Extension
 *
 * Shows LiteLLM gateway spend / remaining budget in the footer status line,
 * refreshed on session start, when the agent settles, and periodically.
 *
 * Ported from an opencode sidebar budget widget.
 *
 * Commands:
 *   /budget          Show full budget details (and force a refresh)
 *   /budget refresh  Same as above
 *
 * Config resolution order (no hardcoded gateway; nothing org-specific here):
 *   API key : LITELLM_API_KEY -> LLM_GATEWAY_API_KEY -> TRUSTLY_LLM_GATEWAY_API_KEY
 *             -> auth.json (litellm.key)
 *   Budget  : key max_budget -> team-membership budget (/team/info) when the key has none
 *             LITELLM_MAX_BUDGET overrides the cap (USD) when the gateway exposes none
 *   Base URL: LITELLM_BASE_URL -> LLM_GATEWAY_URL -> auth.json (litellm.env.LITELLM_BASE_URL)
 *
 * If no base URL resolves, the extension stays silent rather than guessing a host.
 */

import { readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const STATUS_ID = "litellm-budget";
const REFRESH_MS = 60_000;
const MIN_REFRESH_MS = 15_000;
const FETCH_TIMEOUT_MS = 10_000;

type BudgetInfo = {
	spend: number;
	maxBudget: number | null;
	budgetDuration: string | null;
	budgetResetAt: string | null;
	keyAlias: string | null;
	/** Where the budget numbers came from. */
	source: "key" | "team-membership";
};

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

function agentDir(): string {
	return process.env.PI_CODING_AGENT_DIR ?? join(homedir(), ".pi", "agent");
}

/** Read the litellm entry from auth.json, if present. */
function readAuthEntry(): { key?: string; baseUrl?: string } {
	try {
		const raw = readFileSync(join(agentDir(), "auth.json"), "utf8");
		const parsed = JSON.parse(raw) as Record<
			string,
			{ key?: string; env?: Record<string, string> } | undefined
		>;
		const entry = parsed.litellm;
		if (!entry) return {};
		return { key: entry.key, baseUrl: entry.env?.LITELLM_BASE_URL };
	} catch {
		return {};
	}
}

/** Treat empty/whitespace env values as absent so a blank export doesn't win. */
function cleanEnv(value: string | undefined): string | undefined {
	const trimmed = value?.trim();
	return trimmed ? trimmed : undefined;
}

function resolveConfig(): { apiKey: string; baseUrl: string } {
	const auth = readAuthEntry();
	const apiKey =
		cleanEnv(process.env.LITELLM_API_KEY) ??
		cleanEnv(process.env.LLM_GATEWAY_API_KEY) ??
		// Legacy name, kept so existing shells keep working.
		cleanEnv(process.env.TRUSTLY_LLM_GATEWAY_API_KEY) ??
		auth.key ??
		"";
	const baseUrl =
		cleanEnv(process.env.LITELLM_BASE_URL) ??
		cleanEnv(process.env.LLM_GATEWAY_URL) ??
		auth.baseUrl ??
		"";
	return { apiKey, baseUrl };
}

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------

async function getJson(url: string, apiKey: string): Promise<Record<string, unknown>> {
	const resp = await fetch(url, {
		headers: { accept: "application/json", "x-litellm-api-key": apiKey },
		signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
	});
	if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
	return (await resp.json()) as Record<string, unknown>;
}

const num = (v: unknown): number | null => (typeof v === "number" ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

/** Optional manual cap (USD) for when the gateway does not expose the real one. */
function overrideBudget(): number | null {
	const n = Number(cleanEnv(process.env.LITELLM_MAX_BUDGET));
	return Number.isFinite(n) && n > 0 ? n : null;
}

/**
 * Per-user budget from the team membership (resets each cycle). Returns null
 * if the user is not a member or the call fails: the key view still works.
 */
async function fetchMembershipBudget(
	root: string,
	apiKey: string,
	teamId: string,
	userId: string,
): Promise<Pick<BudgetInfo, "spend" | "maxBudget" | "budgetDuration" | "budgetResetAt"> | null> {
	try {
		const json = await getJson(`${root}/team/info?team_id=${encodeURIComponent(teamId)}`, apiKey);
		const members = (json.team_memberships ?? []) as Array<Record<string, unknown>>;
		const m = members.find((x) => x.user_id === userId);
		if (!m) return null;
		const table = (m.litellm_budget_table ?? {}) as Record<string, unknown>;
		return {
			spend: num(m.spend) ?? 0,
			maxBudget: num(table.max_budget),
			budgetDuration: str(table.budget_duration),
			budgetResetAt: str(table.budget_reset_at),
		};
	} catch {
		return null;
	}
}

async function fetchBudgetInfo(baseUrl: string, apiKey: string): Promise<BudgetInfo> {
	// Strip a trailing /v1 so both gateway styles work.
	const root = baseUrl.replace(/\/+$/, "").replace(/\/v1$/, "");
	const json = await getJson(`${root}/key/info`, apiKey);
	const info = (json.info ?? json) as Record<string, unknown>;

	const result: BudgetInfo = {
		spend: num(info.spend) ?? 0,
		maxBudget: num(info.max_budget),
		budgetDuration: str(info.budget_duration),
		budgetResetAt: str(info.budget_reset_at),
		keyAlias: str(info.key_alias),
		source: "key",
	};

	const override = overrideBudget();
	const keyHasCap = result.maxBudget !== null && result.maxBudget > 0;
	const teamId = str(info.team_id);
	const userId = str(info.user_id);

	// Key has no cap: the key's `spend` is lifetime and never resets, so use the
	// per-user team-membership budget (cycle spend + cap) when one exists.
	if (!keyHasCap && teamId && userId) {
		const m = await fetchMembershipBudget(root, apiKey, teamId, userId);
		if (m && (override !== null || (m.maxBudget !== null && m.maxBudget > 0))) {
			Object.assign(result, m, { source: "team-membership" as const });
		}
	}
	if (!keyHasCap && override !== null) result.maxBudget = override;

	return result;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/** Bar is filled proportionally to the value passed in (we pass % remaining). */
function progressBar(pct: number, width = 10): string {
	const clamped = Math.max(0, Math.min(100, pct));
	const filled = Math.round((clamped / 100) * width);
	return "█".repeat(filled) + "░".repeat(width - filled);
}

function formatMoney(n: number): string {
	return n >= 100 ? `$${Math.round(n)}` : `$${n.toFixed(2)}`;
}

function formatResetDate(iso: string): string {
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return iso;
	return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function daysUntil(iso: string): number | null {
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return null;
	return Math.max(0, Math.ceil((d.getTime() - Date.now()) / 86_400_000));
}

const UNIT_MS: Record<string, number> = {
	s: 1_000,
	m: 60_000,
	h: 3_600_000,
	d: 86_400_000,
	w: 604_800_000,
	mo: 30 * 86_400_000,
};

/** Parse LiteLLM durations like "30d", "1mo", "12h" into ms. */
function parseDurationMs(s: string | null): number | null {
	const m = s?.trim().match(/^(\d+)\s*(mo|s|m|h|d|w)$/i);
	return m ? Number(m[1]) * UNIT_MS[m[2].toLowerCase()] : null;
}

type Eta = { emptyAt: Date; beforeReset: boolean; perDay: number };

/**
 * Project when the budget runs out, from the average burn rate this cycle
 * (cycle start = reset date - duration). Null when it cannot be estimated.
 */
function computeEta(d: BudgetInfo): Eta | null {
	if (!d.maxBudget || d.maxBudget <= 0 || d.spend <= 0 || !d.budgetResetAt) return null;
	const dur = parseDurationMs(d.budgetDuration);
	const reset = new Date(d.budgetResetAt).getTime();
	if (!dur || Number.isNaN(reset)) return null;
	const now = Date.now();
	const elapsed = now - (reset - dur);
	if (elapsed < 3_600_000) return null; // too early in the cycle to extrapolate
	const rate = d.spend / elapsed; // USD per ms
	const emptyAt = new Date(now + Math.max(0, d.maxBudget - d.spend) / rate);
	return { emptyAt, beforeReset: emptyAt.getTime() < reset, perDay: rate * 86_400_000 };
}

function formatEtaIn(ms: number): string {
	if (ms < 3_600_000) return "<1h";
	if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)}h`;
	return `${Math.round(ms / 86_400_000)}d`;
}

// ---------------------------------------------------------------------------
// Spend history (local; the gateway blocks daily-usage routes for virtual keys)
// ---------------------------------------------------------------------------

/** k = budget view the number came from; spend from different views is not comparable. */
type Snapshot = { t: number; s: number; k?: string };
const HISTORY_KEEP_MS = 35 * 86_400_000;
const SNAPSHOT_MIN_MS = 3_600_000;
const SPARK_DAYS = 7;
const SPARK = "▁▂▃▄▅▆▇█";

const historyPath = () => join(agentDir(), "litellm-budget-history.json");

function readHistory(): Snapshot[] {
	try {
		const arr = JSON.parse(readFileSync(historyPath(), "utf8")) as Snapshot[];
		return Array.isArray(arr) ? arr.filter((x) => Number.isFinite(x?.t) && Number.isFinite(x?.s)) : [];
	} catch {
		return [];
	}
}

/** Append a snapshot when spend changed (or hourly); prune old entries. Never throws. */
function recordSnapshot(spend: number, k: string) {
	try {
		const now = Date.now();
		const hist = readHistory().filter((x) => now - x.t < HISTORY_KEEP_MS);
		const last = hist[hist.length - 1];
		if (last && last.s === spend && now - last.t < SNAPSHOT_MIN_MS) return;
		hist.push({ t: now, s: spend, k });
		writeFileSync(historyPath(), JSON.stringify(hist));
	} catch {
		// history is best-effort
	}
}

const dayKey = (t: number) => {
	const d = new Date(t);
	return d.getFullYear() * 10_000 + (d.getMonth() + 1) * 100 + d.getDate();
};

/** Spend per local day for the last `days` days (oldest first); null = no data yet. */
function dailySpend(hist: Snapshot[], days = SPARK_DAYS): Array<number | null> {
	const sorted = [...hist].sort((a, b) => a.t - b.t);
	const perDay = new Map<number, number>();
	for (let i = 1; i < sorted.length; i++) {
		const a = sorted[i - 1];
		const b = sorted[i];
		if (!a.k || !b.k || a.k !== b.k) continue; // untagged or different budget view: not comparable
		// A drop to under half the previous value is a cycle reset (new value is fresh
		// spend); a small dip is gateway noise and counts as nothing.
		const delta = b.s >= a.s ? b.s - a.s : b.s < a.s * 0.5 ? b.s : 0;
		const k = dayKey(b.t);
		perDay.set(k, (perDay.get(k) ?? 0) + delta);
	}
	const firstKey = sorted.length ? dayKey(sorted[0].t) : Infinity;
	const out: Array<number | null> = [];
	for (let i = days - 1; i >= 0; i--) {
		const k = dayKey(Date.now() - i * 86_400_000);
		out.push(k < firstKey ? null : (perDay.get(k) ?? 0));
	}
	return out;
}

function sparkline(values: Array<number | null>): string {
	const max = Math.max(0, ...values.map((v) => v ?? 0));
	return values
		.map((v) =>
			v === null ? "·" : max <= 0 ? SPARK[0] : SPARK[Math.min(7, Math.floor((v / max) * 7.999))],
		)
		.join("");
}

/** Multi-line vertical bar chart of daily spend (oldest left, today right). */
function renderChart(days: number, height: number): string {
	const values = dailySpend(readHistory(), days);
	if (!values.some((v) => v !== null)) return "No spend history recorded yet.";
	const max = Math.max(0.01, ...values.map((v) => v ?? 0));
	const total = values.reduce<number>((a, v) => a + (v ?? 0), 0);
	const known = values.filter((v) => v !== null).length;
	const axisW = formatMoney(max).length;
	const rows: string[] = [];
	for (let r = height - 1; r >= 0; r--) {
		const label = r === height - 1 ? formatMoney(max) : r === 0 ? formatMoney(0) : "";
		const cells = values.map((v) => {
			if (v === null) return r === 0 ? " · " : "   ";
			const fill = Math.max(0, Math.min(8, Math.round((v / max) * height * 8) - r * 8));
			return fill === 0 ? "   " : `${SPARK[fill - 1].repeat(2)} `;
		});
		rows.push(`${label.padStart(axisW)} │${cells.join("")}`);
	}
	const dates = values.map((_, i) => String(new Date(Date.now() - (days - 1 - i) * 86_400_000).getDate()).padStart(2) + " ");
	rows.push(`${" ".repeat(axisW)} └${"───".repeat(days)}`);
	rows.push(`${" ".repeat(axisW)}  ${dates.join("")}`);
	rows.push(`Last ${days}d: ${formatMoney(total)} total · avg ${formatMoney(total / Math.max(1, known))}/day · peak ${formatMoney(max)}`);
	return rows.join("\n");
}

/** Color name based on percentage of budget remaining. */
function remainingColor(pctRemaining: number): "success" | "warning" | "error" {
	if (pctRemaining < 15) return "error";
	if (pctRemaining < 50) return "warning";
	return "success";
}

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
	let cached: BudgetInfo | undefined;
	let lastError: string | undefined;
	let timer: ReturnType<typeof setInterval> | undefined;
	let running = false;
	let disposed = false;

	/** Render the compact footer status from current state. */
	function renderStatus(ctx: ExtensionContext) {
		const theme = ctx.ui.theme;

		if (!cached) {
			// Nothing fetched yet: only surface hard errors, stay quiet otherwise.
			if (lastError) ctx.ui.setStatus(STATUS_ID, theme.fg("dim", `budget: ${lastError}`));
			else ctx.ui.setStatus(STATUS_ID, undefined);
			return;
		}

		const d = cached;
		const hasMax = d.maxBudget !== null && d.maxBudget > 0;
		const label = theme.fg("dim", "LLM ");

		if (!hasMax) {
			const spent = theme.fg("muted", formatMoney(d.spend));
			const unlimited = theme.fg("success", " ∞");
			ctx.ui.setStatus(STATUS_ID, label + spent + unlimited);
			return;
		}

		const pctUsed = (d.spend / d.maxBudget!) * 100;
		const pctRemaining = Math.max(0, 100 - pctUsed);
		const amounts = theme.fg("muted", `${formatMoney(d.spend)}/${formatMoney(d.maxBudget!)} `);
		const gauge = theme.fg(
			remainingColor(pctRemaining),
			`${progressBar(pctRemaining)} ${Math.round(pctRemaining)}% left`,
		);
		// Stale-data marker when the last refresh failed.
		const stale = lastError ? theme.fg("dim", " (stale)") : "";
		const eta = computeEta(d);
		const etaText = eta?.beforeReset
			? theme.fg(
					"warning",
					` · empty ~${formatEtaIn(eta.emptyAt.getTime() - Date.now())}`,
				)
			: "";

		const daily = dailySpend(readHistory());
		const spark = daily.some((v) => v !== null)
			? theme.fg("dim", ` ${sparkline(daily)}`) + theme.fg("muted", ` ${formatMoney(daily[daily.length - 1] ?? 0)} today`)
			: "";

		ctx.ui.setStatus(STATUS_ID, label + amounts + gauge + etaText + spark + stale);
	}

	/** Fetch and update the status line. Never throws. */
	async function refresh(ctx: ExtensionContext) {
		if (running || disposed) return;

		const { apiKey, baseUrl } = resolveConfig();
		if (!apiKey) {
			lastError = "no API key";
			renderStatus(ctx);
			return;
		}
		if (!baseUrl) {
			lastError = "no gateway URL";
			renderStatus(ctx);
			return;
		}

		running = true;
		try {
			cached = await fetchBudgetInfo(baseUrl, apiKey);
			// Uncapped key spend is lifetime (never resets) and also what we fall back to when the
			// membership lookup transiently fails: recording it fakes thousands of "daily" spend.
			if (cached.maxBudget) recordSnapshot(cached.spend, `${cached.source}:cap`);
			lastError = undefined;
		} catch (error) {
			lastError = errorMessage(error); // keep previous `cached` as stale data
		} finally {
			running = false;
			if (!disposed) renderStatus(ctx);
		}
	}

	// Start polling (session-scoped, per docs: no timers from the factory).
	pi.on("session_start", async (_event, ctx) => {
		disposed = false;
		void refresh(ctx);
		if (!timer) {
			timer = setInterval(() => void refresh(ctx), Math.max(MIN_REFRESH_MS, REFRESH_MS));
			// Do not hold the process open just for the budget poll.
			timer.unref?.();
		}
	});

	// Refresh once pi is done working, so spend reflects the turn just finished.
	pi.on("agent_settled", async (_event, ctx) => {
		void refresh(ctx);
	});

	// Idempotent teardown.
	pi.on("session_shutdown", async () => {
		disposed = true;
		if (timer) {
			clearInterval(timer);
			timer = undefined;
		}
	});

	pi.registerCommand("budget", {
		description: "Show LiteLLM gateway spend and remaining budget",
		getArgumentCompletions: (prefix: string) => {
			const items = [
				{ value: "refresh", label: "refresh", description: "Force a refresh" },
				{ value: "graph", label: "graph", description: "Daily spend chart: graph [days] [height]" },
			];
			const filtered = items.filter((i) => i.value.startsWith(prefix));
			return filtered.length > 0 ? filtered : null;
		},
		handler: async (args, ctx) => {
			await refresh(ctx);

			const parts = (args ?? "").trim().split(/\s+/);
			if (parts[0] === "graph") {
				const cols = process.stdout.columns || 100;
				const maxDays = Math.max(7, Math.floor((cols - 12) / 3));
				const days = Math.min(maxDays, 30, Math.max(2, Number(parts[1]) || 14));
				const height = Math.min(20, Math.max(4, Number(parts[2]) || 10));
				ctx.ui.notify(renderChart(days, height), "info");
				return;
			}

			const { apiKey, baseUrl } = resolveConfig();
			if (!apiKey) {
				ctx.ui.notify(
					"No LiteLLM API key found. Set LITELLM_API_KEY or LLM_GATEWAY_API_KEY, or add litellm.key to auth.json.",
					"error",
				);
				return;
			}
			if (!baseUrl) {
				ctx.ui.notify(
					"No LiteLLM gateway URL found. Set LITELLM_BASE_URL or LLM_GATEWAY_URL, or add litellm.env.LITELLM_BASE_URL to auth.json.",
					"error",
				);
				return;
			}
			if (!cached) {
				ctx.ui.notify(`Budget unavailable: ${lastError ?? "unknown error"}`, "error");
				return;
			}

			const d = cached;
			const hasMax = d.maxBudget !== null && d.maxBudget > 0;
			const lines: string[] = [];

			lines.push(`Gateway:   ${baseUrl}`);
			if (d.keyAlias) lines.push(`Key:       ${d.keyAlias}`);
			lines.push(`Source:    ${d.source === "team-membership" ? "team membership budget" : "key budget"}`);
			lines.push(`Spent:     ${formatMoney(d.spend)}${hasMax ? ` / ${formatMoney(d.maxBudget!)}` : ""}`);

			if (hasMax) {
				const pctUsed = (d.spend / d.maxBudget!) * 100;
				const pctRemaining = Math.max(0, 100 - pctUsed);
				const left = Math.max(0, d.maxBudget! - d.spend);
				lines.push(`Remaining: ${formatMoney(left)} (${Math.round(pctRemaining)}%)`);
				lines.push(`           ${progressBar(pctRemaining)}`);
				const eta = computeEta(d);
				if (eta) {
					lines.push(`Burn rate: ~${formatMoney(eta.perDay)}/day (cycle average)`);
					lines.push(
						eta.beforeReset
							? `Empty ETA: ~${formatEtaIn(eta.emptyAt.getTime() - Date.now())} (${eta.emptyAt.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}), before reset`
							: "Empty ETA: not before reset at this pace",
					);
				}
			} else {
				lines.push("Budget:    unlimited");
			}

			const daily = dailySpend(readHistory());
			if (daily.some((v) => v !== null)) {
				lines.push(`Last ${SPARK_DAYS}d:   ${sparkline(daily)}  (${daily.map((v) => (v === null ? "-" : v.toFixed(2))).join(" ")})`);
			}

			if (d.budgetDuration) lines.push(`Cycle:     ${d.budgetDuration}`);
			if (d.budgetResetAt) {
				const days = daysUntil(d.budgetResetAt);
				const suffix = days === null ? "" : ` (in ${days} day${days === 1 ? "" : "s"})`;
				lines.push(`Resets:    ${formatResetDate(d.budgetResetAt)}${suffix}`);
			}
			if (lastError) lines.push(`Warning:   refresh failed - ${lastError}`);

			ctx.ui.notify(lines.join("\n"), lastError ? "warning" : "info");
		},
	});
}

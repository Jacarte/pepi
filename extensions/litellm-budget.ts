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
 *   Base URL: LITELLM_BASE_URL -> LLM_GATEWAY_URL -> auth.json (litellm.env.LITELLM_BASE_URL)
 *
 * If no base URL resolves, the extension stays silent rather than guessing a host.
 */

import { readFileSync } from "node:fs";
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

async function fetchBudgetInfo(baseUrl: string, apiKey: string): Promise<BudgetInfo> {
	// Strip a trailing /v1 so both gateway styles work.
	const url = `${baseUrl.replace(/\/+$/, "").replace(/\/v1$/, "")}/key/info`;
	const resp = await fetch(url, {
		headers: { accept: "application/json", "x-litellm-api-key": apiKey },
		signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
	});
	if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);

	const json = (await resp.json()) as Record<string, unknown>;
	const info = (json.info ?? json) as Record<string, unknown>;
	const num = (v: unknown): number | null => (typeof v === "number" ? v : null);
	const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

	return {
		spend: num(info.spend) ?? 0,
		maxBudget: num(info.max_budget),
		budgetDuration: str(info.budget_duration),
		budgetResetAt: str(info.budget_reset_at),
		keyAlias: str(info.key_alias),
	};
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

		ctx.ui.setStatus(STATUS_ID, label + amounts + gauge + stale);
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
			const items = [{ value: "refresh", label: "refresh", description: "Force a refresh" }];
			const filtered = items.filter((i) => i.value.startsWith(prefix));
			return filtered.length > 0 ? filtered : null;
		},
		handler: async (_args, ctx) => {
			await refresh(ctx);

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
			lines.push(`Spent:     ${formatMoney(d.spend)}${hasMax ? ` / ${formatMoney(d.maxBudget!)}` : ""}`);

			if (hasMax) {
				const pctUsed = (d.spend / d.maxBudget!) * 100;
				const pctRemaining = Math.max(0, 100 - pctUsed);
				const left = Math.max(0, d.maxBudget! - d.spend);
				lines.push(`Remaining: ${formatMoney(left)} (${Math.round(pctRemaining)}%)`);
				lines.push(`           ${progressBar(pctRemaining)}`);
			} else {
				lines.push("Budget:    unlimited");
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

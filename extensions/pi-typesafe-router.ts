import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
// Import to read file and o get the root folder of this extension
import fs from "fs";

const THIS_FILE_FOLDER = fs.realpathSync(__dirname);
const ROOT_FOLDER = fs.realpathSync(`${THIS_FILE_FOLDER}/..`);
const LOGS_FOLDER = `${ROOT_FOLDER}/logs`;

const LOG_APPEND_FILE = `${LOGS_FOLDER}/router.log`;
// open the file and append
let logWriter = fs.createWriteStream(LOG_APPEND_FILE, { flags: "a" });

// Copy exact provider/model IDs from: pi --list-models
// load from file at root models.json
const MODELS_FILE = `${ROOT_FOLDER}/models-router.json`;
interface Target {
  provider: string;
  id: string;
}

const models = fs.existsSync(MODELS_FILE)
  ? JSON.parse(fs.readFileSync(MODELS_FILE, "utf-8")) as Record<string, Target>
  : {};

const CHEAP = models.CHEAP
const FRONTIER = models.FRONTIER
const LOCAL = models.LOCAL
const HUMAN = models.HUMAN


// Thresholds on the 0..1 scores. Checked in priority order (see pickTarget).
const T = { human: 0.8, hard: 0.5, trivial: 0.5, routine: 0.7 };


const QUESTIONS = {
  human: {
    type: "noul",
    instructions:
      "Does this task need a human's judgment or approval before an AI agent acts? " +
      "Examples: ambiguous or conflicting requirements, business or policy decisions, " +
      "irreversible or destructive actions, production changes, credentials or secrets, " +
      "or the user explicitly asking for a person. " +
      "Answer with a number between 0 and 1, where 0 means an AI can safely proceed alone " +
      "and 1 means a human must decide.",
  },
  hard: {
    type: "noul",
    instructions:
      "Is this task difficult or high-stakes? Examples: multi-file refactors, architecture or design, " +
      "debugging subtle or intermittent issues, security-sensitive changes, or anything needing deep " +
      "multi-step reasoning. Answer with a number between 0 and 1, where 0 means easy and low-stakes " +
      "and 1 means very hard or high-stakes.",
  },
  trivial: {
    type: "noul",
    instructions:
      "Is this task trivial and self-contained? Examples: a quick factual question, renaming one " +
      "variable, formatting a snippet, a one-line shell command, a short summary of text that is " +
      "already provided. Answer with a number between 0 and 1, where 0 means not trivial at all " +
      "and 1 means completely trivial.",
  },
  routine: {
    type: "noul",
    instructions:
      "Is this task routine, repetitive, or well-defined? Examples: writing a straightforward " +
      "function, adding tests for existing code, small bug fixes with a clear cause, boilerplate, " +
      "docs or comment updates. Answer with a number between 0 and 1, where 0 means not routine " +
      "at all and 1 means very routine.",
  },
} as const;

type Scores = Record<keyof typeof QUESTIONS, number>;

const DEFAULT = CHEAP

// Priority order matters: safety first, then difficulty, then cheapest eligible model.
function pickTarget(s: Scores): Target {
  if (s.human >= T.human) return HUMAN;
  if (s.hard >= T.hard) return FRONTIER;
  if (s.trivial >= T.trivial) return LOCAL;
  if (s.routine >= T.routine) return CHEAP;
  return DEFAULT;
}


export default function(pi: ExtensionAPI) {
  pi.on("input", async (event, ctx) => {
    if (!ctx.isIdle()) return { action: "continue" };

    const warn = (message: string) => {
      if (ctx.hasUI) ctx.ui.notify(message, "warning");
      else console.error(message);
    };

    let target: Target | typeof HUMAN = DEFAULT;

    try {
      const key = process.env.TYPESAFE_API_KEY;
      if (!key) throw new Error("Missing TYPESAFE_API_KEY");

      // Images and unexpanded slash prompts go to DEFAULT without classification.
      // (Images also avoid text-only local models this way.)
      if (!event.images?.length && !event.text.trimStart().startsWith("/")) {
        const response = await fetch("https://api.typesafe.ai/v1/systemone", {
          method: "POST",
          headers: {
            Authorization: `Bearer ${key}`,
            "Content-Type": "application/json",
          },
          signal: AbortSignal.timeout(2000),
          body: JSON.stringify({
            model: "jev-latest",
            state: { task: event.text },
            questions: QUESTIONS,
          }),
        });
        if (!response.ok) throw new Error(`TypeSafe HTTP ${response.status}`);

        const data = (await response.json()) as {
          answers?: Record<string, { noul?: number } | undefined>;
        };

        logWriter.write("\n======================\n")
        logWriter.write(`${JSON.stringify(data)}\n`)

        const scores = {} as Scores;
        for (const name of Object.keys(QUESTIONS) as (keyof Scores)[]) {
          const p = data?.answers?.[name]?.noul;
          if (typeof p !== "number" || !Number.isFinite(p) || p < 0 || p > 1) {
            throw new Error(`Invalid routing response for "${name}"`);
          }
          scores[name] = p;
        }
        target = pickTarget(scores);
      }
    } catch (error) {
      warn(`Router: using ${DEFAULT.id}. ${String(error)}`);
      target = DEFAULT;
    }

    // Human escalation: do not call any model. Tell the interface / parent and stop.
    if (target === HUMAN) {
      const msg =
        "Router: this task needs a human (ambiguous, high-impact, or needs approval). " +
        "No model was called. Please review and decide, then re-send with more direction.";
      if (ctx.hasUI) {
        ctx.ui.notify(msg, "warning");
        ctx.ui.setStatus("router", "Router: needs human");
      } else {
        // Headless / sub-agent run: surface on stderr so the parent process can see it.
        console.error(msg);
      }
      return { action: "handled" };
    }

    // Try the chosen model; if it is unavailable, fall back to DEFAULT before giving up.
    const candidates = target === DEFAULT ? [target] : [target, DEFAULT];
    let chosen: Target | undefined;
    for (const c of candidates) {
      try {
        const model = ctx.modelRegistry.find(c.provider, c.id);
        if (model && (await pi.setModel(model))) {
          chosen = c;
          logWriter.write(`${JSON.stringify(chosen)}\n`)
          logWriter.write(`${event.text}\n`)
          break;
        }
        warn(`Router: could not select ${c.provider}/${c.id}. Check the model ID and provider authentication.`);
      } catch (error) {
        warn(`Router: error selecting ${c.provider}/${c.id}: ${String(error)}`);
      }
    }

    if (!chosen) {
      warn("Router stopped the task: no usable model.");
      return { action: "handled" };
    }

    if (ctx.hasUI) ctx.ui.setStatus("router", `Router: ${chosen.id}`);
    return { action: "continue" };
  });
}

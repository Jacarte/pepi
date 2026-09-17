// Two-stage workflow triage.
//
// args:
// {
//   task: string
// }
//
// The first pass is request-only. When it cannot classify safely from the
// request alone, a bounded scout inspects only the current repository and a
// fresh oracle confirms or revises the tier from that evidence.

const task = typeof args.task === "string" ? args.task.trim() : "";

if (!task) {
  throw new Error("args.task is required");
}

const classificationSchema = {
  type: "object",
  properties: {
    tier: {
      type: "string",
      enum: ["T0", "T1", "T1R", "T2", "T3"]
    },
    confidence: {
      type: "string",
      enum: ["low", "medium", "high"]
    },
    needsScout: {
      type: "boolean"
    },
    reason: {
      type: "string"
    },
    riskSignals: {
      type: "array",
      items: { type: "string" },
      maxItems: 16
    }
  },
  required: [
    "tier",
    "confidence",
    "needsScout",
    "reason",
    "riskSignals"
  ],
  additionalProperties: false
};

const initialResult = await runs.run("triage", {
  agent: "oracle",
  context: "fresh",
  task: [
    "Classify this software-engineering request for workflow routing.",
    "",
    "This is request-only triage.",
    "Do NOT inspect the repository, filesystem, shell, project files, or external sources in this step.",
    "If repository evidence is needed to distinguish tiers safely, set needsScout=true instead of searching for it.",
    "",
    "Request:",
    task,
    "",
    "Workflow tiers:",
    "",
    "T0:",
    "- explanation, advice, or other answer-only work",
    "- no repository mutation required",
    "",
    "T1:",
    "- obvious, reversible implementation change",
    "- expected to stay in one component and roughly three files or fewer",
    "- no material public contract, data migration, security, deployment, infrastructure, or concurrency boundary",
    "",
    "T1R:",
    "- repair of an existing failing test, lint, typecheck, or build",
    "- expected behavior is already defined by repository evidence",
    "- bounded repair rather than new product behavior",
    "",
    "T2:",
    "- ordinary coordinated feature, fix, or refactor",
    "- planning/decomposition or independent acceptance verification is useful",
    "- may involve multiple related implementation tasks",
    "",
    "T3:",
    "- migration, security boundary, destructive/interruption-sensitive operation",
    "- public or cross-service contract change",
    "- material concurrency or data-integrity risk",
    "- deployment, infrastructure, rollout, or rollback risk",
    "- explicitly strict/high-risk work",
    "",
    "Classification rules:",
    "- choose the cheapest safe tier justified by the request",
    "- uncertainty about code location or implementation details is NOT itself a reason to promote to T2/T3",
    "- missing repository facts should normally produce needsScout=true",
    "- T1R applies only when the request is specifically about repairing an existing failing check",
    "- list only concrete risk signals visible in the request wording",
    "- keep reason concise and factual"
  ].join("\n"),
  outputSchema: classificationSchema
});

if (!initialResult || !initialResult.structuredOutput) {
  throw new Error("triage returned no structured output");
}

const initial = initialResult.structuredOutput;

if (!initial.needsScout) {
  return {
    initial,
    confirmed: initial,
    scoutUsed: false
  };
}

const scoutResult = await runs.run("triage-scout", {
  agent: "scout",
  context: "fresh",
  task: [
    "Inspect repository evidence only to support workflow classification.",
    "",
    "Repository boundary:",
    "- the current working directory is the repository root",
    "- restrict all discovery to the current working directory",
    "- never inspect parent directories",
    "- never inspect $HOME or ~",
    "- never inspect /Users, /home, /tmp, or filesystem root",
    "- never use .. to escape the current working directory",
    "- do not search for other repositories",
    "- if a required fact is not available under cwd, report it as unknown",
    "",
    "Safety:",
    "- do not modify application, test, configuration, documentation, or VCS files",
    "- use read-only inspection only",
    "- do not run builds, tests, formatters, generators, migrations, or mutating VCS commands",
    "",
    "Request:",
    task,
    "",
    "Initial classification:",
    JSON.stringify(initial),
    "",
    "Determine only the repository facts needed to classify safely:",
    "- likely affected components and approximate change scope",
    "- public or cross-service contract boundaries",
    "- persistence or migration impact",
    "- security boundaries",
    "- concurrency or data-integrity risk",
    "- deployment or infrastructure impact",
    "- whether the request is a bounded repair of an existing failing check",
    "",
    "Return concise evidence with exact paths/symbols where available. Do not implement anything."
  ].join("\n")
});

if (!scoutResult || typeof scoutResult.output !== "string" || !scoutResult.output.trim()) {
  throw new Error("triage scout returned no evidence");
}

const confirmedResult = await runs.run("triage-confirm", {
  agent: "oracle",
  context: "fresh",
  task: [
    "Confirm or revise the workflow classification using only the supplied request and repository evidence.",
    "",
    "Do NOT inspect the repository, filesystem, shell, project files, or external sources in this step.",
    "Do not perform another scout. Use only the evidence below.",
    "",
    "Request:",
    task,
    "",
    "Initial classification:",
    JSON.stringify(initial),
    "",
    "Repository evidence:",
    scoutResult.output,
    "",
    "Use the same T0/T1/T1R/T2/T3 policy as the initial triage.",
    "Choose the cheapest safe tier justified by concrete evidence.",
    "Unknown implementation details are not themselves a reason to promote.",
    "Promote only when repository evidence demonstrates a higher-risk or broader boundary.",
    "Demote when repository evidence proves the request is narrower than initially assumed.",
    "Set needsScout=false when the evidence is sufficient.",
    "If a material classification fact remains unavailable, keep needsScout=true and state the exact gap; do not search again.",
    "Keep reason concise and factual."
  ].join("\n"),
  outputSchema: classificationSchema
});

if (!confirmedResult || !confirmedResult.structuredOutput) {
  throw new Error("triage confirmation returned no structured output");
}

return {
  initial,
  confirmed: confirmedResult.structuredOutput,
  scoutUsed: true
};

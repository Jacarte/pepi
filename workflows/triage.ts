// Request-only workflow triage.
//
// args:
// {
//   task: string
// }
//
// This first pass intentionally does not inspect the repository. If repository
// evidence is needed to classify safely, it returns needsScout=true. A later
// workflow stage may then scout and confirm/revise the tier.

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

const result = await runs.run("triage", {
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

if (!result || !result.structuredOutput) {
  throw new Error("triage returned no structured output");
}

return result.structuredOutput;

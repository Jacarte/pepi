// workflows/implement.js
//
// args:
// {
//   task: string,
//   tier?: "T1" | "T1R" | "T2" | "T3",
//   approvedPlan?: string,
//   failingCommand?: string,
//   research?: string
// }
//
// T1:
//   worker -> fresh verifier
//
// T1R:
//   worker bounded repair -> fresh verifier
//
// T2:
//   scout -> oracle plan -> APPROVAL
//   worker -> fresh verifier -> parallel reviewers
//   -> worker fix -> fresh verifier -> final review
//
// T3:
//   optional researcher + evidence-auditor
//   scout -> oracle plan -> plan reviewer -> oracle revision -> APPROVAL
//   then same execution pipeline as T2

const task = args.task;
const tier = args.tier || "T2";
const approvedPlan = args.approvedPlan || "";
const failingCommand = args.failingCommand || "";
const researchQuestion = args.research || "";

if (!task) {
  throw new Error("args.task is required");
}

if (!["T1", "T1R", "T2", "T3"].includes(tier)) {
  throw new Error(`Unsupported tier: ${tier}`);
}

if (tier === "T1R" && !failingCommand) {
  throw new Error("T1R requires args.failingCommand");
}

const verificationSchema = {
  type: "object",
  properties: {
    verdict: {
      type: "string",
      enum: ["PASS", "FAIL", "BLOCKED"]
    },
    summary: {
      type: "string"
    },
    commands: {
      type: "array",
      items: { type: "string" }
    },
    evidence: {
      type: "array",
      items: { type: "string" }
    }
  },
  required: ["verdict", "summary"],
  additionalProperties: false
};

const reviewSchema = {
  type: "object",
  properties: {
    verdict: {
      type: "string",
      enum: ["OK", "OK_WITH_NOTES", "BLOCK"]
    },
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          severity: {
            type: "string",
            enum: ["P0", "P1", "P2"]
          },
          summary: {
            type: "string"
          },
          location: {
            type: "string"
          }
        },
        required: ["severity", "summary"],
        additionalProperties: false
      }
    },
    summary: {
      type: "string"
    }
  },
  required: ["verdict", "findings", "summary"],
  additionalProperties: false
};

const jjBoundary = [
  "Jujutsu boundary:",
  "",
  "If this repository contains .jj:",
  "- inspect the current change and parent before editing",
  "- preserve unrelated work",
  "- never use git commit to shape the stack",
  "- never push or move remote-facing bookmarks",
  "",
  "After the logical slice is complete and its checks pass:",
  "- run jj describe with a concise imperative description",
  "- record the finalized change ID and commit ID",
  "- run jj new",
  "- verify the new working copy is blank",
  "- verify its parent is the finalized change",
  "",
  "Return finalized_change_id, finalized_commit_id,",
  "next_change_id, next_commit_id, and blank-child evidence.",
  "",
  "If this is not a Jujutsu repository, ignore this boundary."
].join("\n");

function verifierTask(originalTask, command) {
  const lines = [
    "Act only as an independent acceptance verifier.",
    "",
    "Do NOT modify files.",
    "Do NOT propose implementation changes unless verification fails.",
    "Worker test results are diagnostic only.",
    "Run the relevant acceptance checks yourself against the current repository state.",
    "",
    "Original requirement:",
    originalTask
  ];

  if (command) {
    lines.push(
      "",
      "At minimum, run this exact acceptance command fresh:",
      command
    );
  }

  lines.push(
    "",
    "Return PASS only when current repository evidence supports the requirement.",
    "Return FAIL when code executes but does not satisfy the requirement.",
    "Return BLOCKED only when verification itself cannot be performed."
  );

  return lines.join("\n");
}

function implementationTask(originalTask, plan) {
  return [
    "Implement the following task.",
    "",
    "Original requirement:",
    originalTask,
    "",
    plan ? "Approved plan:" : "",
    plan || "",
    "",
    "Rules:",
    "- you are the single writer",
    "- make the smallest coherent change",
    "- follow existing repository patterns",
    "- run focused diagnostic checks",
    "- do not invent product or architecture decisions",
    "- if a new material decision is required, contact the supervisor",
    "- do not push, publish, or deploy",
    "",
    jjBoundary
  ].join("\n");
}

// -----------------------------------------------------------------------------
// T1 — obvious small edit
// -----------------------------------------------------------------------------

if (tier === "T1") {
  const implementation = await runs.run("implement", {
    agent: "worker",
    task: implementationTask(task, "")
  });

  let verification = await runs.run("verify", {
    agent: "oracle",
    context: "fresh",
    task: verifierTask(task, ""),
    outputSchema: verificationSchema
  });

  if (verification.structuredOutput.verdict === "FAIL") {
    const repair = await runs.run("verification-fix", {
      agent: "worker",
      task: [
        "Fix the failed independent verification.",
        "",
        "Original task:",
        task,
        "",
        "Verification evidence:",
        verification.output,
        "",
        "Make only the smallest evidence-backed correction.",
        "",
        jjBoundary
      ].join("\n")
    });

    verification = await runs.run("verify-after-fix", {
      agent: "oracle",
      context: "fresh",
      task: verifierTask(task, ""),
      outputSchema: verificationSchema
    });

    return {
      state:
        verification.structuredOutput.verdict === "PASS"
          ? "complete"
          : "verification_failed",
      tier,
      implementation,
      repair,
      verification
    };
  }

  return {
    state:
      verification.structuredOutput.verdict === "PASS"
        ? "complete"
        : "verification_blocked",
    tier,
    implementation,
    verification
  };
}

// -----------------------------------------------------------------------------
// T1R — bounded test/build repair
// -----------------------------------------------------------------------------

if (tier === "T1R") {
  const repair = await runs.run("repair", {
    agent: "worker",
    task: [
      "Repair this existing failing check.",
      "",
      "Original task:",
      task,
      "",
      "Failing command:",
      failingCommand,
      "",
      "You may perform at most 3 evidence-producing edit/test iterations.",
      "",
      "For each iteration:",
      "1. form one concrete hypothesis",
      "2. make the smallest coherent edit",
      "3. rerun the exact failing command",
      "",
      "Do not broaden scope merely because the first hypothesis fails.",
      "Escalate only if repository evidence reveals a real architecture,",
      "security, migration, or product decision.",
      "",
      jjBoundary
    ].join("\n")
  });

  let verification = await runs.run("verify", {
    agent: "oracle",
    context: "fresh",
    task: verifierTask(task, failingCommand),
    outputSchema: verificationSchema
  });

  if (verification.structuredOutput.verdict === "FAIL") {
    const secondRepair = await runs.run("verification-fix", {
      agent: "worker",
      task: [
        "Independent verification still fails.",
        "",
        "Original task:",
        task,
        "",
        "Required command:",
        failingCommand,
        "",
        "Verification evidence:",
        verification.output,
        "",
        "Perform one focused repair cycle only.",
        "",
        jjBoundary
      ].join("\n")
    });

    verification = await runs.run("verify-after-fix", {
      agent: "oracle",
      context: "fresh",
      task: verifierTask(task, failingCommand),
      outputSchema: verificationSchema
    });

    return {
      state:
        verification.structuredOutput.verdict === "PASS"
          ? "complete"
          : "verification_failed",
      tier,
      repair,
      secondRepair,
      verification
    };
  }

  return {
    state:
      verification.structuredOutput.verdict === "PASS"
        ? "complete"
        : "verification_blocked",
    tier,
    repair,
    verification
  };
}

// -----------------------------------------------------------------------------
// T2 / T3 planning phase
//
// Important:
// If approvedPlan is absent, this workflow STOPS after producing a plan.
// The parent must show it to the user and obtain approval.
// Then invoke this workflow again with approvedPlan.
// -----------------------------------------------------------------------------

if (!approvedPlan) {
  let externalEvidence = "";

  if (tier === "T3" && researchQuestion) {
    const research = await runs.run("research", {
      agent: "researcher",
      context: "fresh",
      task: [
        "Research this implementation question using primary sources where possible.",
        "",
        researchQuestion,
        "",
        "Return only information materially relevant to the implementation decision."
      ].join("\n")
    });

    const audit = await runs.run("research-audit", {
      agent: "evidence-auditor",
      context: "fresh",
      task: [
        "Audit the following research.",
        "Check whether important claims are actually supported.",
        "Identify unsupported or overstated claims.",
        "",
        research.output
      ].join("\n")
    });

    externalEvidence = [
      "External research:",
      research.output,
      "",
      "Evidence audit:",
      audit.output
    ].join("\n");
  }

  const scout = await runs.run("scout", {
    agent: "scout",
    context: "fresh",
    task: [
      "Investigate this task before implementation.",
      "",
      "Task:",
      task,
      "",
      "Find:",
      "- relevant entry points",
      "- important types/functions/interfaces",
      "- current behavior and data flow",
      "- files likely to change",
      "- existing tests and verification commands",
      "- architecture/security/concurrency/data risks",
      "- concrete unknowns",
      "",
      "Do not implement anything."
    ].join("\n")
  });

  let plan = await runs.run("plan", {
    agent: "oracle",
    task: [
      "Produce a bounded implementation plan.",
      "",
      "Original requirement:",
      task,
      "",
      "Repository context:",
      scout.output,
      "",
      externalEvidence,
      "",
      "The plan must contain:",
      "- implementation slices in dependency order",
      "- concrete files/symbols where known",
      "- acceptance criteria",
      "- verification commands where established",
      "- architecture/product assumptions",
      "- risks and rollback/resumption notes",
      "- explicit Jujutsu slice boundaries if .jj is present",
      "",
      "Do not implement anything."
    ].join("\n")
  });

  if (tier === "T3") {
    const critique = await runs.run("plan-review", {
      agent: "reviewer",
      context: "fresh",
      task: [
        "Review this high-risk implementation plan.",
        "",
        "Original requirement:",
        task,
        "",
        "Plan:",
        plan.output,
        "",
        "Check specifically for:",
        "- missing rollback/resumption behavior",
        "- hidden contract changes",
        "- migration/destructive risks",
        "- concurrency/security/data-integrity risks",
        "- incomplete verification",
        "- unnecessary complexity",
        "",
        "Do not implement anything."
      ].join("\n")
    });

    plan = await runs.run("plan-revision", {
      agent: "oracle",
      task: [
        "Produce the final plan revision.",
        "",
        "Original requirement:",
        task,
        "",
        "Initial plan:",
        plan.output,
        "",
        "Independent critique:",
        critique.output,
        "",
        "Incorporate valid criticism only.",
        "Do not add speculative work.",
        "Do not implement anything."
      ].join("\n")
    });

    return {
      state: "needs_approval",
      tier,
      scout: scout.output,
      plan: plan.output,
      planReview: critique.output
    };
  }

  return {
    state: "needs_approval",
    tier,
    scout: scout.output,
    plan: plan.output
  };
}

// -----------------------------------------------------------------------------
// T2 / T3 approved execution phase
// -----------------------------------------------------------------------------

const implementation = await runs.run("implement", {
  agent: "worker",
  task: implementationTask(task, approvedPlan)
});

let verification = await runs.run("verify", {
  agent: "oracle",
  context: "fresh",
  task: verifierTask(task, ""),
  outputSchema: verificationSchema
});

// Failed fresh verification gets one focused repair before review.
if (verification.structuredOutput.verdict === "FAIL") {
  const verificationFix = await runs.run("verification-fix", {
    agent: "worker",
    task: [
      "Fix the failed independent verification.",
      "",
      "Original task:",
      task,
      "",
      "Approved plan:",
      approvedPlan,
      "",
      "Verification evidence:",
      verification.output,
      "",
      "Make only evidence-backed corrections.",
      "Do not reopen planning unless the approved approach is materially invalid.",
      "",
      jjBoundary
    ].join("\n")
  });

  verification = await runs.run("verify-after-fix", {
    agent: "oracle",
    context: "fresh",
    task: verifierTask(task, ""),
    outputSchema: verificationSchema
  });

  if (verification.structuredOutput.verdict !== "PASS") {
    return {
      state: "verification_failed",
      tier,
      implementation,
      verificationFix,
      verification
    };
  }
}

if (verification.structuredOutput.verdict === "BLOCKED") {
  return {
    state: "verification_blocked",
    tier,
    implementation,
    verification
  };
}

// Independent reviewers inspect the stable verified snapshot.
const reviews = await runs.all([
  {
    key: "correctness",
    agent: "reviewer",
    context: "fresh",
    task: [
      "Review the current implementation for correctness and regressions.",
      "",
      "Original requirement:",
      task,
      "",
      "Approved plan:",
      approvedPlan,
      "",
      "Report only concrete findings caused or exposed by this change."
    ].join("\n"),
    outputSchema: reviewSchema
  },
  {
    key: "tests",
    agent: "reviewer",
    context: "fresh",
    task: [
      "Review the current implementation specifically for test quality.",
      "",
      "Original requirement:",
      task,
      "",
      "Look for missing cases, weak assertions, untested failure paths,",
      "and acceptance criteria not actually demonstrated.",
      "",
      "Report only concrete findings."
    ].join("\n"),
    outputSchema: reviewSchema
  },
  {
    key: "simplicity",
    agent: "reviewer",
    context: "fresh",
    task: [
      "Review the current implementation for unnecessary complexity.",
      "",
      "Original requirement:",
      task,
      "",
      "Look for duplication, architecture drift, speculative abstraction,",
      "dead code, or a materially simpler implementation.",
      "",
      "Report only concrete findings."
    ].join("\n"),
    outputSchema: reviewSchema
  }
]);

const importantFindings = reviews.flatMap((review) => {
  const findings = review.structuredOutput?.findings || [];
  return findings.filter(
    (finding) => finding.severity === "P0" || finding.severity === "P1"
  );
});

let reviewFix = null;

// Only route substantive findings back to the writer.
if (importantFindings.length > 0) {
  reviewFix = await runs.run("review-fix", {
    agent: "worker",
    task: [
      "Resolve valid P0/P1 review findings.",
      "",
      "Original requirement:",
      task,
      "",
      "Approved plan:",
      approvedPlan,
      "",
      "Reviews:",
      reviews.map((review) => review.output).join("\n\n---\n\n"),
      "",
      "Rules:",
      "- verify each finding against the actual code",
      "- do not blindly implement reviewer suggestions",
      "- ignore style-only or unsupported findings",
      "- make the smallest coherent correction",
      "- run focused diagnostic checks",
      "",
      jjBoundary
    ].join("\n")
  });

  // Any code change invalidates previous verification.
  verification = await runs.run("verify-after-review-fix", {
    agent: "oracle",
    context: "fresh",
    task: verifierTask(task, ""),
    outputSchema: verificationSchema
  });

  if (verification.structuredOutput.verdict !== "PASS") {
    return {
      state: "verification_failed_after_review",
      tier,
      implementation,
      reviews,
      reviewFix,
      verification
    };
  }
}

// Final fresh read-only blocker review.
const finalReview = await runs.run("final-review", {
  agent: "reviewer",
  context: "fresh",
  task: [
    "Perform a final blockers-only review of the current stable implementation.",
    "",
    "Original requirement:",
    task,
    "",
    "Approved plan:",
    approvedPlan,
    "",
    "Only report concrete P0/P1 issues that should prevent completion.",
    "Do not repeat resolved findings.",
    "Do not report stylistic preferences."
  ].join("\n"),
  outputSchema: reviewSchema
});

const finalBlockingFindings =
  finalReview.structuredOutput?.findings?.filter(
    (finding) =>
      finding.severity === "P0" || finding.severity === "P1"
  ) || [];

return {
  state:
    finalBlockingFindings.length === 0
      ? "complete"
      : "review_blocked",
  tier,
  implementation,
  verification,
  reviews,
  reviewFix,
  finalReview
};

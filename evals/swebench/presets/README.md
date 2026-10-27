# Custom Verified20 v1 / 300-second preset

**Custom SWE-bench Verified 20-task subset; not an official Verified score.**
The shell and direct Python CLI default to `verified20-v1.ids` and a 300-second
**per-task Pi invocation** deadline. Explicit `PEPI_EVAL_IDS` (file or comma-separated
IDs), `PEPI_EVAL_TIMEOUT_SECONDS`, `prepare --ids`, and `run --timeout-seconds` still
work. Selection does not change the dataset name: `SWE-bench/SWE-bench_Verified`, test.
The task-source commit must be `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d` for
this preset. A mismatch fails; use an explicit different selection for other work.

## Frozen inputs and provenance

- `verified20-v1.ids`: ordered 20 unique IDs, SHA256
  `11fa9243c458222ee0c2dadd3fde52092862bcd927a0895c6551af2de186a3b6`.
- `verified20-v1.json`: versioned definition, label, revisions and input hashes.
- `verified20-v1.metadata.json`: 500 local canonical Verified/test metadata rows,
  containing **only** instance_id, repo, datasets and split. No patches, tests,
  problem statements, model outcomes or difficulty measures were used to select.

Starting from 2,519 local task records, filter Verified/test and require exactly
500 unique IDs (fail on count/duplicate drift). Eligible repos have >=4 cases;
sort by descending eligible count, lexicographic repo tie-break, take five.
Within each, sort by ascending SHA256 hex of UTF-8 `42\n{repo}\n{instance_id}`
(no trailing newline), then instance_id, take four. Freeze repo-major/rank-major:
**django/django, sympy/sympy, sphinx-doc/sphinx, matplotlib/matplotlib,
scikit-learn/scikit-learn**, four tasks each. The snapshot establishes local
membership, not independently verified remote equality.

`prepare` records the canonical tasklist hash (UTF-8 lines with final newline),
actual source revision, and matching preset definition in `manifest.json` and
hashed `build/selection.json`. Only the exact ordered preset gets its label;
other selections remain ordinary selections, not a fabricated custom dataset.
Readers reject changed definition/selection/source/repo metadata and preserve
legacy manifests without selection metadata. Frozen input changes need a new
preset version and corresponding code/hash/test review.

## Future authorized runs (not executed by offline tests)

Use fresh run names and a pre-existing externally restricted `pepi-eval` network.
Pin identical approved Pi/subagents versions and a digest-pinned Node image across
profiles; retain CPU=4, memory=8g, linux/amd64, sequential execution, MCP/network
policy and separate grading timeout=1800. Bedrock additionally needs its approved
exact LiteLLM pin and evaluation-only gateway credentials. Codex needs Pi 1.1.0
and an evaluation-only OAuth auth.json supplied at runtime. See ../README.md for
credential/MCP provisioning; never put credentials into config-dir/build inputs.
Do **not** use config-label for suite naming (a pre-existing prediction/report
config-label mismatch is outside this change).

```sh
# Set these to approved installed pins before preparing either profile.
export PEPI_EVAL_PI_VERSION=1.1.0
export PEPI_EVAL_SUBAGENTS_VERSION='<approved exact pin>'
export PEPI_EVAL_NODE_IMAGE='node:22-bookworm-slim@sha256:<approved digest>'
export PEPI_EVAL_LITELLM_VERSION='<approved exact bedrock pin>'
export PEPI_EVAL_TASK_REPO=/Users/javcab/swe-bench-tasks
# Keep IDS/TIMEOUT overrides unset to use 20/300. Never set CONFIG_LABEL for the suite.
unset PEPI_EVAL_IDS PEPI_EVAL_TIMEOUT_SECONDS PEPI_EVAL_CONFIG_LABEL

# Review commands without building or calling a provider:
PEPI_EVAL_PROFILE=codex PEPI_EVAL_RUN_NAME=verified20-v1-codex-01 \
  evals/pepi-eval.sh --dry-run -y all
PEPI_EVAL_PROFILE=bedrock PEPI_EVAL_RUN_NAME=verified20-v1-bedrock-01 \
  evals/pepi-eval.sh --dry-run -y all

# Paid execution only after authorization and provisioning evaluation-only secrets:
PEPI_EVAL_PROFILE=codex PEPI_EVAL_RUN_NAME=verified20-v1-codex-01 \
  PEPI_EVAL_SECRETS_DIR=/path/to/codex-eval-secrets evals/pepi-eval.sh -y all
PEPI_EVAL_PROFILE=bedrock PEPI_EVAL_RUN_NAME=verified20-v1-bedrock-01 \
  PEPI_EVAL_SECRETS_DIR=/path/to/bedrock-eval-secrets evals/pepi-eval.sh -y all

# One-task smoke before a paid suite; explicit timeout can retain old 1200 behavior.
PEPI_EVAL_PROFILE=codex PEPI_EVAL_RUN_NAME=verified20-smoke-01 \
  PEPI_EVAL_IDS=django__django-11095 PEPI_EVAL_TIMEOUT_SECONDS=300 \
  PEPI_EVAL_SECRETS_DIR=/path/to/codex-eval-secrets evals/pepi-eval.sh -y all

python -m evals.swebench.reporting compare \
  evals/swebench/runs/verified20-v1-codex-01 \
  evals/swebench/runs/verified20-v1-bedrock-01
```

Twenty sequential invocations allow at most **100 minutes of agent invocation**;
build/setup/export/grading are outside that deadline. Timeout submits a partial
diff after stopping agent processes; it is not proof of task failure. The unchanged
official grader gets only selected host task snapshots, never agent-mounted gold
or test inputs. Reports label this custom subset, raw **X/20 resolved**, and unknown
grades separately; they do not claim a full 500-task Verified score.

New preset-aware comparisons require identical selection/source/task identity,
execution protocol, shared Pi/subagents pins, Node image reference and per-ID
platforms. Optional LiteLLM pins must match when both profiles need it; Codex need
not install LiteLLM. Models/providers/profiles and resulting wrapper image IDs may
intentionally differ. Legacy comparisons retain their existing behavior.
Matching mutable Node/base-image tags is not proof of matching image content or
transitive npm dependencies: pin digests and inspect saved immutable build image
IDs. Builds still call Docker build and naturally reuse matching layers; no unsafe
cross-profile wrapper reuse, cache-skip promise or no-build guarantee is added.

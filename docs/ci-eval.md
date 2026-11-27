# PR evals in CI

`.github/workflows/eval.yml` evaluates a PR's agent setup when a writer adds a label, posts the
results in the PR, and commits them as the tier's baseline when the PR merges. The logic lives in
the Dagger functions `ci-eval-run`, `ci-eval-report` and `ci-baseline-prepare` of
`.dagger/modules/daggervals`. Only same-repo PRs against the default branch are evaluated.
What each task measures and how an eval runs end to end are in [evals-tasks.md](evals-tasks.md).

## Use

| Label | Tier | Shards (parallel jobs) | Runs | Rough cost, time |
|---|---|---|---|---|
| `eval` | fast | rust (8 tasks), go (5), tasks-memory (6) | 1 | ~$3-5, up to ~2 h |
| `eval-full` | full | the fast shards + tasks-long (five-modules, kv-database, legacy-monolith, memory-carryover, twelve-modules) | 3 | ~$15-25, up to ~6 h |

The costs are estimates from local runs (agent cost only; the judge and compare calls come on
top). Check the real figures in the judge comment after the first runs. Each job's timeout is
120 min; tasks-long gets 330.

1. A user with write access adds the label. Labels added by anyone else are removed with a note.
2. The owner approves the `eval-pr` deployment. The run is named `eval #<PR> <head sha>`: check
   that the sha is the head you reviewed. Expect two prompts: one for all shard jobs, which wait
   together, and one for the report job, which starts after the shards. Verify this on the first
   run.
3. The run posts two comments that it updates in place:
   - The judge comment (`<!-- pepi-eval:judge -->`) has the summary table and each shard's judge
     report, cut to fit GitHub's size limit.
   - The comparison comment (`<!-- pepi-eval:compare -->`) has the compare-runs report against
     the tier's baseline, or one line saying why there is no comparison.
   - The job summary and the `eval-report-<tier>-<sha>` artifact hold the full text. Shard and
     report artifacts are kept 30 days.
4. The commit status `pepi-eval/<tier>` on the PR head turns green whenever the eval ran,
   whatever the verdict. It turns red only on infrastructure failures: a shard or report failed,
   timed out or was not approved, or `status.json` has `infra_ok: false`.

Any push that ends an eval label removes the labels and posts `eval invalidated by <sha>`. Add a
label again to rerun. A run that is already going is not cancelled; its comments name the head
they evaluated.

Only one eval runs at a time, repo-wide. GitHub keeps one pending run per concurrency group, so a
newer label event replaces a waiting one, which shows as cancelled. This is accepted: add the
label again later. A run waiting for approval also holds the queue, so reject runs you don't want.
Other events (pushes, merges, other labels) never wait in this queue.

**Both labels.** The `eval-full` event runs the full tier. The `eval` event yields to it and posts
a gate note. If both labels are added while the queue is busy, the `eval` event can replace the
pending `eval-full` run. Then nothing runs and the gate note is the only trace: remove and add
`eval-full` again. When a run starts after waiting in the queue, the gate also skips it if the
head moved, the PR closed or the label is gone.

## Approval checklist (the load-bearing control)

Approving `eval-pr` means trusting the PR's code with the eval secrets. The shards and the report
run the PR's own Dagger module, profiles and extensions. That code can read every secret of the
job and send it anywhere: the agent containers have open network egress, and this is accepted
unless restricted later. Before clicking, read these parts of the PR diff:

- `.dagger/**`
- `profiles/**`
- `extensions/**`
- `AGENTS.md`
- `.github/workflows/eval.yml`
- `eval-tasks.ref`

Never approve a PR that edits `eval.yml` or the secret handling. `pull_request_target` runs the
base branch's `eval.yml`, so such an edit is only active after its merge. Review it as a normal
PR and don't eval it.

## Baselines

When a same-repo PR into the default branch merges and still carries a tier label, the baseline
job commits that tier's results as `eval baseline for #<N> (<tier>)`:

- `eval-baseline/<tier>/` is replaced wholesale.
- That tier's entry in `.eval.track` is updated (YAML): setup hash, tasks repo and sha, profile,
  runs, tasks, files, PR, evaluated sha, merge sha and date.

Because a push removes the labels, a label that survives to the merge means the evaluated head is
the merged head; `ci-baseline-prepare` checks this again. Baseline jobs run one at a time
(concurrency group `eval-baseline`). A third one waiting replaces the pending one, which is rare
when PRs are kept up to date. A merge without a label commits nothing. Fork PRs and closed
unmerged PRs do nothing.

A PR is compared only with its own tier's baseline, and only when the baseline is comparable: the
same tasks repo and sha, profile, task list and runs, and a setup hash equal to the hash of the
PR's merge base. Otherwise the comparison comment shows the reason, for example
`baseline is for <hash>, main is at <hash>`. This happens after any merge that changes the setup
without an eval.

The setup hash includes the content of the agent env file. The removal of the model router changed
the setup hash of every run, so every baseline from before it is incomparable until a new baseline is
committed.

### Trust chain and accepted risks

The PR's code produces `status.json` and the result files, so they are untrusted data:

- The publish job reads only `infra_ok` and the verdict from `status.json`, and validates both.
- Comment bodies are passed as files and never printed to the log.
- The baseline job uses only the `eval-report-<tier>-<head sha>` artifact of one specific run.
  That run must be:
  - a successful `pull_request_target` run of `.github/workflows/eval.yml`, whose logic comes
    from the default branch;
  - named `eval #<PR> <head sha>`;
  - for this PR, when GitHub lists the run's PRs;
  - one whose `eval-pr` deployment was approved, as its approval history shows.
- The artifact holds the report's `status.json` and the exact shard files the report read.
- `ci-baseline-prepare` comes from the merged default branch and holds no secrets. It refuses on a
  head mismatch, an incomplete eval, disallowed names or sizes, or a secret-shaped match. A
  refusal turns the run red and posts a comment that does not repeat the matched text.

Accepted risks:

- **Scores are not recomputed.** The baseline keeps the scores the PR produced; the merged code is
  trusted.
- **Secret scanning is best-effort.** Exact secret values are scanned in `ci-eval-run` and
  `ci-eval-report`, and secret shapes when the baseline is prepared. Encoding or splitting a
  secret evades both.
- **Env files are not Dagger Secrets.** `--agent-env` and `--hippocampus-env` are passed as Dagger
  Files, so Dagger does not scrub their values from its logs. The workflow masks every value
  (and every string in `PI_AUTH`) in the GitHub log before writing the files.
- **Bot push to `main`.** The baseline commit is pushed with `GITHUB_TOKEN` and needs `main`
  unprotected. If `main` gets protected, add GitHub Actions as a bypass actor in the ruleset, or
  change the baseline job to open a follow-up PR (not implemented). Pushes made with
  `GITHUB_TOKEN` start no workflows. `daggervals.yml` also ignores `eval-baseline/**` and
  `.eval.track`; changes to `eval.yml` and to these docs still trigger it.

### Tasks repo pin and known-missing tasks

The tasks shards take the tasks-repo commit from `eval-tasks.ref`, a full 40-hex sha. Bump it in a
PR; this makes every baseline incomparable until a new baseline is committed.

The current pin is `2d35918`. It contains the six memory-* tasks, five-modules, kv-database,
legacy-monolith and twelve-modules. It is missing only `memory-carryover`.

Until `memory-carryover` is pushed to the tasks repo and the pin is bumped, the tasks-long shard
fails at start with `unknown task "memory-carryover"`. Every `eval-full` run is then red with no
comment results. The rust, go and tasks-memory shards still run their 3 runs, roughly $10-15 and
up to 2 h, and their files stay in the run's shard artifacts. Don't use `eval-full` until then.
The fast tier is not affected.

### Skill tasks (not in CI yet)

A `tasks-skills` shard for the skill ablation (see `evals-tasks.md`, "Skill ablation") is not in
`ci/tiers.go`. Its tasks, `skill-<name>`, must exist in the tasks repo at the pinned commit, and the
CI must stage each task's vendored skill (`--global-skills`). Add the shard, with both, in the PR
that bumps `eval-tasks.ref`. Until then the shard would fail at start with `unknown task`, as
`memory-carryover` does now.

## Manual GitHub setup (once)

The existing `evals` environment of `daggervals.yml` stays as it is. Adding reviewers there would
make every push to `main` wait for approval.

1. **Labels.** Create `eval` and `eval-full`.
2. **Dedicated environment `eval-pr`** (Settings > Environments):
   - Required reviewers: the owner.
   - Prevent self-review: off, because the owner both adds the label and approves.
   - Deployment branches: selected branches, `main` only. This is a security control, not a
     convenience. `pull_request_target` jobs run with the base branch as their ref, so they pass.
     A `pull_request` workflow added on a PR branch, or a PR against another branch, can't get
     the secrets. Verify this on the first run.
3. **Secrets of `eval-pr`.** Add `PI_AUTH` and `HIPPOCAMPUS_ENV`, plus the optional variable
   `PROFILE` (default `plain`).
   - Keep the secrets environment-scoped only, never repository secrets: `pull_request` workflows
     on same-repo branches can read repository secrets.
   - Use dedicated eval credentials found nowhere else. Each should be spend-capped, revocable,
     and have budget alerts.
   - `HIPPOCAMPUS_ENV`, the memory server's gateway key, has the largest blast radius. Its format
     is `KEY=VALUE` lines only, values limited to `[A-Za-z0-9._~+/=:@%?&,!*-]`, with no quotes,
     `$`, `#` or comments.
   - `PI_AUTH` must be valid JSON.
4. **Workflow permissions** (Settings > Actions > General): read by default. Each job declares the
   permissions it needs.
5. **`main` stays unprotected** for the bot push; see the accepted risks above.

## Verify on the first run

- The number of approval prompts.
- That the `main`-only deployment branch policy admits `pull_request_target` jobs.
- That the `pepi-eval/<tier>` status appears on the PR head.
- That the collaborator permission endpoint works with `GITHUB_TOKEN`.
- That the run API reports `path` as `.github/workflows/eval.yml` and `display_title` as the
  run-name.
- That `/actions/runs/<id>/approvals` lists the `eval-pr` approval.
- That "Re-run failed jobs" sees the earlier attempt's shard artifacts.

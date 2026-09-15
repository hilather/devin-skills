---
name: execute-plan
description: Execute a PR Plan DAG from a design document. Parses and validates the plan, linearizes it into a stack, implements each PR sequentially in the shared tree, runs a mandatory read-only reviewer loop per PR, and assembles a plain-git branch stack with compare URLs or draft PRs.
when-to-use: Use when asked to "execute plan", "run the plan", "implement the design", "build the PR stack", or "/execute-plan".
argument-hint: "<design-doc-path> [--effort N] [--concurrency N] [--dry-run] [--resume <PLAN_ID>] [--instructions \"...\"] [--no-graphite] [--auto-pr]"
triggers:
  - user
  - model
---

# Execute Plan Skill

You are an **inline orchestrator** (this skill omits `subagent` and `allowed-tools` so you keep `run_subagent`). You take a `## PR Plan` DAG (produced by `/design`), parse and validate it, linearize it into a stack, implement each PR **yourself, in the shared tree, on a per-PR branch**, run a mandatory read-only `pr-reviewer` subagent per PR, and assemble the results into a pushed branch stack.

You are the **single point of control** for all git operations, all implementation, and all stack assembly. The reviewer is the only subagent, and it is read-only — its job is independent verification, not gate-witness duty.

## Honest gaps (Devin ≠ Grok)

This is a port of Grok Build's `/execute-plan`. These deltas are deliberate, not bugs:

- **Sequential execution only.** Grok launches parallel implementer subagents in isolated worktrees via `spawn_subagent(isolation="worktree")` + `wait_any` + kill. Devin `run_subagent` has no `isolation`, no `cwd`, no `wait_any`, no timeout, and no kill — background children share the parent tree. `--concurrency` is parsed for CLI parity, forced to 1, and reported.
- **The parent implements each PR.** Child `write` is not assumed to re-enter hooks (same rule as `/design`); a write-capable implementer profile could bypass the write-lock entirely, or be locked like the parent — the design does not hinge on an unverified answer. Do not spawn `subagent_general` or any write-capable profile for implementation.
- **The reviewer is a new read-only `pr-reviewer` profile**, not `code-skeptic`. A skeptic `PASS` mints session-wide `code-passed` — minting that mid-run on one PR's coverage would falsely satisfy the Stop-lock for the whole stack. `pr-reviewer` never emits `GATES_VERDICT`.
- **Plain-git only.** `graphite_available` is fixed `false`; `--no-graphite` is parsed and ignored (there is no Graphite mode to disable).
- **No `memory.py`.** Grok's workspace memory helper becomes: Step 0 scans prior runs' `lessons.md` under the execute-plan cache root into a `past_issues_briefing`; Step 10 writes this run's `lessons.md`. Durable merge into `~/git/agent-skills` is the user's call.
- **No PR timeout.** Grok kills implementers at 15 min; a hung in-turn step here is user-interruptible instead.
- **No mid-stack retry.** Linear stack ancestry (below) means a node can only be retried on `--resume` when no later node is `completed` — see Step 7's positional retry rule.
- **`main` is hardcoded** as the default branch (`origin/main`, `git checkout main`, `parent_branch="main"`), matching upstream.
- **Locked-phase discipline:** while write-locked, `exec` is restricted — no `command -v`, `ls`, `find`, redirects, chaining, or command substitution. Use `find_file_by_name` for enumeration, gate CLI subcommands for gate work, and `request_scope` for out-of-workspace access. The `gh` probe is deferred to Step 8 (post-unlock).
- **Plugin installs have no gate.** Without the hook install, `allow-exec-plan` does not exist; the documented fallbacks (below) apply and the run is unenforced.

## Linear stack ancestry (replaces Grok's DAG ancestry + cherry-pick)

Grok branches each PR off its declared dependencies' commits, then re-linearizes at assembly with `checkout -B` + range cherry-pick. We do it once, up front: each PR's branch is created **at the top of its Step 4 iteration** with `git checkout -B <pr.branch> <prev_tip>`, where `prev_tip` is the `commit_sha` of the last `completed` node in `linearized_order` (or `origin/main` when none are completed). Sequential + topological order makes `prev_tip` transitively contain every declared dependency's commits, so `dependencies` govern **readiness and cascade-skip only**, never ancestry. The pushed result is a true linear stack and each PR's `parent_branch` is truthful by construction. No merges, no cherry-picks, no rebase — anywhere in the run.

## Tool-call discipline

Every action you describe must correspond to a real tool call in the same response. Never claim a reviewer "is being launched" without a `run_subagent` call in that response; confirm in past tense once the result lands. Do not ask permission to continue the loop — pick the next step and proceed.

## Todo scaffold

Canonical ids (reseed from `state.json` after compaction; do not invent new names):

- `setup` — Setup + Steps 0–2
- `preflight` — Step 2.5 gate preflight
- `implement-<pr.id>` / `review-<pr.id>` — Steps 4–5, per PR
- `assemble` — Step 8
- `lessons` / `final-report` — Steps 9–10 + report

**Reseed after compaction** from `<exec_plan_allow_root>/state.json` (`dag.nodes[].status`, `linearized_order`, `plan_id`, `design_doc_path`, `exec_plan_allow_root`). Do not advance until the todo list is back.

## Invocation

```
/execute-plan <design-doc-path> [--effort N] [--concurrency N] [--dry-run]
              [--resume <PLAN_ID>] [--instructions "..."] [--no-graphite] [--auto-pr]
```

| Flag | Default | Semantics |
| --- | --- | --- |
| `<design-doc-path>` | required | Path to a design doc containing `## PR Plan`. Not required with `--resume`. |
| `--effort N` | 1 | Parsed and reported; changes nothing (upstream's `2` is itself a no-op). |
| `--concurrency N` | 4 | **Forced to 1** — sequential execution; a note is printed. |
| `--dry-run` | off | Print order/levels/branches and exit **before** the plan-skeptic preflight. Never mints. |
| `--resume <PLAN_ID>` | off | Resume a prior run; takes precedence over every other flag. |
| `--instructions "..."` | none | Injected into every implementer context and reviewer spawn/re-review. |
| `--no-graphite` | off | Truthful no-op (plain-git is the only mode). |
| `--auto-pr` | off | After push, run `gh pr create --base <parent_branch> --head <branch> --fill --draft` per branch when `gh` + GitHub remote exist. |

## Setup [DEVIN]

1. If `--resume <PLAN_ID>` was passed, skip to **Step 7** now (resume issues its own root and loads state; nothing else in Setup runs first).
2. Parse remaining flags. Record `user_instructions`, `effort`, `auto_pr_flag`, `no_graphite_flag`. Print: `"Sequential execution (v1): --concurrency <N> parsed, forced to 1"` when `--concurrency` ≠ 1.
3. Run:

```
python3 ~/.config/devin/hooks/devin-gates.py allow-exec-plan
```

   Literal `~`, no `$HOME`, no `--file`. Do **not** `read`/`grep`/`ls` the gate script or `current_session` (protected). If it fails, retry the repo copy:

```
python3 <absolute dirname of this SKILL.md>/../../hooks/devin-gates.py allow-exec-plan
```

4. Parse `plan_id=` and `exec_plan_allow_root=` from stdout. **If both commands fail while writes are locked: print stderr and stop** — no `mkdir -p` fallback exists while locked (it is itself denied). Post-unlock or in gate-free plugin installs, the unenforced fallback applies: self-generate `plan_id` (8 lowercase hex), `exec mkdir -p ~/.cache/devin-skills/execute-plan/<PLAN_ID>`, set `exec_plan_allow_root` to it, and print `"unenforced run — no gate present"`.
5. `request_scope` the **parent dir** `~/.cache/devin-skills/execute-plan/` (covers the run root and prior runs' `lessons.md` for Step 0) and the design doc's directory if it is outside the workspace.
6. Initialize `<exec_plan_allow_root>/state.json` (schema at the end of this file): `v:1`, `plan_id`, `design_doc_path` (absolute), `status:"executing"`, `created_at`, `effort`, `max_concurrent:1`, `user_instructions`, `gh_available:null`, `graphite_available:false`, `auto_pr_flag`, `no_graphite_flag`, empty `linearized_order`/`dag.nodes`, `stack_assembly_started:false`, `stack_assembly_progress:[]`, `stack_pushed:false`, `pr_urls:[]`, `pr_create_commands:[]`, `issue_patterns:[]`, `total_issues_by_severity:{}`.

   `state.json` is unsigned scratch — like `/design`'s, it is not an enforcement primitive. Persist it **after every status transition**.

## Step 0 [DEVIN] — memory retrieval

Enumerate prior runs' `lessons.md` files under `~/.cache/devin-skills/execute-plan/` (excluding the current run's) with **`find_file_by_name`** — never `exec ls`/`find` (denied while locked). `read` each; distill the generalized patterns into a `past_issues_briefing` block:

```
## Past Issues to Avoid
1. <pattern> (seen N times)
...
```

Dedup by description; count occurrences across runs. No files, or any read failure → `past_issues_briefing = ""` and proceed. **Never fail the run on this step.** When non-empty, inject the block verbatim into the Step 4 implementer context and every Step 5 reviewer spawn/re-review.

## Step 0.5 [DEVIN] — deferred tool detection

`command -v` is not on the locked-exec allowlist. Set `gh_available = null` (Grok's null-until-probed convention); the probe runs at the Step 8 preflight, post-unlock. `graphite_available = false` always. Persist both in `state.json`.

## Step 1 [DEVIN] — self-parse + validate

1. Read `<design_doc_path>`. **Strip fenced code blocks first** (mirror the validator's preprocessing exactly — a `## PR Plan` example inside a code fence must not reach the self-parse, and the id cross-check must not diverge).
2. Locate `## PR Plan`; split at each `### PR N:` heading. For each entry extract:
   - `id` = `pr-<N>`; `title` = text after the colon.
   - `files` from `Files/components affected:` (comma-split, `**`-stripped).
   - `dependencies` from `Dependencies:` — `PR 1`, `PR 2` → `pr-1`, `pr-2`; `None`/`N/A`/`-` → `[]`. Support continuation lines like the validator does.
   - `description` from `Description:`.
   - `slug` from the title: lowercase → spaces/underscores → hyphens → strip non-`[a-z0-9-]` → collapse hyphens → strip leading/trailing hyphens → drop `.lock` suffix → truncate to 50 chars (trim trailing hyphens after truncation) → `"unnamed"` if empty.
3. Run the deterministic check **through the gate** (locked-allowlisted):

```
python3 ~/.config/devin/hooks/devin-gates.py exec-plan-validate --file <design_doc_path>
```

   (repo-copy fallback path per Setup). Exit `0` → parse stdout JSON. Exit `1` → print the JSON `errors` verbatim and **stop**. Exit `2` → **not a plan-validation verdict** (validator missing/timed out *or* the `--file` itself unreadable); fall back to direct invocation **only when writes are unlocked or no gate exists** (plugin install): `python3 <dirname of this SKILL.md>/scripts/validate-plan.py <design_doc_path>`. If both paths fail → print stderr and **stop** — never proceed on an unchecked hand-parse in a gated install. In a gate-free plugin install where neither is reachable, print a loud `unverified plan — validator unavailable` warning and continue on the self-parse alone.
4. Cross-check: self-parsed id set must equal the validator's `level_assignments` keys. Mismatch → self-parse bug → report and stop.
5. From the JSON take `levels`, `level_assignments`, `linearized_order`, `max_parallelism`. Fill `dag.nodes` (status `pending`) and `linearized_order`; persist state.

Report: `"Parsed PR Plan: <N> PRs found."`

## Step 2 — flags, branches, dry-run

- `--resume` precedence: `--resume --dry-run` takes the Step 7 item-4 early exit (read-only reconcile, no preflight, no normalize, no state write); `--resume --instructions` is ignored in favor of persisted `user_instructions` (print a warning line).
- Compute `branch = "execute-plan/<PLAN_ID>-pr-<n>-<slug>"` for each node; persist.
- **`--dry-run` exits here**: print linearized order, level assignments, branch names, `max_parallelism` — and stop. It never reaches Step 2.5, never spawns a sweep, never mints. Every operation it used is locked-allowlisted.

## Step 2.5 [DEVIN] — gate preflight

`python3 ~/.config/devin/hooks/devin-gates.py status`; look for `plan-skeptic: present|missing` — an absent or malformed token counts as `missing`.

- `present` → Step 3.
- `missing` → run **one** embedded `plan-skeptic` sweep over the full design doc using the `/skeptic-plan` task contract: `run_subagent(profile: "plan-skeptic", is_background: false)`, `resume` **omitted** (fresh spawn — spawnable while locked), full plan text inlined in `task`, no sample verdict line.
  - `GATES_VERDICT: PASS` → the gate mints `plan-passed` → **re-run `status` and confirm `plan-skeptic: present` before continuing.** If still `missing` after a PASS, report and stop — do not die mid-run on the first mutating exec.
  - `FAIL`/`BLOCKED` → report the findings verbatim and stop. `/skeptic-plan` owns the triage loop; this skill does not re-implement it.
- Gate-free plugin install: no `status` exists; skip preflight with a printed note.

## Step 3 [DEVIN] — preconditions + stack base

- `git status --porcelain` must be empty — the parent checks out branches in the shared tree, so a dirty-tree refusal replaces Grok's "never switch the orchestrator's branch" guardrail. Not clean → report and stop.
- `git fetch origin main`.
- Derive `prev_tip` = `commit_sha` of the last `completed` node in `linearized_order`; when none are `completed` (every fresh run), `prev_tip = git rev-parse origin/main`. **Never persisted** — always derived from reconciled statuses.

  The invariant that makes "last completed" correct: under Step 7's positional retry rule, every non-`pending` node before the first `pending` node is terminal (`completed`, or positionally-ineligible `failed`/`skipped`), and terminal-noncompleted nodes never advance `prev_tip`.

## Step 4 [DEVIN] — sequential implement

For each node in `linearized_order`, skipping `completed` and `failed` (a `completed` node's branch already points at its finished work — resetting it onto a successor's tip would corrupt the stack; `skipped` nodes are *reached*, not pre-skipped, for the lazy re-check in item 1):

1. **Lazy dependency re-check.** A `skipped` node is reached, not pre-skipped: if every declared dep is now `completed` **and** no later node in `linearized_order` is `completed`, set it `pending` and execute it this iteration — a dependent of a PR retried to completion earlier in this same resume run proceeds without a second `--resume`. If any dep is `failed`/`skipped` (or a later node is `completed`), it stays `skipped`; continue.
2. `git checkout -B <pr.branch> <prev_tip>`; `pr.base_sha = <prev_tip>`; `status = implementing`, `started_at` set; persist. `-B` (create-or-reset) is deliberate: the node is `pending`, so its branch either does not exist or holds stale mid-flight work a resume deliberately discarded.
3. **Implement inline** under the embedded implementer discipline (Grok's `implementer.md` as orchestrator rules):
   - Smallest change that satisfies the PR's scope; follow existing patterns.
   - Concise WHY-comments only; no unrequested features; no scope creep beyond `pr.files`/`pr.description`.
   - Inject `past_issues_briefing` and `user_instructions` into your working context.
   - Run the repo's formatter/linter/typecheck and a compile check (`python -m py_compile`, `tsc --noEmit`, `cargo check`, etc. as appropriate) via `exec` before committing.
4. `git status --porcelain` empty after implementation (nothing to commit) → **empty-diff is not a failure**: write `summary-<pr.id>.md` noting `empty`, set `commit_sha = <prev_tip>`, `status = completed`, `completed_at`; persist; `prev_tip` stays `<prev_tip>`; continue.
5. Otherwise `git add -A` + `git commit -m "<title>\n\n<description>"` — on a git lock error, wait 2s and retry, up to 3 attempts.
6. Write `summary-<pr.id>.md` under the run root (files changed, key decisions, deviations). `commit_sha = git rev-parse HEAD`; `status = reviewing`; persist.
7. Report: `"Implemented <pr.id> (<title>) — <commit_sha>. Starting review..."` → Step 5.

**Failure path — restore a clean tree before continuing:** on implementation/verify/commit failure, `git reset --hard <pr.base_sha>` then `git clean -fd` (removes untracked files the failed attempt created — partial work must never leak into the next PR's diff; the failure and context live in `state.json`/`summary-<pr.id>.md`), then `git checkout main`, mark `failed` with the error, persist, → Step 6 cascade-skip, continue the loop.

## Step 5 [DEVIN] — review + fix loop (per PR)

1. Derive 2–3 `reviewer_focus_areas` from the impl summary.
2. `git diff <pr.base_sha>..<pr.commit_sha>` — the candidate under review.
3. Spawn a **fresh** `pr-reviewer`: `run_subagent(profile: "pr-reviewer", is_background: false)`, no `resume`. Task:

```
Review this PR's diff. The workspace is checked out on branch <pr.branch>;
verify findings against the real files with read/grep — do not take the diff
at face value. You are read-only: do not call write, do not emit GATES_VERDICT.

## PR
Title: <pr.title>
Description: <pr.description>

## Focus areas
<reviewer_focus_areas>

## Diff (git diff <base_sha>..<commit_sha>)
<full diff>

## Implementation summary
<summary-<pr.id>.md contents>

<if past_issues_briefing non-empty, include verbatim + "Be proactive about avoiding these patterns.">
<if user_instructions non-empty: "## User Instructions\n<user_instructions>\nThese instructions apply to all work in this plan. Follow them strictly.">

End with one outer-fenced (four or more backticks) complete review-notes file
per your profile's schema. Unfenced: the count of Status: open issues.
```

   Save the returned `agent_id` as `pr.reviewer_subagent_id`; persist. If the spawn itself is refused/fails → treat as PR failure → Step 6 cascade-skip (a post-unlock profile allowlist surfacing here must not wedge the run).
4. Copy the reviewer's fenced review → `review-<pr.id>.md`. Count `Status: open`; tally into `total_issues_by_severity`.
   - **0 open** → `status = completed`, `completed_at` set; `prev_tip = pr.commit_sha`; persist; next node.
   - **>0 open** → fix them yourself (same implementer discipline; `wontfix` is legitimate **with a technical defense**). Edit `review-<pr.id>.md`'s `Status`/`Response` fields yourself (the file is yours, under the allow-root). Commit `fix: address review feedback for <title>`; re-capture `commit_sha`; `review_rounds += 1`; persist.
5. `resume` the reviewer (`run_subagent(profile: "pr-reviewer", resume: <reviewer_subagent_id>)`) — the task pastes the **updated review file** (with your `Status`/`Response`/`wontfix` justifications) **plus** the fresh `git diff <base_sha>..<new commit_sha>`. The child cannot read the allow-root; a reviewer that never sees your justifications would re-open every `wontfix` into a spurious stalemate. `resume` failure → fresh `pr-reviewer` spawn with the same pasted review + diff ("disk is the memory").
6. **No round cap.** Stalemate — a `wontfix` issue (match on file + description) re-opened by the reviewer — escalate to the user with both sides; the decision is final (`Status: addressed`). After ~3 fix rounds on one PR, post a soft non-blocking check-in: `"<pr.id> review round <n>: still converging; interject to stop."`

## Step 6 — cascade-skip

On a `failed` node: walk declared `dependencies` edges transitively; every still-`pending` dependent → `skipped` (record error `"skipped: depends on failed <id>"`); persist. Independent later PRs continue — a `failed`/`skipped` node never advances `prev_tip`, so they stack on the last success and the pushed chain still reads correctly. Report skipped ids.

## Step 7 [DEVIN] — resume

`--resume <PLAN_ID>` runs this ordered sequence — **pinned: dry-run early-exit → preflight → normalize → reconcile → Step 3**:

1. `python3 <gate> allow-exec-plan --id <PLAN_ID>` (re-issues the session-scoped root — `exist_ok`; one root pair per session, last `allow-exec-plan` wins). `request_scope` the cache parent dir.
2. Load `<exec_plan_allow_root>/state.json`. **Missing or unparseable → report and stop** — there is nothing to reconcile without it. **Empty `dag.nodes` → report `"no PRs recorded under <PLAN_ID>"` and stop** — a run that wrote `state.json` at Setup and stopped before Step 1 persisted the DAG (validation failure, unreadable doc, interruption) leaves a resumable id that must not silently "complete" an empty run; start a fresh run instead.
3. `request_scope` `dirname(<design_doc_path>)` if outside the workspace; refusal → report and stop. **Re-read `<design_doc_path>`** — restores the prose context implementation and reviewer tasks lean on.
4. **`--resume --dry-run` exits here — truly read-only.** Run the reconcile pass **in memory only** (item 7's rules; the `git cat-file -t` commit checks may be exec-denied while locked — if so, reconcile statuses without commit verification and print that degrade), print the surviving order/levels/branches, and exit. No preflight sweep, no normalization, no mid-assembly cleanup, no `state.json` write — it never mints and never touches the tree.
5. Re-run **Step 2.5 preflight** — a new session needs `plan-passed` before any mutating exec; pinned here because normalization (next item) is the first mutating step.
6. **Normalize the tree** (scoped, not blanket): capture `git branch --show-current` + `git status --porcelain`; only if HEAD is on an `execute-plan/*` branch **or** the tree is dirty, run `git checkout -f main` → `git reset --hard` → `git clean -fd`, and report exactly what was discarded. A clean tree on `main` skips normalization entirely.
7. **Reconcile statuses only — never juggle branch refs** (`reset --hard` does not remove untracked files; `checkout <branch>` can fail on collision; `branch -D` refuses while HEAD is on it; Step 4's `checkout -B` recreates refs unconditionally — ref state is non-load-bearing). Per node, in `linearized_order`, under the **positional retry rule** — a node may return to `pending` only if **no later node in `linearized_order` is `completed`**:
   - `completed` → `git cat-file -t <commit_sha>` must print `commit`. Absent: if no later node is `completed` → reset to `pending`, clear `commit_sha`/`completed_at`/`base_sha`/`review_rounds`/`reviewer_subagent_id`/`error`. If a later node **is** `completed` → mark `failed` with `"commit <sha> absent; ineligible for in-place retry — later nodes completed"` and cascade-skip its still-`pending` dependents.
   - `implementing`/`reviewing`/`failed`/`branch_created` → **eligible** (no later `completed`): `pending`; clear `error`/`commit_sha`/`base_sha`/`review_rounds`/`reviewer_subagent_id`/`started_at`. (`branch_created` is a legacy status no current version emits — reconcile treats it like `implementing` so it cannot bypass positional eligibility.) **Ineligible** (a later node is `completed`): marked `failed` — leave branch ref and error, and report: `"pr-N cannot be retried in place — later node(s) completed on a stack that no longer includes it; retrying would require rebuilding completed successors. Re-run those PRs manually or start a fresh plan run."`
   - `skipped` → leave `skipped`; un-skip is decided lazily in Step 4 (a dep being retried this run is `pending` at reconcile time, not `completed` — an eager check would force a second `--resume`; lazy re-check sees the retried dep's final outcome).
   - `pending` → unchanged.
8. Mid-assembly crash (`stack_assembly_started && !stack_pushed`): `git cherry-pick/merge/rebase --abort` tolerantly, `git checkout -f main`, `git reset --hard origin/main`, clear `stack_assembly_started`/`stack_assembly_progress`. **Never delete `execute-plan/<PLAN_ID>-*` branches.**
9. Continue at **Step 3** — `prev_tip` derives from the reconciled statuses.

## Step 8a [DEVIN] — plain-git assembly

Preflight (now post-unlock): `command -v gh` → set `gh_available`; persist. Then:

1. `git status --porcelain` must be empty → `git fetch origin main` → `git checkout main` → `git reset --hard origin/main`. Commits already live on `<pr.branch>` in linear stack order — **no cherry-pick exists**.
2. Per PR in `linearized_order` (skip `failed`/`skipped`):
   - **Ancestry check:** `git merge-base --is-ancestor <pr.base_sha> <pr.commit_sha>`; failure (externally-broken ancestry) → treat as push failure: `failed` + cascade-skip, `parent_branch` frozen.
   - **Ref check:** `git rev-parse <pr.branch>` must equal `<pr.commit_sha>` — merge-base verifies the object, not the ref; a deleted ref fails late at push and a drifted ref would push un-reviewed content. Mismatch → `git branch -f <pr.branch> <commit_sha>` if merely stale/missing, else `failed` + cascade-skip.
   - `git push --force-with-lease origin <pr.branch>` — **never `--force`**. Failure → retry once after 5s → `failed` + cascade-skip; `parent_branch` frozen.
   - `parent_branch` (init `main`) advances to `<pr.branch>` only on success — it is the PR's actual parent by construction.
   - Record progress in `stack_assembly_started`/`stack_assembly_progress`; persist.

## Step 8b — URLs and optional PR creation

Parse `git remote get-url origin` with `^(?:git@|ssh://(?:[^@]+@)?|https?://(?:[^@/]+@)?)([^:/]+)[:/](?:[^/]+/)*([^/]+)/([^/]+?)(?:\.git)?/?$`; host must match `github.com` or `github.*` (GHE). **Strip credentials before any logging or storing.**

- GitHub host + `--auto-pr` + `gh_available` → sequential `gh pr create --base <parent_branch> --head <pr.branch> --fill --draft`; per-branch failure → `kind:"compare"` + `note`, continue. Record each PR URL as `{branch, url, kind:"pr"}`.
- Otherwise → `kind:"compare"` entries `https://<host>/<owner>/<repo>/compare/<parent_branch>...<pr.branch>` plus the equivalent `gh pr create` text in `pr_create_commands`.
- Non-GitHub remote → `kind:"pushed-only"` + note.

Set `stack_pushed = true`; persist.

## Step 9 [DEVIN] — cleanup

No worktrees to tear down; no subagents to kill. Post-unlock, delete `summary-*`/`review-*` files under the run root if convenient (`rm` is legal unlocked; leaving them is also fine). Keep `state.json` and `lessons.md` — resume and Step 0 retrieval depend on them.

## Step 10 [DEVIN] — lessons

Accumulate `issue_patterns` during review rounds; generalize per Grok's Step 10a rules (strip task-specific names; reusable principles; reuse existing categories). Write `lessons.md` under the run root — this is what Step 0 scans on later runs. Point the user at `~/git/agent-skills` for durable merge. **Never fail the run on this step.**

## Final report

1. Per-PR outcome: `completed <commit_sha>` / `failed <error>` / `skipped <reason>` / `failed-ineligible` report lines.
2. Stack: branches in push order with `parent_branch` chain; `pr_urls` (or compare URLs / `gh` commands); skipped pushes.
3. `total_issues_by_severity` and review rounds per PR.
4. Run root path (`state.json`, `lessons.md`).
5. Remaining limitations hit (sequential, ineligible retries, non-GitHub remote, etc.).
6. **Next step for the user:** the Stop-lock still applies — tell them `/skeptic-review` covers the stack before claiming done.

## Guardrails

- Never `git push --force` — only `--force-with-lease`.
- Never commit to or push `main`; `main` is only ever `checkout`/`reset --hard origin/main`.
- The orchestrator owns **all** git ops and implementation; reviewer independence is the check.
- Stack ops strictly sequential.
- Persist `state.json` after every transition.
- Never merge PRs.
- `git checkout` only onto `execute-plan/*` branches or back to `main`.
- A failed PR's partial work is **always** discarded (`reset --hard` + `clean -fd`) before the next iteration.
- Positional retry on resume: never reset a node to `pending` when a later-positioned node is `completed`; report ineligible nodes instead.
- No merge/cherry-pick/rebase conflict-resolution path exists in v1 — implementation-time conflicts are the implementer's to fix on the branch.
- Foreground reviewer only (`is_background: false`); fresh spawn first round, `resume` for re-review, fresh-spawn fallback on resume failure.

## State file schema

`<exec_plan_allow_root>/state.json` — unsigned scratch:

```json
{
  "v": 1,
  "plan_id": "a1b2c3d4",
  "design_doc_path": "/abs/design-doc.md",
  "status": "executing",
  "created_at": "<ISO8601>",
  "effort": 1,
  "max_concurrent": 1,
  "user_instructions": "",
  "gh_available": null,
  "graphite_available": false,
  "auto_pr_flag": false,
  "no_graphite_flag": false,
  "linearized_order": ["pr-1", "pr-3", "pr-2"],
  "dag": { "nodes": [
    { "id": "pr-1", "number": "1", "title": "...", "slug": "...",
      "description": "...", "files": ["..."], "dependencies": [],
      "level": 0, "status": "pending",
      "branch": "execute-plan/a1b2c3d4-pr-1-...",
      "base_sha": null, "commit_sha": null,
      "error": null, "reviewer_subagent_id": null,
      "review_rounds": 0, "started_at": null, "completed_at": null }
  ]},
  "stack_assembly_started": false,
  "stack_assembly_progress": [],
  "stack_pushed": false,
  "pr_urls": [],
  "pr_create_commands": [],
  "issue_patterns": [],
  "total_issues_by_severity": {}
}
```

Statuses: `pending → implementing → reviewing → completed | failed | skipped` (`branch_created` is retained in the enum for resume-compat with older state files but is no longer emitted). `prev_tip` is deliberately absent — derived from `commit_sha` of completed nodes in `linearized_order`.

## In-progress reporting

- After parse: `"Parsed PR Plan: <N> PRs found."`
- Sequential note: `"Sequential execution (v1): --concurrency <N> parsed, forced to 1"`
- Per PR: `"Implementing <pr.id> (<title>)..."` → `"Implemented <pr.id> — <sha>. Starting review..."` → `"<pr.id> review round <n>: <N> issues..."` → `"<pr.id> completed."`
- Cascade: `"<pr.id> failed: <error>. Skipping dependents: <ids>"`
- Assembly: `"Pushing stack: <b1> → <b2> → ..."`; per-branch push result.
- Resume ineligibility: the Step 7 report line verbatim.

# How it works

Plain-English internals for people who want to know what they are installing. Everyday use is in [usage.md](usage.md).

## The one-sentence version

Skills tell Devin *what* to do. The gate hook is the only layer that can *stop* it.

## Layers

| Layer | Role | Can block writes? |
| --- | --- | --- |
| Devin builtin `/plan` | Host read-only draft, approval UI, `exit_plan_mode`. Not our code. | No |
| User/project hooks (`devin-gates.py`) | HMAC markers, write-lock, Stop-lock, audited bypass. | **Yes** |
| Custom subagents | Read-only personas (`design-writer`, `design-reviewer`, `plan-skeptic`, `finding-skeptic`, `code-skeptic`, `goal-verifier`), `model: swe-2-high`. | No |
| Skills | Orchestrators and slash commands. Prompts only. | No |
| Tiny `AGENTS.md` | Pointers, not playbooks. | No |
| Optional `plugin/` | Skills + agents + the tiny rule, for sharing. **No `hooks.json`.** | No — plugin hooks fail-open |

Plugin packaging must not carry the lock. If a plugin hook fails to load, Devin continues without it.

## Why markers are signed

A naive lock would look for a file named `plan-passed`. Devin could write that file itself.

The gate:

- Holds a secret in the state directory (mode `0600`), generated at install, not writable by the agent.
- Mints markers itself, HMAC-signed, only when a `plan-skeptic` / `code-skeptic` subagent result contains `GATES_VERDICT: PASS` (with extra guards for code: `plan-passed` present, `source_seq` matching).
- Refuses forged files.

`/gate-bypass <reason>` is also HMAC-audited and scoped to the current session id.

## Write-lock vs Stop-lock

**Write-lock.** After `/plan` is approved — and by default in any normal session — workspace writes stay blocked until a plan-skeptic marker exists.

**Stop-lock.** "I'm done" stays blocked until a code-skeptic marker exists, unless:

- `mode == plan` (from `write_plan`, cleared on `exit_plan_mode` — **not** parsed from prompt text)
- `source_seq == 0` (no successful source mutations this session)
- an audited `/gate-bypass`
- `DEVIN_GATES_OFF=1` in the shell that starts `devin`
- this `prompt_id` has already been Stop-blocked 3 times (loop guard)

An attached `active` **goal blocks Stop ahead of the `mode == plan`, `source_seq == 0`, and `code-passed` early-allows** — a zero-mutation research goal still gates, even in plan mode — while honoring `gates_off`, an audited override, and the loop guard. Paused, blocked, cleared, and completed goals impose nothing; ordinary rules apply. Goal and code blocks share the same 3-per-`prompt_id` budget.

The loop guard exists because Devin can retry a blocked Stop forever. After 3, the turn is allowed to end. That residual is **High** for the Stop product claim. The honest skip for real work is `/gate-bypass`, not Stop-retry.

`source_seq` increments on source-mutating tools (`write` / `edit` / `apply_patch` / …). A later edit after a code PASS clears the marker so you cannot remint from a stale review.

## Grok → Devin mapping

This repo is an approximation of a Grok Build workflow on Devin CLI (verified on **3000.10.21**). Honest gaps included.

| Grok | Devin approximation | Honest gap |
| --- | --- | --- |
| `/plan` harness: read-only except plan file; approval UI | **Keep built-in `/plan`.** Hooks take over **after** approval until a skeptic marker exists. | No `/view-plan` skill. Devin's plan file lives under `~/.devin/plans/`. We do not replace the approval UI. |
| `/design` writer/reviewer/`resume_from` | Orchestrator skill + read-only `design-writer` / `design-reviewer`. Parent copies fenced markdown onto `design_allow_root`. `resume` for revise / re-review. | Parent sees a distilled result, not the raw transcript. v1 does **not** assume child `write` re-enters hooks. |
| `/goal` (host rounds, pause/resume/clear, token budget, independent evidence review) | **Gate-backed `/goal`.** Workspace-scoped signed goal state; `pause`/`resume`/`clear`/`update` CLI; Stop blocked while an attached goal is `active`; completion minted **only** by a fresh `goal-verifier` `GATES_VERDICT: PASS`; post-completion mutations reopen the goal. | No host round driver — "rounds" are turn structure. No token budget. `goal-pause` lands at the next hook boundary, not mid-turn. The agent can still `goal-clear` itself (audited, like `/gate-bypass`). Stop-loop guard unchanged. |
| Plan skeptic before implement (3-sweep cap, BLOCKED, failed-sweep autopsy) | `/skeptic-plan` + `plan-skeptic` + **write-lock hook**. Fresh subagent per sweep. Lock lifts only on `GATES_VERDICT: PASS`. | Hook cannot run the skeptic (timeouts). A parent that jailbreaks the skeptic via the task prompt can still produce PASS. Residual, documented. |
| Code skeptic at done (finding-skeptic + implementation sweep) | `/skeptic-review` + `finding-skeptic` + `code-skeptic` + **Stop hook**. Auto-mint requires a `code-skeptic` PASS whose witness `source_seq` is ≥ current `source_seq`. | Same residual. Stop-loop guard is required. Parent must embed the **full** `git diff` in the task; large diffs may truncate. |
| Independent skeptic (fresh, no attachment) | Fresh Devin subagent per sweep; parent must not self-review. Profiles omit write/edit/`exec`. | Parent still writes the task prompt (jailbreak residual) **and** the pasted diff. |

## What we did not copy

**Do not ship a prompt-only `/goal` imitation.** Grok `/goal` is host logic, not a prompt: token budget, pause/resume/clear, autonomous multi-round driver, and an independent evidence review that can **refuse** completion. A skill named `/goal` that only *says* "keep going" would let the parent mark itself complete — it would *look* like Grok `/goal` and fail silently. That is still forbidden.

What ships instead is the gate-backed approximation: the durable parts of the harness — signed state, the completion gate, the independent refusal — live in the gate, the only layer that can enforce them.

- **`/goal <objective>`** registers a workspace-scoped objective in an HMAC-signed goal file and attaches the session.
- **`goal-update`** is the audited progress-reporting analog (`--message`, `--claim-done`, `--blocked --reason`).
- **`goal-pause` / `goal-resume` / `goal-clear`** transition status; reasons are required and audited, like `/gate-bypass`.
- **Completion is a gate state transition, not a self-report.** Only a fresh `goal-verifier` subagent emitting `GATES_VERDICT: PASS` moves `status=complete` — the verifier judges a parent-maintained `checklist.md` plus an evidence bundle, per item (`VERIFIED` / `REFUTED` / `UNVERIFIABLE — needs <evidence>`).
- **A workspace mutation counter (`mutation_seq`) binds the witness to the tree it verified.** Any later source mutation — in any session — reopens the goal to `active`, and Stop re-blocks because the attach survives completion.

What Devin CLI 3000.10.21 still does not give us, and what `/goal` honestly does not do:

- **Host-driven observe–plan–act rounds.** Nothing advances the goal between turns; the Stop-block only keeps an in-flight turn alive.
- **A token budget.** `verifier_sweeps` and update counts are the only "rounds" metric.
- **Mid-turn pause.** `goal-pause` takes effect at the next hook boundary.
- **An un-escapable guarantee.** The parent can `goal-pause`/`goal-clear`/`--blocked` itself — mitigated by mandatory `--reason` and the audit trail, identical to `/gate-bypass`. The 3-per-`prompt_id` Stop-loop guard can also end a turn unverified (**High** residual, unchanged).

## Honest limits

- **Parent jailbreak.** The parent writes the skeptic `task`. Instructing PASS can still mint. The lock verified that the profile emitted PASS, not that the plan/diff is independently good.
- **Stop-loop cap of 3** per `prompt_id` is required (Devin can loop on blocking Stop) and is a **High** residual for the Stop claim.
- **Large diffs** may truncate in the pasted `task`.
- **`apply_patch` schema** on 3000.10.21 is not fully known: fail closed while locked; after unlock, fail open except a protected-path scan.
- **Custom subagents** are experimental; pin Devin 3000.10.21 in mind when the format moves.
- **Builtin `/bypass` / `/yolo` / `/dangerous`** are Devin permission mode, not a gate override. If a future Devin build skips PreToolUse in permission-bypass, the lock dies. Treat that as "permission-bypass disables the lock." See [smoke-test.md](smoke-test.md) §8.

## Design-time writes

`/design` needs somewhere to put artifacts while the workspace is still locked. The gate's `allow-design` subcommand issues a `design_allow_root` (usually under `~/.cache/devin-skills/design/<id>/`). Parent writes succeed there. Workspace source stays locked. Do not symlink out of that root.

The loop itself:

```mermaid
flowchart LR
  W[design-writer] -->|fenced markdown| P[Parent copies to disk]
  RV[design-reviewer] -->|fenced review| P
  P -->|paste files into task| W
  P -->|paste files into task| RV
```

Writer and reviewer have no `write` / `edit` / `exec`. That is why the parent copies fences. Child `write` is not assumed to re-enter hooks. Full loop: [usage.md](usage.md#design-docs).

### The PR Plan is not an executor

`design-writer` must emit `## PR Plan` with `### PR N:` slices (files, dependencies, description) "so a later execute path can parse it." `/design` Step 6 **presents** that plan. Nothing in this repo **runs** it.

The execute path is:

- Devin builtin `/plan` (one slice, or the whole spec if it is small)
- `/skeptic-plan` + write-lock
- implement
- `/skeptic-review` + Stop-lock
- next PR

A skill that loops those PRs until the spec is fully landed would be a host-driven `/goal` harness — `/goal` ships the gate-backed single-objective version instead (see the Grok mapping above), not a PR walker. See [From design to implementation](usage.md#from-design-to-implementation).

---
name: goal
description: Run a gate-enforced workspace objective — durable state, audited progress, and a fresh goal-verifier PASS as the only path to done. Use for /goal, long objectives, "keep going until X works", or multi-turn tasks with a verifiable completion condition.
argument-hint: "<objective> | status | pause | resume | clear"
triggers:
  - user
  - model
---

# Goal Skill

You are an **inline** orchestrator (this skill omits `subagent` and `allowed-tools` so you keep `run_subagent`). You run a **work → claim → verify** loop against one workspace objective until a fresh `goal-verifier` subagent emits `GATES_VERDICT: PASS` — the **only** path to `status=complete`. You cannot mark the goal done yourself; `goal-update --claim-done` records a claim, it never completes.

You coordinate the workflow; the **gate enforces it**. Goal state lives in an HMAC-signed file the gate owns (`$STATE_DIR/goals/<ws-hash>/<id>.json`). While your session is attached to an `active` goal, the Stop hook **blocks your turn from ending** — keep working, verify, or explicitly `goal-pause`/`goal-clear`/`goal-update --blocked` with a reason.

## Honest gaps (Devin ≠ Grok)

- There is **no host-owned round driver**. Grok's `/goal` runs observe–plan–act rounds in the host; here "rounds" are just your turn structure. The Stop-block keeps an in-flight turn alive and `UserPromptSubmit` reminders nudge each new prompt — nothing advances the goal while the user is away.
- There is **no token budget equivalent**. `verifier_sweeps` and update counts are the only "rounds" metric.
- **Mid-turn pause does not exist.** `goal-pause` takes effect at the next hook boundary; it cannot interrupt a running tool call.
- The **Stop-loop guard is a real residual**: after 3 Stop blocks per `prompt_id` the turn is allowed to end unverified (audited `stop_loop_guard_fired`). The honest exit is `goal-pause`/`goal-clear`/`--blocked` with a reason — not Stop-retry.
- You **can** run `goal-pause`/`goal-clear`/`goal-update --blocked` yourself — same audited-explicit-escape model as `/gate-bypass`. `--reason` is mandatory; every transition lands in `audit.jsonl`.
- There is **no** Grok `spawn_subagent` host object. Use Devin `run_subagent` (`title`, `task`, `profile`, `is_background: false`). A verifier spawned with `resume` set is **not** a witness — fresh spawn every time.

## Invocation

```
/goal <objective text>     → set up a new goal and start working it
/goal status               → goal-status (any session; no attach needed)
/goal pause  --reason ...  → goal-pause --reason "..."
/goal resume               → goal-resume (attaches this session to the workspace goal)
/goal clear  --reason ...  → goal-clear --reason "..."
```

`status|pause|resume|clear` map to gate CLI calls below. Anything else is an objective → Setup.

## Gate CLI contract

All commands run while write-locked (goal subcommands are exec-allowlisted). Use a literal `~` (not `$HOME` — locked `exec` treats `$` as a metachar). If the installed path fails, retry the repo copy `hooks/devin-gates.py`.

```
python3 ~/.config/devin/hooks/devin-gates.py set-goal --objective "<text>" [--id <8 lowercase hex>]
python3 ~/.config/devin/hooks/devin-gates.py goal-update --message "<text>"
python3 ~/.config/devin/hooks/devin-gates.py goal-update --claim-done
python3 ~/.config/devin/hooks/devin-gates.py goal-update --blocked --reason "<text>"
python3 ~/.config/devin/hooks/devin-gates.py goal-pause --reason "<text>"
python3 ~/.config/devin/hooks/devin-gates.py goal-resume
python3 ~/.config/devin/hooks/devin-gates.py goal-clear --reason "<text>"
python3 ~/.config/devin/hooks/devin-gates.py goal-status
```

- `set-goal` prints `goal_id=`, `goal_allow_root=`, `status=active`. It **refuses** if the workspace has any non-`cleared` goal — `goal-clear` the old one first (a `complete` goal still occupies the slot; a later mutation can reopen it).
- `goal-resume` attaches the calling session and prints `goal_id=`, `goal_allow_root=`, `objective=` — this is how a new session adopts the goal. Refused on `complete`/`cleared`.
- `pause`, `--blocked`, and `clear` require `--reason` (audited, like `bypass --reason`).
- `goal-update` takes exactly one of `--message` / `--claim-done` / `--blocked --reason`.

## Setup (new objective)

1. Run `set-goal` (no `--file`, no `$HOME`, no `$STATE_DIR`; do not `read`/`grep` the gate or `current_session` — protected). Parse `goal_id=` and `goal_allow_root=`. On failure, retry the repo `hooks/devin-gates.py` path. If both fail, print stderr and **stop**.
2. `request_scope` the printed `goal_allow_root` (e.g. `~/.cache/devin-skills/goal/<id>/`). It is outside the workspace — without this, parent `write`/`read` is refused as out-of-workspace and the loop dies while still locked.
3. Write `<goal_allow_root>/checklist.md`: the per-item completion contract for the verifier — concrete, checkable, covering the whole objective. The verifier judges items, not vibes.
4. Write scratch `<goal_allow_root>/state.json`:

```json
{"goal_id": "<id>", "goal_allow_root": "<root>", "checklist_file": "<root>/checklist.md", "evidence_dir": "<root>/evidence", "verifier_sweeps_seen": 0, "claim_done": false}
```

5. Writes under `goal_allow_root` are allowlisted for the attached session (realpath containment, symlinks rejected). They are **outside the workspace** — they do not bump the goal's `mutation_seq` and cannot reopen it. Workspace source stays under the ordinary write-lock rules.

## Work rounds (turn structure, not host rounds)

Per round:

1. Do the work. Implementation goals still need `plan-passed` before writes and `code-passed` before claiming done — `/goal` **composes** with `/skeptic-plan` and `/skeptic-review`; it does not bypass them. A zero-mutation goal (research, endpoint liveness, docs) needs no markers.
2. `goal-update --message "<what changed, what remains>"` — the audited `update_goal` analog.
3. Update `checklist.md` and drop evidence under `<goal_allow_root>/evidence/` — test output, `git diff` text, endpoint transcripts, file excerpts. The verifier cannot run commands; the bundle **is** its ability to check.

## Claim and verify

When the checklist looks done:

1. `goal-update --claim-done` — records `kind=claim`. Still `active`; nothing is minted.
2. Spawn a **fresh** verifier — `resume` **unset**, `is_background: false`:

```
title: "[verifier] Verify goal <id>"
profile: "goal-verifier"
task: |
  Verify completion of goal <goal_id>.

  Objective: <full objective text>

  Checklist:
  <entire checklist.md contents>

  Evidence bundle:
  <test output, diffs, transcripts, file excerpts — everything the items need>

  Emit per-item verdicts (VERIFIED / REFUTED / UNVERIFIABLE — needs <evidence>),
  then exactly one unfenced last line GATES_VERDICT: PASS|FAIL|BLOCKED.
```

The `goal_id` string **must appear in `task`** — the gate binds the witness to it; a task missing it is ignored as a late result.

3. On the result:

- **PASS** → the gate mints `status=complete` (witness bound to `mutation_seq`). Report done. **Stay attached** — a later workspace mutation reopens the goal and re-blocks Stop.
- **FAIL / BLOCKED / missing verdict** → sweep counted, goal stays `active`, Stop still blocks. Read the per-item verdicts, gather the named evidence (or fix the refuted item), work another round, claim again, spawn a **fresh** verifier.
- **Third FAIL** → `goal-update --blocked --reason "<autopsy: what was claimed, what the verifier refuted, what evidence never materialized>"` and report BLOCKED to the user; suggest `/goal resume` or `/goal clear`.

## Pause / resume / clear

- **Pause:** `goal-pause --reason "..."` — releases the Stop block at the next boundary; goal stays on disk.
- **Resume:** `goal-resume` — attaches this session, prints `goal_allow_root`/`objective`. Then `request_scope` the root, `read` `checklist.md` and scratch `state.json`, and continue work rounds.
- **Clear:** `goal-clear --reason "..."` — tombstones the goal (`cleared`) and detaches this session. Late verifier results for a cleared goal are ignored (audited `goal_verifier_late_result`).

## Reseed after compaction

`PostCompaction` emits a one-liner with `goal_id`, status, checklist path, and sweep count (if attached). On any uncertainty: `goal-status`, then `request_scope` the root and `read` `checklist.md` + `state.json`. A **new session** in the same workspace is *not* attached — `SessionStart` reminds it; `/goal resume` attaches.

## Rules

- **The gate owns completion.** Never claim the goal is done on your own say-so; only a fresh-verifier `GATES_VERDICT: PASS` mints `complete`.
- **`--claim-done` before every verifier spawn.** The claim entry is the audited marker the verifier judges.
- **Fresh spawn every verification.** Never pass `resume` to `goal-verifier`; a resumed verifier is not a witness.
- **`goal_id` in the task.** Missing → late result, no sweep, no witness.
- **Compose, don't bypass.** Implementation goals still need `plan-passed` + `code-passed`; run `/skeptic-review` on the frozen diff before the verifier when the goal has a diff.
- **Three FAILs → `--blocked` with an autopsy**, not endless retries.
- **Escapes are audited, not forbidden.** Prefer `goal-pause`/`goal-clear`/`--blocked` with an honest reason over Stop-retry or jailbreaking the verifier task.
- **Don't fabricate evidence.** The verifier cross-examines the bundle against the tree; fabricated evidence is the documented parent-jailbreak residual, not a strategy.

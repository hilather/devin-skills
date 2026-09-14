# Live Devin smoke test

Optional checklist on a **throwaway repo**. Unit tests are the merge gate:

```sh
python3 -m unittest tests.test_install_merge tests.test_gate -v
```

This checklist confirms the lock against a real Devin session. Use a throwaway git repo, not this one, so a failed lock cannot rewrite `devin-gates.py`.

Default is locked. Builtin `/plan` stays the planner. Do not create a skill named `plan`. `/goal` ships only as the gate-backed version — never a prompt-only imitation.

## 0. Install

- [ ] `sh install.sh` from this repo (or `--prefix` / `--src` as in [install.md](install.md)).
- [ ] In Devin, `/hooks` lists **both** `herdr-agent-state.sh` (if you had it) and `devin-gates.py`.
- [ ] herdr command strings look unchanged. Gate is one dispatcher per event, herdr first.

## 1. Write blocked (no `/plan`)

New session. Ask Devin to edit a file (typo, README, anything) **without** `/plan` or `/skeptic-plan`.

- [ ] `write` / `edit` is **blocked** (`Write blocked: no plan-skeptic marker…`).
- [ ] `run_subagent` with `profile=subagent_general` is **blocked**.
- [ ] `mcp__github__*` / `mcp_call_tool` is **blocked** while locked.

## 2. `/plan` — still locked after approval

- [ ] `/plan` a tiny change. `write_plan` works (builtin plan file under `~/.devin/plans/`).
- [ ] Approve `exit_plan_mode`.
- [ ] Workspace write is **still blocked**. Approval is not a skeptic marker.

## 3. `/skeptic-plan` — then implement

- [ ] `/skeptic-plan`. Parent spawns **fresh** `plan-skeptic` (`resume` unset).
- [ ] On `GATES_VERDICT: PASS`, writes become allowed. `/gate-status` shows `plan-skeptic: present`.
- [ ] **Dump** the live `PostToolUse` payload for `run_subagent` (redact secrets). This is the Phase 1 auto-mint spike. If `tool_response.output` is a stub, note that auto-mint may need `read_subagent` instead.
- [ ] Confirm a **blocked** `subagent_general` spawn never ran child tool calls. Child re-entry, if it exists, is defense in depth — the spawn block is the lock.

Implement the tiny change.

## 4. Stop blocked — then `/skeptic-review`

- [ ] Stop / "I'm done" **without** `/skeptic-review` → **blocked** (`Stop blocked: no code-skeptic marker…`).
- [ ] On the **same** turn, Stop 1–3 still block (`block N/3`); Stop 4 is **allowed** (loop guard). Observe this, then do not rely on it — `/gate-bypass` is the honest skip.
- [ ] Fresh session (or after `/skeptic-plan` + implement): `/skeptic-review` on the frozen candidate → `code-passed` → Stop **allowed**.

## 5. Bypass and forge

- [ ] Ask the agent to `echo passed` into the state dir (`~/.local/share/devin-skills/` or `$DEVIN_SKILLS_STATE_DIR`). Writes stay **blocked** (HMAC).
- [ ] `/gate-bypass <reason>` (reason required) → writes and Stop allowed for **this session**. Confirm it is audited and not builtin `/bypass`.
- [ ] New session: locked again (bypass is per-session).

## 6. `DEVIN_GATES_OFF`

- [ ] `DEVIN_GATES_OFF=1` in the **shell that starts** `devin` → writes allowed.
- [ ] `tool_input.env.DEVIN_GATES_OFF` on `exec` must **not** unlock (unit T19b; do not treat agent env as hook env).

## 7. herdr still there

- [ ] With `HERDR_ENV=1`, herdr still logs pane/session identity.
- [ ] `/hooks` still shows both herdr and `devin-gates.py`.

## 8. Builtin `/bypass` (permission mode)

- [ ] Enable Devin `/bypass` / `/yolo` / `/dangerous`. Confirm **PreToolUse still fires** (write still blocked without a marker / override / gates-off).
- [ ] If PreToolUse does **not** fire, **README must warn** that permission-bypass disables the lock. Builtin `/bypass` is not `/gate-bypass`.

## 9. Plugin has no lock

- [ ] `plugin/` has **no** `hooks.json`. Do not add one.
- [ ] `devin plugins install --local …/plugin` is optional and **not** a substitute for `install.sh`. Commands would be `/devin-skills:design`, not `/design`. The plugin `/goal` skill is prompt-only — no signed state, no Stop block, no verifier mint.

## 10. `/goal`

"Stop allowed" below means the **goal's** block lifts — the ordinary Stop rules still apply.

- [ ] `/goal <objective>` → `set-goal` prints `goal_id=`, `goal_allow_root=`, `status=active`, `kind=` (default `general`; `--kind research` prints `kind=research`, an invalid kind is refused). `goal-status` shows `attached: yes` plus the telemetry line (`claims`, `updates`, `age`, `tool_errors`).
- [ ] Stop / "I'm done" → **blocked** with the goal reason — even with `source_seq == 0` and in plan mode.
- [ ] `goal-pause --reason "…"` → Stop **allowed** at the next boundary. `goal-resume` is then **refused** — `needs_user_prompt` is set and the model cannot clear it. Send a real user prompt (`UserPromptSubmit` clears it; `goal-status` shows `resume_gate=open`), then `goal-resume --reason "…"` re-attaches and re-blocks. If the host never emits the event, `DEVIN_GOAL_PROMPT_GATE=0` is the documented escape — record which happened.
- [ ] `goal-update --claim-done` → records a claim; `goal-status` still `ACTIVE`. Nothing mints.
- [ ] **Claim before verify:** a fresh `goal-verifier` spawn with **no** claim → **blocked** (`no claim`); after `--claim-done` → allowed; a workspace edit after the claim → spawn blocked again (`stale claim`) until re-claim. Session `/gate-bypass` does **not** lift this; `DEVIN_GATES_OFF=1` does. A second fresh spawn while the first is still in flight → **blocked** (`already in flight`); a `resume`d spawn needs no claim and is never a witness. A stale binding (mutation since spawn) is voided at the next spawn and the orphaned result lands late; a still-current wedge recovers via `goal-pause` → prompt → `goal-resume`.
- [ ] **Objective binding:** a `goal-verifier` spawn whose task does not contain the signed objective verbatim → **blocked**. A task that does → allowed.
- [ ] **Checklist baseline:** write `checklist.md` under `goal_allow_root`, run `goal-baseline` → `goal-status` shows `baseline_sha256`; a second `goal-baseline` is refused. Edit `checklist.md`, then claim or spawn → `checklist_drifted` appears in `goal-status` (audit `goal_checklist_drift`).
- [ ] Fresh `goal-verifier` spawn whose task contains the `goal_id` → `GATES_VERDICT: PASS` → `goal-status` shows `COMPLETE`; Stop allowed. **Dump** the live `PostToolUse` payload for `profile=goal-verifier` — the same auto-mint spike as §3 applies (if `tool_response.output` is a stub, the mint may need `read_subagent`).
- [ ] **Contradictory verdict demotes:** a `goal-verifier` result whose `goal-verdict` JSON lists findings (or per-item `REFUTED`/`UNVERIFIABLE` lines) but ends `GATES_VERDICT: PASS` → counted as **FAIL**, not minted.
- [ ] After completion, edit a workspace file → `goal-status` back to `ACTIVE` (audit `goal_reopened`); Stop **re-blocked** in the same session (attach survived completion).
- [ ] `run_subagent` with `profile=goal-verifier` while unattached or paused → **blocked**, even after markers exist (unless `DEVIN_GATES_OFF` or a session `/gate-bypass` is set).
- [ ] **Auto-block on repeated gaps:** two consecutive FAIL sweeps flagging the *same* gap → goal auto-blocks; `goal-status` shows `BLOCKED` with `blocked_reason=no-progress: …` (the reason is surfaced by `goal-status`, not only in the signed file); Stop allowed; audit `goal_no_progress`.
- [ ] **BLOCKED verdict auto-block:** a verifier `GATES_VERDICT: BLOCKED` with a `goal-verdict` JSON block (`blocking: unverifiable` findings + `blocker_key`) → goal auto-blocks `blocking: <key>`; a resume + same `blocker_key` re-block escalates to `external-repeat:`; audit `goal_blocked_auto` / `goal_blocker_repeat`.
- [ ] **Sweep cap:** `DEVIN_GOAL_SWEEP_CAP=2` + two FAIL sweeps with *distinct* gaps → `sweep-cap: 2/2` (identical gaps hit `no-progress:` at 2 first); `goal-status` shows the effective cap (and `stall` counter) on the status line.
- [ ] **Blocked streak:** `goal-update --blocked --reason "…"` attempts 1–2 are refused (`blocked attempt N/3 — keep working`, audit `goal_blocked_attempt`); attempt 3 is honored. `--blocker-key` without `--blocked` is rejected.
- [ ] **Unclaimed work:** `DEVIN_GOAL_UNCLAIMED_CAP=3` + 3 workspace mutations without a claim → auto-block `unclaimed-work:` (nudge reminders start earlier, at `DEVIN_GOAL_CLAIM_NUDGE`).
- [ ] **Infra-error streak:** `DEVIN_GOAL_ERROR_STREAK=3` + 3 consecutive failed `run_subagent`/`mcp_call_tool`/`mcp__*`/`write_to_process` calls → auto-block `infra-errors:`; `goal-status` shows `tool_errors`. `exec`/`read`/`grep`/`write`/`edit` failures must **not** count — record a failed `exec` and confirm the streak is unchanged.
- [ ] **Strategist:** 3 consecutive FAIL sweeps with *different* gap sets → `strategist_pending` set, cap +2, stall threshold 5; `--claim-done` refused until `strategy.md` exists under `goal_allow_root` (fresh — written after the fire); `goal-strategist` spawn works only while pending.
- [ ] `goal-clear --reason "…"` → `goal-status` prints `goal: none`; a late verifier verdict is ignored (audit `goal_verifier_late_result`).
- [ ] **Reminder observability (unverified):** record whether hook stdout on `SessionStart` / `UserPromptSubmit` / `PostCompaction` actually reaches the agent. If it does not, the goal still survives on disk — `/goal status` recovers it; degraded, not broken.

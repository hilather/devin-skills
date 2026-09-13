# Live Devin smoke test

Optional checklist on a **throwaway repo**. Unit tests (`python3 -m unittest tests.test_install_merge tests.test_gate -v`) are the merge gate; this confirms the lock against a real Devin session.

Use a throwaway git repo, not this one, so a failed lock cannot rewrite `devin-gates.py`.

Default is locked. Builtin `/plan` stays the planner. Do not create a skill named `plan`. Do not ship `/goal`.

## 0. Install

- [ ] `sh install.sh` from this repo (or `--prefix` / `--src` as in [install.md](install.md)).
- [ ] In Devin, `/hooks` lists **both** `herdr-agent-state.sh` and `devin-gates.py`.
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
- [ ] `devin plugins install --local …/plugin` is optional and **not** a substitute for `install.sh`. Commands would be `/devin-skills:design`, not `/design`.

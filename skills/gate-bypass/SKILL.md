---
name: gate-bypass
description: Set an audited per-session gate override. Requires a reason. Not Devin's builtin /bypass.
argument-hint: "<reason>"
triggers:
  - user
---

# Gate Bypass

Set a **per-session**, HMAC-audited override so writes and Stop are allowed without skeptic markers. This is **not** Devin's builtin `/bypass` / `/yolo` / `/dangerous` (those are permission mode). Hooks may still fire under permission-bypass; this command is the honest skip.

`triggers` are user-only. Do not auto-invoke.

## Reason is required

The user argument **is** the reason. Examples: `/gate-bypass tiny typo` or `/gate-bypass need tests before skeptic`.

If the user gave **no** reason (empty argument and no reason in the prompt), **refuse**. Ask for a reason. Do not invent one. Do not pass `--reason ""`.

## Command

Prefer the **installed** copy. While locked, only that realpath is on the `exec` allowlist for gate subcommands.

```
python3 ~/.config/devin/hooks/devin-gates.py bypass --reason "<reason>"
```

Quote the reason. If the installed file is missing, resolve `../../hooks/devin-gates.py` from this SKILL.md and say that the repo copy will be blocked while writes are locked.

Do not pass `--resolved-blockers`. Do not call `mint-plan` or `mint-code` as a substitute for an explicit bypass.

## Confirm to the user

On success, confirm all of:

1. Override is **audited** (appended to the local audit log).
2. Override is **per-session** (this `session_id` only; a new session is locked again).
3. This is **not** builtin `/bypass` / `/yolo` / `/dangerous`.
4. The durable alternative is `/skeptic-plan` / `/skeptic-review`. Env unlock is `DEVIN_GATES_OFF=1` in the **shell that starts** `devin` (not `exec.env`).

On failure (missing session, CLI error), print stderr and stop. Do not claim the lock is lifted.

---
name: gate-status
description: Print HMAC gate status (markers, sweeps, override, source_seq, attached goal) for this session. Read-only.
triggers:
  - user
---

# Gate Status

Read-only. Run the gate CLI `status` subcommand and print its output to the user (markers, sweeps, override, `source_seq`, attached goal). Do not mint, bypass, or edit state.

## Command

Prefer the **installed** copy (the live lock). The repo copy is not on the locked `exec` allowlist.

```
python3 ~/.config/devin/hooks/devin-gates.py status
```

If that file does not exist, resolve the repo copy from this SKILL.md: `../../hooks/devin-gates.py` (repo root `hooks/devin-gates.py`) and run `python3 <that path> status`. While writes are locked, only the installed realpath is allowlisted — if the installed copy is missing, say so; do not try to unlock in order to run status from the repo.

Print stdout as-is. Typical lines:

- `[devin-gates] writes: LOCKED|UNLOCKED | plan-skeptic: present|missing | code-skeptic: present|missing | override: yes|no`
- `session_id=... source_seq=... sweeps=...`
- `goal: <id>:<status>` — only when this session is attached to a goal

Explain briefly: missing `plan-passed` means run `/skeptic-plan` before implementing; missing `code-passed` after source mutations means run `/skeptic-review` before claiming done. An override is a per-session `/gate-bypass`, not Devin's builtin `/bypass`.

Do not read `$STATE_DIR` files yourself (`secret` / `state.json` are protected even after unlock). Do not run `mint-plan`, `mint-code`, `bypass`, or the mutating goal subcommands (`set-goal`, `goal-update`, `goal-pause`, `goal-resume`, `goal-clear`). `goal-status` is the read-only one.

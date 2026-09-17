# How it works

Plain-English internals. Everyday use is in [usage.md](usage.md).

## The one-sentence version

`/design` is a writer/reviewer loop. `/plan` is Devin's builtin. Nothing in this repo blocks writes.

## Layers

| Layer | Role |
| --- | --- |
| Devin builtin `/plan` | Host read-only draft, approval UI, `exit_plan_mode`. Not our code. |
| Custom subagents | Read-only personas (`design-writer`, `design-reviewer`), `model: swe-2-high`. |
| `/design` skill | Orchestrator. Prompts only. |
| Tiny `AGENTS.md` | Pointers, not a playbook. |
| Optional `plugin/` | Same skills + agents, for sharing. |

## The design loop

`/design` needs somewhere to put artifacts without dumping them into your repo. `skills/design/scripts/setup-design.py` creates `design_allow_root` (usually under `~/.cache/devin-skills/design/<id>/`). The parent `request_scope`s that directory so Devin's workspace sandbox allows `read`/`write` there.

Writer and reviewer have no `write` / `edit` / `exec`. That is why the parent copies fenced markdown onto disk.

```mermaid
flowchart LR
  W[design-writer] -->|fenced markdown| P[Parent copies to disk]
  RV[design-reviewer] -->|fenced review| P
  P -->|paste files into task| W
  P -->|paste files into task| RV
```

They loop until the reviewer reports 0 `Status: open` issues. Nits count. No round cap. Stalemate or `needs-user-input` goes to you; your answer is final.

The doc must include **Key Decisions** and a **PR Plan** (`### PR N:` slices with files, dependencies, description) so you can take one slice at a time through builtin `/plan`.

## Honest gaps (Devin ≠ Grok)

- There is no Grok `spawn_subagent` / `resume_from`. Use Devin `run_subagent` with `profile` and optional `resume`.
- The parent sees a distilled subagent result, not the raw transcript. The task must demand a complete fenced file.
- This orchestrator must run on the parent (depth 0). `run_subagent` is disabled inside a subagent.

## What we removed

Older versions of this repo installed an HMAC write-lock (`hooks/devin-gates.py`) plus skeptic, `/goal`, and `/execute-plan` skills. Those are gone. `install.sh` strips leftover gate hook entries so a reinstall actually unlocks an existing machine.

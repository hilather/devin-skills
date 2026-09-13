# Vendor origin

These files are a **snapshot fallback** of agent-hints hunt lists. They are not a long-term fork.

Preferred runtime source (when present):

- `~/git/agent-skills/knowledge/plan-skepticism/README.md`
- `~/git/agent-skills/knowledge/code-review-skepticism/README.md`

Cursor skill sources (operational templates the personas adapt):

- `~/.cursor/skills/skeptic-plan-review/SKILL.md`
- `~/.cursor/skills/skeptic-code-review/SKILL.md`

Refresh the snapshots (POSIX, no extra deps):

```
sh scripts/refresh-vendor.sh
```

That copies the two `~/git/agent-skills/knowledge/*/README.md` files into this directory and prepends an origin pointer (knowledge path + Cursor skill). Override the clone with `AGENT_SKILLS_ROOT` if it is not at `~/git/agent-skills`.

Do **not** treat this repo or `agent-skills` as a hilather product. The gated hilather block is copied as-is and stays gated: apply only when the workspace is a hilather product.

Do not ship a skill named `plan`. Devin's builtin `/plan` is the read-only planner.

## T11 / live PostToolUse dump

A live Devin 3000.10.21 `PostToolUse` payload for `run_subagent` was **not** captured (transcripts have tool calls, not hook stdin). Auto-mint still keys off `tool_response.output` matching `^GATES_VERDICT: (PASS|FAIL|BLOCKED)\s*$` as specified. Gate test T11 remains labeled synthetic (`tests/fixtures/synthetic_plan_skeptic_pass_post.json`). Do not invent a fake live dump.

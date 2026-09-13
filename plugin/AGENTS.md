# Devin Skills (workflow pointers)

This machine uses ~/git/devin-skills/ (installed into ~/.config/devin/).

- After /plan is approved, do not implement until /skeptic-plan. Writes are hook-blocked until a plan-skeptic marker exists.
- Before claiming done, run /skeptic-review. Stop is hook-blocked until a code-skeptic marker exists.
- Design docs: /design (writer/reviewer loop). Mandatory PR Plan and Key Decisions.
- Small tasks: /gate-bypass <reason> or DEVIN_GATES_OFF=1. Default is locked.
- Do not create a skill named plan; the builtin /plan is the read-only planner.
- Hunt lists: ~/git/agent-skills/knowledge/plan-skepticism and code-review-skepticism (vendored snapshot if that repo is absent).

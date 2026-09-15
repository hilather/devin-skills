# Devin Skills (workflow pointers)

This machine uses ~/git/devin-skills/ (installed into ~/.config/devin/).

_Plugin install: these pointers describe the user-level lock from `install.sh`. A plugin-only install ships the skills without the gate — writes and Stop are not actually hook-blocked._

- After /plan is approved, do not implement until /skeptic-plan. Writes are hook-blocked until a plan-skeptic marker exists.
- Before claiming done, run /skeptic-review. Stop is hook-blocked until a code-skeptic marker exists.
- Design docs: /design (writer/reviewer loop). Mandatory PR Plan and Key Decisions.
- Execute a design doc's PR Plan: /execute-plan <doc>. Sequential, parent implements, read-only pr-reviewer per PR, --resume <PLAN_ID> after a crash. Plugin installs run it unenforced (no gate).
- Small tasks: /gate-bypass <reason> or DEVIN_GATES_OFF=1. Default is locked.
- Long objectives: /goal — gate-tracked; Stop blocks while attached+active; only a fresh goal-verifier PASS completes.
- Do not create a skill named plan; the builtin /plan is the read-only planner. Do not invent a prompt-only /goal — the gate-backed one exists.
- Hunt lists: ~/git/agent-skills/knowledge/plan-skepticism and code-review-skepticism (vendored snapshot if that repo is absent).

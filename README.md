# Devin Skills

Layered design / plan-skeptic / code-skeptic workflow on Devin CLI extension points (verified on **3000.10.21**).

**The product is the write-lock** in `hooks/devin-gates.py`. Skills are prompts; without the lock, Devin can ignore them and edit anyway. Markers are HMAC-signed and minted only by the gate script. Plugin packaging is optional and **must not** carry the lock (plugin hooks fail-open).

This repo is the source of truth. Install it into `~/.config/devin/` (or a project `.devin/`) with `install.sh`. Existing **herdr** hooks stay in place; the gate is merged beside them.

## What it is

After Devin's built-in `/plan` is approved — and by default in any normal session — workspace writes stay blocked until a **plan-skeptic** marker exists. Stop ("I'm done") stays blocked until a **code-skeptic** marker exists, unless nothing mutated, an audited override is set, gates are off, or the Stop-loop guard fires.

Keep Devin's built-in `/plan`. **Do not** add a skill named `plan`. **Do not** ship `/goal`. Default is **locked**.

| Layer | Role |
| --- | --- |
| Devin builtin `/plan` | Host read-only draft. Not our code. |
| User/project hooks (`devin-gates.py`) | The only layer that can `decision: block`. Never in a plugin. |
| Custom subagents | Read-only personas (`design-writer`, `design-reviewer`, `plan-skeptic`, `finding-skeptic`, `code-skeptic`), `model: swe-2-high`. |
| Skills | Orchestrators and slash commands. Prompts only. |
| Tiny `AGENTS.md` | Pointers, not playbooks. |
| Optional `plugin/` | Skills + agents + the tiny rule for sharing. **No `hooks.json`.** |

## Commands

| Command | What it does |
| --- | --- |
| `/plan` | **Builtin.** Read-only draft (`write_plan`, approval UI, `exit_plan_mode`). Do **not** name a skill `plan`. |
| `/design` | Writer/reviewer loop. Parent is the only writer (artifacts under `design_allow_root`). Mandatory PR Plan and Key Decisions. |
| `/skeptic-plan` | Fresh `plan-skeptic` sweeps until `GATES_VERDICT: PASS` (or BLOCKED after 3 FAIL sweeps). Lifts the write-lock. |
| `/skeptic-review` | Finding-skeptic then code-skeptic on a frozen diff. Lifts the Stop-lock. Requires `plan-passed`. |
| `/gate-bypass` | Audited per-session override. **Requires a reason.** Not Devin's builtin `/bypass` / `/yolo` / `/dangerous`. |
| `/gate-status` | Print markers, witnesses, sweeps, override. Read-only. |

There is **no** `/goal` and **no** skill named `plan`. Builtin `/ask`, `/loop`, and `/bypass` are Devin's; do not ship colliding skills.

## Grok → Devin mapping

| Grok | Devin approximation | Honest gap |
| --- | --- | --- |
| `/plan` harness: read-only except plan file; `enter_plan_mode` / `exit_plan_mode`; approval UI; `/view-plan` | **Keep built-in `/plan`.** Hooks take over **after** approval until a skeptic marker exists. | No `/view-plan` skill. Devin's plan file lives under `~/.devin/plans/`, not Grok's session `plan.md`. We do not replace the approval UI. |
| `/design` writer/reviewer/`resume_from` | Orchestrator skill + read-only `design-writer` / `design-reviewer`. Parent copies fenced markdown onto `design_allow_root`. `resume` for revise / re-review. | Parent sees a distilled result, not the raw transcript. v1 does **not** assume child `write` re-enters hooks. |
| `/goal` (host rounds, pause/resume/clear, token budget, independent evidence review) | **v1 non-goal. Do not ship `/goal`.** Closest honest pieces already in v1: builtin `/plan` + write-lock until plan-skeptic PASS + Stop-lock until code-skeptic PASS. | Grok `/goal` is **host** logic. Devin has none of that. A skill named `/goal` would be a prompt saying "keep going" that *looks* like Grok `/goal` and fails silently. See [Goal harness: not approximated](#goal-harness-not-approximated). |
| Plan skeptic before implement (3-sweep cap, BLOCKED, failed-sweep autopsy) | `/skeptic-plan` + `plan-skeptic` + **write-lock hook**. Fresh subagent per sweep. Lock lifts only on `GATES_VERDICT: PASS`. | Hook cannot run the skeptic (timeouts). A parent that jailbreaks the skeptic via the task prompt can still produce PASS. Residual, documented. |
| Code skeptic at done (finding-skeptic + implementation sweep, kick-back vs ordinary) | `/skeptic-review` + `finding-skeptic` + `code-skeptic` + **Stop hook**. Auto-mint requires a `code-skeptic` PASS whose witness `source_seq` is ≥ current `source_seq`. | Same residual. Stop-loop guard is required (High for the Stop claim). Parent must embed the **full** `git diff` in the task; large diffs may truncate. |
| Independent skeptic (fresh, no attachment) | Fresh Devin subagent per sweep; parent must not self-review. Profiles omit write/edit/`exec`. | Parent still writes the task prompt (jailbreak residual) **and** the pasted diff. |

## Goal harness: not approximated

**Do not ship a skill named `/goal`.** Grok `/goal` is host logic, not a prompt: token budget, pause/resume/clear, autonomous multi-round driver, and an independent evidence review that can **refuse** completion.

Devin CLI 3000.10.21 exposes **none** of that. A skill named `/goal` would only say "keep going until you think you are done." The parent would mark itself complete. That would *look* like Grok `/goal` and fail silently — worse than an honest non-goal.

The durable part of a goal on this stack is already here, without faking the harness:

- builtin `/plan` (read-only draft);
- write-lock until a **plan-skeptic PASS**;
- Stop-lock until a **code-skeptic PASS** (with `source_seq` so a later edit cannot remint).

That is "don't implement until the plan survives; don't claim done until the diff survives."

## Default is locked

Sessions that never used `/plan` are still locked. Small tasks use `/gate-bypass <reason>` or `DEVIN_GATES_OFF=1`. Bypass is explicit.

Stop is allowed without a code-skeptic marker only when:

- `mode == plan` (from `write_plan`, cleared on `exit_plan_mode` — **not** parsed from prompt text);
- `source_seq == 0` (no successful source mutations);
- an audited `/gate-bypass` override;
- `DEVIN_GATES_OFF=1` in the **shell that starts `devin`**;
- or this `prompt_id` has already been Stop-blocked 3 times (loop guard). After 3, the turn is allowed to end. That residual is **High** for the Stop product claim. The honest skip for real work is `/gate-bypass`, not Stop-retry.

`echo passed > marker` does not unlock. Minting happens only from the gate script, HMAC-signed with a secret the agent cannot write.

## Install

Preferred path is **user-level** `install.sh`: symlink skills/agents so slash commands are `/design` (not `/devin-skills:design`); **copy** the gate script (the lock must not be a live symlink into a writable repo).

See **[docs/install.md](docs/install.md)** (and **[docs/uninstall.md](docs/uninstall.md)**).

```sh
sh install.sh
# or:
sh install.sh --prefix ~/.config/devin --src /path/to/devin-skills
```

After install, in Devin run `/hooks` and confirm both `herdr-agent-state.sh` and `devin-gates.py` are listed.

Python 3 stdlib and POSIX `sh` only. No npm packages, crates, or frameworks.

```sh
python3 -m unittest tests.test_install_merge tests.test_gate -v
```

Live Devin checklist (optional): **[docs/smoke-test.md](docs/smoke-test.md)**.

Keep `~/git/agent-skills` cloned for hunt lists; `sh scripts/refresh-vendor.sh` after hint updates. Fallback: `vendor/agent-hints/`.

## herdr coexistence

`~/.config/devin/config.json` already wires `herdr-agent-state.sh` on several events. Those hooks report pane/session identity and **always exit 0**. Replacing that file would break herdr.

`install.sh`:

- Backs up `config.json` to `config.json.bak-devin-skills-<timestamp>`.
- Appends **one** gate dispatcher per event (`matcher: ""`, `devin-gates.py hook`) iff that event does not already have a `devin-gates.py` command.
- Creates `PostCompaction` and `SessionEnd` if missing.
- Leaves herdr command strings **byte-identical**. herdr stays first.
- Does **not** add a gate hook on `PermissionRequest` (herdr stays the only one there).
- Never edits `herdr-agent-state.sh`.

Uninstall drops only `devin-gates.py` elements. herdr remains.

## Bypass and `DEVIN_GATES_OFF`

| Mechanism | Scope | Notes |
| --- | --- | --- |
| `/gate-bypass <reason>` | This session | HMAC `override_reason`; audited. Reason is required. |
| `DEVIN_GATES_OFF=1` | Process tree of the shell that **starts** `devin` | Hook `os.environ` only. `tool_input.env.DEVIN_GATES_OFF` does **not** unlock. |
| `sh uninstall.sh` | Structural off | Does not delete state unless `--purge`. |
| Builtin `/bypass` / `/yolo` / `/dangerous` | Devin **permission mode** | **Not** a gate override. Hooks may still fire (unverified — see [docs/smoke-test.md](docs/smoke-test.md) §8). If a future Devin build skips PreToolUse in permission-bypass, the lock dies; treat that as "permission-bypass disables the lock." |

Dogfood gate-script changes with `/gate-bypass` or `DEVIN_GATES_OFF=1`, then re-run `install.sh` (copy + refresh hash). Do not edit the installed copy from a locked session.

## Optional plugin (no hooks)

`plugin/` packs skills, agents, and the tiny rule for sharing. It is **not** the install default.

```sh
devin plugins install --local /path/to/devin-skills/plugin
```

That namespaces commands as `/devin-skills:design` (and plugin subagents as `devin-skills:plan-skeptic`). The skills in this repo spawn un-namespaced profiles (`plan-skeptic`, …). **User-level `install.sh` remains the path that actually locks writes.**

**Do not put hooks in the plugin.** Plugin hooks are documented as best-effort and fail-open — if a hook fails to load, the session continues without it. There is **no** `plugin/hooks.json`. The write-lock lives only in user `~/.config/devin/config.json` and/or project `.devin/hooks.v1.json`.

`plugin/skills/` and `plugin/agents/` are copies of the repo trees. If they drift, recopy (`cp -a skills agents plugin/` and `cp rules/AGENTS.md plugin/AGENTS.md`) or ignore the plugin and use `install.sh`.

## Honest residuals

- Parent jailbreak: the parent writes the skeptic `task`. Instructing PASS can still mint. The lock verified that the profile emitted PASS, not that the plan/diff is independently good.
- Stop-loop cap of 3 per `prompt_id` is required (Devin can loop on blocking Stop) and is a **High** residual for the Stop claim.
- Large diffs may truncate in the pasted `task`.
- `apply_patch` schema on 3000.10.21 is not fully known: fail closed while locked; after unlock, fail open except a protected-path scan.
- Custom subagents are experimental; pin Devin 3000.10.21 in mind when the format moves.

## Layout

```
hooks/devin-gates.py     # THE product
hooks/hook-entries.json  # dispatcher JSON; install.sh merges this
install.sh / uninstall.sh
skills/                  # /design /skeptic-plan /skeptic-review /gate-bypass /gate-status
agents/                  # five read-only personas
rules/AGENTS.md          # < 20 lines; pointers only
docs/install.md
docs/uninstall.md
docs/smoke-test.md
plugin/                  # optional; no hooks.json
tests/                   # unittest; no live Devin required
```

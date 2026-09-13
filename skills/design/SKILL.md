---
name: design
description: Run the design-writer / design-reviewer loop until 0 open issues. Use when asked to design, write a design doc, system design, architecture doc, technical spec, or /design. Mandatory PR Plan and Key Decisions.
argument-hint: "<description of what to design>"
triggers:
  - user
  - model
---

# Design Skill

You are an **inline** orchestrator (this skill omits `subagent` and `allowed-tools` so you keep `run_subagent`). You run the write → review → revise loop using the **read-only** custom profiles `design-writer` and `design-reviewer` until the reviewer reports **0** `Status: open` issues.

You coordinate only. You **must not** author the design-document body or invent review findings. **All** document prose comes from `design-writer`. **All** review notes come from `design-reviewer`. You are the **only writer on disk**: those profiles have no `write` / `edit` / `exec`. You copy their fenced markdown onto files under `design_allow_root`.

Do **not** spawn `subagent_general`. Do **not** give writer/reviewer write tools. Do **not** wait for a child-hooks-reenter spike (user 2026-09-13; Key Decision 19). Child `write` is not assumed to re-enter hooks; that is why these profiles are read-only.

## Honest gaps (Devin ≠ Grok)

- There is **no** Grok `spawn_subagent` host object. Use Devin `run_subagent` (`title`, `task`, `profile`, optional `resume`, `is_background: false`).
- There is **no** Grok `resume_from` host object. Use `run_subagent.resume` with the agent id from the previous result. If `resume` fails, launch a **fresh** `design-writer` / `design-reviewer` and paste the files that are already on disk (the disk is the memory).
- You see a distilled subagent result, not the raw transcript. The task must demand a **complete** fenced file; `resume` if truncated.
- Nested `subagent: true` skills run **inline** inside a subagent. This orchestrator must run on the **parent** (depth 0). Do not re-invoke `/design` from inside a subagent (`run_subagent` is disabled there).
- `exec rm` is not on the locked allowlist. Leave summary/review in the cache root; do not try to delete them while writes are locked.

## Tool-call discipline

Every action you describe must correspond to a real tool call in the same response. Emit `run_subagent` **before** any "writer is starting" narration. After the tool result is in history, confirm in past tense ("Writer subagent launched"). Do not ask permission to continue the loop — pick the next step and proceed.

## Todo scaffold

Canonical ids (reseed from these after compaction; do not invent new names):

- `write-round-1` — Step 1
- `review-round-1` — Step 2
- `revise-round-N` / `rereview-round-N` — Steps 4–5
- `summarize` — Step 6
- `final-report`

Exit the loop when `rereview-round-N` (or first review) has 0 open issues.

**Reseed after compaction** from scratch `state.json` (`round_count`, `writer_id`, `reviewer_id`, artifact paths). Do not advance until the todo list is back.

## Invocation

```
/design <description>
```

`<description>` is the design task (feature, architecture, migration, spec). Include file paths, links, and conversation context in the writer task.

## Setup

1. Resolve `GATE` as the first existing regular file:
   - `$HOME/.config/devin/hooks/devin-gates.py` (installed copy — prefer this; locked `exec` allowlists it)
   - `<absolute dirname of this SKILL.md>/../../hooks/devin-gates.py` (repo copy; allowlisted via `source_realpath`)
2. Run `python3 <GATE> allow-design` with **no** `--file`. Optional `--id` is 8 lowercase hex only; omit to generate. Session id comes from `DEVIN_SESSION_ID`, `--session-id`, or `$STATE_DIR/current_session` (hooks write that). Do not invent a session id.
3. Parse stdout lines `design_id=...` and `design_allow_root=...`. If the command fails or `design_allow_root` is empty, report the error and **stop**. Spawn of `design-writer` / `design-reviewer` is **blocked** until this root is set.
4. Define paths (thread these for the whole loop; never regenerate):
   - `design_doc_file`: `<design_allow_root>/design-doc.md`
   - `summary_file`: `<design_allow_root>/summary.md`
   - `review_file`: `<design_allow_root>/review.md`
   - scratch `state.json`: `<design_allow_root>/state.json`
5. Parent `write` / `edit` **only** succeed under `design_allow_root` (realpath). Workspace source stays locked. Do not symlink out of that root.
6. Initialize scratch `state.json` (not the HMAC session blob):

```
{"round_count": 0, "writer_id": "", "reviewer_id": "", "design_id": "<id>", "design_allow_root": "<root>", "design_doc_file": "...", "summary_file": "...", "review_file": "..."}
```

Also keep in working memory: `total_issues_by_severity` (cumulative map) and `previous_review_snapshot` (prior review text for stalemate detection). Persist them in scratch `state.json` when you can.

## Copying fenced markdown (parent only)

Writer/reviewer wrap each artifact in an **outer** fence of **four or more** backticks (e.g. four backticks immediately followed by `markdown`) so inner Mermaid and ` ``` ` code can nest. If the body would contain four backticks, they use a longer outer fence.

After each subagent result:

1. Find outer-fenced blocks (opening line of N≥4 backticks, optional `markdown` info string; closing line of **exactly N** backticks).
2. Copy the **inner** body (not the outer fence lines) with `write` onto the artifact path.
3. Do not rephrase, trim required sections, or splice diffs into the doc. Paste what the subagent emitted.
4. If a required block is missing or truncated, `resume` that agent and ask for the complete file(s) again. Do not ship a partial doc.

First draft (writer) → **two** blocks, in order: design doc, then summary.  
Revision (writer) → **two** blocks: full updated design doc, then complete updated review notes.  
Reviewer → **one** block: complete review notes.

## `run_subagent` contract

```
title: "<short title>"
task: "<prompt>"
profile: "design-writer" | "design-reviewer"
is_background: false
resume: "<agent id>"   # omit on first spawn; set on revise / re-review
```

Save `agent_id` from the result (e.g. `Subagent agent_id=... completed`) into scratch `writer_id` / `reviewer_id`. Update the saved id after every successful spawn or resume.

Personas live in `~/.config/devin/agents/design-writer.md` and `design-reviewer.md` (or this repo's `agents/`). **Do not** prepend those files to `task` and **do not** pass a `persona` parameter (unsupported). On `resume`, the profile is already in the child transcript — do not re-inject it.

The cache dir is usually outside the workspace; children may not be able to `read` it. **Paste** the current doc / summary / review into `task` every round. Disk files are for you and for a fresh relaunch.

## Step 1: Write

`run_subagent` with `profile: design-writer`, no `resume`.

`title`: `[writer] Write design doc: <short summary>`

`task`:
```
Write a design document for the following:

<full user description and all relevant conversation context>

You are read-only. Do not call write. Return complete files as outer-fenced markdown (four or more backticks) as specified in your profile.

Mandatory sections: ## Key Decisions and ## PR Plan (last major section), with ### PR N: headings, files/components, dependencies, and description. Each PR must be independently reviewable and mergeable.

Emit exactly two outer-fenced blocks: (1) the full design document, (2) a short summary of what was produced.
```

Wait for completion. On failure, report and stop.

Save `writer_id`. Copy fence 1 → `design_doc_file`, fence 2 → `summary_file`. Update scratch `state.json`.

Report: "Design document drafted. Starting review..."

## Step 2: Review

`run_subagent` with `profile: design-reviewer`, no `resume`.

`title`: `[reviewer] Review design document`

`task`:
```
Review the design document. You are read-only. Do not rewrite the doc. Do not call write.

The design document:

<paste full design-doc.md>

The writer's summary:

<paste full summary.md>

Review thoroughly. Pay special attention to:
- Whether ## PR Plan is present, realistic, properly ordered (### PR N:, files/components, dependencies, description)
- Whether ## Key Decisions is present, well-reasoned, and complete
- Whether an engineer could implement from this
- Whether alternatives were meaningfully explored

Missing ## PR Plan or ## Key Decisions is at least major.

Every issue Status: open on first review. End with one outer-fenced complete review-notes file (four or more backticks). In unfenced text, state the count of Status: open issues and the verdict.
```

Wait. Save `reviewer_id`. Copy the review fence → `review_file`. Increment `round_count`. Tally open issues by severity into `total_issues_by_severity`.

## Step 3: Exit condition

Read `review_file` yourself. Count `Status: open`, `Status: wontfix`, `Status: needs-user-input`. Do **not** self-author the doc body.

- **0 open AND 0 needs-user-input**: Step 6.
- **Any needs-user-input**: Step 3a.
- **Any open (>0)**: Step 4.

**Stalemate:** if an issue (match on section + description) was `wontfix` in a prior round and the reviewer has now set `Status: open` on it **twice** (wontfix re-opened twice), stop looping that item — Step 3a. Do not spin.

Then set `previous_review_snapshot` to the current review text.

### Step 3a: Ask the user

Present each `needs-user-input` or stalemate item with both sides, options, and enough design context. After the user answers, go to Step 4 and tell the writer those decisions are **final** (`Status: addressed`, no further debate).

## Step 4: Revise (`resume` writer)

`run_subagent` `profile: design-writer`, `resume: <writer_id>`.

`title`: `[writer] Revise design doc`

`task`:
```
The reviewer found issues. Address ALL Status: open issues, including nits.

Current design document:

<paste design-doc.md>

Review notes:

<paste review.md>

For each open issue, revise the document, set Status: addressed, and add a Response. You may Status: wontfix with a clear technical explanation — do not comply just to please the reviewer. Ambiguous product/user questions: Status: needs-user-input.

<If the user just decided something: treat it as final; incorporate it; Status: addressed.>

You are read-only. Do not call write. Emit two outer-fenced blocks: (1) the complete updated design document, (2) the complete updated review notes (every issue with Status/Response, plus Revision Summary). Outer fence: four or more backticks.
```

If `resume` fails, spawn a **fresh** `design-writer` with the same pasted files and "this is a continuation; the previous agent could not be resumed."

Wait. Update `writer_id`. Copy fences onto `design_doc_file` and `review_file`.

Report: "Revisions applied. Running re-review..."

## Step 5: Re-review (`resume` reviewer)

`run_subagent` `profile: design-reviewer`, `resume: <reviewer_id>`.

`title`: `[reviewer] Re-review design doc`

`task`:
```
The writer addressed the review issues. Re-review.

Updated design document:

<paste design-doc.md>

Updated review notes (with writer responses):

<paste review.md>

Writer summary:

<paste summary.md>

If a previous issue was properly addressed, do not re-list it. New problems: new Status: open. Not properly addressed: re-list Status: open. Accept convincing wontfix; re-opening a wontfix without new evidence creates a stalemate the parent must escalate.

You are read-only. Do not rewrite the doc. Do not call write. One outer-fenced complete review-notes file. Unfenced: count of Status: open and the verdict.
```

If `resume` fails, fresh `design-reviewer` with the files pasted.

Wait. Update `reviewer_id`. Copy review fence → `review_file`. Increment `round_count`.

**Go back to Step 3.** Repeat until 0 open. No iteration cap.

## Step 6: Present Key Decisions, Open Questions, PR Plan

Read the final `design_doc_file`.

1. Extract **Key Decisions** — list each with rationale.
2. Extract **Open Questions** (if any remain). Ask the user; do not silently resolve. If they answer, `resume` the writer once to incorporate, then **skip** re-review (user decisions, not design issues).
3. Extract **PR Plan** and present it.

Keep `design_doc_file`. Leave `summary_file` / `review_file` in the cache root (do not `rm` while locked).

## Final report

1. Design document path (`design_doc_file`)
2. Key Decisions
3. Review rounds (`round_count`)
4. Total issues addressed (`total_issues_by_severity`)
5. PR Plan
6. Open questions (resolved answers, or none)

## In-progress reporting

- After write: "Design document drafted. Starting review..."
- After review (0 issues): "Review passed with 0 issues. Finalizing..."
- After review (N issues): "Reviewer found N issues (X critical, Y major, Z minor/nit). Resuming writer to revise..."
- After revise: "Revisions applied. Running re-review..."
- Include the round: "Re-review (round 2)..."

## Rules

- **Parent is the only writer.** Copy fences; never self-author the doc body.
- **`allow-design` first.** No `--file`. One HMAC `design_allow_root` directory.
- **`resume` for revise / re-review.** If resume fails, fresh spawn + files on disk.
- **No Grok `spawn_subagent` / `resume_from`.** `run_subagent` only.
- **No child `write`.** Do not wait on a child-hooks-reenter spike.
- **Outer fences are four-or-more backticks** so inner Mermaid/code can nest.
- **Mandatory `## PR Plan` and `## Key Decisions`.**
- **Loop until 0 open.** No max-rounds cap. Nits count.
- **Escalate, don't spin.** wontfix re-opened twice, or `needs-user-input` → ask the user. User decisions are final.
- **Foreground only** (`is_background: false`).
- **Do not name a skill `plan`.** Do not invent a `/goal` harness.
- **Error handling:** subagent failure → report and stop. Do not continue with missing artifacts.

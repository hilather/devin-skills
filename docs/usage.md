# How to use Devin Skills

Everyday workflow. Internals are in [how-it-works.md](how-it-works.md). Install flags are in [install.md](install.md).

You need Devin CLI and a completed `sh install.sh` (or the plugin). There is no write-lock.

## Everyday workflow

Optional, before step 1, if the change needs a spec: **[/design](#how-to-use-design)**. Then take the first PR slice into `/plan`.

### 1. Plan

```
/plan add a retry around the GitHub API client
```

Devin's built-in planner drafts a plan (read-only). Approve it when it looks right.

Do **not** name a skill `plan`.

### 2. Implement

Devin can edit. Do the work.

## How to use /design

Use this when you'd write a spec by hand: a system design, a migration, a feature that needs Key Decisions. Do **not** use it for a typo, a one-file bugfix, or "just implement it." Those go straight to `/plan`.

`/design` writes a document. It does not edit your repo.

### 1. Invoke it

In Devin:

```
/design replace the sync job with a queue worker. Keep the existing Job row shape. No new infra.
```

Put the problem, constraints, and paths in the argument. Vague prompt → vague spec.

### 2. What happens

1. Devin runs `skills/design/scripts/setup-design.py` and gets a folder under `~/.cache/devin-skills/design/<id>/`.
2. A read-only **writer** drafts the spec. A read-only **reviewer** attacks it.
3. They loop until zero open issues. Nits count. No round cap.
4. If the reviewer needs a product call, Devin **stops and asks you**. Answer it — that call is final.
5. When it finishes, Devin prints the doc path, **Key Decisions**, and the **PR Plan**.

| File | What it is |
| --- | --- |
| `design-doc.md` | The spec. Always includes Key Decisions and a PR Plan. |
| `summary.md` | Short writer's summary. |
| `review.md` | Review notes (open / addressed / wontfix). |

### 3. After design

**Stop using `/design`.** It does not implement. Hand each `### PR N:` to Devin's builtin `/plan`, then implement.

## Commands

| Command | What it does |
| --- | --- |
| `/plan` | **Built into Devin.** Read-only draft. Do not add a skill named `plan`. |
| `/design` | Writer/reviewer loop. Writes a spec with **Key Decisions** and a **PR Plan**. Does not implement. |

## FAQ

**Why isn't there a `/plan` skill?** Devin already has a host planner with an approval UI. A second skill named `plan` would collide with it.

**Where do the design files go?** `$XDG_CACHE_HOME/devin-skills/design/<id>/` (default `~/.cache/...`), so they do not land in your repo unless you copy them.

**Can the writer edit my repo?** No. `design-writer` and `design-reviewer` are read-only. The parent copies fenced markdown onto the cache root.

**I used to have a write-lock.** Re-run `sh install.sh`. It strips leftover `devin-gates.py` hook entries and deletes the copied gate script. herdr stays.

# Live Devin smoke test

Optional checklist on a **throwaway repo**. Unit tests are the merge gate:

```sh
python3 -m unittest tests.test_install_merge -v
```

## 0. Install

- [ ] `sh install.sh` from this repo (or `--prefix` / `--src` as in [install.md](install.md)).
- [ ] `/design` is available. `/plan` is still Devin's builtin.
- [ ] If you previously had the write-lock: `/hooks` does **not** list `devin-gates.py`. herdr stays if you had it.

## 1. Writes are allowed

New session. Ask Devin to edit a file (typo, README, anything) **without** `/plan` or `/design`.

- [ ] `write` / `edit` succeeds. There is no "Write blocked: no plan-skeptic marker" message.

## 2. `/plan`

- [ ] `/plan` a tiny change. `write_plan` works.
- [ ] Approve `exit_plan_mode`.
- [ ] Workspace write still works. Implement the tiny change.

## 3. `/design`

- [ ] `/design` a small spec (one constraint, one path). Setup script prints `design_id=` and `design_allow_root=`.
- [ ] Writer then reviewer run. Parent copies fenced files onto the cache root.
- [ ] Loop ends with 0 open issues. Final report includes Key Decisions and a PR Plan.
- [ ] Workspace source was not required to change. Design artifacts live under `~/.cache/devin-skills/design/<id>/`.

## 4. Plugin (optional)

- [ ] `plugin/` has **no** `hooks.json`.
- [ ] `devin plugins install --local …/plugin` exposes `/devin-skills:design`.

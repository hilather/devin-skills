# Install

Installs `/design` (and the writer/reviewer agents) into your Devin config. It never replaces `config.json` wholesale and never edits `herdr-agent-state.sh`.

If an older version of this repo installed a write-lock, this installer **removes** those `devin-gates.py` hook entries.

## Prerequisites

- [Devin CLI](https://docs.devin.ai/cli)
- Python 3 (stdlib only — no pip packages)
- POSIX `sh`

## Quick install

```sh
git clone https://github.com/hilather/devin-skills.git
cd devin-skills
sh install.sh
```

Or, from a clone that already exists:

```sh
sh install.sh --prefix ~/.config/devin --src /path/to/devin-skills
```

Default prefix is `$HOME/.config/devin` (not `XDG_CONFIG_HOME`).

## What it does

1. Creates `~/.config/devin/{skills,agents}` if needed.
2. **Symlinks** each skill and agent into `~/.config/devin/` so slash commands are `/design`, not `/devin-skills:design`. Refuses to clobber a real file unless you pass `--force`.
3. **Prunes** stale skill/agent symlinks that used to point at this repo (skeptics, `/goal`, `/execute-plan`, gate commands).
4. Merges [rules/AGENTS.md](../rules/AGENTS.md) into `~/.config/devin/AGENTS.md` inside `<!-- devin-skills:begin -->` … `<!-- devin-skills:end -->`.
5. If `config.json` still has `devin-gates.py` hook entries: backs it up to `config.json.bak-devin-skills-<timestamp>`, then drops those entries. herdr command strings stay byte-identical. Empty events created only for the old lock (`PostCompaction` / `SessionEnd` with herdr present) go away.
6. Deletes leftover copies of `devin-gates.py` (and the old goal/execute-plan splices) under `$PREFIX/hooks/`.

Second install is safe to re-run.

A clean herdr config is not rewritten. There is no backup unless leftover gate entries were actually removed.

## Flags

| Flag | Meaning |
| --- | --- |
| `--prefix DIR` | Install root (default `~/.config/devin`) |
| `--src DIR` | Repo root to symlink from (default: directory of `install.sh`) |
| `--force` | Replace a non-symlink skill/agent destination |
| `--project` | Also symlink skills/agents under `.devin/` in the current directory, and strip leftover `.devin/hooks.v1.json` gate entries |
| `--help` | Usage |

User-level install always runs. `--project` is extra, not instead-of.

## herdr

`~/.config/devin/config.json` may already wire `herdr-agent-state.sh`. This installer never edits that file and never rewrites herdr command strings. It only removes `devin-gates.py` elements if they are still there.

## After install

In Devin you should see `/design`. `/plan` is Devin's builtin.

## Tests

```sh
python3 -m unittest tests.test_install_merge -v
```

`tests/test_install_merge.py` uses a temp `HOME` / XDG tree and a fixture copied from a real herdr `config.json`. It must never write the live `~/.config/devin`.

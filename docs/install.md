# Install

Installs the write-lock **beside** whatever is already in your Devin config. It never replaces `config.json` wholesale and never edits `herdr-agent-state.sh`.

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

After it finishes, start Devin and run:

```
/hooks
```

You should see `devin-gates.py`. If you already used herdr, you should still see `herdr-agent-state.sh` as well.

## What it does

In plain English:

1. Creates `~/.config/devin/{skills,agents,hooks}` and a private state directory (mode `0700`).
2. Generates a random HMAC secret once (`os.urandom` 32 bytes, mode `0600`). Reinstall does not rotate it.
3. **Copies** `hooks/devin-gates.py` into `~/.config/devin/hooks/`. A symlink back to this repo would let a later edit of the lock file bypass itself.
4. Records an install hash and the real path it copied from.
5. **Symlinks** each skill and agent into `~/.config/devin/` so slash commands are `/design`, not `/devin-skills:design`. Refuses to clobber a real file unless you pass `--force`.
6. Backs up `config.json` to `config.json.bak-devin-skills-<timestamp>`, then merges [hooks/hook-entries.json](../hooks/hook-entries.json):
   - Unknown top-level keys (`agent`, `devin`, `shell`, `theme_mode`, `version`, …) stay.
   - One gate dispatcher per event. Missing events (`PostCompaction`, `SessionEnd`) are created.
   - herdr entries stay first; herdr command strings are not rewritten.
   - `PermissionRequest` is left to herdr. The gate is not added there.
   - The installed command path is the **copied** gate, not the repo path.
7. Merges [rules/AGENTS.md](../rules/AGENTS.md) into `~/.config/devin/AGENTS.md` inside `<!-- devin-skills:begin -->` … `<!-- devin-skills:end -->`.

Second install is safe to re-run: no duplicate hook entries, secret unchanged, gate copy and hashes refreshed.

## State directory

Same rules as the gate script:

1. `DEVIN_SKILLS_STATE_DIR` if set
2. Else `$XDG_DATA_HOME/devin-skills` if `XDG_DATA_HOME` is set
3. Else `~/.local/share/devin-skills`

## Flags

| Flag | Meaning |
| --- | --- |
| `--prefix DIR` | Install root (default `~/.config/devin`) |
| `--src DIR` | Repo root to copy/symlink from (default: directory of `install.sh`) |
| `--force` | Replace a non-symlink skill/agent destination |
| `--project` | Also write `.devin/hooks.v1.json` in the current directory (see below) |
| `--help` | Usage |

## herdr

`~/.config/devin/config.json` may already wire `herdr-agent-state.sh` on several events. Those hooks report pane/session identity and always exit 0. Replacing that file would break herdr, so this installer never does.

Summary:

- Backs up `config.json` first.
- Appends **one** gate dispatcher per event if that event does not already mention `devin-gates.py`.
- Leaves herdr command strings **byte-identical**. herdr stays first.
- Does **not** add a gate hook on `PermissionRequest`.
- Never edits `herdr-agent-state.sh`.

Uninstall drops only `devin-gates.py` elements. herdr remains.

## Project install

User-level is the default so the lock applies to every repo.

To also lock the current project:

```sh
sh install.sh --project
```

That writes `.devin/hooks.v1.json` (Devin project hooks format) with **gate entries only** — no herdr, no wrapping `"hooks"` object. Skills and agents are symlinked under `.devin/` when present. The gate binary and secret still live in the user prefix. User `config.json` is not merged in this mode.

## After install

Dogfood gate-script changes with `/gate-bypass` or `DEVIN_GATES_OFF=1` in the shell that starts `devin`, then re-run `install.sh` (copy + refresh hash). Do not edit the installed copy from a locked session.

Hunt lists for the skeptics live in `vendor/agent-hints/`. If you keep a clone of the upstream hints at `~/git/agent-skills`, run `sh scripts/refresh-vendor.sh` after they update.

## Tests

```sh
python3 -m unittest tests.test_install_merge tests.test_gate -v
```

`tests/test_install_merge.py` uses a temp `HOME` / XDG tree and a fixture copied from a real herdr `config.json`. It must never write the live `~/.config/devin`.

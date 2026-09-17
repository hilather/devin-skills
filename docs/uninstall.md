# Uninstall

Removes our skill/agent symlinks, the `AGENTS.md` span, leftover gate files, and leftover `devin-gates.py` hook entries. It **never** touches `herdr-agent-state.sh` or herdr command strings.

```sh
sh uninstall.sh
```

Also drop leftover lock state (secret, audit log) from an older install:

```sh
sh uninstall.sh --purge
```

## What it does

1. Drops `config.json` hook elements whose `command` contains `devin-gates.py` (no-op if none remain).
2. Removes the `<!-- devin-skills:begin -->` … `<!-- devin-skills:end -->` span from `$PREFIX/AGENTS.md`. Other content is kept. The file is deleted only if nothing remains.
3. Removes leftover `$PREFIX/hooks/devin-gates.py` (and old goal/execute-plan copies) if present. Does not follow a symlink into this git repo and delete the source.
4. Unlinks `$PREFIX/skills/<name>` and `$PREFIX/agents/<name>.md` when they are symlinks to this repo. Real files are left alone.
5. `--purge` deletes the leftover state directory from the old lock. Without `--purge`, that directory is left in place if it still exists.

A second uninstall is a no-op success.

## Flags

| Flag | Meaning |
| --- | --- |
| `--prefix DIR` | Same as install (default `~/.config/devin`) |
| `--src DIR` | Repo root used to recognize our skill/agent symlinks |
| `--project` | Also unlink `.devin/` skills/agents and strip leftover `.devin/hooks.v1.json` gate entries |
| `--purge` | Delete leftover lock state under `$XDG_DATA_HOME/devin-skills` (or `~/.local/share/devin-skills`) |
| `--help` | Usage |

## If a leftover-lock backup exists

Restore the timestamped backup instead of editing by hand:

```sh
cp ~/.config/devin/config.json.bak-devin-skills-<timestamp> ~/.config/devin/config.json
```

Uninstall does not restore that backup automatically and does not delete backups. Those backups were created when the lock was **installed**; restoring one would put the lock back.

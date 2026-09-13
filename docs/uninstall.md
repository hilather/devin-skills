# Uninstall

Removes Devin Skills hook entries, the copied gate, our skill/agent symlinks, and the `AGENTS.md` span. It **never** touches `herdr-agent-state.sh` or herdr command strings.

```sh
sh uninstall.sh
```

Also drop the secret, audit log, and session state:

```sh
sh uninstall.sh --purge
```

## What it does

1. Drops only `config.json` hook elements whose `command` contains `devin-gates.py`.
2. Deletes an event key if its array is then empty — any event the install created goes away (`PostCompaction` / `SessionEnd` with herdr present; all seven on a fresh config). herdr's events stay.
3. Removes the `<!-- devin-skills:begin -->` … `<!-- devin-skills:end -->` span from `$PREFIX/AGENTS.md`. Other content is kept. The file is deleted only if nothing remains.
4. Removes `$PREFIX/hooks/devin-gates.py` and `$PREFIX/hooks/devin_gates_goal.py` (the copied files or a leftover symlink). Does not follow a symlink into this git repo and delete the source.
5. Unlinks `$PREFIX/skills/<name>` and `$PREFIX/agents/<name>.md` only when they are symlinks to this repo. Real files are left alone.
6. `--purge` deletes the state directory (secret, `install-hash`, `source_realpath`, `audit.jsonl`, `current_session`, sessions, `goals/`). Without `--purge`, state is left in place so a reinstall can reuse the same secret.

A second uninstall is a no-op success.

## Flags

| Flag | Meaning |
| --- | --- |
| `--prefix DIR` | Same as install (default `~/.config/devin`) |
| `--src DIR` | Repo root used to recognize our skill/agent symlinks |
| `--project` | Also strip gate entries from `.devin/hooks.v1.json` in the current directory |
| `--purge` | Delete the state directory |
| `--help` | Usage |

State directory resolution matches [install.md](install.md#state-directory).

## If merge went wrong

Restore the timestamped backup instead of editing by hand:

```sh
cp ~/.config/devin/config.json.bak-devin-skills-<timestamp> ~/.config/devin/config.json
```

Uninstall does not restore that backup automatically and does not delete backups.

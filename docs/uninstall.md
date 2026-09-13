# Uninstall

Removes only Devin Skills hook entries, the copied gate, our skill/agent symlinks, and the `AGENTS.md` span. **Never** touches `herdr-agent-state.sh` or herdr command strings.

```sh
sh uninstall.sh
# drop secret + audit + session state as well:
sh uninstall.sh --purge
```

## What it does

1. Drops only `config.json` hook elements whose `command` contains `devin-gates.py`.
2. Deletes an event key if its array is then empty (`PostCompaction` / `SessionEnd` go away if we created them). herdr’s six events stay.
3. Removes the `<!-- devin-skills:begin -->` … `<!-- devin-skills:end -->` span from `$PREFIX/AGENTS.md`. Other content is kept; the file is deleted only if nothing remains.
4. Removes `$PREFIX/hooks/devin-gates.py` (the copied file or a leftover symlink). Does not follow a symlink into the git repo and delete the source.
5. Unlinks `$PREFIX/skills/<name>` and `$PREFIX/agents/<name>.md` only when they are symlinks to this repo. Leaves real files alone.
6. `--purge` deletes `$STATE_DIR` (secret, `install-hash`, `source_realpath`, `audit.jsonl`, sessions). Without `--purge`, state is left in place.

Idempotent: a second uninstall is a no-op success.

## Flags

| Flag | Meaning |
| --- | --- |
| `--prefix DIR` | Same as install (default `~/.config/devin`) |
| `--src DIR` | Repo root used to recognize our skill/agent symlinks |
| `--project` | Also strip gate entries from `.devin/hooks.v1.json` in the current directory |
| `--purge` | Delete `$STATE_DIR` |
| `--help` | Usage |

State directory resolution matches install / `devin-gates.py`.

## If merge went wrong

Restore the timestamped backup instead of editing by hand:

```sh
cp ~/.config/devin/config.json.bak-devin-skills-<timestamp> ~/.config/devin/config.json
```

Uninstall does not restore that backup automatically and does not delete backups.

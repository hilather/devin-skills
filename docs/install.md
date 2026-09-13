# Install

User-level install merges the write-lock **beside** existing herdr hooks. It never replaces `config.json` wholesale and never edits `herdr-agent-state.sh`.

```sh
sh install.sh
# or:
sh install.sh --prefix ~/.config/devin --src /path/to/devin-skills
```

Requires `python3`. Default prefix is `$HOME/.config/devin` (not `XDG_CONFIG_HOME`).

## What it does

1. Creates `$PREFIX/{skills,agents,hooks}` and the state directory (`0700`).
2. Generates `$STATE_DIR/secret` once (`os.urandom` 32 bytes, mode `0600`). Reinstall does not overwrite it.
3. **Copies** (does not symlink) `hooks/devin-gates.py` to `$PREFIX/hooks/devin-gates.py`. A live symlink of the lock into a writable repo would be an after-`plan-passed` self-edit bypass.
4. Writes `$STATE_DIR/install-hash` (sha256 of the installed copy) and `$STATE_DIR/source_realpath` (`os.path.realpath` of the file copied from).
5. Symlinks each `skills/<name>/` and `agents/<name>.md` into `$PREFIX` when those directories exist in the repo. Missing `skills/` or `agents/` is skipped. Refuses to clobber a non-symlink without `--force`.
6. Backs up `$PREFIX/config.json` to `config.json.bak-devin-skills-<timestamp>`, then merges `hooks/hook-entries.json`:
   - Preserve unknown top-level keys (`agent`, `devin`, `shell`, `theme_mode`, `version`, …).
   - One dispatcher per event. If the event is missing, create it (`PostCompaction` and `SessionEnd` are not in the current herdr config).
   - Else append the gate element iff no existing command on that event contains `devin-gates.py`.
   - herdr entries stay first; herdr command strings are not rewritten.
   - `PermissionRequest` is not given a gate hook.
   - Installed command path is the **copied** gate, not the repo path.
7. Merges `rules/AGENTS.md` into `$PREFIX/AGENTS.md` inside `<!-- devin-skills:begin -->` … `<!-- devin-skills:end -->`, replacing only that span.

Second install is idempotent: no duplicate gate entries, secret unchanged, gate copy and hashes refreshed.

## State directory

Same resolution as `devin-gates.py`:

1. `DEVIN_SKILLS_STATE_DIR` if set
2. Else `$XDG_DATA_HOME/devin-skills` if `XDG_DATA_HOME` is set
3. Else `~/.local/share/devin-skills`

## Flags

| Flag | Meaning |
| --- | --- |
| `--prefix DIR` | Install root (default `~/.config/devin`) |
| `--src DIR` | Repo root to copy/symlink from (default: directory of `install.sh`) |
| `--force` | Replace a non-symlink skill/agent destination |
| `--project` | Write `.devin/hooks.v1.json` in the current directory with **gate entries only** (no herdr, no `"hooks"` wrapper). Still copies the gate and secret into the user prefix. Does not merge user `config.json`. |
| `--help` | Usage |

## After install

In Devin, run `/hooks` and confirm both `herdr-agent-state.sh` and `devin-gates.py` are listed.

```sh
python3 -m unittest tests.test_install_merge tests.test_gate -v
```

Dogfood gate-script changes with `/gate-bypass` or `DEVIN_GATES_OFF=1` in the shell that starts `devin`, then re-run `install.sh` (copy + refresh hash). Do not edit the installed copy from a locked session.

Keep `~/git/agent-skills` cloned for hunt lists; `sh scripts/refresh-vendor.sh` after hint updates (that script ships in a later PR).

## Project install

Optional. User-level remains the default so the lock applies to every repo.

```sh
sh install.sh --project
```

`.devin/hooks.v1.json` is the entire hooks object (Devin format). Skills/agents are symlinked under `.devin/` when present.

## Tests

`tests/test_install_merge.py` uses a temp `HOME` / XDG tree and a fixture copied from the real six-event herdr `config.json`. It must never write the live `~/.config/devin`.

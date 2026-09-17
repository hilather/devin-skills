#!/bin/sh
# Remove our skill/agent symlinks, AGENTS.md span, and leftover gate files/hooks.
set -eu

usage() {
  cat <<'EOF'
Usage: sh uninstall.sh [--prefix DIR] [--src DIR] [--project] [--purge] [--help]

  --prefix DIR   Install root (default: $HOME/.config/devin)
  --src DIR      Repo root (default: directory of this script)
  --project      Also unlink skills/agents under .devin/ and strip leftover gates
  --purge        Delete leftover $STATE_DIR (old secret/audit from the lock)
  --help         Show this help

Never touches herdr-agent-state.sh.
EOF
}

PREFIX=""
SRC=""
PROJECT=0
PURGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)
      [ $# -ge 2 ] || { echo "uninstall.sh: --prefix needs an argument" >&2; exit 1; }
      PREFIX=$2
      shift 2
      ;;
    --src)
      [ $# -ge 2 ] || { echo "uninstall.sh: --src needs an argument" >&2; exit 1; }
      SRC=$2
      shift 2
      ;;
    --project)
      PROJECT=1
      shift
      ;;
    --purge)
      PURGE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "uninstall.sh: unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ -z "${HOME:-}" ]; then
  HOME=$(python3 -c 'import os; print(os.path.expanduser("~"))')
  export HOME
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -z "$SRC" ]; then
  SRC=$SCRIPT_DIR
fi
if [ -z "$PREFIX" ]; then
  PREFIX=$HOME/.config/devin
fi

if [ -n "${DEVIN_SKILLS_STATE_DIR:-}" ]; then
  STATE_DIR=$DEVIN_SKILLS_STATE_DIR
elif [ -n "${XDG_DATA_HOME:-}" ]; then
  STATE_DIR=$XDG_DATA_HOME/devin-skills
else
  STATE_DIR=$HOME/.local/share/devin-skills
fi

command -v python3 >/dev/null 2>&1 || {
  echo "uninstall.sh: python3 is required" >&2
  exit 1
}

export DEVIN_SKILLS_INSTALL_PREFIX="$PREFIX"
export DEVIN_SKILLS_INSTALL_SRC="$SRC"
export DEVIN_SKILLS_INSTALL_STATE_DIR="$STATE_DIR"
export DEVIN_SKILLS_INSTALL_PROJECT="$PROJECT"
export DEVIN_SKILLS_INSTALL_PROJECT_DIR="${DEVIN_SKILLS_PROJECT_DIR:-$(pwd)}"
export DEVIN_SKILLS_INSTALL_PURGE="$PURGE"

python3 - <<'UNINSTALL_PY'
from __future__ import annotations

import json
import os
import shutil
import stat
import sys

PREFIX = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_PREFIX"])
SRC = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_SRC"])
STATE_DIR = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_STATE_DIR"])
PROJECT = os.environ.get("DEVIN_SKILLS_INSTALL_PROJECT") == "1"
PURGE = os.environ.get("DEVIN_SKILLS_INSTALL_PURGE") == "1"
PROJECT_DIR = os.path.realpath(os.environ.get("DEVIN_SKILLS_INSTALL_PROJECT_DIR") or os.getcwd())
BEGIN = "<!-- devin-skills:begin -->"
END = "<!-- devin-skills:end -->"
LEFTOVER_GATES = (
    "devin-gates.py",
    "devin_gates_goal.py",
    "devin_gates_execplan.py",
    "devin_execplan_validate_plan.py",
)


def fail(msg):
    sys.stderr.write("uninstall.sh: %s\n" % msg)
    sys.exit(1)


def iter_commands(obj):
    if isinstance(obj, dict):
        cmd = obj.get("command")
        if isinstance(cmd, str):
            yield cmd
        for key, val in obj.items():
            if key == "command":
                continue
            yield from iter_commands(val)
    elif isinstance(obj, list):
        for val in obj:
            yield from iter_commands(val)


def has_gates_command(obj):
    return any("devin-gates.py" in cmd for cmd in iter_commands(obj))


def unmerge_hooks_obj(hooks):
    if not isinstance(hooks, dict):
        return hooks, False
    changed = False
    for event in list(hooks.keys()):
        val = hooks[event]
        if not isinstance(val, list):
            continue
        kept = [el for el in val if not has_gates_command(el)]
        if len(kept) == len(val):
            continue
        changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return hooks, changed


def existing_mode(path, default):
    try:
        st = os.lstat(path)
    except OSError:
        return default
    if stat.S_ISLNK(st.st_mode):
        return default
    return stat.S_IMODE(st.st_mode)


def atomic_write_regular(path, data, mode):
    """Replace path with a regular file; never write through a dest symlink."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = path + ".tmp-devin-skills"
    if os.path.islink(tmp) or os.path.isfile(tmp):
        os.remove(tmp)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, mode)
    try:
        os.write(fd, data)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    os.chmod(path, mode)


def dump_json(path, obj):
    mode = existing_mode(path, 0o600)
    payload = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    atomic_write_regular(path, payload, mode)


def unmerge_user_config():
    config_path = os.path.join(PREFIX, "config.json")
    if not os.path.isfile(config_path):
        return
    with open(config_path, "r", encoding="utf-8") as fh:
        try:
            cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            fail("cannot parse %s: %s" % (config_path, exc))
    if not isinstance(cfg, dict):
        return
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return
    cfg["hooks"], changed = unmerge_hooks_obj(hooks)
    if not changed:
        return
    dump_json(config_path, cfg)


def unmerge_project_hooks():
    path = os.path.join(PROJECT_DIR, ".devin", "hooks.v1.json")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        try:
            hooks = json.load(fh)
        except json.JSONDecodeError as exc:
            fail("cannot parse %s: %s" % (path, exc))
    if not isinstance(hooks, dict):
        return
    hooks, changed = unmerge_hooks_obj(hooks)
    if not hooks:
        os.remove(path)
    elif changed:
        dump_json(path, hooks)


def restore_agents_md(path):
    if not (os.path.islink(path) or os.path.isfile(path)):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return
    start = text.find(BEGIN)
    end = text.find(END)
    if start == -1 or end == -1 or end < start:
        return
    end_at = end + len(END)
    text = text[:start] + text[end_at:]
    if not text.strip():
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        return
    mode = existing_mode(path, 0o644)
    atomic_write_regular(path, text, mode)


def is_our_symlink(dest, src):
    if not os.path.islink(dest):
        return False
    target = os.readlink(dest)
    src_abs = os.path.abspath(src)
    src_real = os.path.realpath(src) if os.path.lexists(src) else src_abs
    dest_real = os.path.realpath(dest) if os.path.lexists(dest) else ""
    return os.path.normpath(target) in (src_abs, src_real) or dest_real == src_real


def unlink_skills_agents(dest_root):
    skills_src = os.path.join(SRC, "skills")
    agents_src = os.path.join(SRC, "agents")
    dest_skills = os.path.join(dest_root, "skills")
    dest_agents = os.path.join(dest_root, "agents")
    if os.path.isdir(dest_skills):
        for name in os.listdir(dest_skills):
            dest = os.path.join(dest_skills, name)
            src = os.path.join(skills_src, name)
            if is_our_symlink(dest, src) or (
                os.path.islink(dest) and SRC in os.path.normpath(os.readlink(dest))
            ):
                os.remove(dest)
    if os.path.isdir(dest_agents):
        for name in os.listdir(dest_agents):
            dest = os.path.join(dest_agents, name)
            src = os.path.join(agents_src, name)
            if is_our_symlink(dest, src) or (
                os.path.islink(dest) and SRC in os.path.normpath(os.readlink(dest))
            ):
                os.remove(dest)


def remove_copied_gate():
    hooks_dir = os.path.join(PREFIX, "hooks")
    for name in LEFTOVER_GATES:
        path = os.path.join(hooks_dir, name)
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)


def main():
    herdr = os.path.join(PREFIX, "herdr-agent-state.sh")
    herdr_existed = os.path.lexists(herdr)
    unmerge_user_config()
    if PROJECT:
        unmerge_project_hooks()
        unlink_skills_agents(os.path.join(PROJECT_DIR, ".devin"))
    unlink_skills_agents(PREFIX)
    restore_agents_md(os.path.join(PREFIX, "AGENTS.md"))
    remove_copied_gate()
    if herdr_existed and not os.path.lexists(herdr):
        fail("refusing to continue: herdr-agent-state.sh disappeared")
    if PURGE and os.path.isdir(STATE_DIR):
        shutil.rmtree(STATE_DIR)
    sys.stdout.write("Uninstall complete. herdr-agent-state.sh was not modified.\n")
    if not PURGE and os.path.isdir(STATE_DIR):
        sys.stdout.write("Leftover lock state in %s (pass --purge to delete).\n" % STATE_DIR)


if __name__ == "__main__":
    main()
UNINSTALL_PY

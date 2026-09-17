#!/bin/sh
# User-level install: symlink skills/agents, merge AGENTS.md, strip leftover gate hooks.
set -eu

usage() {
  cat <<'EOF'
Usage: sh install.sh [--prefix DIR] [--src DIR] [--force] [--project] [--help]

  --prefix DIR   Install root (default: $HOME/.config/devin)
  --src DIR      Repo root (default: directory of this script)
  --force        Replace a non-symlink skill/agent destination
  --project      Also symlink skills/agents under .devin/ in the current directory
  --help         Show this help

Re-running this installer removes leftover devin-gates.py hook entries from
config.json (and from .devin/hooks.v1.json with --project).
EOF
}

PREFIX=""
SRC=""
FORCE=0
PROJECT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)
      [ $# -ge 2 ] || { echo "install.sh: --prefix needs an argument" >&2; exit 1; }
      PREFIX=$2
      shift 2
      ;;
    --src)
      [ $# -ge 2 ] || { echo "install.sh: --src needs an argument" >&2; exit 1; }
      SRC=$2
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --project)
      PROJECT=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "install.sh: unknown argument: $1" >&2
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

command -v python3 >/dev/null 2>&1 || {
  echo "install.sh: python3 is required" >&2
  exit 1
}

[ -d "$SRC/skills" ] || {
  echo "install.sh: missing $SRC/skills" >&2
  exit 1
}

export DEVIN_SKILLS_INSTALL_PREFIX="$PREFIX"
export DEVIN_SKILLS_INSTALL_SRC="$SRC"
export DEVIN_SKILLS_INSTALL_FORCE="$FORCE"
export DEVIN_SKILLS_INSTALL_PROJECT="$PROJECT"
export DEVIN_SKILLS_INSTALL_PROJECT_DIR="${DEVIN_SKILLS_PROJECT_DIR:-$(pwd)}"

python3 - <<'INSTALL_PY'
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import time

PREFIX = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_PREFIX"])
SRC = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_SRC"])
FORCE = os.environ.get("DEVIN_SKILLS_INSTALL_FORCE") == "1"
PROJECT = os.environ.get("DEVIN_SKILLS_INSTALL_PROJECT") == "1"
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
    sys.stderr.write("install.sh: %s\n" % msg)
    sys.exit(1)


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


def backup_config(config_path):
    ts = time.strftime("%Y%m%d%H%M%S")
    bak = os.path.join(os.path.dirname(config_path), "config.json.bak-devin-skills-%s" % ts)
    if os.path.exists(bak):
        bak = "%s-%s" % (bak, os.getpid())
    shutil.copy2(config_path, bak)
    sys.stdout.write("Backup: %s\n" % bak)


def strip_user_config_gates():
    config_path = os.path.join(PREFIX, "config.json")
    if not os.path.isfile(config_path):
        return
    with open(config_path, "r", encoding="utf-8") as fh:
        try:
            cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            fail("cannot parse %s: %s" % (config_path, exc))
    if not isinstance(cfg, dict):
        fail("%s is not a JSON object" % config_path)
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return
    cfg["hooks"], changed = unmerge_hooks_obj(hooks)
    if not changed:
        return
    backup_config(config_path)
    dump_json(config_path, cfg)
    sys.stdout.write("Removed leftover gate hooks from %s\n" % config_path)


def strip_project_hooks():
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
    if not changed:
        return
    if not hooks:
        os.remove(path)
        sys.stdout.write("Removed leftover project gate file: %s\n" % path)
    else:
        dump_json(path, hooks)
        sys.stdout.write("Removed leftover gate hooks from %s\n" % path)


def remove_copied_gate():
    hooks_dir = os.path.join(PREFIX, "hooks")
    for name in LEFTOVER_GATES:
        path = os.path.join(hooks_dir, name)
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
            sys.stdout.write("Removed leftover %s\n" % path)


def ensure_symlink(src, dest):
    src = os.path.realpath(src)
    dest_dir = os.path.dirname(dest)
    os.makedirs(dest_dir, exist_ok=True)
    if os.path.lexists(dest) or os.path.islink(dest):
        if os.path.islink(dest) and os.path.realpath(dest) == src:
            os.remove(dest)
        elif FORCE:
            if os.path.isdir(dest) and not os.path.islink(dest):
                shutil.rmtree(dest)
            else:
                os.remove(dest)
        else:
            fail("refusing to clobber %s (not our symlink); pass --force" % dest)
    os.symlink(src, dest)


def is_our_symlink(dest, src):
    if not os.path.islink(dest):
        return False
    target = os.readlink(dest)
    src_abs = os.path.abspath(src)
    src_real = os.path.realpath(src) if os.path.lexists(src) else src_abs
    dest_real = os.path.realpath(dest) if os.path.lexists(dest) else ""
    return os.path.normpath(target) in (src_abs, src_real) or dest_real == src_real


def prune_stale_symlinks(dest_root):
    for kind in ("skills", "agents"):
        dest_dir = os.path.join(dest_root, kind)
        src_dir = os.path.join(SRC, kind)
        if not os.path.isdir(dest_dir):
            continue
        for name in os.listdir(dest_dir):
            dest = os.path.join(dest_dir, name)
            src = os.path.join(src_dir, name)
            if not os.path.islink(dest):
                continue
            if os.path.lexists(src):
                continue
            if is_our_symlink(dest, src) or SRC in os.path.normpath(os.readlink(dest)):
                os.remove(dest)


def link_skills_agents(dest_root):
    os.makedirs(os.path.join(dest_root, "skills"), exist_ok=True)
    os.makedirs(os.path.join(dest_root, "agents"), exist_ok=True)
    skills_src = os.path.join(SRC, "skills")
    agents_src = os.path.join(SRC, "agents")
    if os.path.isdir(skills_src):
        for name in sorted(os.listdir(skills_src)):
            if name.startswith("."):
                continue
            src = os.path.join(skills_src, name)
            if not os.path.isdir(src):
                continue
            ensure_symlink(src, os.path.join(dest_root, "skills", name))
    if os.path.isdir(agents_src):
        for name in sorted(os.listdir(agents_src)):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            src = os.path.join(agents_src, name)
            if not os.path.isfile(src):
                continue
            ensure_symlink(src, os.path.join(dest_root, "agents", name))
    prune_stale_symlinks(dest_root)


def wrap_agents(content):
    body = content.strip("\n")
    return BEGIN + "\n" + body + "\n" + END + "\n"


def merge_agents_md(dest_path, content):
    block = wrap_agents(content)
    mode = existing_mode(dest_path, 0o644)
    text = None
    if os.path.islink(dest_path) or os.path.isfile(dest_path):
        try:
            with open(dest_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
    elif os.path.lexists(dest_path):
        fail("refusing to clobber %s" % dest_path)
    if text is None:
        atomic_write_regular(dest_path, block, mode)
        return
    start = text.find(BEGIN)
    end = text.find(END)
    if start != -1 and end != -1 and end > start:
        end_at = end + len(END)
        text = text[:start] + block.rstrip("\n") + text[end_at:]
        if not text.endswith("\n"):
            text += "\n"
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        text += block
    atomic_write_regular(dest_path, text, mode)


def main():
    os.makedirs(os.path.join(PREFIX, "skills"), exist_ok=True)
    os.makedirs(os.path.join(PREFIX, "agents"), exist_ok=True)
    strip_user_config_gates()
    remove_copied_gate()
    link_skills_agents(PREFIX)
    rules = os.path.join(SRC, "rules", "AGENTS.md")
    if os.path.isfile(rules):
        with open(rules, "r", encoding="utf-8") as fh:
            merge_agents_md(os.path.join(PREFIX, "AGENTS.md"), fh.read())
    if PROJECT:
        dest_root = os.path.join(PROJECT_DIR, ".devin")
        os.makedirs(dest_root, exist_ok=True)
        strip_project_hooks()
        link_skills_agents(dest_root)
    sys.stdout.write("Installed skills/agents under %s\n" % PREFIX)
    sys.stdout.write("Commands: /design  (Devin's builtin /plan is unchanged)\n")
    sys.stdout.write("Tests: python3 -m unittest tests.test_install_merge -v\n")


if __name__ == "__main__":
    main()
INSTALL_PY

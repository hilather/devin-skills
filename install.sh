#!/bin/sh
# User-level install: copy the gate, merge one dispatcher per event beside herdr.
set -eu

usage() {
  cat <<'EOF'
Usage: sh install.sh [--prefix DIR] [--src DIR] [--force] [--project] [--help]

  --prefix DIR   Install root (default: $HOME/.config/devin)
  --src DIR      Repo root (default: directory of this script)
  --force        Replace a non-symlink skill/agent destination
  --project      Write .devin/hooks.v1.json (gate only); skip user config merge
  --help         Show this help
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

if [ -n "${DEVIN_SKILLS_STATE_DIR:-}" ]; then
  STATE_DIR=$DEVIN_SKILLS_STATE_DIR
elif [ -n "${XDG_DATA_HOME:-}" ]; then
  STATE_DIR=$XDG_DATA_HOME/devin-skills
else
  STATE_DIR=$HOME/.local/share/devin-skills
fi

command -v python3 >/dev/null 2>&1 || {
  echo "install.sh: python3 is required" >&2
  exit 1
}

[ -f "$SRC/hooks/devin-gates.py" ] || {
  echo "install.sh: missing $SRC/hooks/devin-gates.py" >&2
  exit 1
}
[ -f "$SRC/hooks/hook-entries.json" ] || {
  echo "install.sh: missing $SRC/hooks/hook-entries.json" >&2
  exit 1
}

mkdir -p "$PREFIX/skills" "$PREFIX/agents" "$PREFIX/hooks"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

INSTALLED_GATE=$PREFIX/hooks/devin-gates.py
if [ -L "$INSTALLED_GATE" ]; then
  # cp would write through a symlink into the repo
  rm -f "$INSTALLED_GATE"
fi
cp "$SRC/hooks/devin-gates.py" "$INSTALLED_GATE"

export DEVIN_SKILLS_INSTALL_PREFIX="$PREFIX"
export DEVIN_SKILLS_INSTALL_SRC="$SRC"
export DEVIN_SKILLS_INSTALL_STATE_DIR="$STATE_DIR"
export DEVIN_SKILLS_INSTALL_GATE="$INSTALLED_GATE"
export DEVIN_SKILLS_INSTALL_FORCE="$FORCE"
export DEVIN_SKILLS_INSTALL_PROJECT="$PROJECT"
export DEVIN_SKILLS_INSTALL_PROJECT_DIR="${DEVIN_SKILLS_PROJECT_DIR:-$(pwd)}"

python3 - <<'INSTALL_PY'
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import time

PREFIX = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_PREFIX"])
SRC = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_SRC"])
STATE_DIR = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_STATE_DIR"])
INSTALLED = os.path.realpath(os.environ["DEVIN_SKILLS_INSTALL_GATE"])
FORCE = os.environ.get("DEVIN_SKILLS_INSTALL_FORCE") == "1"
PROJECT = os.environ.get("DEVIN_SKILLS_INSTALL_PROJECT") == "1"
PROJECT_DIR = os.path.realpath(os.environ.get("DEVIN_SKILLS_INSTALL_PROJECT_DIR") or os.getcwd())
BEGIN = "<!-- devin-skills:begin -->"
END = "<!-- devin-skills:end -->"
SKIP_EVENTS = {"PermissionRequest"}


def fail(msg):
    sys.stderr.write("install.sh: %s\n" % msg)
    sys.exit(1)


def write_mode(path, data, mode, binary=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, mode)
    try:
        if binary:
            os.write(fd, data)
        else:
            os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, mode)


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


def shell_single_quote(path):
    return "'" + path.replace("'", "'\\''") + "'"


def rewrite_gate_element(element, installed):
    el = copy.deepcopy(element)
    new_cmd = "python3 %s hook" % shell_single_quote(installed)

    def walk(obj):
        if isinstance(obj, dict):
            cmd = obj.get("command")
            if isinstance(cmd, str) and "devin-gates.py" in cmd:
                obj["command"] = new_cmd
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for val in obj:
                walk(val)

    walk(el)
    return el


def merge_hooks_obj(hooks, entries, installed):
    if not isinstance(hooks, dict):
        hooks = {}
    for event, elements in entries.items():
        if event in SKIP_EVENTS:
            continue
        if not isinstance(elements, list):
            continue
        rewritten = [rewrite_gate_element(el, installed) for el in elements]
        existing = hooks.get(event)
        if not existing:
            hooks[event] = rewritten
        elif has_gates_command(existing):
            continue
        else:
            if not isinstance(existing, list):
                existing = [existing]
                hooks[event] = existing
            existing.extend(rewritten)
    return hooks


def dump_json(path, obj):
    tmp = path + ".tmp-devin-skills"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


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


def link_skills_agents(dest_root):
    skills_src = os.path.join(SRC, "skills")
    agents_src = os.path.join(SRC, "agents")
    if os.path.isdir(skills_src):
        dest_skills = os.path.join(dest_root, "skills")
        os.makedirs(dest_skills, exist_ok=True)
        for name in sorted(os.listdir(skills_src)):
            if name.startswith("."):
                continue
            src = os.path.join(skills_src, name)
            if not os.path.isdir(src):
                continue
            ensure_symlink(src, os.path.join(dest_skills, name))
    if os.path.isdir(agents_src):
        dest_agents = os.path.join(dest_root, "agents")
        os.makedirs(dest_agents, exist_ok=True)
        for name in sorted(os.listdir(agents_src)):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            src = os.path.join(agents_src, name)
            if not os.path.isfile(src):
                continue
            ensure_symlink(src, os.path.join(dest_agents, name))


def wrap_agents(content):
    body = content.strip("\n")
    return BEGIN + "\n" + body + "\n" + END + "\n"


def merge_agents_md(dest_path, content):
    block = wrap_agents(content)
    if not os.path.exists(dest_path):
        write_mode(dest_path, block, 0o644)
        return
    with open(dest_path, "r", encoding="utf-8") as fh:
        text = fh.read()
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
    write_mode(dest_path, text, 0o644)


def ensure_secret():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    path = os.path.join(STATE_DIR, "secret")
    if os.path.exists(path):
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, os.urandom(32))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def write_hash_and_source():
    if not os.path.isfile(INSTALLED):
        fail("installed gate missing: %s" % INSTALLED)
    with open(INSTALLED, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    write_mode(os.path.join(STATE_DIR, "install-hash"), digest + "\n", 0o600)
    source = os.path.realpath(os.path.join(SRC, "hooks", "devin-gates.py"))
    write_mode(os.path.join(STATE_DIR, "source_realpath"), source + "\n", 0o600)


def backup_and_merge_user_config(entries):
    config_path = os.path.join(PREFIX, "config.json")
    if os.path.isfile(config_path):
        ts = time.strftime("%Y%m%d%H%M%S")
        bak = os.path.join(PREFIX, "config.json.bak-devin-skills-%s" % ts)
        if os.path.exists(bak):
            bak = "%s-%s" % (bak, os.getpid())
        shutil.copy2(config_path, bak)
        sys.stdout.write("Backup: %s\n" % bak)
        with open(config_path, "r", encoding="utf-8") as fh:
            try:
                cfg = json.load(fh)
            except json.JSONDecodeError as exc:
                fail("cannot parse %s: %s" % (config_path, exc))
        if not isinstance(cfg, dict):
            fail("%s is not a JSON object" % config_path)
    else:
        cfg = {}
    hooks = cfg.get("hooks")
    if hooks is None:
        hooks = {}
        cfg["hooks"] = hooks
    cfg["hooks"] = merge_hooks_obj(hooks, entries, INSTALLED)
    dump_json(config_path, cfg)


def merge_project_hooks(entries):
    dest_root = os.path.join(PROJECT_DIR, ".devin")
    os.makedirs(dest_root, exist_ok=True)
    path = os.path.join(dest_root, "hooks.v1.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            try:
                hooks = json.load(fh)
            except json.JSONDecodeError as exc:
                fail("cannot parse %s: %s" % (path, exc))
        if not isinstance(hooks, dict):
            fail("%s is not a JSON object" % path)
    else:
        hooks = {}
    dump_json(path, merge_hooks_obj(hooks, entries, INSTALLED))
    link_skills_agents(dest_root)


def main():
    gate_path = os.environ["DEVIN_SKILLS_INSTALL_GATE"]
    if os.path.islink(gate_path) or not os.path.isfile(INSTALLED):
        fail("gate must be a regular file copy at %s" % gate_path)

    entries_path = os.path.join(SRC, "hooks", "hook-entries.json")
    with open(entries_path, "r", encoding="utf-8") as fh:
        entries = json.load(fh)
    if not isinstance(entries, dict):
        fail("hook-entries.json must be an object")

    ensure_secret()
    write_hash_and_source()

    if PROJECT:
        merge_project_hooks(entries)
    else:
        link_skills_agents(PREFIX)
        backup_and_merge_user_config(entries)
        rules = os.path.join(SRC, "rules", "AGENTS.md")
        if os.path.isfile(rules):
            with open(rules, "r", encoding="utf-8") as fh:
                merge_agents_md(os.path.join(PREFIX, "AGENTS.md"), fh.read())

    sys.stdout.write("Copied gate: %s\n" % INSTALLED)
    sys.stdout.write("State dir: %s\n" % STATE_DIR)
    sys.stdout.write("In Devin, run /hooks to confirm herdr and devin-gates.py both appear.\n")
    sys.stdout.write("Tests: python3 -m unittest tests.test_install_merge tests.test_gate -v\n")


if __name__ == "__main__":
    main()
INSTALL_PY

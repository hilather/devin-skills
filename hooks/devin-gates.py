#!/usr/bin/env python3
"""Devin Skills write-lock dispatcher. Python 3 stdlib only."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import sys
import urllib.parse
from datetime import datetime, timezone

STOP_BLOCK_MAX = 3
STATE_VERSION = 1
GATE_SUBCOMMANDS = ("status", "mint-plan", "mint-code", "bypass", "allow-design")
SKEPTIC_PROFILES = ("plan-skeptic", "code-skeptic", "finding-skeptic")
LOCKED_SPAWN = ("plan-skeptic", "subagent_explore")
DESIGN_SPAWN = ("design-writer", "design-reviewer")
READONLY_PROFILES = set(SKEPTIC_PROFILES) | {"subagent_explore", "design-writer", "design-reviewer"}
MUTATING_FILE_TOOLS = ("write", "edit", "apply_patch", "notebook_edit")
SOURCE_FILE_TOOLS = ("write", "edit", "apply_patch", "notebook_edit", "write_to_process")
LOCKED_GIT = (
    "status",
    "log",
    "diff",
    "show",
    "rev-parse",
    "ls-files",
    "blame",
    "cat-file",
)
GIT_NON_SOURCE = LOCKED_GIT + ("add", "commit", "push", "fetch")
LOCKED_RO_BINS = ("ls", "cat", "head", "tail", "wc", "file", "stat", "rg", "grep")
TEST_RUNNER_MODS = ("pytest", "unittest")
SYSTEM_BIN_DIRS = (
    "/bin",
    "/usr/bin",
    "/usr/local/bin",
    "/sbin",
    "/usr/sbin",
    "/opt/homebrew/bin",
)
GIT_GLOBAL_TAKES_ARG = (
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--super-prefix",
    "--config-env",
    "--attr-source",
    "--exec-path",
)
GIT_GLOBAL_FLAGS = (
    "--no-pager",
    "--paginate",
    "--bare",
    "--no-replace-objects",
    "--no-optional-locks",
    "--literal-pathspecs",
    "--glob-pathspecs",
    "--noglob-pathspecs",
    "--icase-pathspecs",
    "--no-advice",
    "--no-lazy-fetch",
)
GIT_LOCKED_DENY = ("-c", "--exec-path", "--config-env")
PYTHON_TAKES_ARG = ("-W", "-X", "-m", "-Q", "--check-hash-based-pycs")
PATH_DELIM_CHARS = frozenset({os.sep, '"', "'", "\n", "\r", " ", "\t", ":"})
MCP_LIST = ("mcp_list_servers", "mcp_list_tools")
REMINDER_EVENTS = ("SessionStart", "UserPromptSubmit", "PostCompaction")
MUTATING_PRE_TOOLS = {
    "write",
    "edit",
    "apply_patch",
    "notebook_edit",
    "exec",
    "write_to_process",
    "run_subagent",
    "mcp_call_tool",
    "mcp_read_resource",
}
EXEC_METACHARS = ("$(", "${", "`", ">>", ">", "<", "|", ";", "&", "\n", "\r", "$")
VERDICT_RE = re.compile(r"^GATES_VERDICT: (PASS|FAIL|BLOCKED)\s*$", re.MULTILINE)
AGENT_ID_RE = re.compile(r"agent_id=([A-Za-z0-9._-]+)")
PYTHON_VER_RE = re.compile(r"^python[0-9.]*$")
SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HEX8_RE = re.compile(r"^[0-9a-f]{8}$")

WRITE_BLOCK_REASON = (
    "Write blocked: no plan-skeptic marker for this session.\n"
    "Run /skeptic-plan (or /gate-bypass with a reason). "
    "Override: DEVIN_GATES_OFF=1 in the shell that starts devin."
)
FAIL_CLOSED_REASON = "gate error (fail closed)"
PROFILE_KIND = {
    "plan-skeptic": "plan",
    "code-skeptic": "code",
    "finding-skeptic": "finding",
}


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def state_dir():
    env = os.environ.get("DEVIN_SKILLS_STATE_DIR")
    if env:
        return os.path.realpath(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.realpath(os.path.join(xdg, "devin-skills"))
    return os.path.realpath(os.path.expanduser("~/.local/share/devin-skills"))


def cache_home():
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.realpath(xdg)
    return os.path.realpath(os.path.expanduser("~/.cache"))


def design_parent():
    return os.path.join(cache_home(), "devin-skills", "design")


def gates_off():
    return os.environ.get("DEVIN_GATES_OFF") == "1"


def running_script():
    return os.path.realpath(__file__)


def read_source_realpath():
    path = os.path.join(state_dir(), "source_realpath")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read().strip()
    except OSError:
        return ""
    if not data:
        return ""
    line = data.splitlines()[0].strip()
    if not line:
        return ""
    rp = os.path.realpath(os.path.expanduser(line))
    if os.path.isfile(rp):
        return rp
    return ""


def secret_path():
    return os.path.join(state_dir(), "secret")


def load_secret():
    path = secret_path()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return b""
    return data


def hmac_hex(payload, secret=None):
    if secret is None:
        secret = load_secret()
    body = {k: v for k, v in payload.items() if k != "hmac"}
    msg = canonical_dumps(body).encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def default_state(session_id):
    return {
        "v": STATE_VERSION,
        "session_id": session_id or "",
        "project_dir": os.environ.get("DEVIN_PROJECT_DIR") or "",
        "mode": "normal",
        "plan_path": "",
        "plan_hash": "",
        "mutations": 0,
        "source_seq": 0,
        "stop_blocks": {},
        "sweeps": {"plan": 0, "code": 0, "finding": 0},
        "witnesses": [],
        "markers": [],
        "design_id": "",
        "design_allow_root": "",
        "override_reason": "",
    }


def sanitize_session_id(session_id):
    sid = session_id or ""
    if not sid or not SAFE_SESSION_RE.match(sid):
        return ""
    return sid


def session_dir(session_id):
    sid = sanitize_session_id(session_id)
    if not sid:
        return ""
    return os.path.join(state_dir(), "sessions", sid)


def session_state_path(session_id):
    d = session_dir(session_id)
    if not d:
        return ""
    return os.path.join(d, "state.json")


def atomic_write(path, data, mode=0o600):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def audit(session_id, event, tool_name, decision, reason):
    rec = {
        "ts": utc_now(),
        "session_id": session_id or "",
        "event": event or "",
        "tool_name": tool_name or "",
        "decision": decision or "",
        "reason": reason or "",
    }
    line = canonical_dumps(rec) + "\n"
    path = os.path.join(state_dir(), "audit.jsonl")
    try:
        os.makedirs(state_dir(), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def remember_session(session_id):
    sid = sanitize_session_id(session_id)
    if not sid:
        return
    path = os.path.join(state_dir(), "current_session")
    try:
        atomic_write(path, (sid + "\n").encode("utf-8"), 0o600)
    except OSError:
        pass


def verify_hmac(obj):
    secret = load_secret()
    if not secret or not isinstance(obj, dict):
        return False
    got = obj.get("hmac")
    if not isinstance(got, str) or not got:
        return False
    expect = hmac_hex(obj, secret)
    return hmac.compare_digest(got, expect)


def load_state(session_id):
    sid = sanitize_session_id(session_id)
    empty = default_state(sid)
    if not sid:
        return empty
    path = session_state_path(sid)
    if not path or not os.path.isfile(path):
        return empty
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        audit(sid, "forge", "", "block", "unreadable state.json")
        return empty
    if not isinstance(obj, dict) or not verify_hmac(obj) or obj.get("v") != STATE_VERSION:
        audit(sid, "forge", "", "block", "hmac/version mismatch")
        return empty
    return obj


def save_state(state):
    sid = sanitize_session_id(state.get("session_id") or "")
    if not sid:
        return False
    secret = load_secret()
    if not secret:
        return False
    payload = {k: v for k, v in state.items() if k != "hmac"}
    payload["session_id"] = sid
    payload["v"] = STATE_VERSION
    digest = hmac_hex(payload, secret)
    out = dict(payload)
    out["hmac"] = digest
    raw = canonical_dumps(out) + "\n"
    path = session_state_path(sid)
    if not path:
        return False
    atomic_write(path, raw.encode("utf-8"), 0o600)
    return True


def has_marker(state, kind):
    for marker in state.get("markers") or []:
        if isinstance(marker, dict) and marker.get("kind") == kind:
            return True
    return False


def add_marker(state, kind, subagent_id="", verdict="PASS"):
    markers = [m for m in (state.get("markers") or []) if not (isinstance(m, dict) and m.get("kind") == kind)]
    markers.append(
        {
            "kind": kind,
            "created_at": utc_now(),
            "plan_hash": state.get("plan_hash") or "",
            "subagent_id": subagent_id or "",
            "verdict": verdict,
        }
    )
    state["markers"] = markers


def clear_marker(state, kind):
    state["markers"] = [
        m for m in (state.get("markers") or []) if not (isinstance(m, dict) and m.get("kind") == kind)
    ]


def last_witness(state, kind):
    for wit in reversed(state.get("witnesses") or []):
        if isinstance(wit, dict) and wit.get("kind") == kind:
            return wit
    return None


def has_escape(state):
    if gates_off():
        return True
    return bool((state.get("override_reason") or "").strip())


def is_write_locked(state):
    if has_escape(state):
        return False
    if has_marker(state, "plan-passed"):
        return False
    return True


def bak_config_paths():
    cfg_dir = os.path.expanduser("~/.config/devin")
    out = []
    try:
        for name in os.listdir(cfg_dir):
            if name.startswith("config.json.bak-devin-skills-"):
                out.append(os.path.realpath(os.path.join(cfg_dir, name)))
    except OSError:
        pass
    return out


def protected_paths():
    paths = []
    paths.append(os.path.realpath(state_dir()))
    paths.append(os.path.realpath(os.path.expanduser("~/.config/devin/hooks")))
    paths.append(os.path.realpath(os.path.expanduser("~/.config/devin/config.json")))
    paths.extend(bak_config_paths())
    paths.append(running_script())
    src = read_source_realpath()
    if src:
        paths.append(src)
    cleaned = []
    seen = set()
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            cleaned.append(p)
    return cleaned


def realpath_candidate(token, cwd=None):
    if token is None:
        return ""
    text = str(token)
    if not text:
        return ""
    expanded = os.path.expanduser(text)
    if cwd and not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.realpath(expanded)


def path_hits(candidate, protected):
    if not candidate:
        return False
    cand = realpath_candidate(candidate)
    for prot in protected:
        if not prot:
            continue
        if cand == prot or cand.startswith(prot + os.sep):
            return True
    return False


def _string_embeds_protected(text, protected):
    if not text:
        return False
    for prot in protected:
        if not prot:
            continue
        if text == prot or text.startswith(prot + os.sep):
            return True
        if (prot + os.sep) in text:
            return True
        idx = 0
        while True:
            j = text.find(prot, idx)
            if j < 0:
                break
            end = j + len(prot)
            ok_end = end == len(text) or text[end] in PATH_DELIM_CHARS
            ok_start = j == 0 or text[j - 1] in PATH_DELIM_CHARS
            if ok_end and ok_start:
                return True
            idx = j + 1
    return False


def json_values_hit_protected(obj, protected):
    if isinstance(obj, dict):
        return any(json_values_hit_protected(v, protected) for v in obj.values())
    if isinstance(obj, list):
        return any(json_values_hit_protected(v, protected) for v in obj)
    if isinstance(obj, str):
        if path_hits(obj, protected):
            return True
        return _string_embeds_protected(obj, protected)
    return False


def raw_json_hits_protected(tool_input, protected):
    return json_values_hit_protected(tool_input or {}, protected)


def text_hits_protected(text, protected):
    if not text:
        return False
    blob = str(text)
    for prot in protected:
        if prot and prot in blob:
            return True
    return False


def extract_tool_paths(tool_name, tool_input):
    inp = tool_input or {}
    paths = []
    if tool_name in ("write", "edit", "read"):
        p = inp.get("file_path")
        if p:
            paths.append(p)
    if tool_name in ("notebook_edit", "notebook_read"):
        p = inp.get("notebook_path")
        if p:
            paths.append(p)
    if tool_name in ("grep", "glob", "find_file_by_name"):
        p = inp.get("path")
        if p:
            paths.append(p)
    if tool_name == "request_scope":
        p = inp.get("path")
        if p:
            paths.append(p)
    if tool_name == "exec":
        p = inp.get("workdir")
        if p:
            paths.append(p)
    if tool_name == "mcp_read_resource":
        uri = inp.get("resource_uri") or ""
        parsed = _file_uri_path(uri)
        if parsed:
            paths.append(parsed)
    return paths


def _file_uri_path(uri):
    text = str(uri or "").strip().strip("<>")
    if not text:
        return ""
    if text.startswith("file://"):
        parsed = urllib.parse.urlparse(text)
        return urllib.parse.unquote(parsed.path or "")
    if text.startswith("/") or text.startswith("~"):
        return text
    return ""


def plans_root():
    return os.path.realpath(os.path.expanduser("~/.devin/plans"))


def path_under(root, candidate):
    if not root or not candidate:
        return False
    rr = os.path.realpath(os.path.expanduser(root))
    cc = realpath_candidate(candidate)
    return cc == rr or cc.startswith(rr + os.sep)


def is_allowlisted_write_path(state, path):
    if not path:
        return False
    if path_under(plans_root(), path):
        return True
    root = (state.get("design_allow_root") or "").strip()
    if root and path_under(root, path):
        return True
    return False


def block(reason, session_id="", event="", tool_name=""):
    audit(session_id, event, tool_name, "block", reason.split("\n", 1)[0][:300])
    payload = {"decision": "block", "reason": reason}
    return canonical_dumps(payload), 0


def fail_closed(session_id="", event="", tool_name=""):
    audit(session_id, event, tool_name, "block", FAIL_CLOSED_REASON)
    payload = {"decision": "block", "reason": FAIL_CLOSED_REASON}
    return canonical_dumps(payload), 2


def allow():
    return "", 0


def is_mutating_pre(tool_name):
    name = tool_name or ""
    if name in MUTATING_PRE_TOOLS:
        return True
    if name.startswith("mcp__"):
        return True
    return False


def is_python_bin(name):
    return bool(name) and bool(PYTHON_VER_RE.match(name))


def argv0_basename(token):
    text = str(token or "")
    expanded = os.path.expanduser(text)
    if os.sep in expanded or os.path.exists(expanded):
        return os.path.basename(os.path.realpath(expanded))
    return os.path.basename(text)


def argv0_is_trusted_bin(token):
    text = str(token or "")
    if not text or text in (".", ".."):
        return False
    expanded = os.path.expanduser(text)
    if os.sep not in expanded:
        return True
    rp = os.path.realpath(expanded)
    parent = os.path.dirname(rp)
    system = {os.path.realpath(d) for d in SYSTEM_BIN_DIRS}
    return parent in system


def is_gate_cli(argv):
    if len(argv) < 3:
        return False
    if not is_python_bin(argv0_basename(argv[0])):
        return False
    script = os.path.realpath(os.path.expanduser(argv[1]))
    me = running_script()
    src = read_source_realpath()
    if script != me and (not src or script != src):
        return False
    if argv[2] not in GATE_SUBCOMMANDS:
        return False
    return True


def tokenize_command(command):
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    return argv


def command_has_metachar(command):
    for needle in EXEC_METACHARS:
        if needle in command:
            return True
    return False


def _python_uses_dash_c(rest):
    i = 0
    n = len(rest)
    while i < n:
        tok = str(rest[i])
        if tok == "-c" or (tok.startswith("-c") and not tok.startswith("--")):
            return True
        if tok == "--":
            return False
        if tok == "-m" or (tok.startswith("-m") and not tok.startswith("--")):
            return False
        if tok in PYTHON_TAKES_ARG:
            i += 2
            continue
        if tok.startswith("-W") and tok != "-W":
            i += 1
            continue
        if tok.startswith("-X") and tok != "-X":
            i += 1
            continue
        if tok.startswith("--check-hash-based-pycs="):
            i += 1
            continue
        if not tok.startswith("-"):
            return False
        i += 1
    return False


def always_blocked_exec(command, argv):
    if "<<" in command:
        return True
    argv = argv or []
    for i, tok in enumerate(argv):
        if is_python_bin(argv0_basename(tok)) and _python_uses_dash_c(argv[i + 1 :]):
            return True
        if argv0_basename(tok) == "devin" and "-p" in argv[i + 1 :]:
            return True
    return False


def git_subcommand(argv):
    if not argv or argv0_basename(argv[0]) != "git":
        return None, -1
    i = 1
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            i += 1
            break
        if tok in GIT_GLOBAL_TAKES_ARG:
            i += 2
            continue
        if any(tok.startswith(p + "=") for p in GIT_GLOBAL_TAKES_ARG if p.startswith("--")):
            i += 1
            continue
        if tok in GIT_GLOBAL_FLAGS:
            i += 1
            continue
        break
    if i >= n:
        return None, -1
    return argv[i], i


def git_has_locked_config_injection(argv):
    for tok in argv[1:]:
        if tok in GIT_LOCKED_DENY:
            return True
        if tok.startswith("--exec-path=") or tok.startswith("--config-env="):
            return True
    return False


def token_hits_protected(token, protected, cwd=None):
    text = str(token or "")
    if not text:
        return False
    if path_hits(text, protected):
        return True
    if cwd and not os.path.isabs(os.path.expanduser(text)):
        joined = os.path.join(cwd, text)
        if path_hits(joined, protected):
            return True
    return False


def argv_hits_protected(command, argv, protected, skip_indexes=None, cwd=None):
    skip = set(skip_indexes or [])
    for i, tok in enumerate(argv or []):
        if i in skip:
            continue
        if token_hits_protected(tok, protected, cwd=cwd):
            return True
    scan = list(protected)
    if is_gate_cli(argv):
        skip_paths = {running_script(), read_source_realpath()}
        scan = [p for p in scan if p and p not in skip_paths]
    if text_hits_protected(command, scan):
        return True
    return False


def locked_exec_allowed(argv):
    if not argv:
        return False
    if is_gate_cli(argv):
        return True
    if not argv0_is_trusted_bin(argv[0]):
        return False
    b0 = argv0_basename(argv[0])
    if b0 == "git":
        if git_has_locked_config_injection(argv):
            return False
        sub, idx = git_subcommand(argv)
        if sub not in LOCKED_GIT:
            return False
        for tok in argv[idx:]:
            if tok == "--output" or tok.startswith("--output=") or tok == "-o":
                return False
        return True
    if b0 in LOCKED_RO_BINS:
        return True
    return False


def is_git_non_source(argv):
    if not argv or argv0_basename(argv[0]) != "git":
        return False
    if not argv0_is_trusted_bin(argv[0]):
        return False
    sub, _idx = git_subcommand(argv)
    return sub in GIT_NON_SOURCE


def is_test_runner(argv):
    if not argv:
        return False
    b0 = argv0_basename(argv[0])
    if is_python_bin(b0) and len(argv) >= 3 and argv[1] == "-m" and argv[2] in TEST_RUNNER_MODS:
        return True
    if b0 == "go" and len(argv) >= 2 and argv[1] == "test":
        return True
    if b0 == "cargo" and len(argv) >= 2 and argv[1] == "test":
        return True
    if b0 in ("pytest", "py.test"):
        return True
    return False


def tool_response_output(resp):
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        out = resp.get("output")
        if isinstance(out, str):
            return out
        if out is None:
            return ""
        return out if isinstance(out, str) else json.dumps(out)
    return str(resp)


def tool_response_success(resp):
    return isinstance(resp, dict) and resp.get("success") is True


def parse_verdict(output):
    match = VERDICT_RE.search(output or "")
    if not match:
        return None
    return match.group(1)


def reminder_text(state, prompt=""):
    locked = is_write_locked(state)
    writes = "LOCKED" if locked else "UNLOCKED"
    plan = "present" if has_marker(state, "plan-passed") else "missing"
    code = "present" if has_marker(state, "code-passed") else "missing"
    ov = "yes" if (state.get("override_reason") or "").strip() else "no"
    lines = [
        "[devin-gates] writes: %s | plan-skeptic: %s | code-skeptic: %s | override: %s"
        % (writes, plan, code, ov),
        "After /plan approval, run /skeptic-plan before implementing. Run /skeptic-review before claiming done.",
    ]
    if "/design" in (prompt or ""):
        lines.append("Design docs: /design (writer/reviewer).")
    blocks = state.get("stop_blocks") or {}
    if any(int(v or 0) >= STOP_BLOCK_MAX for v in blocks.values()):
        lines.append("Stop-loop guard fired (3/3); turn allowed to end.")
    text = "\n".join(lines)
    return text[:500]


def reminder_payload(event_name, text):
    return canonical_dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": text,
            }
        }
    )


def can_mint_code(state):
    if not has_marker(state, "plan-passed"):
        return False
    if int(state.get("source_seq") or 0) <= 0:
        return False
    wit = last_witness(state, "code")
    if not wit or wit.get("verdict") != "PASS":
        return False
    if int(wit.get("source_seq") or 0) < int(state.get("source_seq") or 0):
        return False
    return True


def apply_source_mutation(state):
    state["source_seq"] = int(state.get("source_seq") or 0) + 1
    state["mutations"] = state["source_seq"]
    if has_marker(state, "code-passed"):
        clear_marker(state, "code-passed")


def exec_is_source_mutation(command):
    argv = tokenize_command(command or "")
    if not argv:
        return True
    if is_git_non_source(argv):
        return False
    if is_test_runner(argv):
        return False
    if is_gate_cli(argv):
        return False
    return True


def protected_hit_on_tool(tool_name, tool_input, protected):
    inp = tool_input or {}
    for path in extract_tool_paths(tool_name, inp):
        if path_hits(path, protected):
            return True
    if tool_name in ("write_to_process", "mcp_call_tool", "mcp_read_resource", "exec") or tool_name.startswith("mcp__"):
        if raw_json_hits_protected(inp, protected):
            return True
    if tool_name == "write_to_process":
        if text_hits_protected(inp.get("text_input") or "", protected):
            return True
        if text_hits_protected(inp.get("bytes_input") or "", protected):
            return True
    return False


def pre_write(state, tool_name, tool_input, session_id, event):
    inp = tool_input or {}
    protected = protected_paths()
    escape = has_escape(state)
    if tool_name == "apply_patch":
        if is_write_locked(state):
            return block(
                "apply_patch blocked while writes are locked (schema unknown; fail closed).",
                session_id,
                event,
                tool_name,
            )
        if not escape and raw_json_hits_protected(inp, protected):
            return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, tool_name)
        return allow()

    paths = extract_tool_paths(tool_name, inp)
    if not escape and any(path_hits(p, protected) for p in paths):
        return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, tool_name)

    if not is_write_locked(state):
        return allow()

    for p in paths:
        if is_allowlisted_write_path(state, p):
            return allow()
    return block(WRITE_BLOCK_REASON, session_id, event, tool_name)


def pre_exec(state, tool_input, session_id, event):
    inp = tool_input or {}
    command = inp.get("command")
    if not isinstance(command, str):
        command = "" if command is None else str(command)
    protected = protected_paths()
    cwd = inp.get("workdir") or None
    if cwd and path_hits(cwd, protected):
        return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, "exec")

    argv = tokenize_command(command)
    if argv is None:
        if is_write_locked(state):
            return block("Exec blocked: unbalanced quotes.", session_id, event, "exec")
        argv = []

    skip = {1} if is_gate_cli(argv) else set()
    if always_blocked_exec(command, argv):
        return block("Exec blocked: python -c, here-doc, or nested devin -p.", session_id, event, "exec")
    if argv_hits_protected(command, argv, protected, skip_indexes=skip, cwd=cwd):
        return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, "exec")

    if not is_write_locked(state):
        return allow()

    if command_has_metachar(command):
        return block("Exec blocked: command substitution, redirect, or chaining.", session_id, event, "exec")
    if argv is None or not locked_exec_allowed(argv):
        return block("Exec blocked: command not on the locked allowlist.", session_id, event, "exec")
    return allow()


def pre_subagent(state, tool_input, session_id, event):
    profile = (tool_input or {}).get("profile") or ""
    if not is_write_locked(state):
        return allow()
    allowed = set(LOCKED_SPAWN)
    if (state.get("design_allow_root") or "").strip():
        allowed.update(DESIGN_SPAWN)
    if profile in allowed:
        return allow()
    return block(
        "run_subagent blocked: profile not allowed while writes are locked.",
        session_id,
        event,
        "run_subagent",
    )


def pre_mcp(state, tool_name, tool_input, session_id, event):
    protected = protected_paths()
    if tool_name in MCP_LIST:
        return allow()
    if is_write_locked(state):
        if tool_name.startswith("mcp__") or tool_name in ("mcp_call_tool", "mcp_read_resource"):
            return block("MCP blocked while writes are locked.", session_id, event, tool_name)
    if protected_hit_on_tool(tool_name, tool_input, protected):
        return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, tool_name)
    if tool_name == "mcp_read_resource":
        uri = (tool_input or {}).get("resource_uri") or ""
        path = _file_uri_path(uri)
        if path and path_hits(path, protected):
            return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, tool_name)
        if is_write_locked(state):
            return block("MCP blocked while writes are locked.", session_id, event, tool_name)
    return allow()


def pre_readish(state, tool_name, tool_input, session_id, event):
    protected = protected_paths()
    for path in extract_tool_paths(tool_name, tool_input):
        if path_hits(path, protected):
            return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, tool_name)
    if tool_name == "request_scope" and raw_json_hits_protected(tool_input, protected):
        return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, tool_name)
    return allow()


def pre_write_to_process(state, tool_input, session_id, event):
    if is_write_locked(state):
        return block("write_to_process blocked while writes are locked.", session_id, event, "write_to_process")
    protected = protected_paths()
    if protected_hit_on_tool("write_to_process", tool_input, protected):
        return block("Path is protected (state/secret/hooks/config/gate source).", session_id, event, "write_to_process")
    return allow()


def handle_pre(event, state):
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    sid = sanitize_session_id(event.get("session_id") or "")
    if tool in MUTATING_FILE_TOOLS:
        if not sid and not gates_off():
            return block(WRITE_BLOCK_REASON, "", "PreToolUse", tool)
        return pre_write(state, tool, inp, sid, "PreToolUse")
    if tool == "exec":
        return pre_exec(state, inp, sid, "PreToolUse")
    if tool == "write_to_process":
        return pre_write_to_process(state, inp, sid, "PreToolUse")
    if tool == "run_subagent":
        return pre_subagent(state, inp, sid, "PreToolUse")
    if tool in MCP_LIST:
        return allow()
    if tool == "mcp_call_tool" or tool == "mcp_read_resource" or tool.startswith("mcp__"):
        return pre_mcp(state, tool, inp, sid, "PreToolUse")
    if tool in ("read", "grep", "glob", "find_file_by_name", "notebook_read", "request_scope"):
        return pre_readish(state, tool, inp, sid, "PreToolUse")
    if tool == "write_plan":
        if sid:
            state["mode"] = "plan"
            save_state(state)
        return allow()
    if tool in ("exit_plan_mode", "todo_write", "skill", "webfetch", "read_subagent", "get_output", "kill_shell"):
        return allow()
    return allow()


def handle_post_subagent(event, state):
    inp = event.get("tool_input") or {}
    profile = inp.get("profile") or ""
    if profile not in SKEPTIC_PROFILES:
        return allow()
    resume = inp.get("resume")
    if resume:
        return allow()
    output = tool_response_output(event.get("tool_response"))
    verdict = parse_verdict(output) or "FAIL"
    kind = PROFILE_KIND[profile]
    sweeps = dict(state.get("sweeps") or {"plan": 0, "code": 0, "finding": 0})
    sweeps[kind] = int(sweeps.get(kind) or 0) + 1
    state["sweeps"] = sweeps
    match = AGENT_ID_RE.search(output or "")
    subagent_id = match.group(1) if match else ""
    wit = {
        "kind": kind,
        "sweep": sweeps[kind],
        "verdict": verdict,
        "subagent_id": subagent_id,
        "source_seq": int(state.get("source_seq") or 0),
    }
    witnesses = list(state.get("witnesses") or [])
    witnesses.append(wit)
    state["witnesses"] = witnesses
    extra = ""
    if parse_verdict(output) is None:
        extra = "Skeptic returned no GATES_VERDICT line; treat as FAIL."
    if verdict == "PASS" and profile == "plan-skeptic":
        add_marker(state, "plan-passed", subagent_id=subagent_id, verdict="PASS")
        audit(state.get("session_id"), "mint", "run_subagent", "mint", "plan-passed")
    if verdict == "PASS" and profile == "code-skeptic":
        if can_mint_code(state):
            add_marker(state, "code-passed", subagent_id=subagent_id, verdict="PASS")
            audit(state.get("session_id"), "mint", "run_subagent", "mint", "code-passed")
    save_state(state)
    if extra:
        return reminder_payload("PostToolUse", extra), 0
    return allow()


def handle_post(event, state):
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    resp = event.get("tool_response")
    sid = sanitize_session_id(event.get("session_id") or "")
    if not sid:
        return allow()

    if tool == "run_subagent":
        out = handle_post_subagent(event, state)
        profile = (inp or {}).get("profile") or ""
        if tool_response_success(resp) and profile and profile not in READONLY_PROFILES:
            apply_source_mutation(state)
            save_state(state)
        return out

    if tool == "exit_plan_mode":
        plan = inp.get("plan") or ""
        if isinstance(plan, str):
            state["plan_hash"] = hashlib.sha256(plan.encode("utf-8")).hexdigest()
        state["mode"] = "normal"
        save_state(state)
        return allow()

    if tool == "write_plan":
        state["mode"] = "plan"
        out = tool_response_output(resp).strip()
        if out and (out.startswith("/") or out.startswith("~")):
            state["plan_path"] = out.splitlines()[0].strip()
        save_state(state)
        return allow()

    mutated = False
    if tool_response_success(resp):
        if tool in SOURCE_FILE_TOOLS:
            mutated = True
        elif tool == "exec":
            mutated = exec_is_source_mutation(inp.get("command") or "")
        elif tool == "mcp_call_tool" or (tool.startswith("mcp__") and tool not in MCP_LIST):
            mutated = True
    if mutated:
        apply_source_mutation(state)
        save_state(state)
    return allow()


def handle_stop(event, state):
    sid = sanitize_session_id(event.get("session_id") or "")
    if gates_off():
        return allow()
    if (state.get("override_reason") or "").strip():
        return allow()
    if (state.get("mode") or "normal") == "plan":
        return allow()
    if int(state.get("source_seq") or 0) == 0:
        return allow()
    if has_marker(state, "code-passed"):
        return allow()
    pid = event.get("prompt_id") or "_missing"
    blocks = dict(state.get("stop_blocks") or {})
    n = int(blocks.get(pid) or 0)
    if n >= STOP_BLOCK_MAX:
        audit(sid, "stop_loop_guard_fired", "Stop", "allow", "stop_loop_guard_fired")
        return allow()
    blocks[pid] = n + 1
    state["stop_blocks"] = blocks
    if sid:
        save_state(state)
    nth = blocks[pid]
    reason = (
        "Stop blocked: no code-skeptic marker. Run /skeptic-review on the frozen candidate "
        "(or /gate-bypass). Stop-loop: block %d/%d this turn; after 3 the turn will be allowed to end."
        % (nth, STOP_BLOCK_MAX)
    )
    return block(reason, sid, "Stop", "Stop")


def handle_reminder(event, state):
    name = event.get("hook_event_name") or "SessionStart"
    prompt = event.get("prompt") or ""
    text = reminder_text(state, prompt=prompt)
    return reminder_payload(name, text), 0


def reminder_session_id(event):
    sid = sanitize_session_id(event.get("session_id") or "")
    if sid:
        return sid
    proj = os.environ.get("DEVIN_PROJECT_DIR") or ""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(("%s|%s" % (proj, day)).encode("utf-8")).hexdigest()[:16]


def dispatch(event):
    name = event.get("hook_event_name") or ""
    sid = sanitize_session_id(event.get("session_id") or "")
    if sid:
        remember_session(sid)
    if gates_off() and sid:
        audit(sid, name or "hook", event.get("tool_name") or "", "allow", "gates_off")

    if name in REMINDER_EVENTS:
        load_sid = sid or reminder_session_id(event)
        state = load_state(sid) if sid else default_state(load_sid)
        return handle_reminder(event, state)
    if name == "SessionEnd":
        return allow()
    if name == "Stop":
        state = load_state(sid) if sid else default_state("")
        if sid:
            state["session_id"] = sid
        return handle_stop(event, state)
    if name == "PreToolUse":
        state = load_state(sid) if sid else default_state("")
        if sid:
            state["session_id"] = sid
        return handle_pre(event, state)
    if name == "PostToolUse":
        state = load_state(sid) if sid else default_state("")
        if sid:
            state["session_id"] = sid
        return handle_post(event, state)
    return allow()


def run_hook(event):
    name = event.get("hook_event_name") or ""
    tool = event.get("tool_name") or ""
    try:
        return dispatch(event)
    except Exception:
        if name in REMINDER_EVENTS:
            return "", 0
        if name == "PreToolUse" and is_mutating_pre(tool):
            return fail_closed(event.get("session_id") or "", name, tool)
        return "", 0


def flag_value(argv, name):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def resolve_session_id(argv):
    sid = flag_value(argv, "--session-id")
    if sid:
        return sanitize_session_id(sid)
    env = os.environ.get("DEVIN_SESSION_ID")
    if env:
        return sanitize_session_id(env)
    path = os.path.join(state_dir(), "current_session")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return sanitize_session_id(fh.read().strip().splitlines()[0].strip())
    except OSError:
        return ""


def cmd_status(argv):
    sid = resolve_session_id(argv)
    if not sid:
        sys.stdout.write("no session\n")
        return 0
    state = load_state(sid)
    sys.stdout.write(reminder_text(state) + "\n")
    sys.stdout.write("session_id=%s source_seq=%s sweeps=%s\n" % (sid, state.get("source_seq"), state.get("sweeps")))
    return 0


def cmd_mint_plan(argv):
    if "--resolved-blockers" in argv:
        sys.stderr.write("mint-plan does not accept --resolved-blockers\n")
        return 1
    sid = resolve_session_id(argv)
    if not sid:
        sys.stderr.write("missing session_id\n")
        return 1
    state = load_state(sid)
    state["session_id"] = sid
    wit = last_witness(state, "plan")
    if not wit or wit.get("verdict") != "PASS":
        sys.stderr.write("mint-plan requires last witness-plan PASS\n")
        return 1
    add_marker(state, "plan-passed", subagent_id=wit.get("subagent_id") or "", verdict="PASS")
    if not save_state(state):
        sys.stderr.write("failed to save state\n")
        return 1
    audit(sid, "mint-plan", "", "mint", "plan-passed")
    sys.stdout.write("plan-passed\n")
    return 0


def cmd_mint_code(argv):
    sid = resolve_session_id(argv)
    if not sid:
        sys.stderr.write("missing session_id\n")
        return 1
    state = load_state(sid)
    state["session_id"] = sid
    if not can_mint_code(state):
        sys.stderr.write("mint-code refused (need plan-passed, source_seq>0, fresh PASS witness)\n")
        return 1
    wit = last_witness(state, "code")
    add_marker(state, "code-passed", subagent_id=(wit or {}).get("subagent_id") or "", verdict="PASS")
    if not save_state(state):
        sys.stderr.write("failed to save state\n")
        return 1
    audit(sid, "mint-code", "", "mint", "code-passed")
    sys.stdout.write("code-passed\n")
    return 0


def cmd_bypass(argv):
    reason = flag_value(argv, "--reason")
    if not reason or not str(reason).strip():
        sys.stderr.write("bypass requires --reason\n")
        return 1
    sid = resolve_session_id(argv)
    if not sid:
        sys.stderr.write("missing session_id\n")
        return 1
    state = load_state(sid)
    state["session_id"] = sid
    state["override_reason"] = str(reason).strip()
    if not save_state(state):
        sys.stderr.write("failed to save state\n")
        return 1
    audit(sid, "bypass", "", "allow", state["override_reason"][:300])
    sys.stdout.write("override set\n")
    return 0


def cmd_allow_design(argv):
    if "--file" in argv:
        sys.stderr.write("allow-design does not take --file\n")
        return 1
    sid = resolve_session_id(argv)
    if not sid:
        sys.stderr.write("missing session_id\n")
        return 1
    design_id = flag_value(argv, "--id")
    if design_id is None:
        design_id = secrets.token_hex(4)
    if not HEX8_RE.match(design_id):
        sys.stderr.write("id must be 8 lowercase hex chars\n")
        return 1
    parent = design_parent()
    os.makedirs(parent, mode=0o700, exist_ok=True)
    root = os.path.join(parent, design_id)
    os.makedirs(root, mode=0o700, exist_ok=True)
    rr = os.path.realpath(root)
    parent_r = os.path.realpath(parent)
    if not (rr == parent_r or rr.startswith(parent_r + os.sep)):
        sys.stderr.write("design root escaped cache\n")
        return 1
    state = load_state(sid)
    state["session_id"] = sid
    state["design_id"] = design_id
    state["design_allow_root"] = rr
    if not save_state(state):
        sys.stderr.write("failed to save state\n")
        return 1
    audit(sid, "allow-design", "", "allow", rr)
    sys.stdout.write("design_id=%s\ndesign_allow_root=%s\n" % (design_id, rr))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(
            "usage: devin-gates.py <hook|status|mint-plan|mint-code|bypass|allow-design> ...\n"
        )
        return 2
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "hook":
        raw = sys.stdin.read()
        try:
            event = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {"decision": "block", "reason": FAIL_CLOSED_REASON}
            sys.stdout.write(canonical_dumps(payload) + "\n")
            return 2
        if not isinstance(event, dict):
            event = {}
        out, code = run_hook(event)
        if out:
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
        return code
    if cmd == "status":
        return cmd_status(rest)
    if cmd == "mint-plan":
        return cmd_mint_plan(rest)
    if cmd == "mint-code":
        return cmd_mint_code(rest)
    if cmd == "bypass":
        return cmd_bypass(rest)
    if cmd == "allow-design":
        return cmd_allow_design(rest)
    sys.stderr.write("unknown command: %s\n" % cmd)
    return 2


# devin-skills-goal:begin — managed by hooks/apply_goal_patch.py; do not edit
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import devin_gates_goal as _devin_goal

    dispatch = _devin_goal.wrap_dispatch(dispatch)
    run_hook = _devin_goal.wrap_run_hook(run_hook)
    main = _devin_goal.wrap_main(main)
except Exception:
    pass
# devin-skills-goal:end

# devin-skills-execplan:begin — managed by hooks/apply_execplan_patch.py; do not edit
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import devin_gates_execplan as _devin_execplan

    dispatch = _devin_execplan.wrap_dispatch(dispatch)
    run_hook = _devin_execplan.wrap_run_hook(run_hook)
    main = _devin_execplan.wrap_main(main)
except Exception:
    pass
# devin-skills-execplan:end

if __name__ == "__main__":
    sys.exit(main())

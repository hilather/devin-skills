"""Goal state and CLI for devin-gates.py — spliced in by apply_goal_patch.py.

The gate source is a protected path, so this module ships separately and the
patch block wraps dispatch()/main() to add:

- CLI subcommands: set-goal, goal-update, goal-pause, goal-resume,
  goal-clear, goal-status
- PreToolUse early-allow for those subcommands while locked
- PreToolUse write-allow under the attached goal's allow_root

Gate helpers (state_dir, hmac_hex, load_state, ...) are reached through the
wrapped functions' __globals__ so no circular import is needed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import sys
import threading
from contextlib import contextmanager

_GATE = None

GOAL_SUBCOMMANDS = frozenset(
    (
        "set-goal",
        "goal-update",
        "goal-pause",
        "goal-resume",
        "goal-clear",
        "goal-status",
    )
)
CLEARED = "cleared"
UPDATE_CAP = 50
CHECKLIST_TAIL = 15
GOAL_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# Reentrancy flag for the dispatch/run_hook double-wrap: run_hook calls
# dispatch internally, and both are wrapped, so the inner call must delegate
# without re-running goal checks. Thread-local rather than an event-dict key
# so the gate never sees foreign state.
_ACTIVE = threading.local()


def _bind(fn):
    global _GATE
    if _GATE is None:
        _GATE = fn.__globals__
    return fn


def _g(name):
    return _GATE[name]


# ---------------------------------------------------------------- workspace


def _workspace_realpath():
    ws = os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd()
    return os.path.realpath(ws)


def _ws_hash():
    return hashlib.sha256(_workspace_realpath().encode("utf-8")).hexdigest()[:16]


def _goals_dir():
    return os.path.join(_g("state_dir")(), "goals", _ws_hash())


def _goal_path(goal_id):
    return os.path.join(_goals_dir(), goal_id + ".json")


def _goal_allow_root(goal_id):
    return os.path.join(_g("cache_home")(), "devin-skills", "goal", goal_id)


# ---------------------------------------------------------------- state io


def _load_goal_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or not _g("verify_hmac")(obj):
        return None
    return obj


def _save_goal(goal):
    path = _goal_path(goal["goal_id"])
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    signed = dict(goal)
    signed.pop("hmac", None)
    signed["hmac"] = _g("hmac_hex")(signed)
    data = (json.dumps(signed, indent=2) + "\n").encode("utf-8")
    _g("atomic_write")(path, data, 0o600)


def _iter_goals():
    d = _goals_dir()
    if not os.path.isdir(d):
        return
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        goal = _load_goal_file(os.path.join(d, name))
        if goal is not None:
            yield goal


def _current_goal():
    for goal in _iter_goals():
        if goal.get("status") != CLEARED:
            return goal
    return None


def _load_goal_for_cli():
    """Current non-cleared goal; None if absent."""
    return _current_goal()


@contextmanager
def _goal_lock():
    d = _goals_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------- sessions


def _resolve_session_id(opts):
    # Sanitize through the gate's own session-id rules: unsafe ids resolve to
    # None, so attach no-ops and attached-only commands refuse.
    raw = (opts or {}).get("session_id") or os.environ.get("DEVIN_SESSION_ID")
    if raw:
        return _g("sanitize_session_id")(raw) or None
    try:
        with open(
            os.path.join(_g("state_dir")(), "current_session"),
            "r",
            encoding="utf-8",
        ) as fh:
            return _g("sanitize_session_id")(fh.read().strip()) or None
    except OSError:
        return None


def _attach(session_id, goal_id):
    if not session_id:
        return
    st = _g("load_state")(session_id) or _g("default_state")(session_id)
    st["session_id"] = session_id
    st["active_goal_id"] = goal_id
    _g("save_state")(st)


def _detach(session_id):
    if not session_id:
        return
    st = _g("load_state")(session_id)
    if isinstance(st, dict) and st.get("active_goal_id"):
        st.pop("active_goal_id", None)
        st.pop("goal_verify", None)
        _g("save_state")(st)


def _attached_goal_id(session_id):
    if not session_id:
        return None
    st = _g("load_state")(session_id)
    if isinstance(st, dict):
        return st.get("active_goal_id")
    return None


def _sweep_stale_attaches(goal_id, keep_session):
    """A reused goal_id must not silently re-bind sessions that attached to
    a previous goal with the same id — detach everyone but the creator."""
    base = os.path.join(_g("state_dir")(), "sessions")
    try:
        names = os.listdir(base)
    except OSError:
        return
    for sid in names:
        if sid == keep_session:
            continue
        st = _g("load_state")(sid)
        if isinstance(st, dict) and st.get("active_goal_id") == goal_id:
            st.pop("active_goal_id", None)
            st.pop("goal_verify", None)
            _g("save_state")(st)
            _audit(sid, "goal_attach_replaced", "id=%s" % goal_id)


def _audit(session_id, event, reason, tool_name="goal", decision="cli"):
    _g("audit")(session_id or "", event, tool_name, decision, reason)


# ---------------------------------------------------------------- cli parse


def _parse_opts(args, with_value=(), boolean=()):
    """Parse --flag value / --flag args. Returns (opts, positionals, error)."""
    opts = {}
    pos = []
    i = 0
    with_value = set(with_value)
    boolean = set(boolean)
    while i < len(args):
        tok = args[i]
        key = tok.lstrip("-").replace("-", "_")
        if tok in with_value:
            if i + 1 >= len(args):
                return None, None, "%s needs an argument" % tok
            opts[key] = args[i + 1]
            i += 2
        elif tok in boolean:
            opts[key] = True
            i += 1
        elif tok.startswith("--"):
            return None, None, "unknown option: %s" % tok
        else:
            pos.append(tok)
            i += 1
    return opts, pos, None


def _err(msg):
    sys.stderr.write("[devin-gates] goal: %s\n" % msg)
    return 1


def _require_reason(opts, sub):
    reason = (opts or {}).get("reason") or ""
    if not reason.strip():
        return None, _err("%s requires --reason" % sub)
    return reason, None


def _clean(text):
    """Free-text ingest: collapse CR/LF so values can't inject extra lines
    into status output or the audit log."""
    return re.sub(r"[\r\n]+", " ", text or "").strip()


def _require_attached(session_id, goal):
    if not goal:
        return _err("no goal set in this workspace")
    if _attached_goal_id(session_id) != goal.get("goal_id"):
        return _err("session is not attached to goal %s" % goal.get("goal_id"))
    return None


# ---------------------------------------------------------------- commands


def cmd_set_goal(args):
    opts, pos, err = _parse_opts(
        args, with_value=("--objective", "--id", "--session-id")
    )
    if err:
        return _err(err)
    if pos:
        return _err("unexpected arguments: %s" % " ".join(pos))
    objective = _clean(opts.get("objective"))
    if not objective:
        return _err("set-goal requires --objective")
    goal_id = opts.get("id") or os.urandom(4).hex()
    if not GOAL_ID_RE.match(goal_id):
        return _err("--id must be 8 lowercase hex characters")
    session_id = _resolve_session_id(opts)
    with _goal_lock():
        cur = _current_goal()
        if cur is not None:
            return _err(
                "workspace already has goal %s (status=%s); goal-clear first"
                % (cur.get("goal_id"), cur.get("status"))
            )
        root = os.path.realpath(_goal_allow_root(goal_id))
        os.makedirs(root, mode=0o700, exist_ok=True)
        now = _g("utc_now")()
        goal = {
            "v": 1,
            "goal_id": goal_id,
            "workspace": _workspace_realpath(),
            "objective": objective,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "created_session": session_id,
            "allow_root": root,
            "mutation_seq": 0,
            "verify_epoch": 0,
            "verifier_sweeps": 0,
            "updates": [],
            "witnesses": [],
            "blocked_reason": None,
        }
        _save_goal(goal)
        _attach(session_id, goal_id)
        _sweep_stale_attaches(goal_id, session_id)
        _audit(session_id, "goal_set", "id=%s" % goal_id)
    sys.stdout.write("goal_id=%s\n" % goal_id)
    sys.stdout.write("goal_allow_root=%s\n" % root)
    sys.stdout.write("status=active\n")
    return 0


def cmd_goal_update(args):
    opts, pos, err = _parse_opts(
        args,
        with_value=("--message", "--reason", "--session-id"),
        boolean=("--claim-done", "--blocked"),
    )
    if err:
        return _err(err)
    if pos:
        return _err("unexpected arguments: %s" % " ".join(pos))
    chosen = sum(
        1
        for k in ("claim_done", "blocked", "message")
        if opts.get(k)
    )
    if chosen != 1:
        return _err(
            "goal-update requires exactly one of --message, --claim-done, --blocked"
        )
    session_id = _resolve_session_id(opts)
    with _goal_lock():
        goal = _load_goal_for_cli()
        attached_err = _require_attached(session_id, goal)
        if attached_err is not None:
            return attached_err
        status = goal.get("status")
        now = _g("utc_now")()
        if opts.get("claim_done"):
            if status != "active":
                return _err("--claim-done requires status=active (got %s)" % status)
            goal["updates"].append(
                {
                    "ts": now,
                    "session": session_id,
                    "kind": "claim",
                    "message": "claim-done",
                }
            )
            audit_event = "goal_claim"
        elif opts.get("blocked"):
            reason, rerr = _require_reason(opts, "goal-update --blocked")
            if rerr is not None:
                return rerr
            reason = _clean(reason)
            if status != "active":
                return _err("--blocked requires status=active (got %s)" % status)
            goal["status"] = "blocked"
            goal["verify_epoch"] = int(goal.get("verify_epoch") or 0) + 1
            goal["blocked_reason"] = reason
            goal["updates"].append(
                {
                    "ts": now,
                    "session": session_id,
                    "kind": "blocked",
                    "message": reason,
                }
            )
            audit_event = "goal_blocked"
        elif opts.get("message"):
            if status not in ("active", "paused", "blocked"):
                return _err("--message requires active/paused/blocked (got %s)" % status)
            goal["updates"].append(
                {
                    "ts": now,
                    "session": session_id,
                    "kind": "progress",
                    "message": _clean(opts["message"]),
                }
            )
            audit_event = "goal_update"
        goal["updates"] = goal["updates"][-UPDATE_CAP:]
        goal["updated_at"] = now
        _save_goal(goal)
        _audit(session_id, audit_event, "id=%s" % goal["goal_id"])
    sys.stdout.write("goal_id=%s\n" % goal["goal_id"])
    sys.stdout.write("status=%s\n" % goal["status"])
    return 0


def _transition(args, sub, allowed_from, to_status, audit_event):
    opts, pos, err = _parse_opts(
        args, with_value=("--reason", "--session-id")
    )
    if err:
        return _err(err)
    if pos:
        return _err("unexpected arguments: %s" % " ".join(pos))
    session_id = _resolve_session_id(opts)
    reason = None
    if to_status in ("paused", "cleared"):
        reason, rerr = _require_reason(opts, sub)
        if rerr is not None:
            return rerr
        reason = _clean(reason)
    with _goal_lock():
        goal = _load_goal_for_cli()
        if not goal:
            return _err("no goal set in this workspace")
        status = goal.get("status")
        if sub == "goal-resume":
            # resume attaches any session; allowed from paused/blocked,
            # attach-only/no-op on active, refused on complete/cleared.
            if status in ("complete", CLEARED):
                return _err("goal-resume not allowed from status=%s" % status)
            _attach(session_id, goal["goal_id"])
            if status in ("paused", "blocked"):
                goal["status"] = "active"
                goal["blocked_reason"] = None
                goal["updated_at"] = _g("utc_now")()
                _save_goal(goal)
                _audit(session_id, audit_event, "id=%s" % goal["goal_id"])
            else:
                _audit(
                    session_id,
                    audit_event,
                    "id=%s attach-only" % goal["goal_id"],
                )
            sys.stdout.write("goal_id=%s\n" % goal["goal_id"])
            sys.stdout.write("goal_allow_root=%s\n" % goal.get("allow_root", ""))
            sys.stdout.write("objective=%s\n" % goal.get("objective", ""))
            return 0
        if sub == "goal-clear":
            # abort needs no attach precondition
            goal["status"] = CLEARED
            goal["verify_epoch"] = int(goal.get("verify_epoch") or 0) + 1
            goal["updated_at"] = _g("utc_now")()
            goal["updates"].append(
                {
                    "ts": goal["updated_at"],
                    "session": session_id,
                    "kind": "cleared",
                    "message": reason,
                }
            )
            goal["updates"] = goal["updates"][-UPDATE_CAP:]
            _save_goal(goal)
            _detach(session_id)
            _audit(session_id, audit_event, "id=%s reason=%s" % (goal["goal_id"], reason))
            sys.stdout.write("goal_id=%s\n" % goal["goal_id"])
            sys.stdout.write("status=cleared\n")
            return 0
        # goal-pause
        attached_err = _require_attached(session_id, goal)
        if attached_err is not None:
            return attached_err
        if status not in allowed_from:
            return _err("%s not allowed from status=%s" % (sub, status))
        goal["status"] = to_status
        goal["verify_epoch"] = int(goal.get("verify_epoch") or 0) + 1
        goal["updated_at"] = _g("utc_now")()
        goal["updates"].append(
            {
                "ts": goal["updated_at"],
                "session": session_id,
                "kind": to_status,
                "message": reason,
            }
        )
        goal["updates"] = goal["updates"][-UPDATE_CAP:]
        _save_goal(goal)
        _audit(session_id, audit_event, "id=%s reason=%s" % (goal["goal_id"], reason))
        sys.stdout.write("goal_id=%s\n" % goal["goal_id"])
        sys.stdout.write("status=%s\n" % to_status)
        return 0


def cmd_goal_pause(args):
    return _transition(args, "goal-pause", ("active",), "paused", "goal_paused")


def cmd_goal_resume(args):
    return _transition(args, "goal-resume", None, None, "goal_resumed")


def cmd_goal_clear(args):
    return _transition(args, "goal-clear", None, "cleared", "goal_cleared")


def cmd_goal_status(args):
    opts, pos, err = _parse_opts(args, with_value=("--session-id",))
    if err:
        return _err(err)
    if pos:
        return _err("unexpected arguments: %s" % " ".join(pos))
    session_id = _resolve_session_id(opts)
    goal = _current_goal()
    if not goal:
        sys.stdout.write("[devin-gates] goal: none\n")
        return 0
    attached = "yes" if _attached_goal_id(session_id) == goal.get("goal_id") else "no"
    updates = goal.get("updates") or []
    sys.stdout.write(
        "[devin-gates] goal %s: %s | sweeps: %s | updates: %d | attached: %s\n"
        % (
            goal.get("goal_id"),
            str(goal.get("status", "")).upper(),
            goal.get("verifier_sweeps", 0),
            len(updates),
            attached,
        )
    )
    sys.stdout.write("goal_allow_root=%s\n" % goal.get("allow_root", ""))
    sys.stdout.write("objective=%s\n" % goal.get("objective", ""))
    root = goal.get("allow_root") or ""
    lines = []
    if root:
        try:
            with open(
                os.path.join(root, "checklist.md"), "r", encoding="utf-8"
            ) as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
    if lines:
        sys.stdout.write("checklist (last %d):\n" % CHECKLIST_TAIL)
        for line in lines[-CHECKLIST_TAIL:]:
            sys.stdout.write("  %s\n" % line)
    return 0


_GOAL_HANDLERS = {
    "set-goal": cmd_set_goal,
    "goal-update": cmd_goal_update,
    "goal-pause": cmd_goal_pause,
    "goal-resume": cmd_goal_resume,
    "goal-clear": cmd_goal_clear,
    "goal-status": cmd_goal_status,
}


# ---------------------------------------------------------------- hook wrap


def _gate_script_paths():
    paths = set()
    try:
        paths.add(os.path.realpath(_g("running_script")()))
    except Exception:
        pass
    try:
        src = _g("read_source_realpath")()
        if src:
            paths.add(os.path.realpath(src))
    except Exception:
        pass
    paths.discard("")
    return paths


def _is_goal_cli_exec(command):
    """True iff command is exactly `python <gate-script> <goal-sub> [args]`
    with the gate's own metachar policy applied to the raw command string.

    The metachar scan must run on the raw command, not on shlex tokens:
    `x;id` is one shlex token but two shell commands.
    """
    if not command or not isinstance(command, str):
        return False
    if _g("command_has_metachar")(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) < 3:
        return False
    try:
        if not _g("is_python_bin")(_g("argv0_basename")(argv[0])):
            return False
    except Exception:
        return False
    try:
        script = os.path.realpath(os.path.expanduser(argv[1]))
    except (OSError, ValueError):
        return False
    if script not in _gate_script_paths():
        return False
    if argv[2] not in GOAL_SUBCOMMANDS:
        return False
    return True


def _write_path(tool_name, tool_input):
    if tool_name in ("write", "edit"):
        return tool_input.get("file_path")
    if tool_name == "notebook_edit":
        return tool_input.get("notebook_path")
    return None


def _under_goal_root(session_id, path):
    gid = _attached_goal_id(session_id)
    if not gid:
        return False
    goal = _load_goal_file(_goal_path(gid))
    if not goal or goal.get("status") == CLEARED:
        return False
    root = goal.get("allow_root")
    if not root:
        return False
    try:
        cand = os.path.realpath(path)
        root_real = os.path.realpath(root)
    except (OSError, ValueError):
        return False
    if not _g("path_under")(root_real, cand):
        return False
    try:
        if _g("path_hits")(cand, _g("protected_paths")()):
            return False
    except Exception:
        return False
    return True


def _session_end(session_id):
    st = _g("load_state")(session_id)
    if isinstance(st, dict) and st.get("active_goal_id"):
        gid = st["active_goal_id"]
        st.pop("active_goal_id", None)
        st.pop("goal_verify", None)
        _g("save_state")(st)
        _audit(session_id, "goal_active_at_session_end", "id=%s" % gid)


# ---------------------------------------------------------------- verifier


def _goal_spawn_gate(sid):
    """goal-verifier may be spawned only by a session attached to an active
    goal — regardless of lock state (design: no plan-passed required).
    gates_off and an audited /gate-bypass bypass this like every other
    goal check (design rollout: they bypass goal enforcement exactly as
    they bypass the write/Stop locks)."""
    st = _g("load_state")(sid)
    gid = _attached_goal_id(sid)
    goal = _load_goal_file(_goal_path(gid)) if gid else None
    if goal and goal.get("status") == "active":
        # Bind the coming verdict to the goal and tree at spawn time: a
        # workspace mutation or a pause/resume cycle landing mid-
        # verification makes the result late, so the parent must
        # re-verify against fresh evidence.
        if isinstance(st, dict):
            st["session_id"] = sid
            st["goal_verify"] = {
                "gid": goal["goal_id"],
                "seq": int(goal.get("mutation_seq") or 0),
                "epoch": int(goal.get("verify_epoch") or 0),
            }
            _g("save_state")(st)
        return _g("allow")()
    if _g("gates_off")():
        return _g("allow")()
    if isinstance(st, dict) and (st.get("override_reason") or "").strip():
        return _g("allow")()
    return _g("block")(
        "run_subagent blocked: goal-verifier requires an attached active goal.",
        sid,
        "PreToolUse",
        "run_subagent",
    )


def _post_goal_verifier(sid, ti, resp):
    """Own PostToolUse for profile=goal-verifier end-to-end: witness recording,
    sweep counting, the status=complete mint, and late-result rejection.
    Fully intercepts (never delegates) so the gate does not count the
    read-only verifier as a source mutation."""
    if ti.get("resume"):
        return _g("allow")()  # resumed verifier is not a witness
    if not _g("tool_response_success")(resp):
        return _g("allow")()  # an infra failure is not a verdict; no sweep
    output = _g("tool_response_output")(resp)
    verdict = _g("parse_verdict")(output)
    task = ti.get("task") or ""
    match = _g("AGENT_ID_RE").search(output or "")
    subagent_id = match.group(1) if match else ""
    st = _g("load_state")(sid)
    verify = st.get("goal_verify") if isinstance(st, dict) else None
    with _goal_lock():
        goal = _current_goal()
        bound = bool(goal) and goal.get("goal_id") in task
        attached = bool(goal) and _attached_goal_id(sid) == goal.get("goal_id")
        drift = True
        if goal and isinstance(verify, dict):
            drift = (
                verify.get("gid") != goal.get("goal_id")
                or verify.get("seq") != int(goal.get("mutation_seq") or 0)
                or verify.get("epoch") != int(goal.get("verify_epoch") or 0)
            )
        if (
            not goal
            or goal.get("status") != "active"
            or not attached
            or not bound
            or drift
        ):
            _audit(
                sid,
                "goal_verifier_late_result",
                "verdict=%s" % (verdict or "none"),
                tool_name="run_subagent",
                decision="ignore",
            )
            return _g("allow")()
        goal["verifier_sweeps"] = int(goal.get("verifier_sweeps") or 0) + 1
        wit = {
            "kind": "goal",
            "sweep": goal["verifier_sweeps"],
            "verdict": verdict or "FAIL",
            "subagent_id": subagent_id,
            "mutation_seq": int(goal.get("mutation_seq") or 0),
            "ts": _g("utc_now")(),
        }
        goal["witnesses"] = list(goal.get("witnesses") or []) + [wit]
        _audit(
            sid,
            "goal_verifier_witness",
            "id=%s verdict=%s sweep=%d"
            % (goal["goal_id"], wit["verdict"], goal["verifier_sweeps"]),
            tool_name="run_subagent",
            decision="witness",
        )
        if verdict == "PASS":
            goal["status"] = "complete"
            goal["updated_at"] = _g("utc_now")()
            _audit(
                sid,
                "goal_completed",
                "id=%s" % goal["goal_id"],
                tool_name="run_subagent",
                decision="mint",
            )
        _save_goal(goal)
    if verdict is None:
        return (
            _g("reminder_payload")(
                "PostToolUse",
                "goal-verifier returned no GATES_VERDICT line; treated as FAIL.",
            ),
            0,
        )
    return _g("allow")()


# ---------------------------------------------------------------- mutations


def _goal_source_mutation(tool, ti, resp):
    """Whether a successful PostToolUse counts as a workspace-source mutation
    for goal mutation_seq. Mirrors the gate's source_seq classifier, but
    scoped to paths under the workspace realpath (allow-roots live outside
    it, so checklist/evidence writes never count)."""
    if not _g("tool_response_success")(resp):
        return False
    if tool in ("write", "edit", "apply_patch", "notebook_edit"):
        ws = _workspace_realpath()
        extract = _GATE.get("extract_tool_paths")
        if extract is None:
            return False
        for p in extract(tool, ti) or []:
            try:
                if _g("path_under")(ws, os.path.realpath(p)):
                    return True
            except (OSError, ValueError):
                continue
        return False
    if tool == "write_to_process":
        return True
    if tool == "exec":
        cmd = ti.get("command") or ""
        if _is_goal_cli_exec(cmd):
            return False  # goal CLI writes state_dir/cache, never the workspace
        return bool(_g("exec_is_source_mutation")(cmd))
    if tool == "run_subagent":
        prof = (ti.get("profile") or "")
        readonly = set(_g("READONLY_PROFILES")) | {"goal-verifier"}
        return prof not in readonly
    if tool == "mcp_call_tool" or tool.startswith("mcp__"):
        return True
    return False


def _goal_mutation_hook(sid, tool, ti, resp):
    if not _goal_source_mutation(tool, ti, resp):
        return
    with _goal_lock():
        goal = _current_goal()
        if not goal:
            return
        goal["mutation_seq"] = int(goal.get("mutation_seq") or 0) + 1
        goal["updated_at"] = _g("utc_now")()
        if goal.get("status") == "complete":
            goal["status"] = "active"
            _audit(
                sid,
                "goal_reopened",
                "id=%s seq=%d" % (goal["goal_id"], goal["mutation_seq"]),
                tool_name=tool,
                decision="reopen",
            )
        _save_goal(goal)


# ---------------------------------------------------------------- stop


def _goal_stop(event, sid):
    """Block Stop while the session is attached to an active goal. Runs
    before the gate's own early-allows (mode==plan, source_seq==0) so
    zero-mutation goals still gate; shares the same stop_blocks budget."""
    if _g("gates_off")():
        return None
    st = _g("load_state")(sid)
    if not isinstance(st, dict):
        return None
    if (st.get("override_reason") or "").strip():
        return None
    gid = _attached_goal_id(sid)
    if not gid:
        return None
    goal = _load_goal_file(_goal_path(gid))
    if not goal or goal.get("status") != "active":
        return None
    pid = event.get("prompt_id") or "_missing"
    blocks = dict(st.get("stop_blocks") or {})
    n = int(blocks.get(pid) or 0)
    maxb = int(_g("STOP_BLOCK_MAX"))
    if n >= maxb:
        _audit(sid, "stop_loop_guard_fired", "id=%s" % gid, tool_name="Stop", decision="allow")
        return None
    st["session_id"] = sid
    blocks[pid] = n + 1
    st["stop_blocks"] = blocks
    _g("save_state")(st)
    last = ""
    for u in reversed(goal.get("updates") or []):
        if isinstance(u, dict) and u.get("message"):
            last = u["message"]
            break
    reason = (
        "[devin-gates] goal %s active: %s (last update: %s; verifier sweeps: %s). "
        "Continue working, run a fresh goal-verifier, or goal-pause / "
        "goal-update --blocked / goal-clear with --reason. "
        "Stop-loop: block %d/%d this turn; after %d the turn will be allowed to end."
        % (gid, goal.get("objective") or "", last or "-",
           goal.get("verifier_sweeps", 0), n + 1, maxb, maxb)
    )
    return _g("block")(reason, sid, "Stop", "Stop")


# ---------------------------------------------------------------- reminders


def _goal_reminder_line(name, sid):
    goal = _current_goal()
    if not goal:
        return None
    gid = goal.get("goal_id")
    status = goal.get("status") or ""
    objective = goal.get("objective") or ""
    attached = _attached_goal_id(sid) == gid
    if name == "SessionStart":
        hint = (
            "/goal resume to attach, "
            if status in ("paused", "blocked")
            else ""
        )
        return "goal %s %s: %s — %s/goal status for detail" % (
            gid,
            status.upper(),
            objective,
            hint,
        )
    if name == "UserPromptSubmit":
        if attached and status == "active":
            return (
                "goal %s active: %s — Stop is blocked until a fresh "
                "goal-verifier PASSes; goal-pause / goal-update --blocked / "
                "goal-clear with --reason to defer." % (gid, objective)
            )
        return None
    if name == "PostCompaction":
        if not attached:
            return None
        return (
            "goal %s %s: %s | checklist: %s | sweeps: %s | /goal status to reseed"
            % (
                gid,
                status.upper(),
                objective,
                os.path.join(goal.get("allow_root") or "", "checklist.md"),
                goal.get("verifier_sweeps", 0),
            )
        )
    return None


_MERGE_REMINDER = "__goal_merge_reminder__"


def _merge_reminder_payload(out, line, code):
    if not line:
        return (out, code)
    try:
        payload = json.loads(out)
        spec = payload["hookSpecificOutput"]
        ctx = spec.get("additionalContext") or ""
        spec["additionalContext"] = (ctx + "\n" + line).strip()
        return (_g("canonical_dumps")(payload) + "\n", code)
    except (ValueError, AttributeError, KeyError, TypeError):
        return (out, code)


# ---------------------------------------------------------------- dispatch


def _goal_intercept(event):
    """Returns (payload, code) to short-circuit, a _MERGE_REMINDER tuple to
    merge after orig runs once, or None to delegate."""
    name = event.get("hook_event_name")
    sid = _g("sanitize_session_id")(event.get("session_id") or "")
    if name == "PreToolUse":
        tool = event.get("tool_name")
        ti = event.get("tool_input") or {}
        if tool == "exec" and _is_goal_cli_exec(ti.get("command") or ""):
            return _g("allow")()
        if tool == "run_subagent" and (ti.get("profile") or "") == "goal-verifier":
            return _goal_spawn_gate(sid)
        path = _write_path(tool, ti)
        if path and _under_goal_root(sid, path):
            return _g("allow")()
        return None
    if name == "PostToolUse":
        tool = event.get("tool_name")
        ti = event.get("tool_input") or {}
        resp = event.get("tool_response")
        if tool == "run_subagent" and (ti.get("profile") or "") == "goal-verifier":
            return _post_goal_verifier(sid, ti, resp)
        _goal_mutation_hook(sid, tool, ti, resp)
        return None
    if name == "Stop":
        return _goal_stop(event, sid)
    if name == "SessionEnd":
        _session_end(sid)
        return None
    if name in ("SessionStart", "UserPromptSubmit", "PostCompaction"):
        if _g("gates_off")():
            return None
        return (_MERGE_REMINDER, _goal_reminder_line(name, sid))
    return None


def _intercept(event, orig):
    if getattr(_ACTIVE, "in_hook", False):
        # Inner wrap level: run_hook calls dispatch; already handled above.
        return orig(event)
    _ACTIVE.in_hook = True
    try:
        merge = None
        try:
            if isinstance(event, dict):
                out = _goal_intercept(event)
                if out is not None:
                    if out[0] == _MERGE_REMINDER:
                        merge = out[1]
                    else:
                        return out
        except Exception:
            pass
        o, code = orig(event)
        if merge is not None:
            return _merge_reminder_payload(o, merge, code)
        return (o, code)
    finally:
        _ACTIVE.in_hook = False


def wrap_dispatch(orig):
    _bind(orig)

    def wrapped(event):
        return _intercept(event, orig)

    return wrapped


def wrap_run_hook(orig):
    _bind(orig)

    def wrapped(event):
        return _intercept(event, orig)

    return wrapped


def wrap_main(orig):
    _bind(orig)

    def wrapped(argv=None):
        eff = sys.argv[1:] if argv is None else list(argv)
        if eff and eff[0] in GOAL_SUBCOMMANDS:
            raise SystemExit(_GOAL_HANDLERS[eff[0]](eff[1:]))
        rc = orig(argv)
        if eff and eff[0] == "status" and not eff[1:] and rc == 0:
            try:
                sid = _resolve_session_id({})
                gid = _attached_goal_id(sid) if sid else None
                if gid:
                    goal = _load_goal_file(_goal_path(gid))
                    if goal:
                        sys.stdout.write(
                            "goal: %s:%s\n" % (gid, goal.get("status") or "?")
                        )
            except Exception:
                pass
        return rc

    return wrapped

"""Execute-plan run state and CLI for devin-gates.py — spliced in by
apply_execplan_patch.py.

The gate source is a protected path, so this module ships separately and the
patch block wraps dispatch()/run_hook()/main() to add:

- CLI subcommands: allow-exec-plan, exec-plan-validate
- PreToolUse early-allow for those subcommands while locked
- PreToolUse write-allow under the current execute-plan allow_root
- `status` output line `exec-plan: <id>` while a plan is attached
- READONLY_PROFILES += pr-reviewer (the per-PR reviewer profile is
  read-only; its subagent result must not count as a source mutation)

Gate helpers (state_dir, hmac_hex, load_state, ...) are reached through the
wrapped functions' __globals__ so no circular import is needed. When another
splice module (e.g. devin_gates_goal) already wrapped the gate, the wrapper's
globals carry a bound _GATE dict — _bind follows it to the real gate
namespace. This module therefore binds correctly whether it splices first or
second; the reverse is not guaranteed (devin_gates_goal's _bind does not
follow _GATE), so the shipped install order — goal splice, then execplan —
is load-bearing.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading

_GATE = None

EXEC_PLAN_SUBCOMMANDS = frozenset(("allow-exec-plan", "exec-plan-validate"))
EXEC_PLAN_ID_RE = re.compile(r"^[0-9a-f]{8}$")
VALIDATE_TIMEOUT_S = 30
# Installed beside the gate as a copy (install.sh); the namespaced filename
# avoids any future collision in $PREFIX/hooks/. Pinned by __file__ — never
# derived from user input, so a planted script cannot be substituted.
VALIDATOR_SIBLING = "devin_execplan_validate_plan.py"

# Reentrancy flag for the dispatch/run_hook double-wrap: run_hook calls
# dispatch internally, and both are wrapped, so the inner call must delegate
# without re-running exec-plan checks. Thread-local rather than an event-dict
# key so the gate never sees foreign state.
_ACTIVE = threading.local()


def _bind(fn):
    global _GATE
    if _GATE is None:
        g = fn.__globals__
        # If fn is a wrapper from an earlier splice module, its globals are
        # that module's namespace — which holds a _GATE already bound to the
        # real gate __globals__. Follow it so helpers resolve correctly no
        # matter which splice ran first.
        inner = g.get("_GATE")
        if isinstance(inner, dict) and "load_state" in inner:
            g = inner
        _GATE = g
        # pr-reviewer is a read-only profile (no write/edit/exec/
        # run_subagent): register it so the gate's run_subagent mutation
        # classifier treats its spawn and result as non-mutating. Additive
        # only — a missing or unexpected READONLY_PROFILES shape is ignored.
        try:
            ro = _GATE.get("READONLY_PROFILES")
            if isinstance(ro, (set, frozenset)) and "pr-reviewer" not in ro:
                _GATE["READONLY_PROFILES"] = set(ro) | {"pr-reviewer"}
        except Exception:
            pass
    return fn


def _g(name):
    return _GATE[name]


def _exec_plan_allow_root(plan_id):
    return os.path.join(
        _g("cache_home")(), "devin-skills", "execute-plan", plan_id
    )


# ---------------------------------------------------------------- sessions


def _resolve_session_id(opts):
    # Sanitize through the gate's own session-id rules: unsafe ids resolve to
    # None, so attach no-ops.
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


def _attach(session_id, plan_id, root):
    if not session_id:
        return
    st = _g("load_state")(session_id) or _g("default_state")(session_id)
    st["session_id"] = session_id
    st["exec_plan_id"] = plan_id
    st["exec_plan_allow_root"] = root
    _g("save_state")(st)


def _attached_exec_plan(session_id):
    if not session_id:
        return None, None
    st = _g("load_state")(session_id)
    if isinstance(st, dict):
        return st.get("exec_plan_id"), st.get("exec_plan_allow_root")
    return None, None


def _audit(session_id, event, reason, tool_name="execplan", decision="cli"):
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
    sys.stderr.write("[devin-gates] exec-plan: %s\n" % msg)
    return 1


# ---------------------------------------------------------------- cli


def cmd_allow_exec_plan(args):
    opts, pos, err = _parse_opts(
        args, with_value=("--id", "--session-id")
    )
    if err:
        return _err(err)
    if pos:
        return _err("unexpected arguments: %s" % " ".join(pos))
    plan_id = opts.get("id") or os.urandom(4).hex()
    if not EXEC_PLAN_ID_RE.match(plan_id):
        return _err("--id must be 8 lowercase hex characters")
    root = os.path.realpath(_exec_plan_allow_root(plan_id))
    # exist_ok: --resume re-issues the same id and must not fail. makedirs'
    # mode only applies on creation — re-assert it so a loosened root on the
    # resume path is repaired.
    os.makedirs(root, mode=0o700, exist_ok=True)
    for path in (root, os.path.dirname(root)):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    session_id = _resolve_session_id(opts)
    if not session_id:
        # The root is created but nothing is attached: writes under it will
        # still be denied while locked. Say so rather than letting the skill
        # discover it as a generic block on the first state.json write.
        sys.stderr.write(
            "[devin-gates] exec-plan: no session resolved — "
            "root created but not attached\n"
        )
    _attach(session_id, plan_id, root)
    _audit(
        session_id,
        "exec_plan_allow",
        "id=%s root=%s" % (plan_id, root),
    )
    sys.stdout.write("plan_id=%s\n" % plan_id)
    sys.stdout.write("exec_plan_allow_root=%s\n" % root)
    return 0


def _validator_path():
    """The installed validator copy pinned beside this module."""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, VALIDATOR_SIBLING)
    try:
        real = os.path.realpath(cand)
    except (OSError, ValueError):
        return None
    if os.path.isfile(real):
        return real
    return None


def cmd_exec_plan_validate(args):
    opts, pos, err = _parse_opts(
        args, with_value=("--file", "--session-id")
    )
    if err:
        return _err(err)
    if pos:
        return _err("unexpected arguments: %s" % " ".join(pos))
    raw = opts.get("file") or ""
    if not raw:
        return _err("exec-plan-validate requires --file")
    try:
        target = os.path.realpath(os.path.expanduser(raw))
    except (OSError, ValueError):
        return _err("cannot resolve --file path")
    try:
        if _g("path_hits")(target, _g("protected_paths")()):
            return _err("--file resolves to a protected path")
    except Exception:
        return _err("cannot verify --file against protected paths")
    session_id = _resolve_session_id(opts)
    validator = _validator_path()
    if not validator:
        # rc 2 means "not a plan-validation verdict" — infra failure here
        # (missing validator, timeout, launch error) AND the validator's own
        # usage/IO exits (unreadable --file) relay as 2 too. The skill treats
        # any rc 2 as "gate validation unavailable → fall back"; the fallback
        # fails the same way on a missing file, so the conflation is safe.
        sys.stderr.write(
            "[devin-gates] exec-plan: validator missing at %s\n"
            % os.path.join(
                os.path.dirname(os.path.abspath(__file__)), VALIDATOR_SIBLING
            )
        )
        return 2
    try:
        proc = subprocess.run(
            [sys.executable, validator, target],
            capture_output=True,
            timeout=VALIDATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _audit(
            session_id,
            "exec_plan_validate_timeout",
            "file=%s" % target,
        )
        sys.stderr.write(
            "[devin-gates] exec-plan: validator timed out after %ds\n"
            % VALIDATE_TIMEOUT_S
        )
        return 2
    except OSError as exc:
        sys.stderr.write(
            "[devin-gates] exec-plan: validator failed to launch: %s\n" % exc
        )
        return 2
    sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
    if proc.stderr:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    _audit(
        session_id,
        "exec_plan_validate",
        "file=%s rc=%d" % (target, proc.returncode),
    )
    return proc.returncode


_EXEC_PLAN_HANDLERS = {
    "allow-exec-plan": cmd_allow_exec_plan,
    "exec-plan-validate": cmd_exec_plan_validate,
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


def _is_exec_plan_cli_exec(command):
    """True iff command is exactly `python <gate-script> <exec-plan-sub>
    [args]` with the gate's own metachar policy applied to the raw command
    string.

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
    if argv[2] not in EXEC_PLAN_SUBCOMMANDS:
        return False
    return True


def _write_path(tool_name, tool_input):
    if tool_name in ("write", "edit"):
        return tool_input.get("file_path")
    if tool_name == "notebook_edit":
        return tool_input.get("notebook_path")
    return None


def _under_exec_plan_root(session_id, path):
    _pid, root = _attached_exec_plan(session_id)
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


def _exec_plan_intercept(event):
    """Returns (payload, code) to short-circuit, or None to delegate."""
    name = event.get("hook_event_name")
    if name != "PreToolUse":
        return None
    sid = _g("sanitize_session_id")(event.get("session_id") or "")
    tool = event.get("tool_name")
    ti = event.get("tool_input") or {}
    if tool == "exec" and _is_exec_plan_cli_exec(ti.get("command") or ""):
        return _g("allow")()
    path = _write_path(tool, ti)
    if path and _under_exec_plan_root(sid, path):
        return _g("allow")()
    return None


def _intercept(event, orig):
    if getattr(_ACTIVE, "in_hook", False):
        # Inner wrap level: run_hook calls dispatch; already handled above.
        return orig(event)
    _ACTIVE.in_hook = True
    try:
        try:
            if isinstance(event, dict):
                out = _exec_plan_intercept(event)
                if out is not None:
                    return out
        except Exception:
            pass
        return orig(event)
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
        if eff and eff[0] in _EXEC_PLAN_HANDLERS:
            raise SystemExit(_EXEC_PLAN_HANDLERS[eff[0]](eff[1:]))
        rc = orig(argv)
        if eff and eff[0] == "status" and not eff[1:] and rc == 0:
            try:
                sid = _resolve_session_id({})
                pid, _root = _attached_exec_plan(sid) if sid else (None, None)
                if pid:
                    sys.stdout.write("exec-plan: %s\n" % pid)
            except Exception:
                pass
        return rc

    return wrapped

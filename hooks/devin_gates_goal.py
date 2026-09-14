"""Goal state and CLI for devin-gates.py — spliced in by apply_goal_patch.py.

The gate source is a protected path, so this module ships separately and the
patch block wraps dispatch()/main() to add:

- CLI subcommands: set-goal, goal-update, goal-pause, goal-resume,
  goal-clear, goal-status, goal-baseline
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
import time
from contextlib import contextmanager
from datetime import datetime, timezone

_GATE = None

GOAL_SUBCOMMANDS = frozenset(
    (
        "set-goal",
        "goal-update",
        "goal-pause",
        "goal-resume",
        "goal-clear",
        "goal-status",
        "goal-baseline",
    )
)
CLEARED = "cleared"
UPDATE_CAP = 50
CHECKLIST_TAIL = 15
BASELINE_CAP = 16000
GOAL_ID_RE = re.compile(r"^[0-9a-f]{8}$")
BLOCKER_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
GOAL_KINDS = ("code-change", "research", "analysis", "general")
GOAL_SWEEP_CAP = 6
STALL_SAME = 2
STALL_SAME_STRATEGIST = 5
STRATEGIST_DISTINCT = 3
STRATEGIST_BONUS = 2
_MTIME_GRACE_NS = 1_000_000_000
CLEAN_CAP = 800
_FRAME_TAG_RE = re.compile(r"<(?=/?(?:system-reminder|goal-state)\b)", re.I)
_VERDICT_FENCE_RE = re.compile(r"(?m)^```goal-verdict[^\S\n]*\n")
_REFUTED_LINE_RE = re.compile(r"\s*(?:REFUTED|UNVERIFIABLE)\b\s*[—\-:]\s*(.*)")

# Reentrancy flag for the dispatch/run_hook double-wrap: run_hook calls
# dispatch internally, and both are wrapped, so the inner call must delegate
# without re-running goal checks. Thread-local rather than an event-dict key
# so the gate never sees foreign state.
_ACTIVE = threading.local()


def _bind(fn):
    global _GATE
    if _GATE is None:
        _GATE = fn.__globals__
        # The gate's parse_verdict takes the FIRST GATES_VERDICT match, so a
        # verdict line quoted inside inlined evidence could mint ahead of the
        # real terminal verdict. Rebind to last-match: _GATE is the gate
        # module's __globals__, so every call-site lookup (the skeptic mint
        # paths and _g("parse_verdict") below) resolves to this instead.
        _GATE["parse_verdict"] = _parse_verdict_last
    return fn


def _g(name):
    return _GATE[name]


def _parse_verdict_last(output):
    """GATES_VERDICT = the LAST matching line, not the first. The contract
    already requires the verdict as the final line; last-match keeps a
    verdict line quoted inside pasted evidence from minting."""
    try:
        matches = _g("VERDICT_RE").findall(output or "")
    except Exception:
        return None
    if not matches:
        return None
    last = matches[-1]
    return last if isinstance(last, str) else last[0]


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
    # NOTE: a live goal_verify binding is deliberately left alone here —
    # attach must never free it. An orphaned result whose binding was
    # discarded could otherwise mint against a rebound binding; the spawn
    # gate is the only place bindings are voided, and only when stale.
    _g("save_state")(st)


def _void_verify(st, binding):
    """Discard a stale goal_verify binding and record its task sha on the
    session's void list. The orphaned result then lands late at post even
    when the next spawn's task sha collides with the voided one — without
    this, the stale result could consume the rebound binding and mint on
    evidence gathered against an older tree."""
    sha = binding.get("task") if isinstance(binding, dict) else None
    st.pop("goal_verify", None)
    if isinstance(sha, str) and sha:
        voids = st.get("goal_verify_void")
        if not isinstance(voids, list):
            voids = []
        voids.append(sha)
        st["goal_verify_void"] = voids[-16:]
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
    into status output or the audit log; neutralize frame tags and cap
    length so inlined text can't masquerade as a gate frame."""
    t = re.sub(r"[\r\n]+", " ", text or "").strip()
    t = _FRAME_TAG_RE.sub("<" + chr(0x200B), t)  # "<" + ZWSP breaks the tag
    return t[:CLEAN_CAP]


def _require_attached(session_id, goal):
    if not goal:
        return _err("no goal set in this workspace")
    if _attached_goal_id(session_id) != goal.get("goal_id"):
        return _err("session is not attached to goal %s" % goal.get("goal_id"))
    return None


# ---------------------------------------------------------------- checklist


def _checklist_file(goal):
    root = goal.get("allow_root") or ""
    return os.path.join(root, "checklist.md") if root else None


def _read_checklist(goal):
    path = _checklist_file(goal)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _sha16(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _baseline_sha(goal):
    base = goal.get("checklist_baseline")
    return _sha16(base) if isinstance(base, str) and base else None


def _capture_baseline(goal, sid):
    """Explicit `goal-baseline` snapshot: the verbatim checklist text goes
    into the SIGNED goal file, so later tampering breaks the HMAC instead
    of silently changing the baseline. Missing/empty refuses; oversize
    refuses AND audits — a truncated baseline would hash-mismatch the live
    file forever, and padding the file must not evade the cap. Caller
    holds _goal_lock and saves afterwards."""
    text = _read_checklist(goal)
    if text is None or not text.strip():
        return "no checklist at %s" % (_checklist_file(goal) or "<none>")
    if len(text) > BASELINE_CAP:
        _audit(
            sid,
            "goal_baseline_oversize",
            "id=%s chars=%d" % (goal["goal_id"], len(text)),
        )
        return (
            "checklist.md exceeds the %d-char baseline cap; trim or split it"
            % BASELINE_CAP
        )
    goal["checklist_baseline"] = text
    goal["baseline_ts"] = time.time()
    _audit(
        sid,
        "goal_baseline",
        "id=%s sha256=%s" % (goal["goal_id"], _sha16(text)),
    )
    return None


def _implicit_baseline(goal, sid):
    """First-claim fallback capture. Unlike `goal-baseline`, an absent or
    empty checklist.md is legal (checklist-less goals) — but an oversized
    one refuses the claim rather than recording a permanently-drifting
    truncated baseline. Caller holds _goal_lock."""
    if isinstance(goal.get("checklist_baseline"), str) and goal[
        "checklist_baseline"
    ]:
        return None
    text = _read_checklist(goal)
    if text is None or not text.strip():
        return None
    if len(text) > BASELINE_CAP:
        _audit(
            sid,
            "goal_baseline_oversize",
            "id=%s chars=%d" % (goal["goal_id"], len(text)),
        )
        return (
            "checklist.md exceeds the %d-char baseline cap; trim or split it"
            % BASELINE_CAP
        )
    goal["checklist_baseline"] = text
    goal["baseline_ts"] = time.time()
    _audit(
        sid,
        "goal_baseline_implicit",
        "id=%s sha256=%s" % (goal["goal_id"], _sha16(text)),
    )
    return None


def _checklist_drift_stamp(goal, sid):
    """Gate-side drift stamp: compares the live checklist.md hash against
    the recorded baseline; on divergence flips checklist_drifted — sticky
    until goal end — and audits that one transition. Drift is legitimate
    (refinements happen); the stamp makes it a gate-recorded fact rather
    than solely a verifier-judgment call. Returns True when the goal
    changed (caller saves). Caller holds _goal_lock."""
    base = _baseline_sha(goal)
    if base is None or goal.get("checklist_drifted"):
        return False
    live = _read_checklist(goal)
    live_sha = _sha16(live) if live is not None else None
    if live_sha == base:
        return False
    goal["checklist_drifted"] = True
    _audit(
        sid,
        "goal_checklist_drift",
        "id=%s baseline=%s live=%s"
        % (goal["goal_id"], base, live_sha or "none"),
    )
    return True


# ---------------------------------------------------------------- commands


def cmd_set_goal(args):
    opts, pos, err = _parse_opts(
        args,
        with_value=("--objective", "--id", "--session-id", "--kind"),
    )
    if err:
        return _err(err)
    if pos:
        return _err("unexpected arguments: %s" % " ".join(pos))
    objective = _clean(opts.get("objective"))
    if not objective:
        return _err("set-goal requires --objective")
    kind = opts.get("kind") or "general"
    if kind not in GOAL_KINDS:
        return _err(
            "--kind must be one of %s" % "|".join(GOAL_KINDS)
        )
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
            "last_gap_fingerprint": None,
            "consecutive_same": 0,
            "consecutive_distinct": 0,
            "last_blocker_key": None,
            "cap_bonus": 0,
            "strategist_used": False,
            "strategist_fires": 0,
            "distinct_since_fire": 0,
            "strategist_pending": None,
            "goal_kind": kind,
        }
        _save_goal(goal)
        _attach(session_id, goal_id)
        _sweep_stale_attaches(goal_id, session_id)
        _audit(session_id, "goal_set", "id=%s" % goal_id)
    sys.stdout.write("goal_id=%s\n" % goal_id)
    sys.stdout.write("goal_allow_root=%s\n" % root)
    sys.stdout.write("status=active\n")
    sys.stdout.write("kind=%s\n" % kind)
    return 0


def cmd_goal_update(args):
    opts, pos, err = _parse_opts(
        args,
        with_value=("--message", "--reason", "--session-id", "--blocker-key"),
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
    if "blocker_key" in opts:
        if not opts.get("blocked"):
            return _err("--blocker-key requires --blocked")
        if not BLOCKER_KEY_RE.match(opts["blocker_key"] or ""):
            return _err(
                "--blocker-key must match ^[a-z][a-z0-9_]{0,63}$"
            )
    session_id = _resolve_session_id(opts)
    with _goal_lock():
        goal = _load_goal_for_cli()
        attached_err = _require_attached(session_id, goal)
        if attached_err is not None:
            return attached_err
        status = goal.get("status")
        now = _g("utc_now")()
        escalate = None
        if opts.get("claim_done"):
            if status != "active":
                return _err("--claim-done requires status=active (got %s)" % status)
            # A pending strategist fire gates the claim FIRST — a refused
            # claim must not capture a baseline or stamp drift it would
            # then discard unsaved (the audits would record state changes
            # that never happened).
            pending = goal.get("strategist_pending")
            if pending is not None:
                # The model must write <allow_root>/strategy.md (the new
                # HOW) after the fire timestamp. The window check proves a
                # file was written in the window — not that it contains
                # real strategy; the <= now bound rejects touch -d-staged
                # future mtimes that would otherwise pre-satisfy every
                # later fire. Both bounds carry a 1s grace: filesystems
                # with coarse mtime granularity can round a legitimately
                # fresh write below the fire stamp (and skewed clocks a
                # touch above now).
                spath = os.path.join(
                    goal.get("allow_root") or "", "strategy.md"
                )
                try:
                    mtime = os.stat(spath).st_mtime_ns
                    satisfied = (
                        int(pending) - _MTIME_GRACE_NS
                        < mtime
                        <= time.time_ns() + _MTIME_GRACE_NS
                    )
                except (OSError, TypeError, ValueError):
                    satisfied = False
                if not satisfied:
                    return _err(
                        "--claim-done refused: strategist fired — write "
                        "strategy.md under goal_allow_root (the new HOW) "
                        "first"
                    )
                goal["strategist_pending"] = None
            # First-claim fallback baseline capture — checklist.md present
            # snapshots into the signed goal file; absent/empty is legal;
            # oversized refuses the claim rather than recording a baseline
            # that can never match the live file.
            berr = _implicit_baseline(goal, session_id)
            if berr is not None:
                return _err(berr)
            _checklist_drift_stamp(goal, session_id)
            goal["blocked_attempts"] = 0
            goal["unclaimed_rounds"] = 0
            # The claim binds to this exact tree: mutation_seq advances on
            # every workspace source mutation, so a fix after the claim
            # stales it and the spawn gate demands a fresh claim.
            goal["claim_seq"] = int(goal.get("mutation_seq") or 0)
            goal["claim_count"] = int(goal.get("claim_count") or 0) + 1
            goal["update_count"] = int(goal.get("update_count") or 0) + 1
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
            bkey = opts.get("blocker_key")
            if bkey and bkey == goal.get("last_blocker_key"):
                reason = "external-repeat: " + reason
            # Self-reported blocked is honored only after a streak — the
            # first attempts bounce back with "keep working". The streak
            # deliberately does NOT reset on --message: alternating
            # notes/quit attempts must not evade it.
            attempts = int(goal.get("blocked_attempts") or 0) + 1
            streak_min = _blocked_streak()
            if attempts < streak_min:
                goal["blocked_attempts"] = attempts
                goal["update_count"] = int(goal.get("update_count") or 0) + 1
                goal["updates"].append(
                    {
                        "ts": now,
                        "session": session_id,
                        "kind": "blocked_attempt",
                        "message": reason,
                    }
                )
                goal["updates"] = goal["updates"][-UPDATE_CAP:]
                goal["updated_at"] = now
                _save_goal(goal)
                _audit(
                    session_id,
                    "goal_blocked_attempt",
                    "id=%s attempt=%d/%d"
                    % (goal["goal_id"], attempts, streak_min),
                )
                return _err(
                    "blocked attempt %d/%d — keep working, or --claim-done "
                    "if the checklist is met" % (attempts, streak_min)
                )
            goal["blocked_attempts"] = 0
            goal["update_count"] = int(goal.get("update_count") or 0) + 1
            goal["status"] = "blocked"
            goal["verify_epoch"] = int(goal.get("verify_epoch") or 0) + 1
            goal["blocked_reason"] = reason
            goal["needs_user_prompt"] = True
            if bkey:
                goal["last_blocker_key"] = bkey
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
            goal["update_count"] = int(goal.get("update_count") or 0) + 1
            goal["updates"].append(
                {
                    "ts": now,
                    "session": session_id,
                    "kind": "progress",
                    "message": _clean(opts["message"]),
                }
            )
            audit_event = "goal_update"
            if status == "active":
                # Goal-CLI execs never reach _goal_mutation_hook, so the
                # unclaimed-work counter bumps here — otherwise progress
                # notes alone could dodge the unclaimed-work cap.
                escalate = _unclaimed_bump(goal, session_id, "goal-update")
        goal["updates"] = goal["updates"][-UPDATE_CAP:]
        goal["updated_at"] = now
        _save_goal(goal)
        _audit(session_id, audit_event, "id=%s" % goal["goal_id"])
    sys.stdout.write("goal_id=%s\n" % goal["goal_id"])
    sys.stdout.write("status=%s\n" % goal["status"])
    if escalate:
        sys.stdout.write("[devin-gates] %s\n" % escalate)
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
            if status in ("paused", "blocked"):
                # Exiting paused/blocked crosses a host-observed boundary:
                # a UserPromptSubmit event must have landed since the
                # block/pause (needs_user_prompt cleared by the hook, not
                # by the model). gates_off and an audited session
                # /gate-bypass lift the boundary check — kill-switch
                # semantics — but --reason is still required for the
                # audit trail. Order: boundary → reason → attach, so a
                # resume that cannot happen neither demands a reason nor
                # leaves the session attached to a still-stopped goal.
                st = _g("load_state")(session_id)
                bypassed = isinstance(st, dict) and bool(
                    (st.get("override_reason") or "").strip()
                )
                if (
                    goal.get("needs_user_prompt")
                    and _prompt_gate()
                    and not _g("gates_off")()
                    and not bypassed
                ):
                    return _err(
                        "%s is gated on a user prompt boundary — the user "
                        "must send a message first" % sub
                    )
                reason, rerr = _require_reason(opts, sub)
                if rerr is not None:
                    return rerr
                reason = _clean(reason)
                _attach(session_id, goal["goal_id"])
                goal["status"] = "active"
                goal["blocked_reason"] = None
                goal["needs_user_prompt"] = False
                goal["blocked_attempts"] = 0
                goal["unclaimed_rounds"] = 0
                # Resume earns a fresh stall slate — but only on a real
                # transition; the attach-only path must not reset it or a
                # parent could clear the streak at will. The strategist
                # refire cadence and any pending strategy.md requirement
                # restart here too — a status-changing resume is the
                # user-gated escape from the artifact step.
                goal["consecutive_same"] = 0
                goal["consecutive_distinct"] = 0
                goal["distinct_since_fire"] = 0
                goal["last_gap_fingerprint"] = None
                goal["strategist_pending"] = None
                goal["updated_at"] = _g("utc_now")()
                goal["updates"] = (goal.get("updates") or []) + [
                    {
                        "ts": goal["updated_at"],
                        "session": session_id,
                        "kind": "resumed",
                        "message": reason,
                    }
                ]
                goal["updates"] = goal["updates"][-UPDATE_CAP:]
                _save_goal(goal)
                _audit(
                    session_id,
                    audit_event,
                    "id=%s reason=%s" % (goal["goal_id"], reason),
                )
            else:
                _attach(session_id, goal["goal_id"])
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
        if to_status == "paused":
            goal["needs_user_prompt"] = True
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


def _age_str(created_at):
    """Human age from an ISO timestamp — 'Z' suffix normalized for
    fromisoformat; unparseable/missing timestamps degrade to '-'."""
    if not created_at:
        return "-"
    try:
        ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return "-"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
    if secs < 3600:
        return "%dm" % max(1, secs // 60)
    if secs < 86400:
        return "%dh%dm" % (secs // 3600, (secs % 3600) // 60)
    return "%dd%dh" % (secs // 86400, (secs % 86400) // 3600)


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
        "[devin-gates] goal %s: %s | sweeps: %s | log: %d | attached: %s\n"
        % (
            goal.get("goal_id"),
            str(goal.get("status", "")).upper(),
            goal.get("verifier_sweeps", 0),
            len(updates),
            attached,
        )
    )
    bonus = int(goal.get("cap_bonus") or 0)
    fired = goal.get("strategist_fires") or goal.get("strategist_used")
    stall = STALL_SAME_STRATEGIST if fired else STALL_SAME
    sys.stdout.write(
        "[devin-gates] sweep-cap: %d%s | stall: %d/%d | blocked_reason=%s\n"
        % (
            _sweep_cap(),
            "(+%d)" % bonus if bonus else "",
            int(goal.get("consecutive_same") or 0),
            stall,
            goal.get("blocked_reason") or "-",
        )
    )
    sys.stdout.write(
        "[devin-gates] resume_gate=%s | blocked_attempts=%d/%d | "
        "unclaimed=%d | strategist_fires=%d pending=%s\n"
        % (
            "user-prompt" if goal.get("needs_user_prompt") else "open",
            int(goal.get("blocked_attempts") or 0),
            _blocked_streak(),
            int(goal.get("unclaimed_rounds") or 0),
            int(goal.get("strategist_fires") or 0),
            "yes" if goal.get("strategist_pending") is not None else "no",
        )
    )
    sys.stdout.write(
        "[devin-gates] tool_errors=%d/%d\n"
        % (
            int(goal.get("consecutive_tool_errors") or 0),
            _error_streak(),
        )
    )
    claim = goal.get("claim_seq")
    claim_txt = (
        "none"
        if claim is None
        else (
            "current"
            if int(claim) == int(goal.get("mutation_seq") or 0)
            else "stale"
        )
    )
    sys.stdout.write(
        "[devin-gates] kind=%s | claims=%d | updates=%d | age=%s | claim=%s\n"
        % (
            goal.get("goal_kind") or "general",
            int(goal.get("claim_count") or 0),
            int(goal.get("update_count") or 0),
            _age_str(goal.get("created_at")),
            claim_txt,
        )
    )
    sys.stdout.write("goal_allow_root=%s\n" % goal.get("allow_root", ""))
    sys.stdout.write("objective=%s\n" % goal.get("objective", ""))
    live = _read_checklist(goal)
    live_sha = _sha16(live) if live is not None else "none"
    sys.stdout.write(
        "[devin-gates] baseline_sha256=%s | checklist_sha256=%s | "
        "checklist_drifted=%s\n"
        % (
            _baseline_sha(goal) or "none",
            live_sha,
            "true" if goal.get("checklist_drifted") else "false",
        )
    )
    lines = (live or "").splitlines()
    if lines:
        sys.stdout.write("checklist (last %d):\n" % CHECKLIST_TAIL)
        for line in lines[-CHECKLIST_TAIL:]:
            sys.stdout.write("  %s\n" % line)
    return 0


def cmd_goal_baseline(args):
    opts, pos, err = _parse_opts(
        args, with_value=("--session-id",), boolean=("--show",)
    )
    if err:
        return _err(err)
    if pos:
        return _err("unexpected arguments: %s" % " ".join(pos))
    session_id = _resolve_session_id(opts)
    with _goal_lock():
        goal = _load_goal_for_cli()
        attached_err = _require_attached(session_id, goal)
        if attached_err is not None:
            return attached_err
        baseline = goal.get("checklist_baseline")
        if opts.get("show"):
            if not isinstance(baseline, str) or not baseline:
                return _err(
                    "no baseline recorded for goal %s" % goal["goal_id"]
                )
            sys.stdout.write(baseline)
            if not baseline.endswith("\n"):
                sys.stdout.write("\n")
            return 0
        if isinstance(baseline, str) and baseline:
            # Once-only: a second capture would let a weakened checklist
            # replace the recorded one — refuse and audit the attempt.
            _audit(
                session_id,
                "goal_baseline_exists",
                "id=%s" % goal["goal_id"],
            )
            return _err(
                "baseline already recorded for goal %s — it is immutable"
                % goal["goal_id"]
            )
        berr = _capture_baseline(goal, session_id)
        if berr is not None:
            return _err(berr)
        goal["updated_at"] = _g("utc_now")()
        _save_goal(goal)
    sys.stdout.write("goal_id=%s\n" % goal["goal_id"])
    sys.stdout.write("baseline_sha256=%s\n" % _sha16(goal["checklist_baseline"]))
    return 0


_GOAL_HANDLERS = {
    "set-goal": cmd_set_goal,
    "goal-update": cmd_goal_update,
    "goal-pause": cmd_goal_pause,
    "goal-resume": cmd_goal_resume,
    "goal-clear": cmd_goal_clear,
    "goal-status": cmd_goal_status,
    "goal-baseline": cmd_goal_baseline,
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


def _sweep_cap():
    try:
        return max(1, int(os.environ.get("DEVIN_GOAL_SWEEP_CAP") or GOAL_SWEEP_CAP))
    except (TypeError, ValueError):
        return GOAL_SWEEP_CAP


def _extract_verdict_json(output):
    """The last ```goal-verdict fenced block before the final GATES_VERDICT
    line. Returns the parsed dict or None when absent/malformed — fail-closed,
    callers then fall back to per-item lines for the fingerprint."""
    if not output:
        return None
    verdict_pos = -1
    for m in _g("VERDICT_RE").finditer(output):
        verdict_pos = m.start()
    if verdict_pos < 0:
        return None
    start = None
    for m in _VERDICT_FENCE_RE.finditer(output):
        if m.end() <= verdict_pos:
            start = m.end()
    if start is None:
        return None
    end = output.find("```", start)
    if end < 0:
        return None
    try:
        obj = json.loads(output[start:end])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _verdict_json_consistent(verdict, obj):
    """The GATES_VERDICT line is authoritative; a goal-verdict block that
    contradicts it is malformed (fail-closed → treated as no findings)."""
    if not isinstance(obj, dict):
        return False
    findings = obj.get("findings") or []
    if not isinstance(findings, list):
        return False

    def _blocking(f):
        if not isinstance(f, dict):
            return "none"
        return str(f.get("blocking") or "none").lower()

    if verdict == "BLOCKED":
        # BLOCKED = every finding non-model-fixable, plus a blocker_key.
        if not findings:
            return False
        if not all(
            _blocking(f) in ("contradiction", "unverifiable") for f in findings
        ):
            return False
        key = obj.get("blocker_key")
        return isinstance(key, str) and bool(BLOCKER_KEY_RE.match(key))
    if verdict == "FAIL":
        # FAIL = at least one model-fixable finding; all-blocking findings
        # should have been a BLOCKED verdict.
        return not (findings and all(_blocking(f) != "none" for f in findings))
    if verdict == "PASS":
        # The block is optional on PASS, but a NON-EMPTY findings list
        # contradicts the verdict — the caller demotes that to FAIL.
        return not findings
    return True  # missing verdict: keep the block for fingerprinting


def _norm_gap(t):
    t = str(t or "").lower()
    t = re.sub(r"(?:[a-z]:\\|/)[^\s,'\"]+", "<p>", t)  # strip paths
    t = re.sub(r"\d+", "<n>", t)  # strip line numbers / counts
    t = re.sub(r"[^a-z<>:]+", " ", t)
    return " ".join(t.split())[:120]


def _finding_kind(f):
    kind = _clean(str(f.get("kind") or "gap")).lower()
    return kind if kind in ("bug", "gap", "todo") else "gap"


def _gap_fingerprint(findings, output):
    """(kind,item) set → sha256[:16]. Detail text is deliberately excluded so
    identical gaps fingerprint identically across rephrasing."""
    keys = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        item = _norm_gap(f.get("item") or f.get("location") or f.get("detail"))
        keys.append("%s:%s" % (_finding_kind(f), item))
    if not keys:  # fallback: per-item REFUTED/UNVERIFIABLE lines
        for line in (output or "").splitlines():
            m = _REFUTED_LINE_RE.match(line)
            if m:
                keys.append("x:" + _norm_gap(m.group(1)))
    if not keys:
        return None
    return hashlib.sha256("|".join(sorted(keys)).encode("utf-8")).hexdigest()[:16]


def _findings_summary(findings):
    parts = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        bits = f.get("item") or f.get("location") or f.get("detail") or ""
        parts.append("%s:%s" % (_finding_kind(f), _clean(str(bits))[:80]))
    return "; ".join(parts)[:400]


def _auto_block(goal, sid, reason, audit_event, tool="run_subagent"):
    """Gate-initiated blocked transition: same shape as goal-update --blocked
    (epoch bump, updates entry, blocked_reason) plus its own audit event.
    Every auto-block is user-gated: needs_user_prompt means goal-resume
    cannot run until a UserPromptSubmit event lands (see _transition).
    Caller holds _goal_lock and saves afterwards."""
    goal["status"] = "blocked"
    goal["verify_epoch"] = int(goal.get("verify_epoch") or 0) + 1
    goal["blocked_reason"] = reason
    goal["needs_user_prompt"] = True
    goal["updated_at"] = _g("utc_now")()
    goal["updates"] = (goal.get("updates") or []) + [
        {
            "ts": goal["updated_at"],
            "session": sid,
            "kind": "blocked",
            "message": reason,
        }
    ]
    goal["updates"] = goal["updates"][-UPDATE_CAP:]
    _audit(
        sid,
        audit_event,
        "id=%s reason=%s" % (goal["goal_id"], reason),
        tool_name=tool,
        decision="block",
    )


def _prompt_gate():
    """DEVIN_GOAL_PROMPT_GATE=0 disables the user-prompt-boundary resume
    gate for environments where UserPromptSubmit is not delivered."""
    return os.environ.get("DEVIN_GOAL_PROMPT_GATE", "1").strip() != "0"


def _blocked_streak():
    try:
        return max(1, int(os.environ.get("DEVIN_GOAL_BLOCKED_STREAK") or 3))
    except (TypeError, ValueError):
        return 3


def _claim_nudge():
    try:
        return max(1, int(os.environ.get("DEVIN_GOAL_CLAIM_NUDGE") or 8))
    except (TypeError, ValueError):
        return 8


def _unclaimed_cap():
    raw = os.environ.get("DEVIN_GOAL_UNCLAIMED_CAP")
    if raw is not None and raw.strip() == "0":
        return 0  # disabled — must not compare (any count >= 0)
    try:
        return max(1, int(raw or 40))
    except (TypeError, ValueError):
        return 40


def _error_streak():
    try:
        return max(1, int(os.environ.get("DEVIN_GOAL_ERROR_STREAK") or 3))
    except (TypeError, ValueError):
        return 3


def _goal_infra_count(sid, tool, resp):
    """G8 streak bookkeeping: a failed in-scope call increments
    consecutive_tool_errors (and can infra-errors: auto-block); a
    successful one resets the streak. Also called inside the run_subagent
    interceptors — verifier and strategist PostToolUse never reach
    _goal_error_hook (they are fully intercepted), so their bookkeeping
    happens here, before any resume/witness check (a failed RESUMED
    spawn counts too). Returns an escalation message for the caller to
    surface, or None."""
    with _goal_lock():
        goal = _current_goal()
        if not goal or goal.get("status") != "active":
            return None
        if _g("tool_response_success")(resp):
            if int(goal.get("consecutive_tool_errors") or 0):
                goal["consecutive_tool_errors"] = 0
                goal["updated_at"] = _g("utc_now")()
                _save_goal(goal)
            return None
        n = int(goal.get("consecutive_tool_errors") or 0) + 1
        goal["consecutive_tool_errors"] = n
        goal["updated_at"] = _g("utc_now")()
        msg = None
        if n >= _error_streak():
            reason = "infra-errors: %d consecutive failed infra calls" % n
            _auto_block(goal, sid, reason, "goal_infra_errors", tool)
            msg = (
                "goal %s auto-blocked: %s — goal-resume unlocks after "
                "the user's next prompt" % (goal["goal_id"], reason)
            )
        _save_goal(goal)
        return msg


_INFRA_TOOLS = ("run_subagent", "write_to_process", "mcp_call_tool")


def _goal_error_hook(sid, tool, ti, resp):
    """Consecutive infra-channel failures → infra-errors: auto-block.
    Scope is pinned to channels whose failure is unambiguously
    infrastructural: exec is excluded (a nonzero exit is ambiguous — a
    failing test is work, not infra), read/grep/find misses are ordinary
    exploration, and failed write/edit calls are usually the model's own
    error (stale old_string). Goal-CLI execs are doubly excluded."""
    if tool not in _INFRA_TOOLS and not tool.startswith("mcp__"):
        return None
    return _goal_infra_count(sid, tool, resp)


def _unclaimed_bump(goal, sid, tool):
    """One unclaimed work event on an active goal: increment, then nudge
    past DEVIN_GOAL_CLAIM_NUDGE and hard-block past DEVIN_GOAL_UNCLAIMED_CAP.
    Shared by _goal_mutation_hook (workspace mutations) and cmd_goal_update
    (--message — goal-CLI execs never reach the mutation hook, so without
    this call progress notes could dodge the cap). Returns an escalation
    message for the caller to surface, or None. Caller holds _goal_lock."""
    n = int(goal.get("unclaimed_rounds") or 0) + 1
    goal["unclaimed_rounds"] = n
    cap = _unclaimed_cap()
    if cap and n >= cap:
        reason = "unclaimed-work: %d rounds since last claim" % n
        _auto_block(goal, sid, reason, "goal_unclaimed", tool)
        return (
            "goal %s auto-blocked: %s — claim (goal-update --claim-done + "
            "fresh verifier) or the user's next prompt unlocks goal-resume"
            % (goal["goal_id"], reason)
        )
    if n >= _claim_nudge():
        return (
            "goal %s: %d work events since last claim — claim "
            "(goal-update --claim-done + fresh verifier) or pause/block now"
            % (goal["goal_id"], n)
        )
    return None


def _clear_prompt_boundary(sid):
    """The first UserPromptSubmit after a block/pause clears the resume
    gate. UserPromptSubmit is host-emitted in normal flow — the one real
    "a user turn intervened" signal the hooks can gate on. Honest limit:
    an unlocked session could exec the gate CLI with a synthesized event —
    audited like every other escape, not prevented. Runs even under
    gates_off (state maintenance, not enforcement)."""
    with _goal_lock():
        goal = _current_goal()
        if not goal or not goal.get("needs_user_prompt"):
            return
        goal["needs_user_prompt"] = False
        goal["updated_at"] = _g("utc_now")()
        _save_goal(goal)
        _audit(sid, "goal_prompt_boundary", "id=%s" % goal["goal_id"])


def _goal_spawn_gate(sid, ti):
    """goal-verifier may be spawned only by a session attached to an active
    goal that has claimed the current tree — regardless of lock state
    (design: no plan-passed required). gates_off and an audited
    /gate-bypass bypass the ATTACHMENT requirement like every other goal
    check; the claim requirement is witness semantics — only gates_off
    lifts it, /gate-bypass does not (bypass lifts work locks, not the
    claim→verify binding)."""
    st = _g("load_state")(sid)
    gid = _attached_goal_id(sid)
    goal = _load_goal_file(_goal_path(gid)) if gid else None
    if goal and goal.get("status") == "active":
        # The drift stamp is a goal-file write, so the whole
        # load→stamp→validate sequence runs under _goal_lock with status
        # re-validated inside — a lockless read-modify-save could lose a
        # concurrent transition.
        with _goal_lock():
            goal = _load_goal_file(_goal_path(gid))
            st = _g("load_state")(sid)
            if goal and goal.get("status") == "active":
                if ti.get("resume"):
                    # A resumed verifier is never a witness — its post
                    # early-allows — so it needs no claim, no objective
                    # binding, and must not touch the in-flight binding.
                    return _g("allow")()
                task = ti.get("task")
                if not isinstance(task, str):
                    task = ""
                if _checklist_drift_stamp(goal, sid):
                    _save_goal(goal)
                binding = (
                    st.get("goal_verify") if isinstance(st, dict) else None
                )
                binding_current = isinstance(binding, dict) and (
                    binding.get("gid") == goal["goal_id"]
                    and binding.get("seq")
                    == int(goal.get("mutation_seq") or 0)
                    and binding.get("epoch")
                    == int(goal.get("verify_epoch") or 0)
                )
                if not _g("gates_off")():
                    # One verifier in flight per session: the single-slot
                    # goal_verify binding cannot represent two spawns — a
                    # second would rebind seq/epoch and let the first's
                    # stale result pass the post freshness check. A STALE
                    # binding (mutation/epoch bump since spawn, or a
                    # different goal) does not block: its result lands
                    # late anyway, so it is voided below to free the slot.
                    if binding_current:
                        return _g("block")(
                            "run_subagent blocked: a goal-verifier run is "
                            "already in flight for this session — wait for "
                            "its result before spawning another.",
                            sid,
                            "PreToolUse",
                            "run_subagent",
                        )
                    # A task without the goal_id can never bind at post —
                    # it would burn a whole subagent run just to land as a
                    # late result, so refuse at spawn instead.
                    if goal["goal_id"] not in task:
                        return _g("block")(
                            "run_subagent blocked: verifier task must "
                            "contain the goal_id %s — a result that "
                            "cannot bind to the goal lands as a late "
                            "result." % goal["goal_id"],
                            sid,
                            "PreToolUse",
                            "run_subagent",
                        )
                    # `claim_seq` distinguishes absent (None) from a
                    # legitimate claim on a never-mutated goal
                    # (mutation_seq starts at 0, so claim_seq == 0 is a
                    # real claim — a truthiness check would block that
                    # goal's verifier forever).
                    claim = goal.get("claim_seq")
                    if claim is None or int(claim) != int(
                        goal.get("mutation_seq") or 0
                    ):
                        return _g("block")(
                            "run_subagent blocked: goal-verifier requires a "
                            "goal-update --claim-done on the current tree (no "
                            "claim recorded, or a mutation landed after the "
                            "last claim).",
                            sid,
                            "PreToolUse",
                            "run_subagent",
                        )
                    # The verifier judges the objective's actual words, so
                    # the task must carry the SIGNED objective verbatim —
                    # a paraphrased/weakened objective is refused here
                    # rather than caught at verdict time.
                    if (goal.get("objective") or "") not in task:
                        return _g("block")(
                            "run_subagent blocked: verifier task must "
                            "contain the goal objective verbatim — paste "
                            "it unchanged from set-goal/goal-status "
                            "output.",
                            sid,
                            "PreToolUse",
                            "run_subagent",
                        )
                # Bind the coming verdict to the goal and tree at spawn
                # time: a workspace mutation or a pause/resume cycle
                # landing mid-verification makes the result late, so the
                # parent must re-verify against fresh evidence. Written
                # ONLY on the allow path — a refused spawn must not
                # overwrite the binding of a verifier already in flight.
                # Whatever binding was already there is voided first: it
                # is stale-or-superseded (a still-current one refused the
                # spawn above, except under gates_off), and recording its
                # task sha keeps its orphaned result from minting against
                # this new binding when the two tasks collide.
                if isinstance(st, dict):
                    if binding is not None:
                        _void_verify(st, binding)
                    st["session_id"] = sid
                    st["goal_verify"] = {
                        "gid": goal["goal_id"],
                        "seq": int(goal.get("mutation_seq") or 0),
                        "epoch": int(goal.get("verify_epoch") or 0),
                        "task": _sha16(task),
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


def _goal_strategist_spawn_gate(sid):
    """goal-strategist spawns only while a fire is pending — the pending
    window is the only legitimate reason to spawn one. gates_off allows
    (kill-switch semantics, same as the verifier gate)."""
    if _g("gates_off")():
        return _g("allow")()
    gid = _attached_goal_id(sid)
    goal = _load_goal_file(_goal_path(gid)) if gid else None
    if (
        goal
        and goal.get("status") == "active"
        and goal.get("strategist_pending") is not None
    ):
        return _g("allow")()
    return _g("block")(
        "run_subagent blocked: goal-strategist requires an attached active "
        "goal with a pending strategist fire.",
        sid,
        "PreToolUse",
        "run_subagent",
    )


def _post_goal_strategist(sid, ti, resp):
    """Own PostToolUse for profile=goal-strategist end-to-end: fully
    intercepts so the read-only profile never reaches the gate's own
    accounting — neither goal mutation_seq nor gate source_seq advances
    on a strategist run. Performs the in-scope G8 infra-error bookkeeping
    (the event never reaches _goal_error_hook), then allows — the
    strategist is advice, not a witness."""
    msg = _goal_infra_count(sid, "run_subagent", resp)
    if msg:
        return (_g("reminder_payload")("PostToolUse", msg), 0)
    return _g("allow")()


def _post_goal_verifier(sid, ti, resp):
    """Own PostToolUse for profile=goal-verifier end-to-end: witness recording,
    sweep counting, the status=complete mint, and late-result rejection.
    Fully intercepts (never delegates) so the gate does not count the
    read-only verifier as a source mutation."""
    # Infra bookkeeping runs before the resume check and before witness
    # handling — a failed resumed verifier spawn counts against the
    # streak, and a tripped streak must surface even on the early-return
    # paths below.
    emsg = _goal_infra_count(sid, "run_subagent", resp)
    if ti.get("resume"):
        if emsg:
            return (_g("reminder_payload")("PostToolUse", emsg), 0)
        return _g("allow")()  # resumed verifier is not a witness
    task = ti.get("task")
    if not isinstance(task, str):
        task = ""
    # Consume the in-flight binding ONLY for the result that owns it —
    # matched by the spawn-task sha recorded in the binding. A foreign
    # verifier result (an earlier spawn whose binding was voided and
    # rebound, or a run that never bound) must neither mint nor free the
    # slot for the run still in flight. A sha on the void list marks an
    # orphaned result: it lands late, and when its sha collides with the
    # live binding's it consumes that too — we cannot tell which spawn it
    # answered, so the ambiguous result is suppressed and the slot freed.
    st = _g("load_state")(sid)
    verify = None
    voided = False
    if isinstance(st, dict):
        sha = _sha16(task)
        voids = st.get("goal_verify_void")
        if isinstance(voids, list) and sha in voids:
            voids.remove(sha)
            voided = True
        v = st.get("goal_verify")
        if isinstance(v, dict):
            if v.get("task") == sha:
                verify = st.pop("goal_verify")
        elif v is not None:
            st.pop("goal_verify", None)
        st["session_id"] = sid
        _g("save_state")(st)
    if voided:
        verify = None
    if emsg:
        return (_g("reminder_payload")("PostToolUse", emsg), 0)
    if not _g("tool_response_success")(resp):
        return _g("allow")()  # an infra failure is not a verdict; no sweep
    output = _g("tool_response_output")(resp)
    verdict = _g("parse_verdict")(output)
    obj = _extract_verdict_json(output)
    consistent = obj is not None and _verdict_json_consistent(verdict, obj)
    findings = (obj.get("findings") or []) if consistent else []
    summary = (obj.get("summary") or "") if consistent else ""
    bkey = (obj.get("blocker_key") or "") if consistent else ""
    malformed_pass = verdict == "PASS" and (
        (obj is not None and not consistent)
        or any(
            _REFUTED_LINE_RE.match(l) for l in (output or "").splitlines()
        )
    )
    if malformed_pass:
        # PASS contradicted by a non-empty/inconsistent findings block or
        # per-item REFUTED/UNVERIFIABLE lines: demote to FAIL for all
        # downstream bookkeeping — sweep counted, witness records FAIL,
        # streak fingerprints on the RAW findings (consistent=False emptied
        # `findings` above, so re-read them from the parsed block).
        verdict = "FAIL"
        if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
            findings = obj["findings"]
    fsum = _findings_summary(findings)
    match = _g("AGENT_ID_RE").search(output or "")
    subagent_id = match.group(1) if match else ""
    strategist_fired = False
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
        # A counted sweep is fresh engagement — reset the self-reported
        # blocked streak and the unclaimed-work counter alongside the
        # rest of the retry bookkeeping.
        goal["blocked_attempts"] = 0
        goal["unclaimed_rounds"] = 0
        wit = {
            "kind": "goal",
            "sweep": goal["verifier_sweeps"],
            "verdict": verdict or "FAIL",
            "subagent_id": subagent_id,
            "mutation_seq": int(goal.get("mutation_seq") or 0),
            "ts": _g("utc_now")(),
        }
        goal["witnesses"] = (list(goal.get("witnesses") or []) + [wit])[
            -UPDATE_CAP:
        ]
        _audit(
            sid,
            "goal_verifier_witness",
            "id=%s verdict=%s sweep=%d"
            % (goal["goal_id"], wit["verdict"], goal["verifier_sweeps"]),
            tool_name="run_subagent",
            decision="witness",
        )
        if malformed_pass:
            _audit(
                sid,
                "goal_pass_malformed",
                "id=%s" % goal["goal_id"],
                tool_name="run_subagent",
                decision="demote",
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
        elif verdict == "BLOCKED":
            if bkey and bkey == goal.get("last_blocker_key"):
                _auto_block(
                    goal, sid, "external-repeat: %s" % bkey, "goal_blocker_repeat"
                )
            else:
                _auto_block(
                    goal,
                    sid,
                    "blocking: %s"
                    % (bkey or _clean(summary) or "verifier-blocked"),
                    "goal_blocked_auto",
                )
            if bkey:
                goal["last_blocker_key"] = bkey
        else:  # FAIL or missing verdict — streak bookkeeping + auto-blocks
            if verdict is None:
                # Missing verdict: always the sentinel — REFUTED/UNVERIFIABLE
                # lines in contract-breaking output must not feed the
                # distinct-gaps strategist path.
                fp = "malformed"
            else:
                fp = _gap_fingerprint(findings, output) or "malformed"
            wit["fingerprint"] = fp
            if fp == goal.get("last_gap_fingerprint"):
                goal["consecutive_same"] = int(goal.get("consecutive_same") or 0) + 1
                goal["consecutive_distinct"] = 0
                goal["distinct_since_fire"] = 0
                goal["last_gap_fingerprint"] = fp
            else:
                goal["consecutive_same"] = 1
                goal["consecutive_distinct"] = (
                    int(goal.get("consecutive_distinct") or 0) + 1
                )
                goal["distinct_since_fire"] = (
                    int(goal.get("distinct_since_fire") or 0) + 1
                )
                goal["last_gap_fingerprint"] = fp
            stall = (
                STALL_SAME_STRATEGIST
                if (
                    goal.get("strategist_fires")
                    or goal.get("strategist_used")
                )
                else STALL_SAME
            )
            if int(goal.get("consecutive_same") or 0) >= stall:
                _auto_block(
                    goal,
                    sid,
                    "no-progress: identical gaps in %d consecutive sweeps"
                    % goal["consecutive_same"],
                    "goal_no_progress",
                )
            else:
                # Refire at every STRATEGIST_DISTINCT consecutive distinct
                # FAILs — distinct_since_fire resets here, on an identical
                # fingerprint, and on resume/reopen, so the cadence
                # survives streak resets (a lifetime-multiple formula
                # would be unreachable once consecutive_distinct reset).
                if (
                    int(goal.get("distinct_since_fire") or 0)
                    >= STRATEGIST_DISTINCT
                ):
                    goal["strategist_fires"] = (
                        int(goal.get("strategist_fires") or 0) + 1
                    )
                    goal["distinct_since_fire"] = 0
                    goal["strategist_used"] = True  # backward compat
                    goal["cap_bonus"] = min(
                        int(goal.get("cap_bonus") or 0) + STRATEGIST_BONUS,
                        4,
                    )
                    # The claim gate requires strategy.md written after
                    # this stamp — nanosecond epochs compare straight
                    # against st_mtime_ns with no float granularity edge.
                    goal["strategist_pending"] = time.time_ns()
                    strategist_fired = True
                    _audit(
                        sid,
                        "goal_strategist",
                        "id=%s fire=%d bonus=+%d"
                        % (
                            goal["goal_id"],
                            goal["strategist_fires"],
                            goal["cap_bonus"],
                        ),
                        tool_name="run_subagent",
                        decision="grant",
                    )
                eff = _sweep_cap() + int(goal.get("cap_bonus") or 0)
                if goal["status"] == "active" and int(
                    goal["verifier_sweeps"]
                ) >= eff:
                    _auto_block(
                        goal,
                        sid,
                        "sweep-cap: %d/%d" % (goal["verifier_sweeps"], eff),
                        "goal_cap",
                    )
        _save_goal(goal)
    note = (
        " Verifier returned no GATES_VERDICT line; treated as FAIL."
        if verdict is None
        else (
            " Verifier PASS contradicted by findings; treated as FAIL."
            if malformed_pass
            else ""
        )
    )
    drift_txt = ""
    if goal.get("checklist_drifted"):
        drift_txt = (
            " Checklist differs from the recorded baseline — the verifier "
            "will judge whether the drift is legitimate."
        )
    if goal.get("status") == "blocked":
        msg = (
            "goal %s auto-blocked: %s. Evidence and scratch live under "
            "goal_allow_root=%s; goal-resume unlocks after the user's next "
            "prompt, goal-clear to abandon.%s%s"
            % (goal["goal_id"], goal.get("blocked_reason"),
               goal.get("allow_root") or "", note, drift_txt)
        )
        if fsum:
            msg += " Findings: %s" % fsum
        return (_g("reminder_payload")("PostToolUse", msg), 0)
    if strategist_fired:
        msg = (
            "goal %s: %d consecutive distinct gap sets — whack-a-mole. "
            "Spawn a fresh goal-strategist and write strategy.md under "
            "goal_allow_root (the new HOW, not the WHAT) — --claim-done "
            "refuses until it exists. Sweep cap extended to %d, stall "
            "threshold relaxed to %d.%s%s"
            % (
                goal["goal_id"],
                goal.get("consecutive_distinct"),
                _sweep_cap() + int(goal.get("cap_bonus") or 0),
                STALL_SAME_STRATEGIST,
                note,
                drift_txt,
            )
        )
        if fsum:
            msg += " Findings: %s" % fsum
        return (_g("reminder_payload")("PostToolUse", msg), 0)
    if verdict is None:
        return (
            _g("reminder_payload")(
                "PostToolUse",
                "goal-verifier returned no GATES_VERDICT line; treated as "
                "FAIL.%s" % drift_txt,
            ),
            0,
        )
    if malformed_pass:
        msg = (
            "goal-verifier verdict PASS contradicted by findings/REFUTED "
            "lines — treated as FAIL (sweep %d).%s"
            % (goal["verifier_sweeps"], drift_txt)
        )
        if fsum:
            msg += " Findings: %s" % fsum
        return (_g("reminder_payload")("PostToolUse", msg), 0)
    if verdict == "FAIL" and fsum:
        return (
            _g("reminder_payload")(
                "PostToolUse",
                "goal-verifier FAIL (sweep %d). Findings: %s%s"
                % (goal["verifier_sweeps"], fsum, drift_txt),
            ),
            0,
        )
    return _g("allow")()


# ---------------------------------------------------------------- mutations


def _goal_source_mutation(tool, ti, resp):
    """Whether a successful PostToolUse counts as a workspace-source mutation
    for goal mutation_seq. Mirrors the gate's source_seq classifier, but
    scoped to paths under the workspace realpath (allow-roots live outside
    it, so checklist/evidence writes never count). write_to_process is
    counted conservatively — stdin to a background process can mutate the
    tree even though the gate's own classifier skips it."""
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
        # goal-strategist's PostToolUse is fully intercepted before this
        # runs — the union is defense-in-depth if interception order ever
        # changes, keeping the "read-only profiles don't mutate" invariant
        # explicit.
        readonly = set(_g("READONLY_PROFILES")) | {
            "goal-verifier",
            "goal-strategist",
        }
        return prof not in readonly
    if tool == "mcp_call_tool" or tool.startswith("mcp__"):
        return True
    return False


def _goal_mutation_hook(sid, tool, ti, resp):
    """Workspace-source mutation bookkeeping: bump mutation_seq, reopen a
    complete goal, count unclaimed work while active. Returns an
    escalation message to merge into the gate's PostToolUse output (never
    a short-circuit — that would skip the gate's own source_seq
    accounting), or None."""
    if not _goal_source_mutation(tool, ti, resp):
        return None
    with _goal_lock():
        goal = _current_goal()
        if not goal:
            return None
        goal["mutation_seq"] = int(goal.get("mutation_seq") or 0) + 1
        goal["updated_at"] = _g("utc_now")()
        msg = None
        if goal.get("status") == "complete":
            goal["status"] = "active"
            goal["consecutive_same"] = 0
            goal["consecutive_distinct"] = 0
            goal["distinct_since_fire"] = 0
            goal["last_gap_fingerprint"] = None
            goal["blocked_attempts"] = 0
            goal["unclaimed_rounds"] = 0
            _audit(
                sid,
                "goal_reopened",
                "id=%s seq=%d" % (goal["goal_id"], goal["mutation_seq"]),
                tool_name=tool,
                decision="reopen",
            )
        elif goal.get("status") == "active":
            msg = _unclaimed_bump(goal, sid, tool)
        _save_goal(goal)
        return msg


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
    unclaimed = int(goal.get("unclaimed_rounds") or 0)
    escalate = ""
    if unclaimed >= _claim_nudge():
        escalate = (
            " %d work events since last claim — claim "
            "(goal-update --claim-done + fresh verifier) or pause/block "
            "now." % unclaimed
        )
    reason = (
        "[devin-gates] goal %s active: %s (last update: %s; verifier sweeps: %s).%s "
        "Continue working, run a fresh goal-verifier, or goal-pause / "
        "goal-update --blocked / goal-clear with --reason. "
        "Stop-loop: block %d/%d this turn; after %d the turn will be allowed to end."
        % (gid, goal.get("objective") or "", last or "-",
           goal.get("verifier_sweeps", 0), escalate, n + 1, maxb, maxb)
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
            "/goal resume needs a user prompt + --reason, "
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
            line = (
                "goal %s active: %s — Stop is blocked until a fresh "
                "goal-verifier PASSes; goal-pause / goal-update --blocked / "
                "goal-clear with --reason to defer." % (gid, objective)
            )
            n = int(goal.get("unclaimed_rounds") or 0)
            if n >= _claim_nudge():
                line += (
                    " — %d work events since last claim; claim "
                    "(goal-update --claim-done + fresh verifier) or "
                    "pause/block now" % n
                )
            return line
        if status in ("paused", "blocked"):
            # This prompt is exactly the boundary that unlocked resume —
            # surface the block state where the user can see it.
            return (
                "goal %s %s: %s — prompt boundary cleared; goal-resume "
                '--reason "..." to retry, goal-clear --reason "..." to '
                "abandon"
                % (gid, status.upper(), goal.get("blocked_reason") or objective)
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


def _merge_reminder_payload(out, name, line, code):
    if not line:
        return (out, code)
    try:
        payload = json.loads(out)
        if not isinstance(payload, dict):
            raise ValueError("non-dict payload")
        # A parsed orig payload is never discarded — a top-level decision
        # (block/etc.) survives; only hookSpecificOutput is synthesized
        # when absent so the reminder can ride along.
        spec = payload.setdefault("hookSpecificOutput", {})
        spec["hookEventName"] = name
        ctx = spec.get("additionalContext") or ""
        spec["additionalContext"] = (ctx + "\n" + line).strip()
        return (_g("canonical_dumps")(payload) + "\n", code)
    except (ValueError, AttributeError, KeyError, TypeError):
        # orig emitted no mergeable payload (a plain "" allow, the common
        # PostToolUse case) — emit a standalone reminder labelled with the
        # REAL event so the escalation actually reaches the model instead
        # of being silently dropped or mislabelled.
        return (_g("reminder_payload")(name, line), code)


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
        if tool == "run_subagent":
            prof = ti.get("profile") or ""
            if prof == "goal-verifier":
                return _goal_spawn_gate(sid, ti)
            if prof == "goal-strategist":
                return _goal_strategist_spawn_gate(sid)
        path = _write_path(tool, ti)
        if path and _under_goal_root(sid, path):
            return _g("allow")()
        return None
    if name == "PostToolUse":
        tool = event.get("tool_name")
        ti = event.get("tool_input") or {}
        resp = event.get("tool_response")
        if tool == "run_subagent":
            prof = ti.get("profile") or ""
            if prof == "goal-verifier":
                return _post_goal_verifier(sid, ti, resp)
            if prof == "goal-strategist":
                return _post_goal_strategist(sid, ti, resp)
        msg = _goal_mutation_hook(sid, tool, ti, resp)
        emsg = _goal_error_hook(sid, tool, ti, resp)
        if msg or emsg:
            return (
                _MERGE_REMINDER,
                name,
                "\n".join(m for m in (msg, emsg) if m),
            )
        return None
    if name == "Stop":
        return _goal_stop(event, sid)
    if name == "SessionEnd":
        _session_end(sid)
        return None
    if name == "UserPromptSubmit":
        # Host-emitted in normal flow: a real user turn clears the resume
        # gate. State maintenance, not a reminder — it must run before
        # gates_off or a DEVIN_GATES_OFF environment would never clear
        # the flag and resume would be permanently refused. Cleared on
        # ANY session's prompt (the attached session may have ended).
        _clear_prompt_boundary(sid)
        if _g("gates_off")():
            return None
        return (_MERGE_REMINDER, name, _goal_reminder_line(name, sid))
    if name in ("SessionStart", "PostCompaction"):
        if _g("gates_off")():
            return None
        return (_MERGE_REMINDER, name, _goal_reminder_line(name, sid))
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
                        merge = (out[1], out[2])
                    else:
                        return out
        except Exception:
            pass
        o, code = orig(event)
        if merge is not None:
            return _merge_reminder_payload(o, merge[0], merge[1], code)
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

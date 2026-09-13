#!/usr/bin/env python3
"""Unit tests T1–T44 for hooks/devin-gates.py. No live Devin required."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_PY = os.path.join(ROOT, "hooks", "devin-gates.py")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SESSION = "test-session-1"
PROMPT = "prompt-1"


def load_gates():
    spec = importlib.util.spec_from_file_location("devin_gates", GATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gates = load_gates()


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data.pop("_comment", None)
    return data


def pre(tool, tool_input, session_id=SESSION, prompt_id=PROMPT):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "session_id": session_id,
        "prompt_id": prompt_id,
    }


def post(tool, tool_input, output="", success=True, session_id=SESSION, prompt_id=PROMPT):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_response": {"success": success, "output": output, "error": None},
        "session_id": session_id,
        "prompt_id": prompt_id,
    }


def stop_event(prompt_id=PROMPT, session_id=SESSION):
    return {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "prompt_id": prompt_id,
        "stop_hook_active": False,
    }


class GateTest(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()
        self._old_cwd = os.getcwd()
        self.tmpdir = tempfile.mkdtemp(prefix="devin-gates-test-")
        self.state_dir = os.path.join(self.tmpdir, "state")
        self.cache_dir = os.path.join(self.tmpdir, "cache")
        self.workspace = os.path.join(self.tmpdir, "workspace")
        os.makedirs(self.state_dir, mode=0o700)
        os.makedirs(self.cache_dir, mode=0o700)
        os.makedirs(os.path.join(self.workspace, "src"), mode=0o755)
        self.secret = os.urandom(32)
        with open(os.path.join(self.state_dir, "secret"), "wb") as fh:
            fh.write(self.secret)
        os.chmod(os.path.join(self.state_dir, "secret"), 0o600)
        os.environ["DEVIN_SKILLS_STATE_DIR"] = self.state_dir
        os.environ["XDG_CACHE_HOME"] = self.cache_dir
        os.environ["DEVIN_SESSION_ID"] = SESSION
        os.environ["DEVIN_PROJECT_DIR"] = self.workspace
        os.environ.pop("DEVIN_GATES_OFF", None)
        os.environ.pop("XDG_DATA_HOME", None)
        os.chdir(self.tmpdir)
        self.env = os.environ.copy()

    def tearDown(self):
        os.chdir(self._old_cwd)
        for key in list(os.environ):
            if key not in self._old_env:
                os.environ.pop(key, None)
        os.environ.update(self._old_env)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_hook(self, payload, extra_env=None):
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, GATE_PY, "hook"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmpdir,
        )
        return proc

    def run_cli(self, args, extra_env=None):
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, GATE_PY] + list(args),
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmpdir,
        )
        return proc

    def seed(self, **fields):
        st = gates.default_state(SESSION)
        st["session_id"] = SESSION
        st.update(fields)
        self.assertTrue(gates.save_state(st))
        return st

    def load_st(self):
        return gates.load_state(SESSION)

    def marker_kinds(self):
        st = self.load_st()
        return [m.get("kind") for m in (st.get("markers") or []) if isinstance(m, dict)]

    def assert_allow(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        out = proc.stdout.strip()
        if out:
            data = json.loads(out)
            self.assertNotEqual(data.get("decision"), "block", data)

    def assert_block(self, proc, exit_code=0):
        self.assertEqual(proc.returncode, exit_code, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout.strip())
        self.assertEqual(data.get("decision"), "block")
        return data

    def plan_marker(self, **extra):
        marker = {
            "kind": "plan-passed",
            "created_at": "2026-01-01T00:00:00Z",
            "plan_hash": "",
            "subagent_id": "s1",
            "verdict": "PASS",
        }
        marker.update(extra)
        return marker

    def code_marker(self, **extra):
        marker = {
            "kind": "code-passed",
            "created_at": "2026-01-01T00:00:00Z",
            "plan_hash": "",
            "subagent_id": "s2",
            "verdict": "PASS",
        }
        marker.update(extra)
        return marker

    def write_payload(self, path=None, content="x\n"):
        if path is None:
            path = os.path.join(self.workspace, "src", "main.py")
        return pre("write", {"file_path": path, "content": content})

    def test_t01_write_empty_state(self):
        proc = self.run_hook(load_fixture("write_empty.json"))
        data = self.assert_block(proc, 0)
        self.assertIn("plan-skeptic", data.get("reason", ""))

    def test_t02_write_valid_hmac_plan_passed(self):
        self.seed(markers=[self.plan_marker()])
        proc = self.run_hook(self.write_payload())
        self.assert_allow(proc)

    def test_t03_write_forge_no_hmac(self):
        path = gates.session_state_path(SESSION)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"passed": True, "markers": [self.plan_marker()], "v": 1}, fh)
        proc = self.run_hook(self.write_payload())
        self.assert_block(proc, 0)

    def test_t04_write_hmac_wrong_secret(self):
        payload = gates.default_state(SESSION)
        payload["markers"] = [self.plan_marker()]
        body = {k: v for k, v in payload.items() if k != "hmac"}
        msg = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        payload["hmac"] = hmac.new(os.urandom(32), msg, hashlib.sha256).hexdigest()
        path = gates.session_state_path(SESSION)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        proc = self.run_hook(self.write_payload())
        self.assert_block(proc, 0)

    def test_t05_exec_echo_redirect_state(self):
        target = os.path.join(self.state_dir, "sessions", SESSION, "state.json")
        proc = self.run_hook(pre("exec", {"command": "echo passed > %s" % target}))
        self.assert_block(proc, 0)

    def test_t06_exec_python_c_open_state(self):
        target = os.path.join(self.state_dir, "sessions", SESSION, "state.json")
        cmd = "python3 -c \"open(%r,'w').write('x')\"" % target
        proc = self.run_hook(pre("exec", {"command": cmd}))
        self.assert_block(proc, 0)

    def test_t07_exec_git_status_locked(self):
        proc = self.run_hook(load_fixture("exec_git_status.json"))
        self.assert_allow(proc)

    def test_t08_exec_git_commit_locked(self):
        proc = self.run_hook(load_fixture("exec_git_commit.json"))
        self.assert_block(proc, 0)

    def test_t09_exec_allowlisted_mint_plan(self):
        cmd = "%s %s mint-plan" % (sys.executable, GATE_PY)
        proc = self.run_hook(pre("exec", {"command": cmd}))
        self.assert_allow(proc)
        self.assertNotIn("plan-passed", self.marker_kinds())

    def test_t10_mint_plan_without_witness(self):
        proc = self.run_cli(["mint-plan"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("plan-passed", self.marker_kinds())

    def test_t10b_mint_plan_fail_witness_resolved_blockers(self):
        self.seed(
            witnesses=[
                {
                    "kind": "plan",
                    "sweep": 1,
                    "verdict": "FAIL",
                    "subagent_id": "x",
                    "source_seq": 0,
                }
            ],
            sweeps={"plan": 1, "code": 0, "finding": 0},
        )
        proc = self.run_cli(["mint-plan", "--resolved-blockers", "all-fixed"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("plan-passed", self.marker_kinds())

    def test_t11_post_subagent_plan_skeptic_pass_synthetic(self):
        """Synthetic PostToolUse until tests/fixtures/live_run_subagent_post.json exists."""
        payload = load_fixture("synthetic_plan_skeptic_pass_post.json")
        proc = self.run_hook(payload)
        self.assert_allow(proc)
        st = self.load_st()
        self.assertIn("plan-passed", self.marker_kinds())
        kinds = [w.get("kind") for w in (st.get("witnesses") or [])]
        self.assertIn("plan", kinds)
        self.assertEqual((st.get("sweeps") or {}).get("plan"), 1)

    def test_t12_post_subagent_general_no_mint(self):
        payload = load_fixture("synthetic_plan_skeptic_pass_post.json")
        payload["tool_input"]["profile"] = "subagent_general"
        proc = self.run_hook(payload)
        self.assert_allow(proc)
        self.assertNotIn("plan-passed", self.marker_kinds())

    def test_t13_post_subagent_resume_no_mint(self):
        payload = load_fixture("synthetic_plan_skeptic_pass_post.json")
        payload["tool_input"]["resume"] = "agent-123"
        proc = self.run_hook(payload)
        self.assert_allow(proc)
        st = self.load_st()
        self.assertNotIn("plan-passed", self.marker_kinds())
        self.assertEqual((st.get("sweeps") or {}).get("plan", 0), 0)

    def test_t14_stop_no_marker_mutations(self):
        self.seed(source_seq=1, mutations=1)
        proc = self.run_hook(load_fixture("stop.json"))
        self.assert_block(proc, 0)

    def test_t15_stop_valid_code_passed(self):
        self.seed(
            source_seq=1,
            mutations=1,
            markers=[self.plan_marker(), self.code_marker()],
        )
        proc = self.run_hook(stop_event())
        self.assert_allow(proc)

    def test_t16a_stop_1_prior_block(self):
        self.seed(source_seq=1, mutations=1, stop_blocks={PROMPT: 1})
        proc = self.run_hook(stop_event())
        self.assert_block(proc, 0)

    def test_t16b_stop_2_prior_blocks(self):
        self.seed(source_seq=1, mutations=1, stop_blocks={PROMPT: 2})
        proc = self.run_hook(stop_event())
        self.assert_block(proc, 0)

    def test_t16_stop_3_prior_blocks_allow_audit(self):
        self.seed(source_seq=1, mutations=1, stop_blocks={PROMPT: 3})
        proc = self.run_hook(stop_event())
        self.assert_allow(proc)
        audit_path = os.path.join(self.state_dir, "audit.jsonl")
        with open(audit_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("stop_loop_guard_fired", text)

    def test_t17_stop_mutations_zero(self):
        proc = self.run_hook(stop_event())
        self.assert_allow(proc)

    def test_t18_stop_mode_plan_via_write_plan(self):
        self.seed(source_seq=2, mutations=2)
        proc = self.run_hook(pre("write_plan", {"plan": "# plan", "title": "t", "summary": "s"}))
        self.assert_allow(proc)
        self.assertEqual(self.load_st().get("mode"), "plan")
        proc = self.run_hook(stop_event())
        self.assert_allow(proc)

    def test_t19_gates_off_write(self):
        proc = self.run_hook(self.write_payload(), extra_env={"DEVIN_GATES_OFF": "1"})
        self.assert_allow(proc)

    def test_t19b_tool_input_env_gates_off_ignored(self):
        payload = self.write_payload()
        payload["tool_input"]["env"] = {"DEVIN_GATES_OFF": "1"}
        proc = self.run_hook(payload)
        self.assert_block(proc, 0)

    def test_t20_write_plan_path_locked(self):
        path = os.path.expanduser("~/.devin/plans/plan-abc.md")
        proc = self.run_hook(pre("write", {"file_path": path, "content": "# plan\n"}))
        self.assert_allow(proc)

    def test_t21_write_state_dir_blocked_after_unlock(self):
        self.seed(markers=[self.plan_marker()])
        path = os.path.join(self.state_dir, "sessions", SESSION, "state.json")
        proc = self.run_hook(pre("write", {"file_path": path, "content": "x"}))
        self.assert_block(proc, 0)

    def test_t21b_notebook_edit_state_dir(self):
        self.seed(markers=[self.plan_marker()])
        path = os.path.join(self.state_dir, "secret")
        proc = self.run_hook(
            pre(
                "notebook_edit",
                {"notebook_path": path, "cell_number": 0, "new_source": "x"},
            )
        )
        self.assert_block(proc, 0)

    def test_t21c_apply_patch_locked(self):
        proc = self.run_hook(load_fixture("apply_patch_unknown.json"))
        self.assert_block(proc, 0)

    def test_t21d_apply_patch_unlocked_fail_open(self):
        self.seed(markers=[self.plan_marker()])
        proc = self.run_hook(load_fixture("apply_patch_unknown.json"))
        self.assert_allow(proc)

    def test_t21e_apply_patch_unlocked_protected_scan(self):
        self.seed(markers=[self.plan_marker()])
        payload = load_fixture("apply_patch_unknown.json")
        payload["tool_input"]["body"] = "*** Update File: %s\n" % os.path.realpath(self.state_dir)
        proc = self.run_hook(payload)
        self.assert_block(proc, 0)

    def test_t22_write_to_process_locked(self):
        proc = self.run_hook(load_fixture("write_to_process.json"))
        self.assert_block(proc, 0)

    def test_t23_bypass_then_write(self):
        proc = self.run_cli(["bypass", "--reason", "test"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_hook(self.write_payload())
        self.assert_allow(proc)

    def test_t24_exception_session_start(self):
        event = load_fixture("session_start.json")
        with mock.patch.object(gates, "dispatch", side_effect=RuntimeError("boom")):
            out, code = gates.run_hook(event)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_t25_exception_write_pretooluse(self):
        event = self.write_payload()
        with mock.patch.object(gates, "dispatch", side_effect=RuntimeError("boom")):
            out, code = gates.run_hook(event)
        self.assertEqual(code, 2)
        data = json.loads(out)
        self.assertEqual(data.get("decision"), "block")
        self.assertEqual(data.get("reason"), "gate error (fail closed)")

    def test_t26_run_subagent_general_locked(self):
        proc = self.run_hook(load_fixture("run_subagent_general.json"))
        self.assert_block(proc, 0)

    def test_t27_run_subagent_plan_skeptic_locked(self):
        proc = self.run_hook(
            pre(
                "run_subagent",
                {
                    "title": "plan",
                    "task": "review",
                    "profile": "plan-skeptic",
                    "is_background": False,
                    "resume": None,
                },
            )
        )
        self.assert_allow(proc)

    def test_t27b_design_writer_no_root(self):
        proc = self.run_hook(
            pre("run_subagent", {"title": "w", "task": "t", "profile": "design-writer"})
        )
        self.assert_block(proc, 0)

    def test_t27c_design_reviewer_no_root(self):
        proc = self.run_hook(
            pre("run_subagent", {"title": "r", "task": "t", "profile": "design-reviewer"})
        )
        self.assert_block(proc, 0)

    def test_t27d_code_skeptic_locked(self):
        proc = self.run_hook(
            pre("run_subagent", {"title": "c", "task": "t", "profile": "code-skeptic"})
        )
        self.assert_block(proc, 0)

    def test_t27e_finding_skeptic_locked(self):
        proc = self.run_hook(
            pre("run_subagent", {"title": "f", "task": "t", "profile": "finding-skeptic"})
        )
        self.assert_block(proc, 0)

    def test_t28_mcp_locked(self):
        proc = self.run_hook(load_fixture("mcp_call_tool.json"))
        self.assert_block(proc, 0)
        proc = self.run_hook(load_fixture("mcp_github_create_issue.json"))
        self.assert_block(proc, 0)

    def test_t29_mcp_list_tools_locked(self):
        proc = self.run_hook(pre("mcp_list_tools", {"server_name": "github"}))
        self.assert_allow(proc)

    def test_t30_allow_design_write_cache(self):
        proc = self.run_cli(["allow-design", "--id", "abcd1234"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = self.load_st()
        root = st.get("design_allow_root")
        self.assertTrue(root)
        path = os.path.join(root, "design.md")
        proc = self.run_hook(pre("write", {"file_path": path, "content": "# doc\n"}))
        self.assert_allow(proc)

    def test_t31_write_workspace_after_allow_design(self):
        proc = self.run_cli(["allow-design", "--id", "abcd1234"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = os.path.join(self.workspace, "src", "main.py")
        proc = self.run_hook(pre("write", {"file_path": path, "content": "x"}))
        self.assert_block(proc, 0)
        refuse = self.run_cli(["allow-design", "--file", path])
        self.assertNotEqual(refuse.returncode, 0)

    def test_t32_symlink_escape_from_cache_root(self):
        proc = self.run_cli(["allow-design", "--id", "abcd1234"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        root = self.load_st().get("design_allow_root")
        target = os.path.join(self.workspace, "src", "main.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("orig\n")
        link = os.path.join(root, "escape.py")
        os.symlink(target, link)
        proc = self.run_hook(pre("write", {"file_path": link, "content": "pwned\n"}))
        self.assert_block(proc, 0)

    def test_t33_exec_lsof_locked(self):
        proc = self.run_hook(pre("exec", {"command": "lsof"}))
        self.assert_block(proc, 0)

    def test_t34_exec_find_exec_locked(self):
        proc = self.run_hook(pre("exec", {"command": "find -exec rm {} ;"}))
        self.assert_block(proc, 0)

    def test_t35_exec_git_log_output_locked(self):
        proc = self.run_hook(pre("exec", {"command": "git log --output=/tmp/x"}))
        self.assert_block(proc, 0)

    def test_t36_exec_git_status_substitution_locked(self):
        proc = self.run_hook(pre("exec", {"command": "git status $(touch x)"}))
        self.assert_block(proc, 0)

    def test_t37_exec_python3_pytest_locked(self):
        proc = self.run_hook(pre("exec", {"command": "python3 -m pytest"}))
        self.assert_block(proc, 0)

    def test_t38_read_secret_locked_and_unlocked(self):
        secret = os.path.join(self.state_dir, "secret")
        proc = self.run_hook(pre("read", {"file_path": secret}))
        self.assert_block(proc, 0)
        self.seed(markers=[self.plan_marker()])
        proc = self.run_hook(pre("read", {"file_path": secret}))
        self.assert_block(proc, 0)

    def test_t39_request_scope_hooks(self):
        path = os.path.expanduser("~/.config/devin/hooks")
        proc = self.run_hook(pre("request_scope", {"scope": "write", "path": path}))
        self.assert_block(proc, 0)

    def test_t40_write_config_json_after_plan_passed(self):
        self.seed(markers=[self.plan_marker()])
        path = os.path.expanduser("~/.config/devin/config.json")
        proc = self.run_hook(pre("write", {"file_path": path, "content": "{}"}))
        self.assert_block(proc, 0)

    def test_t41_code_skeptic_pass_no_plan_no_mint(self):
        payload = load_fixture("synthetic_code_skeptic_pass_post.json")
        proc = self.run_hook(payload)
        self.assert_allow(proc)
        st = self.load_st()
        self.assertNotIn("code-passed", self.marker_kinds())
        self.assertTrue(any(w.get("kind") == "code" and w.get("verdict") == "PASS" for w in (st.get("witnesses") or [])))
        self.assertEqual(int(st.get("source_seq") or 0), 0)

    def test_t42_code_passed_cleared_on_later_write(self):
        self.seed(markers=[self.plan_marker()], source_seq=1, mutations=1)
        payload = load_fixture("synthetic_code_skeptic_pass_post.json")
        proc = self.run_hook(payload)
        self.assert_allow(proc)
        self.assertIn("code-passed", self.marker_kinds())
        proc = self.run_hook(
            post("write", {"file_path": os.path.join(self.workspace, "src", "main.py"), "content": "y"})
        )
        self.assert_allow(proc)
        st = self.load_st()
        self.assertNotIn("code-passed", self.marker_kinds())
        self.assertEqual(int(st.get("source_seq") or 0), 2)

    def test_t42b_mint_code_no_remint_after_write(self):
        self.seed(markers=[self.plan_marker()], source_seq=1, mutations=1)
        payload = load_fixture("synthetic_code_skeptic_pass_post.json")
        self.assert_allow(self.run_hook(payload))
        self.assertIn("code-passed", self.marker_kinds())
        self.assert_allow(
            self.run_hook(
                post("write", {"file_path": os.path.join(self.workspace, "src", "main.py"), "content": "y"})
            )
        )
        self.assertNotIn("code-passed", self.marker_kinds())
        proc = self.run_cli(["mint-code"])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("code-passed", self.marker_kinds())

    def test_t42c_git_commit_does_not_clear_code_passed(self):
        self.seed(
            markers=[self.plan_marker(), self.code_marker()],
            source_seq=1,
            mutations=1,
            witnesses=[
                {
                    "kind": "code",
                    "sweep": 1,
                    "verdict": "PASS",
                    "subagent_id": "s2",
                    "source_seq": 1,
                }
            ],
        )
        proc = self.run_hook(post("exec", {"command": "git commit -am wip"}, output="ok"))
        self.assert_allow(proc)
        st = self.load_st()
        self.assertIn("code-passed", self.marker_kinds())
        self.assertEqual(int(st.get("source_seq") or 0), 1)

    def test_t43_rg_string_not_path_prefix(self):
        proc = self.run_hook(pre("exec", {"command": "rg 'devin-gates.py'"}))
        self.assert_allow(proc)

    def test_t44_write_source_realpath_target_after_unlock(self):
        target = os.path.join(self.tmpdir, "copied-from-devin-gates.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("# source copy\n")
        with open(os.path.join(self.state_dir, "source_realpath"), "w", encoding="utf-8") as fh:
            fh.write(os.path.realpath(target) + "\n")
        os.chmod(os.path.join(self.state_dir, "source_realpath"), 0o600)
        self.seed(markers=[self.plan_marker()])
        proc = self.run_hook(pre("write", {"file_path": target, "content": "pwned\n"}))
        self.assert_block(proc, 0)

    def test_hook_entries_has_dispatcher_events_not_permission_request(self):
        path = os.path.join(ROOT, "hooks", "hook-entries.json")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for event in (
            "PreToolUse",
            "PostToolUse",
            "Stop",
            "UserPromptSubmit",
            "SessionStart",
            "PostCompaction",
            "SessionEnd",
        ):
            self.assertIn(event, data)
            self.assertEqual(data[event][0]["matcher"], "")
            hook = data[event][0]["hooks"][0]
            self.assertIn("devin-gates.py", hook["command"])
            self.assertTrue(hook["command"].endswith(" hook") or " hook" in hook["command"])
            self.assertEqual(hook["timeout"], 5)
        self.assertNotIn("PermissionRequest", data)


if __name__ == "__main__":
    unittest.main()

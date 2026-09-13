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
REPO_GATE_PY = GATE_PY
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
        self.gate_py = GATE_PY

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
            [sys.executable, self.gate_py, "hook"],
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
            [sys.executable, self.gate_py] + list(args),
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

    def test_t06b_env_python3_c_blocked_after_unlock(self):
        self.seed(markers=[self.plan_marker()])
        proc = self.run_hook(pre("exec", {"command": "env python3 -c 'print(1)'"}))
        self.assert_block(proc, 0)
        proc = self.run_hook(pre("exec", {"command": "/usr/bin/env python3 -c 'print(1)'"}))
        self.assert_block(proc, 0)
        proc = self.run_hook(pre("exec", {"command": "env devin -p hi"}))
        self.assert_block(proc, 0)

    def test_t06c_python_option_arg_then_c_blocked_after_unlock(self):
        self.seed(markers=[self.plan_marker()])
        proc = self.run_hook(pre("exec", {"command": "python3 -W ignore -c 'print(1)'"}))
        self.assert_block(proc, 0)
        proc = self.run_hook(pre("exec", {"command": "python3 -X utf8 -c 'print(1)'"}))
        self.assert_block(proc, 0)
        proc = self.run_hook(pre("exec", {"command": "python3 --check-hash-based-pycs default -c 'print(1)'"}))
        self.assert_block(proc, 0)

    def test_t07_exec_git_status_locked(self):
        proc = self.run_hook(load_fixture("exec_git_status.json"))
        self.assert_allow(proc)
        proc = self.run_hook(pre("exec", {"command": "git --no-pager status"}))
        self.assert_allow(proc)
        proc = self.run_hook(pre("exec", {"command": "git -c alias.status='!touch x' status"}))
        self.assert_block(proc, 0)
        proc = self.run_hook(pre("exec", {"command": "git --exec-path=/tmp status"}))
        self.assert_block(proc, 0)
        proc = self.run_hook(pre("exec", {"command": "git --config-env=GIT_DIR=FOO status"}))
        self.assert_block(proc, 0)

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

    def test_t19c_gates_off_allows_protected_write(self):
        secret = os.path.join(self.state_dir, "secret")
        proc = self.run_hook(
            pre("write", {"file_path": secret, "content": "x"}),
            extra_env={"DEVIN_GATES_OFF": "1"},
        )
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

    def test_t21f_write_content_mentioning_state_dir_after_unlock(self):
        self.seed(markers=[self.plan_marker()])
        content = "notes about %s and %s-backup\n" % (
            os.path.realpath(self.state_dir),
            os.path.realpath(self.state_dir),
        )
        proc = self.run_hook(self.write_payload(content=content))
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

    def test_t32b_exec_planted_ls_under_design_root_locked(self):
        proc = self.run_cli(["allow-design", "--id", "abcd1234"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        root = self.load_st().get("design_allow_root")
        planted = os.path.join(root, "ls")
        proc = self.run_hook(pre("write", {"file_path": planted, "content": "#!/bin/sh\necho pwned\n"}))
        self.assert_allow(proc)
        proc = self.run_hook(pre("exec", {"command": planted}))
        self.assert_block(proc, 0)
        proc = self.run_hook(pre("exec", {"command": "./ls"}))
        self.assert_block(proc, 0)

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

    def test_t41b_code_skeptic_pass_plan_passed_source_seq_zero_no_mint(self):
        self.seed(markers=[self.plan_marker()], source_seq=0, mutations=0)
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
        proc = self.run_hook(post("exec", {"command": "git --no-pager commit -am wip"}, output="ok"))
        self.assert_allow(proc)
        st = self.load_st()
        self.assertIn("code-passed", self.marker_kinds())
        self.assertEqual(int(st.get("source_seq") or 0), 1)
        proc = self.run_hook(post("exec", {"command": "git -C . commit -am wip"}, output="ok"))
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


class GoalTest(GateTest):
    """Goal state/CLI: exercised against a temp copy of the gate with the
    apply_goal_patch.py block spliced in, plus devin_gates_goal.py beside it."""

    def setUp(self):
        super().setUp()
        hooks_dir = os.path.join(self.tmpdir, "hooks")
        os.makedirs(hooks_dir)
        shutil.copy(REPO_GATE_PY, hooks_dir)
        shutil.copy(os.path.join(ROOT, "hooks", "devin_gates_goal.py"), hooks_dir)
        gate_copy = os.path.join(hooks_dir, "devin-gates.py")
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "hooks", "apply_goal_patch.py"),
                gate_copy,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.gate_py = gate_copy
        # Inherited tests embed the module-level GATE_PY in their exec
        # commands; the patched copy is the gate under test here.
        globals()["GATE_PY"] = gate_copy

    def tearDown(self):
        globals()["GATE_PY"] = REPO_GATE_PY
        super().tearDown()

    def goal_dir(self):
        ws = os.path.realpath(self.workspace)
        h = hashlib.sha256(ws.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.state_dir, "goals", h)

    def goal_files(self):
        d = self.goal_dir()
        if not os.path.isdir(d):
            return []
        return [n for n in os.listdir(d) if n.endswith(".json")]

    def load_goal(self, goal_id=None):
        d = self.goal_dir()
        if goal_id is None:
            names = self.goal_files()
            if not names:
                return None
            goal_id = names[0][: -len(".json")]
        with open(os.path.join(d, goal_id + ".json"), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def seed_goal(self, status="active", goal_id="aa11bb22", **extra):
        body = {
            "v": 1,
            "goal_id": goal_id,
            "workspace": os.path.realpath(self.workspace),
            "objective": "seeded objective",
            "status": status,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "created_session": SESSION,
            "allow_root": os.path.join(
                self.cache_dir, "devin-skills", "goal", goal_id
            ),
            "mutation_seq": 0,
            "verifier_sweeps": 0,
            "updates": [],
            "witnesses": [],
            "blocked_reason": None,
        }
        body.update(extra)
        msg = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        body["hmac"] = hmac.new(self.secret, msg, hashlib.sha256).hexdigest()
        d = self.goal_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, goal_id + ".json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        return body

    def set_goal(self, objective="ship it", **kw):
        args = ["set-goal", "--objective", objective]
        proc = self.run_cli(args, **kw)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_goal_01_set_goal_signs_and_attaches(self):
        proc = self.set_goal()
        self.assertIn("goal_id=", proc.stdout)
        self.assertIn("goal_allow_root=", proc.stdout)
        self.assertIn("status=active", proc.stdout)
        goal = self.load_goal()
        self.assertIsNotNone(goal)
        self.assertTrue(gates.verify_hmac(goal))
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["objective"], "ship it")
        st = self.load_st()
        self.assertEqual(st.get("active_goal_id"), goal["goal_id"])

    def test_goal_02_forged_goal_file_treated_absent(self):
        goal = self.seed_goal()
        goal["hmac"] = "0" * 64
        path = os.path.join(self.goal_dir(), goal["goal_id"] + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(goal, fh)
        proc = self.run_cli(["goal-status"])
        self.assertIn("goal: none", proc.stdout)
        # A tampered file does not hold the workspace slot either.
        proc = self.run_cli(["set-goal", "--objective", "x"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_goal_03_pause_requires_reason(self):
        self.set_goal()
        proc = self.run_cli(["goal-pause"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_04_clear_requires_reason(self):
        self.set_goal()
        proc = self.run_cli(["goal-clear"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_04b_blocked_requires_reason(self):
        self.set_goal()
        proc = self.run_cli(["goal-update", "--blocked"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_05_pause_resume_cycle(self):
        self.set_goal("x")
        proc = self.run_cli(["goal-pause", "--reason", "break"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.load_goal()["status"], "paused")
        proc = self.run_cli(["goal-resume"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.load_goal()["status"], "active")
        self.assertIn("goal_allow_root=", proc.stdout)
        self.assertIn("objective=x", proc.stdout)

    def test_goal_06_second_goal_refused_until_cleared(self):
        self.set_goal("x")
        proc = self.run_cli(["set-goal", "--objective", "y"])
        self.assertNotEqual(proc.returncode, 0)
        proc = self.run_cli(["goal-clear", "--reason", "done"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli(["set-goal", "--objective", "y"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_goal_07_set_goal_refused_while_complete(self):
        self.seed_goal(status="complete")
        proc = self.run_cli(["set-goal", "--objective", "y"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_08_update_message_refused_on_complete(self):
        self.seed_goal(status="complete")
        self.seed(active_goal_id="aa11bb22")
        proc = self.run_cli(["goal-update", "--message", "x"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_09_resume_refused_on_complete_and_cleared(self):
        self.seed_goal(status="complete")
        proc = self.run_cli(["goal-resume"])
        self.assertNotEqual(proc.returncode, 0)
        goal = self.load_goal()
        goal["status"] = "cleared"
        goal.pop("hmac", None)
        msg = json.dumps(
            goal, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        goal["hmac"] = hmac.new(self.secret, msg, hashlib.sha256).hexdigest()
        with open(
            os.path.join(self.goal_dir(), goal["goal_id"] + ".json"), "w"
        ) as fh:
            json.dump(goal, fh)
        proc = self.run_cli(["goal-resume"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_10_claim_done_records_claim_not_completion(self):
        self.set_goal()
        proc = self.run_cli(["goal-update", "--claim-done"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        goal = self.load_goal()
        self.assertEqual(goal["status"], "active")
        self.assertTrue(any(u.get("kind") == "claim" for u in goal["updates"]))

    def test_goal_11_claim_while_paused_refused(self):
        self.set_goal()
        self.run_cli(["goal-pause", "--reason", "break"])
        proc = self.run_cli(["goal-update", "--claim-done"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_12_resume_attaches_other_session(self):
        self.set_goal()
        self.run_cli(["goal-pause", "--reason", "break"])
        proc = self.run_cli(
            ["goal-resume"], extra_env={"DEVIN_SESSION_ID": "other-session"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = gates.load_state("other-session")
        self.assertEqual(st.get("active_goal_id"), self.load_goal()["goal_id"])
        self.assertEqual(self.load_goal()["status"], "active")

    def test_goal_13_resume_on_active_is_attach_noop(self):
        self.set_goal()
        proc = self.run_cli(
            ["goal-resume"], extra_env={"DEVIN_SESSION_ID": "other-session"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = gates.load_state("other-session")
        self.assertEqual(st.get("active_goal_id"), self.load_goal()["goal_id"])
        self.assertEqual(self.load_goal()["status"], "active")

    def test_goal_14_status_reports_attachment(self):
        self.set_goal()
        proc = self.run_cli(
            ["goal-status"], extra_env={"DEVIN_SESSION_ID": "other-session"}
        )
        self.assertIn("attached: no", proc.stdout)
        proc = self.run_cli(["goal-status"])
        self.assertIn("attached: yes", proc.stdout)
        self.assertIn("goal_allow_root=", proc.stdout)
        self.assertIn("objective=", proc.stdout)

    def test_goal_15_allow_root_write_attached(self):
        self.set_goal()
        root = self.load_goal()["allow_root"]
        path = os.path.join(root, "checklist.md")
        proc = self.run_hook(
            pre("write", {"file_path": path, "content": "- [ ] item\n"})
        )
        self.assert_allow(proc)

    def test_goal_16_allow_root_write_unattached_blocked(self):
        self.set_goal()
        root = self.load_goal()["allow_root"]
        path = os.path.join(root, "checklist.md")
        proc = self.run_hook(
            pre(
                "write",
                {"file_path": path, "content": "x"},
                session_id="other-session",
            )
        )
        self.assert_block(proc, 0)

    def test_goal_17_symlink_escape_from_goal_root(self):
        self.set_goal()
        root = self.load_goal()["allow_root"]
        target = os.path.join(self.workspace, "src", "main.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("orig\n")
        link = os.path.join(root, "escape.py")
        os.symlink(target, link)
        proc = self.run_hook(
            pre("write", {"file_path": link, "content": "pwned\n"})
        )
        self.assert_block(proc, 0)

    def test_goal_18_exec_goal_subcommand_allowed_while_locked(self):
        cmd = "%s %s set-goal --objective x" % (sys.executable, self.gate_py)
        proc = self.run_hook(pre("exec", {"command": cmd}))
        self.assert_allow(proc)

    def test_goal_19_exec_goal_substitution_blocked(self):
        cmd = "%s %s set-goal --objective $(touch pwned)" % (
            sys.executable,
            self.gate_py,
        )
        proc = self.run_hook(pre("exec", {"command": cmd}))
        self.assert_block(proc, 0)

    def test_goal_20_planted_exec_under_goal_root_blocked(self):
        self.set_goal()
        root = self.load_goal()["allow_root"]
        planted = os.path.join(root, "ls")
        proc = self.run_hook(
            pre("write", {"file_path": planted, "content": "#!/bin/sh\necho pwned\n"})
        )
        self.assert_allow(proc)
        proc = self.run_hook(pre("exec", {"command": planted}))
        self.assert_block(proc, 0)

    def test_goal_21_concurrent_set_goal_single_winner(self):
        env = self.env.copy()
        cmd = [
            sys.executable,
            self.gate_py,
            "set-goal",
            "--objective",
            "race",
        ]
        p1 = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self.tmpdir,
        )
        p2 = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self.tmpdir,
        )
        p1.communicate()
        p2.communicate()
        rcs = sorted([p1.returncode, p2.returncode])
        self.assertEqual(rcs[0], 0)
        self.assertNotEqual(rcs[1], 0)
        self.assertEqual(len(self.goal_files()), 1)

    def test_goal_22_session_end_detaches(self):
        self.set_goal()
        self.assertIsNotNone(self.load_st().get("active_goal_id"))
        proc = self.run_hook(
            {
                "hook_event_name": "SessionEnd",
                "session_id": SESSION,
                "prompt_id": PROMPT,
            }
        )
        self.assert_allow(proc)
        self.assertIsNone(self.load_st().get("active_goal_id"))
        audit_path = os.path.join(self.state_dir, "audit.jsonl")
        with open(audit_path, "r", encoding="utf-8") as fh:
            self.assertIn("goal_active_at_session_end", fh.read())

    def test_goal_23_patch_idempotent(self):
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "hooks", "apply_goal_patch.py"),
                self.gate_py,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already applied", proc.stdout)

    def test_goal_24_blocked_transition_and_resume(self):
        self.set_goal()
        proc = self.run_cli(["goal-update", "--blocked", "--reason", "stuck"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        goal = self.load_goal()
        self.assertEqual(goal["status"], "blocked")
        self.assertEqual(goal["blocked_reason"], "stuck")
        proc = self.run_cli(["goal-update", "--message", "still stuck"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli(["goal-resume"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        goal = self.load_goal()
        self.assertEqual(goal["status"], "active")
        self.assertIsNone(goal["blocked_reason"])

    def test_goal_25_unattached_update_and_pause_refused_clear_ok(self):
        self.set_goal()
        other = {"DEVIN_SESSION_ID": "other-session"}
        proc = self.run_cli(["goal-update", "--message", "x"], extra_env=other)
        self.assertNotEqual(proc.returncode, 0)
        proc = self.run_cli(["goal-pause", "--reason", "x"], extra_env=other)
        self.assertNotEqual(proc.returncode, 0)
        proc = self.run_cli(["goal-clear", "--reason", "x"], extra_env=other)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.load_goal()["status"], "cleared")

    def test_goal_26_embedded_metachars_blocked_while_locked(self):
        # shlex keeps "x;id" as one token; the raw-command metachar scan must
        # catch embedded ;|&>< and $VAR before the exec allowlist allows it.
        for tail in (
            "x;id",
            "a|id",
            "f>out.txt",
            "a&id",
            "$HOME",
            "x`id`",
        ):
            with self.subTest(tail=tail):
                cmd = "%s %s goal-status %s" % (sys.executable, self.gate_py, tail)
                proc = self.run_hook(pre("exec", {"command": cmd}))
                self.assert_block(proc, 0)

    def test_goal_27_unsafe_session_id_never_attaches(self):
        self.set_goal()
        proc = self.run_cli(
            ["goal-status"], extra_env={"DEVIN_SESSION_ID": "../../tmp/evil"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("attached: no", proc.stdout)
        proc = self.run_cli(
            ["goal-update", "--message", "x"],
            extra_env={"DEVIN_SESSION_ID": "../../tmp/evil"},
        )
        self.assertNotEqual(proc.returncode, 0)
        # No state file was written outside the sessions dir.
        self.assertFalse(os.path.lexists(os.path.join(self.tmpdir, "evil")))

    def test_goal_28_status_rejects_positionals(self):
        self.set_goal()
        proc = self.run_cli(["goal-status", "junk"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_29_mixed_update_flags_refused(self):
        self.set_goal()
        proc = self.run_cli(["goal-update", "--claim-done", "--message", "x"])
        self.assertNotEqual(proc.returncode, 0)
        proc = self.run_cli(["goal-update", "--claim-done", "--blocked"])
        self.assertNotEqual(proc.returncode, 0)
        proc = self.run_cli(["goal-update"])
        self.assertNotEqual(proc.returncode, 0)

    def test_goal_30_mutation_seq_increments_on_save(self):
        self.set_goal()
        seq0 = self.load_goal()["mutation_seq"]
        self.run_cli(["goal-update", "--message", "x"])
        seq1 = self.load_goal()["mutation_seq"]
        self.assertGreater(seq1, seq0)


if __name__ == "__main__":
    unittest.main()

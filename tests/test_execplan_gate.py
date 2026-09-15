#!/usr/bin/env python3
"""Unit tests for the execute-plan gate extension (devin_gates_execplan.py).

Exercised against a temp copy of hooks/devin-gates.py with BOTH
apply_goal_patch.py and apply_execplan_patch.py spliced in (install order),
plus devin_gates_goal.py / devin_gates_execplan.py beside it. No live Devin
required.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_GATE_PY = os.path.join(ROOT, "hooks", "devin-gates.py")
VALIDATOR = os.path.join(
    ROOT, "skills", "execute-plan", "scripts", "validate-plan.py"
)
SESSION = "test-session-1"
PROMPT = "prompt-1"

def load_gates():
    spec = importlib.util.spec_from_file_location(
        "devin_gates", REPO_GATE_PY
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gates = load_gates()

VALID_PLAN = """# Design doc

## PR Plan

### PR 1: First
- **Files/components affected:** a.py
- **Dependencies:** None
- **Description:** do first

### PR 2: Second
- **Files/components affected:** b.py
- **Dependencies:** PR 1
- **Description:** do second
"""

INVALID_PLAN = "# Doc\n\nNo plan here.\n"


def pre(tool, tool_input, session_id=SESSION, prompt_id=PROMPT):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "session_id": session_id,
        "prompt_id": prompt_id,
    }


class ExecPlanGateTest(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()
        self._old_cwd = os.getcwd()
        self.tmpdir = tempfile.mkdtemp(prefix="devin-execplan-test-")
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

        hooks_dir = os.path.join(self.tmpdir, "hooks")
        os.makedirs(hooks_dir)
        shutil.copy(REPO_GATE_PY, hooks_dir)
        for mod in ("devin_gates_goal.py", "devin_gates_execplan.py"):
            shutil.copy(os.path.join(ROOT, "hooks", mod), hooks_dir)
        self.gate_py = os.path.join(hooks_dir, "devin-gates.py")
        # Install order: goal splice first, then exec-plan.
        for patch in ("apply_goal_patch.py", "apply_execplan_patch.py"):
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(ROOT, "hooks", patch),
                    self.gate_py,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

        # Installed-layout validator sibling, namespaced beside the gate.
        self.validator_copy = os.path.join(
            hooks_dir, "devin_execplan_validate_plan.py"
        )
        shutil.copy(VALIDATOR, self.validator_copy)

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
        return subprocess.run(
            [sys.executable, self.gate_py, "hook"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmpdir,
        )

    def run_cli(self, args, extra_env=None):
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, self.gate_py] + list(args),
            capture_output=True,
            text=True,
            env=env,
            cwd=self.tmpdir,
        )

    def load_st(self, session_id=SESSION):
        return gates.load_state(session_id)

    def assert_allow(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        out = proc.stdout.strip()
        if out:
            data = json.loads(out)
            self.assertNotEqual(data.get("decision"), "block", data)

    def assert_block(self, proc, exit_code=0):
        self.assertEqual(
            proc.returncode, exit_code, proc.stdout + proc.stderr
        )
        data = json.loads(proc.stdout.strip())
        self.assertEqual(data.get("decision"), "block")
        return data

    def allow_exec_plan(self, plan_id=None):
        args = ["allow-exec-plan"]
        if plan_id:
            args += ["--id", plan_id]
        proc = self.run_cli(args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def plan_file(self, body=VALID_PLAN, name="plan.md"):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    # ------------------------------------------------------------ CLI

    def test_ep01_allow_prints_id_and_root(self):
        proc = self.allow_exec_plan()
        self.assertIn("plan_id=", proc.stdout)
        self.assertIn("exec_plan_allow_root=", proc.stdout)
        root = [
            line.split("=", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("exec_plan_allow_root=")
        ][0]
        self.assertTrue(os.path.isdir(root))
        mode = stat.S_IMODE(os.stat(root).st_mode)
        self.assertEqual(mode & 0o777, 0o700)

    def test_ep02_allow_explicit_id(self):
        proc = self.allow_exec_plan("abcd1234")
        self.assertIn("plan_id=abcd1234", proc.stdout)

    def test_ep03_allow_bad_id_refused(self):
        proc = self.run_cli(["allow-exec-plan", "--id", "BADID!!"])
        self.assertEqual(proc.returncode, 1)
        proc = self.run_cli(["allow-exec-plan", "--id", "abcd123"])
        self.assertEqual(proc.returncode, 1)

    def test_ep04_allow_reissue_exist_ok(self):
        proc = self.allow_exec_plan("abcd1234")
        proc = self.allow_exec_plan("abcd1234")
        self.assertIn("plan_id=abcd1234", proc.stdout)

    def test_ep05_allow_attaches_session(self):
        self.allow_exec_plan("abcd1234")
        st = self.load_st()
        self.assertIsNotNone(st)
        self.assertEqual(st.get("exec_plan_id"), "abcd1234")
        self.assertTrue(st.get("exec_plan_allow_root"))

    def test_ep06_allow_unknown_option_refused(self):
        proc = self.run_cli(["allow-exec-plan", "--bogus"])
        self.assertEqual(proc.returncode, 1)

    # ------------------------------------------------- locked PreToolUse

    def test_ep07_locked_cli_exec_allowed(self):
        proc = self.run_hook(
            pre(
                "exec",
                {"command": "python3 %s allow-exec-plan" % self.gate_py},
            )
        )
        self.assert_allow(proc)

    def test_ep08_locked_validate_exec_allowed(self):
        proc = self.run_hook(
            pre(
                "exec",
                {
                    "command": "python3 %s exec-plan-validate --file p.md"
                    % self.gate_py
                },
            )
        )
        self.assert_allow(proc)

    def test_ep09_locked_cli_metachar_blocked(self):
        proc = self.run_hook(
            pre(
                "exec",
                {
                    "command": "python3 %s allow-exec-plan; id"
                    % self.gate_py
                },
            )
        )
        self.assert_block(proc)

    def test_ep10_locked_cli_other_script_blocked(self):
        other = os.path.join(self.tmpdir, "not-gate.py")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("pass\n")
        proc = self.run_hook(
            pre("exec", {"command": "python3 %s allow-exec-plan" % other})
        )
        self.assert_block(proc)

    def test_ep11_write_under_root_allowed(self):
        self.allow_exec_plan("abcd1234")
        st = self.load_st()
        root = st["exec_plan_allow_root"]
        proc = self.run_hook(
            pre(
                "write",
                {"file_path": os.path.join(root, "state.json"), "content": "{}"},
            )
        )
        self.assert_allow(proc)

    def test_ep12_write_outside_root_blocked(self):
        self.allow_exec_plan("abcd1234")
        proc = self.run_hook(
            pre(
                "write",
                {
                    "file_path": os.path.join(self.workspace, "src", "x.py"),
                    "content": "x",
                },
            )
        )
        self.assert_block(proc)

    def test_ep13_write_under_root_other_session_blocked(self):
        self.allow_exec_plan("abcd1234")
        st = self.load_st()
        root = st["exec_plan_allow_root"]
        proc = self.run_hook(
            pre(
                "write",
                {"file_path": os.path.join(root, "state.json"), "content": "{}"},
                session_id="someone-else",
            )
        )
        self.assert_block(proc)

    def test_ep14_symlink_escape_blocked(self):
        self.allow_exec_plan("abcd1234")
        st = self.load_st()
        root = st["exec_plan_allow_root"]
        target = os.path.join(self.workspace, "src", "main.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("orig\n")
        link = os.path.join(root, "escape.py")
        os.symlink(target, link)
        proc = self.run_hook(
            pre("write", {"file_path": link, "content": "pwned\n"})
        )
        self.assert_block(proc)

    # -------------------------------------------------------- validate

    def test_ep15_validate_valid_plan(self):
        proc = self.run_cli(
            ["exec-plan-validate", "--file", self.plan_file()]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["valid"])
        self.assertEqual(out["pr_count"], 2)

    def test_ep16_validate_invalid_plan(self):
        proc = self.run_cli(
            ["exec-plan-validate", "--file", self.plan_file(INVALID_PLAN)]
        )
        self.assertEqual(proc.returncode, 1)
        out = json.loads(proc.stdout)
        self.assertFalse(out["valid"])
        self.assertTrue(out["errors"])

    def test_ep17_validate_missing_file(self):
        proc = self.run_cli(
            [
                "exec-plan-validate",
                "--file",
                os.path.join(self.tmpdir, "nope.md"),
            ]
        )
        self.assertEqual(proc.returncode, 2)

    def test_ep18_validate_requires_file(self):
        proc = self.run_cli(["exec-plan-validate"])
        self.assertEqual(proc.returncode, 1)

    def test_ep19_validate_protected_file_refused(self):
        secret = os.path.join(self.state_dir, "secret")
        proc = self.run_cli(["exec-plan-validate", "--file", secret])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("protected", proc.stderr)

    def test_ep20_validate_expands_tilde(self):
        # Point HOME at the tmpdir and plant a valid plan there: rc 0 +
        # valid JSON can only happen if expanduser resolved "~".
        with open(
            os.path.join(self.tmpdir, "tilde-plan.md"), "w", encoding="utf-8"
        ) as fh:
            fh.write(VALID_PLAN)
        proc = self.run_cli(
            ["exec-plan-validate", "--file", "~/tilde-plan.md"],
            extra_env={"HOME": self.tmpdir},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(json.loads(proc.stdout)["valid"])

    def test_ep21_validate_missing_validator(self):
        os.remove(self.validator_copy)
        proc = self.run_cli(
            ["exec-plan-validate", "--file", self.plan_file()]
        )
        # rc 2 = infra failure; the skill falls back to direct invocation.
        self.assertEqual(proc.returncode, 2)
        self.assertIn("validator missing", proc.stderr)

    # ---------------------------------------------------------- status

    def test_ep22_status_shows_plan(self):
        self.allow_exec_plan("abcd1234")
        proc = self.run_cli(["status"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("exec-plan: abcd1234", proc.stdout)

    def test_ep23_status_no_plan(self):
        proc = self.run_cli(["status"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("exec-plan:", proc.stdout)

    # ------------------------------------------------------ composition

    def test_ep24_goal_splice_still_works(self):
        proc = self.run_cli(["set-goal", "--objective", "ship it"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("goal_id=", proc.stdout)

    def test_ep25_both_status_lines(self):
        self.run_cli(["set-goal", "--objective", "ship it"])
        self.allow_exec_plan("abcd1234")
        proc = self.run_cli(["status"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("goal:", proc.stdout)
        self.assertIn("exec-plan: abcd1234", proc.stdout)

    def test_ep26_readonly_profiles_extended(self):
        # A second gate instance in this process would otherwise reuse
        # cached splice modules whose _GATE already points at the first
        # instance's namespace — a harness artifact. Pop them so the
        # patched copy binds to itself, mirroring real single-instance use.
        saved = {
            k: sys.modules.pop(k)
            for k in ("devin_gates_goal", "devin_gates_execplan")
            if k in sys.modules
        }
        try:
            spec = importlib.util.spec_from_file_location(
                "devin_gates_patched", self.gate_py
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertIn("pr-reviewer", mod.READONLY_PROFILES)
        finally:
            for k in ("devin_gates_goal", "devin_gates_execplan"):
                sys.modules.pop(k, None)
            sys.modules.update(saved)

    def test_ep27_double_patch_idempotent(self):
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "hooks", "apply_execplan_patch.py"),
                self.gate_py,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already applied", proc.stdout)

    def test_ep28_reissue_repairs_loose_perms(self):
        proc = self.allow_exec_plan("abcd1234")
        root = [
            line.split("=", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("exec_plan_allow_root=")
        ][0]
        os.chmod(root, 0o755)
        parent = os.path.dirname(root)
        os.chmod(parent, 0o755)
        self.allow_exec_plan("abcd1234")
        for path in (root, parent):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode & 0o777, 0o700)

    def test_ep29_no_session_warns_on_stderr(self):
        env = {"DEVIN_SESSION_ID": ""}
        proc = self.run_cli(["allow-exec-plan", "--id", "abcd1234"],
                            extra_env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no session resolved", proc.stderr)


if __name__ == "__main__":
    unittest.main()

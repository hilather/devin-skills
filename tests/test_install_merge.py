#!/usr/bin/env python3
"""Install/uninstall merge tests. Never touch the live ~/.config/devin."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(ROOT, "install.sh")
UNINSTALL_SH = os.path.join(ROOT, "uninstall.sh")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "herdr-config.json")
RULES = os.path.join(ROOT, "rules", "AGENTS.md")
GATE_SRC = os.path.join(ROOT, "hooks", "devin-gates.py")

HERDR_EVENTS = (
    "PermissionRequest",
    "PostToolUse",
    "PreToolUse",
    "SessionStart",
    "Stop",
    "UserPromptSubmit",
)
GATE_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "UserPromptSubmit",
    "SessionStart",
    "PostCompaction",
    "SessionEnd",
)
HERDR_MARK = "herdr-agent-state.sh"
GATE_MARK = "devin-gates.py"
BEGIN = "<!-- devin-skills:begin -->"
END = "<!-- devin-skills:end -->"

# Absolute live paths — do not use expanduser (tests rewrite HOME).
LIVE_CONFIG = "/home/brewerm/.config/devin/config.json"
LIVE_HERDR = "/home/brewerm/.config/devin/herdr-agent-state.sh"
LIVE_PREFIX = "/home/brewerm/.config/devin"


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def commands_for_event(cfg, event):
    hooks = cfg.get("hooks") if "hooks" in cfg else cfg
    cmds = []
    for element in hooks.get(event, []) if isinstance(hooks, dict) else []:
        for hook in element.get("hooks", []):
            cmd = hook.get("command")
            if isinstance(cmd, str):
                cmds.append(cmd)
    return cmds


def marked(cmds, mark):
    return [c for c in cmds if mark in c]


class InstallMergeTest(unittest.TestCase):
    def setUp(self):
        self.live_config_hash = sha256_file(LIVE_CONFIG) if os.path.isfile(LIVE_CONFIG) else None
        self.live_herdr_hash = sha256_file(LIVE_HERDR) if os.path.isfile(LIVE_HERDR) else None
        self.tmpdir = tempfile.mkdtemp(prefix="devin-skills-install-")
        self.home = os.path.join(self.tmpdir, "home")
        os.makedirs(self.home)
        self.prefix = os.path.join(self.home, ".config", "devin")
        os.makedirs(self.prefix)
        shutil.copy(FIXTURE, os.path.join(self.prefix, "config.json"))
        os.chmod(os.path.join(self.prefix, "config.json"), 0o600)
        self.herdr_dummy = os.path.join(self.prefix, "herdr-agent-state.sh")
        with open(self.herdr_dummy, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n# dummy herdr; do not delete\nexit 0\n")
        os.chmod(self.herdr_dummy, 0o755)
        self.herdr_dummy_hash = sha256_file(self.herdr_dummy)
        self.xdg_data = os.path.join(self.home, ".local", "share")
        self.xdg_cache = os.path.join(self.home, ".cache")
        self.state_dir = os.path.join(self.xdg_data, "devin-skills")
        self.env = os.environ.copy()
        self.env["HOME"] = self.home
        self.env["XDG_DATA_HOME"] = self.xdg_data
        self.env["XDG_CONFIG_HOME"] = os.path.join(self.home, ".config")
        self.env["XDG_CACHE_HOME"] = self.xdg_cache
        self.env["TMPDIR"] = self.tmpdir
        self.env.pop("DEVIN_SKILLS_STATE_DIR", None)
        self.assertNotEqual(os.path.realpath(self.prefix), os.path.realpath(LIVE_PREFIX))

    def tearDown(self):
        self._assert_live_untouched()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _assert_live_untouched(self):
        if self.live_config_hash is not None:
            self.assertTrue(os.path.isfile(LIVE_CONFIG))
            self.assertEqual(sha256_file(LIVE_CONFIG), self.live_config_hash)
        if self.live_herdr_hash is not None:
            self.assertTrue(os.path.isfile(LIVE_HERDR))
            self.assertEqual(sha256_file(LIVE_HERDR), self.live_herdr_hash)
        self.assertTrue(os.path.isfile(self.herdr_dummy))
        self.assertEqual(sha256_file(self.herdr_dummy), self.herdr_dummy_hash)

    def install(self, extra=None, src=None, cwd=None, check=True):
        extra = list(extra or [])
        cmd = ["sh", INSTALL_SH, "--prefix", self.prefix, "--src", src or ROOT]
        cmd.extend(extra)
        proc = subprocess.run(
            cmd,
            env=self.env,
            cwd=cwd or self.tmpdir,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            self.fail(
                "install exited %s\nstdout:\n%s\nstderr:\n%s"
                % (proc.returncode, proc.stdout, proc.stderr)
            )
        return proc

    def uninstall(self, extra=None, src=None, cwd=None, check=True):
        extra = list(extra or [])
        cmd = ["sh", UNINSTALL_SH, "--prefix", self.prefix, "--src", src or ROOT]
        cmd.extend(extra)
        proc = subprocess.run(
            cmd,
            env=self.env,
            cwd=cwd or self.tmpdir,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            self.fail(
                "uninstall exited %s\nstdout:\n%s\nstderr:\n%s"
                % (proc.returncode, proc.stdout, proc.stderr)
            )
        return proc

    def config(self):
        return load_json(os.path.join(self.prefix, "config.json"))

    def fixture(self):
        return load_json(FIXTURE)

    def make_src(self, with_skills=False):
        src = os.path.join(self.tmpdir, "src")
        os.makedirs(os.path.join(src, "hooks"), exist_ok=True)
        os.makedirs(os.path.join(src, "rules"), exist_ok=True)
        shutil.copy(GATE_SRC, os.path.join(src, "hooks", "devin-gates.py"))
        shutil.copy(
            os.path.join(ROOT, "hooks", "hook-entries.json"),
            os.path.join(src, "hooks", "hook-entries.json"),
        )
        shutil.copy(RULES, os.path.join(src, "rules", "AGENTS.md"))
        if with_skills:
            os.makedirs(os.path.join(src, "skills", "design"), exist_ok=True)
            with open(
                os.path.join(src, "skills", "design", "SKILL.md"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("# design\n")
            os.makedirs(os.path.join(src, "agents"), exist_ok=True)
            with open(
                os.path.join(src, "agents", "plan-skeptic.md"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("# plan-skeptic\n")
        return src

    def test_fixture_is_six_event_herdr_shape(self):
        cfg = self.fixture()
        self.assertEqual(set(cfg["hooks"].keys()), set(HERDR_EVENTS))
        self.assertNotIn("PostCompaction", cfg["hooks"])
        self.assertNotIn("SessionEnd", cfg["hooks"])
        self.assertEqual(cfg["devin"]["org_id"], "org-REDACTED")
        self.assertEqual(cfg["agent"]["model"], "swe-2-high")
        for event in HERDR_EVENTS:
            herdr = marked(commands_for_event(cfg, event), HERDR_MARK)
            self.assertEqual(len(herdr), 1, event)
            self.assertEqual(len(marked(commands_for_event(cfg, event), GATE_MARK)), 0)

    def test_postcompaction_and_sessionend_exist_after_merge(self):
        self.install()
        cfg = self.config()
        self.assertIn("PostCompaction", cfg["hooks"])
        self.assertIn("SessionEnd", cfg["hooks"])
        for event in GATE_EVENTS:
            gates = marked(commands_for_event(cfg, event), GATE_MARK)
            self.assertEqual(len(gates), 1, event)
            self.assertIn(os.path.join(self.prefix, "hooks", "devin-gates.py"), gates[0])
            self.assertTrue(gates[0].endswith(" hook") or gates[0].endswith("' hook"))

    def test_herdr_command_strings_byte_identical(self):
        before = self.fixture()
        self.install()
        after = self.config()
        for event in HERDR_EVENTS:
            orig = marked(commands_for_event(before, event), HERDR_MARK)
            now = marked(commands_for_event(after, event), HERDR_MARK)
            self.assertEqual(orig, now)
            self.assertEqual(len(orig), 1)

    def test_second_install_does_not_duplicate(self):
        self.install()
        first = self.config()
        self.install()
        second = self.config()
        for event in GATE_EVENTS:
            self.assertEqual(
                len(marked(commands_for_event(first, event), GATE_MARK)),
                1,
                event,
            )
            self.assertEqual(
                len(marked(commands_for_event(second, event), GATE_MARK)),
                1,
                event,
            )
        for event in HERDR_EVENTS:
            self.assertEqual(
                marked(commands_for_event(first, event), HERDR_MARK),
                marked(commands_for_event(second, event), HERDR_MARK),
            )

    def test_uninstall_leaves_herdr_only(self):
        self.install()
        self.uninstall()
        cfg = self.config()
        self.assertNotIn("PostCompaction", cfg["hooks"])
        self.assertNotIn("SessionEnd", cfg["hooks"])
        self.assertEqual(set(cfg["hooks"].keys()), set(HERDR_EVENTS))
        before = self.fixture()
        for event in HERDR_EVENTS:
            cmds = commands_for_event(cfg, event)
            self.assertEqual(len(cmds), 1, event)
            self.assertEqual(len(marked(cmds, GATE_MARK)), 0)
            self.assertEqual(
                marked(cmds, HERDR_MARK),
                marked(commands_for_event(before, event), HERDR_MARK),
            )
        self.assertFalse(
            os.path.lexists(os.path.join(self.prefix, "hooks", "devin-gates.py"))
        )
        self.assertTrue(os.path.isfile(self.herdr_dummy))
        self.assertEqual(sha256_file(self.herdr_dummy), self.herdr_dummy_hash)
        self.assertTrue(os.path.isdir(self.state_dir))

    def test_permission_request_has_no_gate(self):
        self.install()
        cfg = self.config()
        cmds = commands_for_event(cfg, "PermissionRequest")
        self.assertEqual(len(marked(cmds, GATE_MARK)), 0)
        self.assertEqual(len(marked(cmds, HERDR_MARK)), 1)

    def test_unknown_keys_preserved(self):
        self.install()
        cfg = self.config()
        orig = self.fixture()
        self.assertEqual(cfg["agent"], orig["agent"])
        self.assertEqual(cfg["devin"], orig["devin"])
        self.assertEqual(cfg["shell"], orig["shell"])
        self.assertEqual(cfg["theme_mode"], orig["theme_mode"])
        self.assertEqual(cfg["version"], orig["version"])

    def test_backup_created(self):
        self.install()
        backups = [
            name
            for name in os.listdir(self.prefix)
            if name.startswith("config.json.bak-devin-skills-")
        ]
        self.assertEqual(len(backups), 1)
        bak = load_json(os.path.join(self.prefix, backups[0]))
        self.assertEqual(set(bak["hooks"].keys()), set(HERDR_EVENTS))
        self.assertNotIn("PostCompaction", bak["hooks"])

    def test_gate_is_copy_not_symlink(self):
        self.install()
        installed = os.path.join(self.prefix, "hooks", "devin-gates.py")
        self.assertTrue(os.path.isfile(installed))
        self.assertFalse(os.path.islink(installed))
        inst_st = os.stat(installed)
        src_st = os.stat(GATE_SRC)
        self.assertNotEqual((inst_st.st_ino, inst_st.st_dev), (src_st.st_ino, src_st.st_dev))
        with open(GATE_SRC, "rb") as fh:
            src_bytes = fh.read()
        with open(installed, "rb") as fh:
            self.assertEqual(fh.read(), src_bytes)

    def test_preexisting_hardlink_is_replaced(self):
        # tempfile HOME may be another device; hardlink to the repo source needs the same fs.
        same_fs = tempfile.mkdtemp(
            prefix="devin-skills-hl-",
            dir=os.path.dirname(os.path.realpath(ROOT)),
        )
        try:
            prefix = os.path.join(same_fs, "config", "devin")
            os.makedirs(os.path.join(prefix, "hooks"))
            shutil.copy(FIXTURE, os.path.join(prefix, "config.json"))
            os.chmod(os.path.join(prefix, "config.json"), 0o600)
            installed = os.path.join(prefix, "hooks", "devin-gates.py")
            os.link(GATE_SRC, installed)
            src_st = os.stat(GATE_SRC)
            self.assertEqual(
                (os.stat(installed).st_ino, os.stat(installed).st_dev),
                (src_st.st_ino, src_st.st_dev),
            )
            old_prefix = self.prefix
            self.prefix = prefix
            try:
                self.install()
            finally:
                self.prefix = old_prefix
            inst_st = os.stat(installed)
            self.assertNotEqual((inst_st.st_ino, inst_st.st_dev), (src_st.st_ino, src_st.st_dev))
            self.assertFalse(os.path.islink(installed))
            with open(installed, "ab") as fh:
                fh.write(b"\n# mutated dest\n")
            with open(GATE_SRC, "rb") as fh:
                self.assertNotIn(b"mutated dest", fh.read())
        finally:
            shutil.rmtree(same_fs, ignore_errors=True)

    def test_config_json_stays_0600(self):
        path = os.path.join(self.prefix, "config.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.install()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.uninstall()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.uninstall()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_install_hash_and_source_realpath(self):
        self.install()
        installed = os.path.join(self.prefix, "hooks", "devin-gates.py")
        hash_path = os.path.join(self.state_dir, "install-hash")
        src_path = os.path.join(self.state_dir, "source_realpath")
        self.assertEqual(stat.S_IMODE(os.stat(hash_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(src_path).st_mode), 0o600)
        with open(hash_path, "r", encoding="utf-8") as fh:
            recorded = fh.read().strip()
        self.assertEqual(recorded, sha256_file(installed))
        with open(src_path, "r", encoding="utf-8") as fh:
            recorded_src = fh.read().splitlines()[0].strip()
        self.assertEqual(recorded_src, os.path.realpath(GATE_SRC))

    def test_secret_generated_once_not_overwritten(self):
        self.install()
        secret_path = os.path.join(self.state_dir, "secret")
        self.assertEqual(stat.S_IMODE(os.stat(self.state_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(secret_path).st_mode), 0o600)
        with open(secret_path, "rb") as fh:
            first = fh.read()
        self.assertEqual(len(first), 32)
        self.install()
        with open(secret_path, "rb") as fh:
            second = fh.read()
        self.assertEqual(first, second)

    def test_agents_md_span_replace(self):
        agents_path = os.path.join(self.prefix, "AGENTS.md")
        with open(agents_path, "w", encoding="utf-8") as fh:
            fh.write("# keep me\nuser rule\n")
        self.install()
        with open(agents_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(RULES, "r", encoding="utf-8") as fh:
            rules = fh.read().strip("\n")
        self.assertIn("# keep me", text)
        self.assertIn("user rule", text)
        self.assertEqual(text.count(BEGIN), 1)
        self.assertEqual(text.count(END), 1)
        self.assertIn(rules, text)
        start = text.find(BEGIN)
        end = text.find(END)
        self.assertGreater(end, start)
        self.install()
        with open(agents_path, "r", encoding="utf-8") as fh:
            again = fh.read()
        self.assertEqual(again.count(BEGIN), 1)
        self.assertIn("# keep me", again)
        self.uninstall()
        with open(agents_path, "r", encoding="utf-8") as fh:
            restored = fh.read()
        self.assertNotIn(BEGIN, restored)
        self.assertNotIn(END, restored)
        self.assertIn("# keep me", restored)
        self.assertIn("user rule", restored)

    def test_agents_md_preserves_blank_lines_outside_span(self):
        agents_path = os.path.join(self.prefix, "AGENTS.md")
        original = "# keep me\n\n\nuser rule\n\n"
        with open(agents_path, "w", encoding="utf-8") as fh:
            fh.write(original)
        self.install()
        self.uninstall()
        with open(agents_path, "r", encoding="utf-8") as fh:
            restored = fh.read()
        self.assertIn("# keep me\n\n\nuser rule", restored)
        self.assertNotIn(BEGIN, restored)
        self.assertNotIn(END, restored)

    def test_agents_md_symlink_replaced_not_followed(self):
        real = os.path.join(self.tmpdir, "dotfiles", "AGENTS.md")
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("# from dotfiles\n")
        agents_path = os.path.join(self.prefix, "AGENTS.md")
        os.symlink(real, agents_path)
        self.install()
        self.assertFalse(os.path.islink(agents_path))
        self.assertTrue(os.path.isfile(agents_path))
        with open(real, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# from dotfiles\n")
        with open(agents_path, "r", encoding="utf-8") as fh:
            installed = fh.read()
        self.assertIn("# from dotfiles", installed)
        self.assertIn(BEGIN, installed)
        self.uninstall()
        self.assertFalse(os.path.islink(agents_path))
        with open(real, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# from dotfiles\n")
        with open(agents_path, "r", encoding="utf-8") as fh:
            restored = fh.read()
        self.assertIn("# from dotfiles", restored)
        self.assertNotIn(BEGIN, restored)

    def test_skip_missing_skills_and_agents(self):
        src = self.make_src(with_skills=False)
        self.assertFalse(os.path.isdir(os.path.join(src, "skills")))
        self.assertFalse(os.path.isdir(os.path.join(src, "agents")))
        proc = self.install(src=src)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.prefix, "skills")))
        self.assertTrue(os.path.isdir(os.path.join(self.prefix, "agents")))
        self.assertEqual(os.listdir(os.path.join(self.prefix, "skills")), [])
        self.assertEqual(os.listdir(os.path.join(self.prefix, "agents")), [])

    def test_symlink_skills_and_agents_when_present(self):
        src = self.make_src(with_skills=True)
        self.install(src=src)
        skill = os.path.join(self.prefix, "skills", "design")
        agent = os.path.join(self.prefix, "agents", "plan-skeptic.md")
        self.assertTrue(os.path.islink(skill))
        self.assertTrue(os.path.islink(agent))
        self.assertEqual(
            os.path.realpath(skill),
            os.path.realpath(os.path.join(src, "skills", "design")),
        )
        self.assertEqual(
            os.path.realpath(agent),
            os.path.realpath(os.path.join(src, "agents", "plan-skeptic.md")),
        )
        self.uninstall(src=src)
        self.assertFalse(os.path.lexists(skill))
        self.assertFalse(os.path.lexists(agent))

    def test_refuse_clobber_without_force(self):
        src = self.make_src(with_skills=True)
        os.makedirs(os.path.join(self.prefix, "skills"), exist_ok=True)
        dest = os.path.join(self.prefix, "skills", "design")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("not a symlink\n")
        proc = self.install(src=src, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--force", proc.stderr)
        with open(dest, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "not a symlink\n")
        proc = self.install(src=src, extra=["--force"])
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.islink(dest))

    def test_purge_deletes_state_dir(self):
        self.install()
        self.assertTrue(os.path.isdir(self.state_dir))
        self.uninstall()
        self.assertTrue(os.path.isdir(self.state_dir))
        self.uninstall(extra=["--purge"])
        self.assertFalse(os.path.exists(self.state_dir))

    def test_herdr_first_then_gate(self):
        self.install()
        cfg = self.config()
        for event in ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "SessionStart"):
            cmds = commands_for_event(cfg, event)
            self.assertEqual(len(cmds), 2, event)
            self.assertIn(HERDR_MARK, cmds[0])
            self.assertIn(GATE_MARK, cmds[1])

    def test_live_machine_untouched(self):
        self.install()
        self.uninstall()
        self._assert_live_untouched()

    def test_state_dir_env_override(self):
        alt = os.path.join(self.tmpdir, "alt-state")
        self.env["DEVIN_SKILLS_STATE_DIR"] = alt
        self.install()
        self.assertTrue(os.path.isfile(os.path.join(alt, "secret")))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "secret")))

    def test_project_install_writes_hooks_v1(self):
        self.install(extra=["--project"], cwd=self.tmpdir)
        path = os.path.join(self.tmpdir, ".devin", "hooks.v1.json")
        self.assertTrue(os.path.isfile(path))
        hooks = load_json(path)
        self.assertNotIn("hooks", hooks)
        self.assertNotIn("PermissionRequest", hooks)
        for event in GATE_EVENTS:
            self.assertEqual(len(marked(commands_for_event(hooks, event), GATE_MARK)), 1)
            self.assertEqual(len(marked(commands_for_event(hooks, event), HERDR_MARK)), 0)
        user = self.config()
        for event in HERDR_EVENTS:
            self.assertEqual(len(marked(commands_for_event(user, event), GATE_MARK)), 0)
        self.uninstall(extra=["--project"], cwd=self.tmpdir)
        self.assertFalse(os.path.exists(path))

    def test_uninstall_idempotent(self):
        self.install()
        self.uninstall()
        path = os.path.join(self.prefix, "config.json")
        with open(path, "rb") as fh:
            before = fh.read()
        before_mode = stat.S_IMODE(os.stat(path).st_mode)
        self.uninstall()
        with open(path, "rb") as fh:
            after = fh.read()
        self.assertEqual(after, before)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), before_mode)
        cfg = self.config()
        self.assertEqual(set(cfg["hooks"].keys()), set(HERDR_EVENTS))

    def test_gate_timeout_and_matcher(self):
        self.install()
        cfg = self.config()
        for event in GATE_EVENTS:
            elements = cfg["hooks"][event]
            gate_el = [el for el in elements if has_gates(el)]
            self.assertEqual(len(gate_el), 1, event)
            self.assertEqual(gate_el[0].get("matcher"), "")
            self.assertEqual(gate_el[0]["hooks"][0]["timeout"], 5)
            self.assertEqual(gate_el[0]["hooks"][0]["type"], "command")


def has_gates(element):
    for hook in element.get("hooks", []):
        cmd = hook.get("command", "")
        if GATE_MARK in cmd:
            return True
    return False


if __name__ == "__main__":
    unittest.main()

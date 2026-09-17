#!/usr/bin/env python3
"""Install/uninstall tests. Never touch the live ~/.config/devin."""

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

LIVE_CONFIG = "/home/brewerm/.config/devin/config.json"
LIVE_HERDR = "/home/brewerm/.config/devin/herdr-agent-state.sh"
LIVE_PREFIX = "/home/brewerm/.config/devin"


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")


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


def gate_element(installed_path):
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": "python3 '%s' hook" % installed_path,
                "timeout": 5,
            }
        ],
    }


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

    def inject_gates(self):
        cfg = self.config()
        installed = os.path.join(self.prefix, "hooks", "devin-gates.py")
        os.makedirs(os.path.dirname(installed), exist_ok=True)
        with open(installed, "w", encoding="utf-8") as fh:
            fh.write("# leftover gate\n")
        for event in GATE_EVENTS:
            existing = cfg["hooks"].get(event, [])
            cfg["hooks"][event] = list(existing) + [gate_element(installed)]
        dump_json(os.path.join(self.prefix, "config.json"), cfg)
        os.chmod(os.path.join(self.prefix, "config.json"), 0o600)

    def make_src(self, with_skills=False, extra_agent=None):
        src = os.path.join(self.tmpdir, "src")
        os.makedirs(os.path.join(src, "rules"), exist_ok=True)
        os.makedirs(os.path.join(src, "skills"), exist_ok=True)
        os.makedirs(os.path.join(src, "agents"), exist_ok=True)
        shutil.copy(RULES, os.path.join(src, "rules", "AGENTS.md"))
        if with_skills:
            os.makedirs(os.path.join(src, "skills", "design"), exist_ok=True)
            with open(
                os.path.join(src, "skills", "design", "SKILL.md"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("# design\n")
            with open(
                os.path.join(src, "agents", "design-writer.md"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write("# design-writer\n")
            if extra_agent:
                with open(
                    os.path.join(src, "agents", extra_agent),
                    "w",
                    encoding="utf-8",
                ) as fh:
                    fh.write("# extra\n")
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

    def test_install_does_not_add_gates(self):
        self.install()
        cfg = self.config()
        self.assertNotIn("PostCompaction", cfg["hooks"])
        self.assertNotIn("SessionEnd", cfg["hooks"])
        self.assertEqual(set(cfg["hooks"].keys()), set(HERDR_EVENTS))
        for event in HERDR_EVENTS:
            self.assertEqual(len(marked(commands_for_event(cfg, event), GATE_MARK)), 0)
        self.assertFalse(
            os.path.lexists(os.path.join(self.prefix, "hooks", "devin-gates.py"))
        )

    def test_herdr_command_strings_byte_identical(self):
        before = self.fixture()
        self.install()
        after = self.config()
        for event in HERDR_EVENTS:
            orig = marked(commands_for_event(before, event), HERDR_MARK)
            now = marked(commands_for_event(after, event), HERDR_MARK)
            self.assertEqual(orig, now)
            self.assertEqual(len(orig), 1)

    def test_strips_leftover_gate_hooks_and_files(self):
        self.inject_gates()
        cfg = self.config()
        for event in GATE_EVENTS:
            self.assertEqual(len(marked(commands_for_event(cfg, event), GATE_MARK)), 1)
        self.install()
        after = self.config()
        self.assertNotIn("PostCompaction", after["hooks"])
        self.assertNotIn("SessionEnd", after["hooks"])
        self.assertEqual(set(after["hooks"].keys()), set(HERDR_EVENTS))
        for event in HERDR_EVENTS:
            self.assertEqual(len(marked(commands_for_event(after, event), GATE_MARK)), 0)
            self.assertEqual(len(marked(commands_for_event(after, event), HERDR_MARK)), 1)
        self.assertFalse(
            os.path.lexists(os.path.join(self.prefix, "hooks", "devin-gates.py"))
        )
        backups = [
            name
            for name in os.listdir(self.prefix)
            if name.startswith("config.json.bak-devin-skills-")
        ]
        self.assertEqual(len(backups), 1)

    def test_no_backup_when_config_unchanged(self):
        self.install()
        backups = [
            name
            for name in os.listdir(self.prefix)
            if name.startswith("config.json.bak-devin-skills-")
        ]
        self.assertEqual(backups, [])

    def test_unknown_keys_preserved(self):
        self.install()
        cfg = self.config()
        orig = self.fixture()
        self.assertEqual(cfg["agent"], orig["agent"])
        self.assertEqual(cfg["devin"], orig["devin"])
        self.assertEqual(cfg["shell"], orig["shell"])
        self.assertEqual(cfg["theme_mode"], orig["theme_mode"])
        self.assertEqual(cfg["version"], orig["version"])

    def test_config_json_stays_0600(self):
        path = os.path.join(self.prefix, "config.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.install()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.inject_gates()
        os.chmod(path, 0o600)
        self.install()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.uninstall()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_does_not_create_state_dir(self):
        self.install()
        self.assertFalse(os.path.exists(self.state_dir))

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
        agent = os.path.join(self.prefix, "agents", "design-writer.md")
        self.assertTrue(os.path.islink(skill))
        self.assertTrue(os.path.islink(agent))
        self.assertEqual(
            os.path.realpath(skill),
            os.path.realpath(os.path.join(src, "skills", "design")),
        )
        self.assertEqual(
            os.path.realpath(agent),
            os.path.realpath(os.path.join(src, "agents", "design-writer.md")),
        )
        self.uninstall(src=src)
        self.assertFalse(os.path.lexists(skill))
        self.assertFalse(os.path.lexists(agent))

    def test_prunes_retired_skill_and_agent_symlinks(self):
        src = self.make_src(with_skills=True, extra_agent="plan-skeptic.md")
        self.install(src=src)
        stale_skill = os.path.join(self.prefix, "skills", "skeptic-plan")
        stale_agent = os.path.join(self.prefix, "agents", "plan-skeptic.md")
        self.assertTrue(os.path.islink(stale_agent))
        os.makedirs(os.path.join(src, "skills", "skeptic-plan"), exist_ok=True)
        with open(
            os.path.join(src, "skills", "skeptic-plan", "SKILL.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# skeptic-plan\n")
        os.symlink(os.path.join(src, "skills", "skeptic-plan"), stale_skill)
        os.remove(os.path.join(src, "agents", "plan-skeptic.md"))
        shutil.rmtree(os.path.join(src, "skills", "skeptic-plan"))
        self.install(src=src)
        self.assertFalse(os.path.lexists(stale_skill))
        self.assertFalse(os.path.lexists(stale_agent))
        self.assertTrue(os.path.islink(os.path.join(self.prefix, "skills", "design")))

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

    def test_purge_deletes_leftover_state_dir(self):
        os.makedirs(self.state_dir)
        with open(os.path.join(self.state_dir, "secret"), "wb") as fh:
            fh.write(b"x" * 32)
        self.install()
        self.assertTrue(os.path.isdir(self.state_dir))
        self.uninstall()
        self.assertTrue(os.path.isdir(self.state_dir))
        self.uninstall(extra=["--purge"])
        self.assertFalse(os.path.exists(self.state_dir))

    def test_live_machine_untouched(self):
        self.install()
        self.uninstall()
        self._assert_live_untouched()

    def test_project_install_links_without_writing_hooks(self):
        src = self.make_src(with_skills=True)
        self.install(src=src, extra=["--project"], cwd=self.tmpdir)
        path = os.path.join(self.tmpdir, ".devin", "hooks.v1.json")
        self.assertFalse(os.path.exists(path))
        skill = os.path.join(self.tmpdir, ".devin", "skills", "design")
        self.assertTrue(os.path.islink(skill))
        user = self.config()
        for event in HERDR_EVENTS:
            self.assertEqual(len(marked(commands_for_event(user, event), GATE_MARK)), 0)
        self.uninstall(src=src, extra=["--project"], cwd=self.tmpdir)
        self.assertFalse(os.path.lexists(skill))

    def test_project_install_strips_leftover_hooks_v1(self):
        os.makedirs(os.path.join(self.tmpdir, ".devin"))
        path = os.path.join(self.tmpdir, ".devin", "hooks.v1.json")
        dump_json(
            path,
            {
                "PreToolUse": [gate_element("/tmp/devin-gates.py")],
                "Stop": [gate_element("/tmp/devin-gates.py")],
            },
        )
        self.install(extra=["--project"], cwd=self.tmpdir)
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

    def test_real_repo_links_design_only(self):
        self.install()
        self.assertTrue(os.path.islink(os.path.join(self.prefix, "skills", "design")))
        self.assertTrue(
            os.path.islink(os.path.join(self.prefix, "agents", "design-writer.md"))
        )
        self.assertTrue(
            os.path.islink(os.path.join(self.prefix, "agents", "design-reviewer.md"))
        )
        skills = set(os.listdir(os.path.join(self.prefix, "skills")))
        agents = set(os.listdir(os.path.join(self.prefix, "agents")))
        self.assertEqual(skills, {"design"})
        self.assertEqual(agents, {"design-writer.md", "design-reviewer.md"})
        self.assertFalse(
            os.path.lexists(os.path.join(self.prefix, "skills", "skeptic-plan"))
        )
        self.assertFalse(
            os.path.lexists(os.path.join(self.prefix, "skills", "goal"))
        )


class SetupDesignTest(unittest.TestCase):
    def test_prints_id_and_root(self):
        script = os.path.join(ROOT, "skills", "design", "scripts", "setup-design.py")
        tmp = tempfile.mkdtemp(prefix="devin-skills-design-")
        try:
            env = os.environ.copy()
            env["XDG_CACHE_HOME"] = tmp
            proc = subprocess.run(
                ["python3", script],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            lines = dict(
                line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line
            )
            self.assertRegex(lines["design_id"], r"^[0-9a-f]{8}$")
            root = lines["design_allow_root"]
            self.assertTrue(root.startswith(os.path.realpath(tmp)))
            self.assertTrue(os.path.isdir(root))
            self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rejects_bad_id(self):
        script = os.path.join(ROOT, "skills", "design", "scripts", "setup-design.py")
        proc = subprocess.run(
            ["python3", script, "--id", "nothex"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("8 lowercase hex", proc.stderr)


if __name__ == "__main__":
    unittest.main()

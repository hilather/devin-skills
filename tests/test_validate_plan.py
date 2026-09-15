#!/usr/bin/env python3
"""Unit tests for skills/execute-plan/scripts/validate-plan.py (vendored from Grok Build)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR = os.path.join(ROOT, "skills", "execute-plan", "scripts", "validate-plan.py")


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_plan", VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vp = load_validator()


def run_cli(*argv):
    return subprocess.run(
        [sys.executable, VALIDATOR, *argv],
        capture_output=True,
        text=True,
    )


def write_doc(body):
    fh = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
    fh.write(body)
    fh.close()
    return fh.name


PR_ENTRY = """### PR {num}: {title}
- **Files/components affected:** {files}
- **Dependencies:** {deps}
- **Description:** {desc}
"""


def make_doc(*entries, preamble="# Design doc\n\nSome prose.\n\n"):
    return preamble + "## PR Plan\n\n" + "\n".join(entries) + "\n"


class TestParse(unittest.TestCase):
    def test_valid_plan_all_fields(self):
        doc = make_doc(
            PR_ENTRY.format(num=1, title="First", files="a.py, b.py", deps="None", desc="do first"),
            PR_ENTRY.format(num=2, title="Second", files="c.py", deps="PR 1", desc="do second"),
        )
        entries, errors = vp.parse_pr_plan(doc)
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["id"], "pr-1")
        self.assertEqual(entries[0]["title"], "First")
        self.assertEqual(entries[0]["files"], ["a.py", "b.py"])
        self.assertEqual(entries[0]["dependencies"], [])
        self.assertEqual(entries[0]["description"], "do first")
        self.assertEqual(entries[1]["id"], "pr-2")
        self.assertEqual(entries[1]["dependencies"], ["pr-1"])

    def test_missing_pr_plan_section(self):
        entries, errors = vp.parse_pr_plan("# Doc\n\nNo plan here.\n")
        self.assertIsNone(entries)
        self.assertTrue(any("PR Plan" in e for e in errors))

    def test_empty_pr_plan_section(self):
        entries, errors = vp.parse_pr_plan("## PR Plan\n\nNothing.\n")
        self.assertIsNone(entries)
        self.assertTrue(any("No PR entries" in e for e in errors))

    def test_duplicate_ids(self):
        doc = make_doc(
            PR_ENTRY.format(num=1, title="A", files="a.py", deps="None", desc="x"),
            PR_ENTRY.format(num=1, title="B", files="b.py", deps="None", desc="y"),
        )
        entries, _ = vp.parse_pr_plan(doc)
        errors = vp.validate_dag(entries)
        self.assertTrue(any("Duplicate" in e for e in errors))

    def test_unresolvable_dep(self):
        doc = make_doc(
            PR_ENTRY.format(num=1, title="A", files="a.py", deps="PR 99", desc="x"),
        )
        entries, _ = vp.parse_pr_plan(doc)
        errors = vp.validate_dag(entries)
        self.assertTrue(any("does not reference a valid PR ID" in e for e in errors))

    def test_cycle_detection_reports_path(self):
        doc = make_doc(
            PR_ENTRY.format(num=1, title="A", files="a.py", deps="PR 2", desc="x"),
            PR_ENTRY.format(num=2, title="B", files="b.py", deps="PR 1", desc="y"),
        )
        entries, _ = vp.parse_pr_plan(doc)
        errors = vp.validate_dag(entries)
        self.assertTrue(any("Cycle detected" in e for e in errors))
        # The traced path names both nodes.
        self.assertTrue(any("pr-1" in e and "pr-2" in e for e in errors))

    def test_unrecognized_dep_format(self):
        doc = make_doc(
            PR_ENTRY.format(num=1, title="A", files="a.py", deps="banana", desc="x"),
        )
        entries, errors = vp.parse_pr_plan(doc)
        self.assertIsNone(entries)
        self.assertTrue(any("Unrecognized dependency format" in e for e in errors))

    def test_deps_none_na_dash(self):
        for dep in ("None", "n/a", "-"):
            entries, errors = vp.parse_pr_plan(
                make_doc(PR_ENTRY.format(num=1, title="A", files="a.py", deps=dep, desc="x"))
            )
            self.assertEqual(errors, [], dep)
            self.assertEqual(entries[0]["dependencies"], [], dep)

    def test_non_numeric_ids(self):
        doc = make_doc(
            PR_ENTRY.format(num="0a", title="A", files="a.py", deps="None", desc="x"),
            PR_ENTRY.format(num="0b", title="B", files="b.py", deps="PR 0a", desc="y"),
        )
        entries, errors = vp.parse_pr_plan(doc)
        self.assertEqual(errors, [])
        self.assertEqual(entries[0]["id"], "pr-0a")
        self.assertEqual(entries[1]["dependencies"], ["pr-0a"])
        self.assertEqual(vp.validate_dag(entries), [])

    def test_fenced_block_stripping(self):
        fenced = "```\n## PR Plan\n\n### PR 9: Trap\n- **Files/components affected:** t.py\n- **Dependencies:** None\n- **Description:** trap\n```\n"
        doc = fenced + make_doc(
            PR_ENTRY.format(num=1, title="Real", files="a.py", deps="None", desc="x")
        )
        entries, errors = vp.parse_pr_plan(doc)
        self.assertEqual(errors, [])
        self.assertEqual([e["id"] for e in entries], ["pr-1"])


class TestDag(unittest.TestCase):
    def test_diamond_levels(self):
        # pr-1 → {pr-2, pr-3} → pr-4
        doc = make_doc(
            PR_ENTRY.format(num=1, title="A", files="a", deps="None", desc="x"),
            PR_ENTRY.format(num=2, title="B", files="b", deps="PR 1", desc="x"),
            PR_ENTRY.format(num=3, title="C", files="c", deps="PR 1", desc="x"),
            PR_ENTRY.format(num=4, title="D", files="d", deps="PR 2, PR 3", desc="x"),
        )
        entries, _ = vp.parse_pr_plan(doc)
        levels = vp.compute_levels(entries)
        self.assertEqual(
            levels, {"pr-1": 0, "pr-2": 1, "pr-3": 1, "pr-4": 2}
        )

    def test_linearization_numeric_order(self):
        # pr-2 must sort before pr-10 even though "10" < "2" lexically.
        doc = make_doc(
            *[
                PR_ENTRY.format(num=n, title="T{}".format(n), files="f", deps="None", desc="x")
                for n in (1, 2, 10)
            ]
        )
        entries, _ = vp.parse_pr_plan(doc)
        levels = vp.compute_levels(entries)
        order = vp.linearize(entries, levels)
        self.assertEqual(order, ["pr-1", "pr-2", "pr-10"])

    def test_linearization_level_major(self):
        doc = make_doc(
            PR_ENTRY.format(num=1, title="A", files="a", deps="None", desc="x"),
            PR_ENTRY.format(num=3, title="C", files="c", deps="PR 1", desc="x"),
            PR_ENTRY.format(num=2, title="B", files="b", deps="None", desc="x"),
        )
        entries, _ = vp.parse_pr_plan(doc)
        levels = vp.compute_levels(entries)
        order = vp.linearize(entries, levels)
        self.assertEqual(order, ["pr-1", "pr-2", "pr-3"])


class TestCli(unittest.TestCase):
    def test_valid_doc_exit_0_full_json(self):
        path = write_doc(
            make_doc(
                PR_ENTRY.format(num=1, title="A", files="a", deps="None", desc="x"),
                PR_ENTRY.format(num=2, title="B", files="b", deps="PR 1", desc="y"),
            )
        )
        self.addCleanup(os.unlink, path)
        res = run_cli(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        report = json.loads(res.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["pr_count"], 2)
        self.assertEqual(report["levels"], 2)
        self.assertEqual(report["max_parallelism"], 1)
        self.assertEqual(report["linearized_order"], ["pr-1", "pr-2"])
        self.assertEqual(report["level_assignments"], {"pr-1": 0, "pr-2": 1})
        self.assertEqual(report["errors"], [])

    def test_invalid_doc_exit_1(self):
        path = write_doc("# No plan\n")
        self.addCleanup(os.unlink, path)
        res = run_cli(path)
        self.assertEqual(res.returncode, 1)
        report = json.loads(res.stdout)
        self.assertFalse(report["valid"])
        self.assertTrue(report["errors"])

    def test_missing_file_exit_2(self):
        res = run_cli("/nonexistent/definitely-missing-doc.md")
        self.assertEqual(res.returncode, 2)
        report = json.loads(res.stdout)
        self.assertFalse(report["valid"])

    def test_no_args_exit_2(self):
        res = run_cli()
        self.assertEqual(res.returncode, 2)

    def test_extra_args_exit_2(self):
        res = run_cli("a", "b")
        self.assertEqual(res.returncode, 2)


if __name__ == "__main__":
    unittest.main()

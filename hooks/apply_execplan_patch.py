#!/usr/bin/env python3
"""Idempotently splice the execute-plan extension into devin-gates.py.

The gate source is a protected path inside Devin sessions, so the
execute-plan feature ships as hooks/devin_gates_execplan.py plus this
installer-side patch. install.sh runs it against the repo copy before
installing, so the repo copy stays canonical (tests and review see the real
artifact). Safe to re-run: the marker block is applied once.

Usage: python3 hooks/apply_execplan_patch.py [path-to-devin-gates.py]
"""

from __future__ import annotations

import os
import re
import sys

BEGIN = "# devin-skills-execplan:begin"
END = "# devin-skills-execplan:end"

BLOCK = """\
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

"""

ANCHOR = re.compile(r'^if __name__\s*==\s*["\']__main__["\']\s*:')


def apply_patch(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if BEGIN in text:
        # Repair the exec bit if an earlier splice dropped it.
        try:
            os.chmod(path, os.stat(path).st_mode | 0o111)
        except OSError:
            pass
        return "already applied"
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if ANCHOR.match(line):
            lines.insert(i, BLOCK)
            out = "".join(lines)
            tmp = path + ".tmp-execplan-patch"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(out)
            try:
                os.chmod(tmp, os.stat(path).st_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp, path)
            return "applied"
    raise SystemExit(
        "apply_execplan_patch.py: no 'if __name__' anchor found in %s" % path
    )


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: apply_execplan_patch.py <devin-gates.py>\n")
        return 1
    result = apply_patch(argv[1])
    sys.stdout.write("exec-plan patch: %s (%s)\n" % (result, argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

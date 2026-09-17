#!/usr/bin/env python3
"""Create a design artifact directory. Prints design_id= and design_allow_root=."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys

HEX8 = re.compile(r"^[0-9a-f]{8}$")


def fail(msg):
    sys.stderr.write("setup-design: %s\n" % msg)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Create a design artifact directory.")
    parser.add_argument("--id", dest="design_id", default="", help="8 lowercase hex id (omit to generate)")
    args = parser.parse_args()

    design_id = (args.design_id or "").strip().lower()
    if design_id:
        if not HEX8.match(design_id):
            fail("--id must be 8 lowercase hex characters")
    else:
        design_id = secrets.token_hex(4)

    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    root = os.path.join(os.path.realpath(os.path.expanduser(cache)), "devin-skills", "design", design_id)
    os.makedirs(root, mode=0o700, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    sys.stdout.write("design_id=%s\n" % design_id)
    sys.stdout.write("design_allow_root=%s\n" % root)


if __name__ == "__main__":
    main()

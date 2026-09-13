# Devin Skills

Layered design / plan-skeptic / code-skeptic workflow on Devin CLI extension points.

**The product is the write-lock** in `hooks/devin-gates.py`. Skills without it are theater. Markers are HMAC-signed and minted only by the gate script.

This is a stub README (full docs ship later).

- Keep Devin's built-in `/plan`. Do **not** add a skill named `plan`.
- Default is locked until a plan-skeptic HMAC marker exists.
- Stop is blocked until a code-skeptic marker exists.
- Goal harness: **not approximated**. Do not ship `/goal`.
- Python 3 stdlib only. Tests: `python3 -m unittest tests.test_gate -v`

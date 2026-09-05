#!/usr/bin/env python3
"""
encode_yahoo_state.py — Converts a Playwright yahoo_state.json into a single
base64 string suitable for pasting into a .env file as YAHOO_STATE_B64.

Usage:
    python encode_yahoo_state.py yahoo_state.json >> .env

This lets the Yahoo session travel via .env (which your GitLab runner/rsync
setup presumably already handles securely) instead of as a loose JSON file
that could get synced/overwritten unpredictably.
"""
import base64
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print("Usage: python encode_yahoo_state.py <path-to-yahoo_state.json>", file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    print(f"YAHOO_STATE_B64={encoded}")


if __name__ == "__main__":
    main()

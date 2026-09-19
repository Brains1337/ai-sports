#!/usr/bin/env python3
"""
auto_yahoo_state.py — Fully automated Yahoo CFB auth state generator.

Reads YAHOO_USERNAME and YAHOO_PASSWORD from .env, opens a headless (or
visible) browser, performs the Yahoo login flow, saves the Playwright
storage state as base64, and atomically updates YAHOO_STATE_B64 in .env.

Usage:
    python auto_yahoo_state.py [--visible] [--env-file /path/to/.env]

    --visible      Launch browser in headed mode (use when manual 2FA is needed).
    --env-file     Explicit path to .env file (required when running in a
                   container where .env is on a host path not mounted inside).

Requirements:
    pip install playwright
    python -m playwright install chromium

The script reads .env from the current directory (or parent if not found).
"""
import base64
import os
import re
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def find_env_file() -> Path | None:
    """Find .env in common locations: cwd, parents, /opt/stacks/fantasy-dashboard."""
    # Check cwd and all parent directories
    current = Path.cwd()
    while current != current.parent:
        env = current / ".env"
        if env.exists():
            return env
        current = current.parent
    # Check common production paths
    for prod_path in [
        Path("/opt/stacks/fantasy-dashboard/.env"),
        Path("/opt/stacks/.env"),
        Path("/.env"),
    ]:
        if prod_path.exists():
            return prod_path
    return Path(".env")


def load_env(env_file: Path) -> dict[str, str]:
    """Parse .env file into a dict."""
    env_vars = {}
    if not env_file.exists():
        return env_vars
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            env_vars[key.strip()] = val.strip()
    return env_vars


def update_env_state(env_file: Path, encoded_state: str) -> None:
    """Atomically replace YAHOO_STATE_B64 in .env."""
    content = env_file.read_text()

    if "YAHOO_STATE_B64=" in content:
        # Replace existing line
        content = re.sub(
            r'^YAHOO_STATE_B64=.*$',
            f"YAHOO_STATE_B64={encoded_state}",
            content,
            flags=re.MULTILINE,
        )
    else:
        # Append new line
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"YAHOO_STATE_B64={encoded_state}\n"

    # Write atomically
    tmp = env_file.with_suffix(".env.tmp")
    tmp.write_text(content)
    tmp.replace(env_file)


def main():
    visible_flag = "--visible" in sys.argv
    env_override = None
    for i, arg in enumerate(sys.argv):
        if arg == "--env-file" and i + 1 < len(sys.argv):
            env_override = sys.argv[i + 1]

    if env_override:
        env_file = Path(env_override)
    else:
        env_file = find_env_file()

    if env_file and env_file.exists():
        env_vars = load_env(env_file)
        yahoo_user = env_vars.get("YAHOO_USERNAME", os.getenv("YAHOO_USERNAME", ""))
        yahoo_pass = env_vars.get("YAHOO_PASSWORD", os.getenv("YAHOO_PASSWORD", ""))
    else:
        # Fall back to environment variables (e.g., from docker compose env)
        yahoo_user = os.getenv("YAHOO_USERNAME", "")
        yahoo_pass = os.getenv("YAHOO_PASSWORD", "")
        if yahoo_user and yahoo_pass:
            print(
                "[auto-yahoo] Using YAHOO_USERNAME/YAHOO_PASSWORD from environment "
                "(no .env file found)",
                file=sys.stderr,
            )
        else:
            print(
                "ERROR: No .env file found and YAHOO_USERNAME/YAHOO_PASSWORD not set.\n"
                "Set YAHOO_USERNAME and YAHOO_PASSWORD in .env, or pass --env-file.",
                file=sys.stderr,
            )
            sys.exit(1)

    if not yahoo_user or not yahoo_pass:
        print(
            "ERROR: Set YAHOO_USERNAME and YAHOO_PASSWORD in .env first.\n"
            "  Example:\n"
            "  YAHOO_USERNAME=your_email@gmail.com\n"
            "  YAHOO_PASSWORD=your_yahoo_app_password\n",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[auto-yahoo] Logging in as {yahoo_user}...")

    league_url = "https://college.fantasysports.yahoo.com/cfb/37494"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not visible_flag)
        context = browser.new_context()

        page = context.new_page()
        page.goto("https://login.yahoo.com/account/login")

        # Enter username
        page.fill('input[name="username"]', yahoo_user)
        page.click("input#login-signup")

        try:
            # Enter password (may be App Password or regular password)
            page.fill('input[name="password"]', yahoo_pass)
            page.click("input#login-signup")
        except Exception as e:
            print(f"[auto-yahoo] Password entry failed: {e}", file=sys.stderr)

        # Wait for login to complete (redirect to Yahoo homepage or mail)
        try:
            page.wait_for_timeout(3000)  # Give time for login
            # Check if we're still on login page
            try:
                page.wait_for_selector("input[name='username']", timeout=5000)
                # Still on login page — might need 2FA
                print(
                    "[auto-yahoo] Still on login page. You may need --visible for 2FA/MFA.",
                    file=sys.stderr,
                )
                browser.close()
                sys.exit(1)
            except PlaywrightTimeoutError:
                pass  # Login proceeded past the username screen
        except Exception:
            pass

        # Navigate to the Yahoo CFB league page
        page.goto(league_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # Save storage state
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False
        ) as f:
            state_path = f.name

        context.storage_state(path=state_path)
        browser.close()

        # Encode and update .env
        raw = Path(state_path).read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        Path(state_path).unlink()

        if env_file and env_file.exists():
            update_env_state(env_file, encoded)
            print(f"[auto-yahoo] YAHOO_STATE_B64 updated in {env_file}")
            print(f"[auto-yahoo] Length: {len(encoded)} chars")
        else:
            print(f"YAHOO_STATE_B64={encoded}")
            print(f"\n[auto-yahoo] No .env file found. Copy this value into your .env:")
            print(f"YAHOO_STATE_B64={encoded}")


if __name__ == "__main__":
    main()

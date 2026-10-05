"""Shared utilities used across the agent scripts.

Centralises helpers that were previously copy-pasted (e.g. popup, BOM clean).
"""

from __future__ import annotations

import ctypes
import os

import console as con


# ---------------------------------------------------------------------------
# Windows pop-up
# ---------------------------------------------------------------------------

def popup(title: str, message: str, *, error: bool = False) -> None:
    """Show a native Windows message box (falls back to console if unavailable).

    Set the ``AGENT_NO_POPUP`` env var to skip the blocking dialog in
    automated / non-interactive runs.
    """
    MB_OK = 0x0
    MB_ICONINFO = 0x40
    MB_ICONERROR = 0x10
    MB_TOPMOST = 0x40000
    flags = MB_OK | (MB_ICONERROR if error else MB_ICONINFO) | MB_TOPMOST
    if os.environ.get("AGENT_NO_POPUP"):
        prefix = "ERROR: " if error else ""
        print(f"\n[popup] {prefix}{title}\n{message}")
        return
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, flags)
    except Exception:  # noqa: BLE001 - not on Windows / no GUI
        prefix = "ERROR: " if error else ""
        print(f"\n{prefix}{title}\n{message}")


# ---------------------------------------------------------------------------
# Input cleaning
# ---------------------------------------------------------------------------

def clean_input(s: str) -> str:
    """Strip a leading UTF-8 BOM (or its cp1252 mojibake), drop non-printable
    chars, and trim whitespace."""
    for bom in ("\ufeff", "ï»¿"):
        if s.startswith(bom):
            s = s[len(bom):]
    return "".join(ch for ch in s if ch.isprintable()).strip()


# ---------------------------------------------------------------------------
# Path Resolution & Environment for PyInstaller
# ---------------------------------------------------------------------------

import sys
import subprocess

def get_bundled_dir() -> str:
    """Return the path to bundled assets (sys._MEIPASS if frozen, else local dir)."""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def get_user_dir() -> str:
    """Return the path where the executable is physically located, or local dir."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# .env auto-setup for distributable .exe
# ---------------------------------------------------------------------------

_ENV_TEMPLATE = """\
# =============================================================================
# Reporting Orchestrator — Environment Configuration
# =============================================================================
# Place this file next to the Reporting_Orchestrator.exe and fill in the values.
# Lines starting with '#' are comments and are ignored.
# =============================================================================

# ── LLM Configuration (for AI-powered report discovery) ──
# Default: Groq Cloud with LLaMA 3.3 70B. Get your key from https://console.groq.com/
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.groq.com/openai/v1
MODEL_NAME=qwen/qwen3.8-27b

# ── Workday Tenant Credentials (for catalog sync from Workday RaaS) ──
WORKDAY_RAAS_URL=
WORKDAY_ISU_USERNAME=
WORKDAY_ISU_PASSWORD=

# ── Workday Tenant URLs (change these to point to a different tenant) ──
# Source tenant login page (DPT3 by default)
WD_TENANT_LOGIN_URL=https://wd2-impl-identity.workday.com/wday/authgwy/accenture_dpt3/upc/login
# Customer Central login page
WD_CC_LOGIN_URL=https://wd2-impl-identity.workday.com/wday/authgwy/accenture_ptcc/upc/login?redirect=n

# ── Workday Login Credentials (for Migration & Export agents) ──
# These are prompted at runtime if left blank here.
WD_USER=
WD_PASS=

# ── Search Tuning (optional — defaults are fine) ──
BM25_TOP_N=30
LLM_TOP_K=5
"""


def ensure_env_file() -> None:
    """Check for .env next to the executable; create a template if missing.

    Also loads the .env into the process environment so ALL agents pick up
    the values (not just the Discovery Agent's config.py).
    """
    user_dir = get_user_dir()
    env_path = os.path.join(user_dir, ".env")

    created_new = False
    if not os.path.isfile(env_path):
        # First run on this machine — create the template
        try:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(_ENV_TEMPLATE)
            created_new = True
            print(f"\n  [SETUP] Created .env template at: {env_path}")
        except OSError as exc:
            print(f"\n  [WARNING] Could not create .env file: {exc}")

    # Load the .env file into os.environ (override=False so system env wins)
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path, override=False)
    except ImportError:
        pass  # dotenv not available — env vars must be set manually

    # Show clear startup diagnostics
    _print_env_status(created_new, env_path)


def _print_env_status(created_new: bool, env_path: str) -> None:
    """Print a clear summary of which config values are set / missing."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    wd_user = os.environ.get("WD_USER", "").strip()

    con.section("Environment Configuration")
    print()
    if api_key:
        con.info("OPENAI_API_KEY", f"{con.C.B_GREEN}[SET]{con.C.RESET}")
    else:
        con.info("OPENAI_API_KEY", f"{con.C.B_YELLOW}[NOT SET] — LLM scoring disabled{con.C.RESET}")
    if wd_user:
        con.info("WD_USER", f"{con.C.B_GREEN}[SET]{con.C.RESET}")
    else:
        con.info("WD_USER", f"{con.C.DIM}[NOT SET] — will be prompted{con.C.RESET}")

    if created_new:
        msg = (
            "A new .env configuration file has been created at:\n\n"
            f"{env_path}\n\n"
            "Please open this file in a text editor and fill in your\n"
            "API keys and credentials, then restart the application.\n\n"
            "The application will continue WITHOUT LLM scoring for now\n"
            "(basic keyword search will still work)."
        )
        popup("Setup Required — .env File Created", msg, error=True)


def ensure_playwright_installed() -> None:
    """Ensure the Playwright Chromium browser is installed on the user's system."""
    import sys
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    try:
        driver_executable = compute_driver_executable()
        env = get_driver_env()
        # Run the node-based playwright install script bundled with playwright package
        # Silently — output is suppressed to keep the terminal clean.
        subprocess.run(
            [str(driver_executable), "install", "chromium"],
            env=env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # Best-effort; the browser may already be installed

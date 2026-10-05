"""Interactive launcher for the Workday Report Export agent.

Prompts the user for report names, then opens each report in DPT3 and
downloads it as an Excel file via the "Export to Excel" button.

This is designed to run IN PARALLEL with the migration agent (run_agent.py)
in a separate CMD window — each spawns its own independent browser via Playwright.

Credentials are read from the WD_USER / WD_PASS environment variables.

Run:
    .venv\\Scripts\\python.exe run_export.py
"""

from __future__ import annotations

import getpass
import os
import re
import sys

from runner import run_config, StepError
from utils import popup, clean_input, get_user_dir

LOGIN_URL = os.environ.get(
    "WD_TENANT_LOGIN_URL",
    "https://wd2-impl-identity.workday.com/wday/authgwy/accenture_dpt3/upc/login",
)
# Where Excel files will be saved
DOWNLOADS_DIR = os.path.join(get_user_dir(), "exported_reports")


# --- user input ---------------------------------------------------------------
def prompt_reports() -> list[str]:
    print("=" * 60)
    print(" Workday Report Export Agent  (Excel download)")
    print("=" * 60)

    print(
        "\nEnter the report name(s) to export as Excel.\n"
        "  - one per line, OR a single comma-separated line\n"
        "  - press Enter on a blank line when done"
    )
    reports: list[str] = []
    while True:
        line = clean_input(input("  report> "))
        if line == "":
            if reports:
                break
            print("  Please enter at least one report name.")
            continue
        line = line.strip("{}").strip()
        parts = line.split(",") if "," in line else [line]
        for part in parts:
            name = clean_input(part.strip().strip('"').strip("'"))
            if name:
                reports.append(name)

    # de-duplicate while preserving order
    seen: set[str] = set()
    reports = [r for r in reports if not (r in seen or seen.add(r))]
    return reports


def _safe_filename(name: str) -> str:
    """Sanitize a report name for use in screenshot filenames."""
    return re.sub(r'[<>:"/\\|?*]', "_", name)


# --- dynamic step building ----------------------------------------------------
def _export_steps(report_name: str, is_first: bool) -> list[dict]:
    """Steps to search for, open, and export one report as Excel.

    Flow per report:
      1. Type report name in the global search bar and press Enter
      2. Click the Tasks and Reports tab
      3. Scroll to the exact report name and click its "Report Definition" link
      4. Report data page loads — click the report name in the blue header
      5. View Custom Report page opens — click the "Export to Excel" icon
      6. Wait for the "Export Document" popup, then click Download
    """
    safe = _safe_filename(report_name)
    steps: list[dict] = []

    if not is_first:
        # The search bar is visible on every Workday page, so we don't need
        # to navigate home. Just dismiss any leftover popup (e.g. Export
        # Document) so the search bar is accessible, then proceed directly.
        steps.extend([
            {"action": "press", "selector": "body", "key": "Escape", "optional": True,
             "label": "dismiss popup if still open"},
            {"action": "wait", "seconds": 2},
        ])

    # Escape single quotes in report names for use inside CSS selectors.
    # Playwright CSS :has-text() uses unquoted strings, but the locator()
    # API wraps them — so we just need to escape for the Python f-string.
    css_safe_name = report_name.replace("'", "\\'")

    steps.extend([
        # --- 1. search for the report ---
        # Triple-click to select any existing text in the search bar, then type
        # the new report name (fill replaces content, but clearing first avoids
        # stale search state).
        {"action": "click", "selector": "[data-automation-id='globalSearchInput']",
         "timeout": 10000, "label": "focus search bar"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']",
         "key": "Control+A", "label": "select all in search bar"},
        {"action": "type", "selector": "[data-automation-id='globalSearchInput']",
         "text": report_name, "label": f"search for {report_name!r}"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']",
         "key": "Enter", "label": "submit search"},
        # Wait for the Tasks and Reports tab to appear, then click it
        {"action": "wait_for", "selector": "text='Tasks and Reports'", "state": "visible", "timeout": 30000, "label": "wait for Tasks and Reports tab"},
        {"action": "wait", "seconds": 2},
        {"action": "click", "text": "Tasks and Reports", "exact": False, "timeout": 20000, "label": "click Tasks and Reports tab"},
        {"action": "wait", "seconds": 3},

        # --- 2. scroll to the exact report name and click its Report Definition ---
        # The search results may contain multiple reports with similar names
        # (e.g. "Payments Applied This Year" AND "CR Payments Applied This Year").
        # Each result is a block containing the report name + "Report Definition" sub-link.
        # We must click the "Report Definition" that belongs to the EXACT report name.
        #
        # Strategy: use a Playwright locator that scopes to the link matching the
        # exact report name, then navigate to its parent container, and within
        # that container find the "Report Definition" link.
        {"action": "scroll_into_view",
         "selector": f"a:has-text('{css_safe_name}'), [role='link']:has-text('{css_safe_name}')",
         "timeout": 30000, "label": f"scroll to {report_name!r} in search results"},
        {"action": "wait", "seconds": 1},

        # Click the "Report Definition" link scoped to the search-result block
        # that contains the exact report name. Workday wraps each result in a
        # container div — the :has() pseudo-class restricts our click to ONLY
        # the "Report Definition" under the correct report.
        {"action": "click",
         "selector": f"li:has(a:text-is('{css_safe_name}')) a:has-text('Report Definition')",
         "timeout": 20000,
         "label": f"click Report Definition under {report_name!r}"},

        # --- 3. report data page loads — click report name in the blue header ---
        # After clicking the search result, the report data/output page opens
        # directly (table view). The report name appears in the blue header bar.
        # Clicking it navigates to the View Custom Report page.
        {"action": "wait", "seconds": 5, "label": "wait for report data page to load"},
        {"action": "click", "text": report_name, "timeout": 20000,
         "label": f"click {report_name!r} in header to open View Custom Report"},

        # --- 4. View Custom Report page — click "Export to Excel" icon ---
        {"action": "wait", "seconds": 5, "label": "wait for View Custom Report page"},
        {"action": "click", "selector": "[title='Export to Excel']", "timeout": 20000,
         "label": "click Export to Excel"},

        # --- 5. wait for "Export Document" popup, then download ---
        {"action": "wait_for", "selector": "text=Export Document", "state": "visible",
         "timeout": 15000, "label": "wait for Export Document popup"},
        {"action": "wait", "seconds": 1},
        {"action": "download", "text": "Download", "timeout": 60000,
         "download_dir": DOWNLOADS_DIR,
         "label": f"download Excel for {report_name!r}"},
        {"action": "wait", "seconds": 3, "label": "wait after download"},
    ])

    return steps


def build_config(reports: list[str]) -> dict:
    """Build a runner-compatible config dict for the full export flow."""
    steps: list[dict] = [
        # --- login ---
        {"action": "navigate", "url": LOGIN_URL, "wait_until": "domcontentloaded",
         "timeout": 90000, "label": "open DPT3 login"},
        {"action": "type", "selector": "input[name='username']", "env": "WD_USER",
         "label": "enter username"},
        {"action": "type", "selector": "input[name='password']", "secret_env": "WD_PASS",
         "label": "enter password"},
        {"action": "click", "selector": "button[data-automation-id='goButton']",
         "label": "click Sign In"},
        {"action": "wait_for", "selector": "[data-automation-id='globalSearchInput']",
         "state": "visible", "timeout": 60000, "label": "wait for Workday home"},
        {"action": "wait", "seconds": 2, "label": "let home settle"},
    ]

    for i, name in enumerate(reports):
        steps.extend(_export_steps(name, is_first=(i == 0)))

    return {
        "browser": "chromium",
        "channel": "chrome",
        "headless": False,
        "viewport": {"width": 1440, "height": 900},
        "steps": steps,
    }


def main() -> int:
    from utils import ensure_playwright_installed
    ensure_playwright_installed()
    print("=" * 60)
    reports = prompt_reports()

    # Credentials: prefer env vars; prompt if missing.
    if not os.environ.get("WD_USER"):
        os.environ["WD_USER"] = input("Workday username: ").strip()
    if not os.environ.get("WD_PASS"):
        os.environ["WD_PASS"] = getpass.getpass("Workday password (hidden): ")

    print("\nStarting the Report Export agent with:")
    print(f"  Reports ({len(reports)}): {', '.join(reports)}")
    print()

    config = build_config(reports)
    exit_code, error = run_config(config, headless=False)

    if exit_code == 0:
        popup(
            "Report Export Agent",
            f"Successfully exported {len(reports)} report(s) as Excel.\n\n"
            f"Reports:\n  - " + "\n  - ".join(reports),
        )
    else:
        popup(
            "Report Export Agent - Failed",
            f"The export did not complete.\n\n"
            f"Error: {error}\n\nSee defects/error.png for the failure screenshot.",
            error=True,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

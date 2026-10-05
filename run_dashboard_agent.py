"""Config builder for the Workday Custom Dashboard Migration agent.

Creates a Configuration Package with "Custom Dashboards with Tabs" as the
Implementation Type — analogous to run_agent.py which uses "Custom Reports".

The Workday flow is identical to report migration except for the value typed
into the Implementation Types search box and the prompt option selected.

Run standalone:
    .venv\\Scripts\\python.exe run_dashboard_agent.py
"""

from __future__ import annotations

import getpass
import os
import sys

from runner import run_config
from utils import popup, clean_input, get_user_dir

# Reuse shared helpers from the report migration agent
from run_agent import (
    LOGIN_URL,
    CC_LOGIN_URL,
    _row_checkbox_selector,
    _report_steps,          # works for dashboards too — same grid pattern
    _customer_central_steps,
)


# --- user input ---------------------------------------------------------------
def prompt_inputs() -> tuple[str, list[str]]:
    print("=" * 60)
    print(" Workday Custom Dashboard Migration agent")
    print("=" * 60)

    industry = ""
    while not industry:
        industry = clean_input(input("Enter the Industry name: "))
        if not industry:
            print("  Industry name cannot be empty.")

    print(
        "\nEnter the dashboard name(s) to add.\n"
        "  - one per line, OR a single comma-separated line\n"
        "  - press Enter on a blank line when done"
    )
    dashboards: list[str] = []
    while True:
        line = clean_input(input("  dashboard> "))
        if line == "":
            if dashboards:
                break
            print("  Please enter at least one dashboard name.")
            continue
        line = line.strip("{}").strip()
        parts = line.split(",") if "," in line else [line]
        for part in parts:
            name = clean_input(part.strip().strip('"').strip("'"))
            if name:
                dashboards.append(name)

    # de-duplicate while preserving order
    seen: set[str] = set()
    dashboards = [d for d in dashboards if not (d in seen or seen.add(d))]
    return industry, dashboards


# --- dynamic step building ----------------------------------------------------

def build_config(industry: str, dashboards: list[str]) -> dict:
    """Build a Playwright step config for Custom Dashboard migration.

    Identical to the report migration flow except:
      - Implementation Type = "Custom Dashboards with Tabs"
      - Package name uses _Dashboard_Config_Package_ suffix
    """
    package_name = f"{industry}_Dashboard_Config_Package"

    steps: list[dict] = [
        # --- login ---
        {"action": "navigate", "url": LOGIN_URL, "wait_until": "domcontentloaded", "timeout": 90000, "label": "open DPT3 login"},
        {"action": "type", "selector": "input[name='username']", "env": "WD_USER", "label": "enter username"},
        {"action": "type", "selector": "input[name='password']", "secret_env": "WD_PASS", "label": "enter password"},
        {"action": "click", "selector": "button[data-automation-id='goButton']", "label": "click Sign In"},
        {"action": "wait_for", "selector": "[data-automation-id='globalSearchInput']", "state": "visible", "timeout": 60000, "label": "wait for Workday home"},
        {"action": "wait", "seconds": 2},

        # --- search + open the Create Configuration Package task ---
        {"action": "click", "selector": "[data-automation-id='globalSearchInput']", "timeout": 10000, "label": "focus search bar"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']", "key": "Control+A", "label": "select all in search bar"},
        {"action": "type", "selector": "[data-automation-id='globalSearchInput']", "text": "Create Configuration Package", "label": "search task"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']", "key": "Enter", "label": "submit search"},
        {"action": "wait_for", "selector": "text='Tasks and Reports'", "state": "visible", "timeout": 30000, "label": "wait for Tasks and Reports tab"},
        {"action": "wait", "seconds": 2},
        {"action": "click", "text": "Tasks and Reports", "exact": False, "timeout": 20000, "label": "click Tasks and Reports tab"},
        {"action": "wait", "seconds": 3},
        {"action": "click", "byRole": {"role": "link", "name": "Create Configuration Package", "exact": True}, "timeout": 20000, "label": "open the task"},

        # --- name + implementation type (DASHBOARD-SPECIFIC) ---
        {"action": "wait_for", "selector": "[data-automation-id='textInputBox']", "state": "visible", "timeout": 30000, "label": "wait for form"},
        {"action": "wait", "seconds": 2},
        {"action": "type", "text": package_name, "byLabel": "Configuration Package Name", "label": f"name = {package_name}"},
        {"action": "click", "selector": "[data-automation-id='promptIcon']", "label": "open Implementation Types prompt"},
        {"action": "wait", "seconds": 2},
        {"action": "type", "selector": "input[data-automation-id='searchBox']", "text": "Custom Dashboards with Tabs", "sequential": True, "clear": True, "label": "search 'Custom Dashboards with Tabs'"},
        {"action": "press", "selector": "input[data-automation-id='searchBox']", "key": "Enter", "label": "Enter"},
        {"action": "wait", "seconds": 3},
        {"action": "click", "selector": "[data-automation-id='promptOption'][data-automation-label='Custom Dashboards with Tabs']", "timeout": 15000, "label": "select Custom Dashboards with Tabs"},
        {"action": "wait", "seconds": 2},
        {"action": "click", "selector": "button[data-automation-id='wd-CommandButton_uic_okButton']", "confirm_hidden": "button[data-automation-id='wd-CommandButton_uic_okButton']", "timeout": 15000, "label": "click OK (create)"},
        {"action": "wait_for", "selector": "text=Complete your Configuration Package", "state": "visible", "timeout": 40000, "label": "wait for package detail page"},
        {"action": "wait", "seconds": 3},

        # --- add instances ---
        {"action": "click", "selector": "button:has-text('Add Instances')", "timeout": 15000, "label": "click Add Instances"},
        {"action": "wait", "seconds": 10, "label": "wait for Add Instances grid"},
    ]

    # one filter+select block per dashboard the user entered
    for name in dashboards:
        steps.extend(_report_steps(name))

    steps += [
        {"action": "wait", "seconds": 1},
        {"action": "click", "selector": "button[data-automation-id='wd-CommandButton_uic_okButton']", "confirm_hidden": "button[data-automation-id='wd-CommandButton_uic_okButton']", "timeout": 15000, "label": "click OK to add instances"},
        {"action": "wait_for", "selector": "text=Complete your Configuration Package", "state": "visible", "timeout": 40000, "label": "back to detail page"},
        {"action": "wait", "seconds": 3},

        # --- migrate ---
        {"action": "click", "selector": "button:has-text('Migrate')", "timeout": 15000, "label": "click Migrate"},
        {"action": "wait", "seconds": 4},
        # An "acknowledge this message" dialog (with OK) sometimes appears first.
        {"action": "click", "selector": "button[data-automation-id='wd-CommandButton_uic_okButton']", "optional": True, "timeout": 8000, "label": "acknowledge migrate message if shown"},
        {"action": "wait", "seconds": 3},
        # Navigate directly to Customer Central
        {"action": "navigate", "url": CC_LOGIN_URL, "wait_until": "domcontentloaded", "timeout": 90000, "label": "open Customer Central login"},
    ]

    # Customer Central block: create the configuration extract and download it
    steps.extend(_customer_central_steps(industry, package_name))

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
    industry, dashboards = prompt_inputs()

    # Credentials: prefer env vars; prompt if missing.
    if not os.environ.get("WD_USER"):
        os.environ["WD_USER"] = input("Workday username: ").strip()
    if not os.environ.get("WD_PASS"):
        os.environ["WD_PASS"] = getpass.getpass("Workday password (hidden): ")

    package_name = f"{industry}_Dashboard_Config_Package"
    print("\nStarting the agent with:")
    print(f"  Configuration Package : {package_name}")
    print(f"  Dashboards ({len(dashboards)})      : {', '.join(dashboards)}")
    print()

    config = build_config(industry, dashboards)
    exit_code, error = run_config(config, headless=False)

    if exit_code == 0:
        popup(
            "Workday Dashboard Agent",
            "Configuration Extract created successfully.\n\n"
            f"Configuration Package: {package_name}\n"
            f"Dashboards added: {len(dashboards)}\n"
            f"  - " + "\n  - ".join(dashboards) + "\n\n"
            f"Extract file downloaded: {package_name}.dat",
        )
    else:
        popup(
            "Workday Dashboard Agent - Failed",
            f"The task did not complete.\n\nConfiguration Package: {package_name}\n\n"
            f"Error: {error}\n\nSee defects/error.png for the failure screenshot.",
            error=True,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Interactive launcher for the Workday Configuration Package agent.

Prompts the user for an Industry name and one or more report names, then runs the
full Workday flow:
  - creates a Configuration Package named  "<Industry>_Config_Package"
  - implementation type: Custom Reports
  - adds every report the user listed (filtered + selected one by one)
  - clicks Migrate
and finally shows a "Task completed successfully" pop-up.

Credentials are read from the WD_USER / WD_PASS environment variables. If they are not
set, you'll be prompted for them (password input is hidden).

Run:
    .venv\\Scripts\\python.exe run_agent.py
"""

from __future__ import annotations

import getpass
import os
import sys

from runner import run_config
from utils import popup, clean_input, get_user_dir

LOGIN_URL = os.environ.get(
    "WD_TENANT_LOGIN_URL",
    "https://wd2-impl-identity.workday.com/wday/authgwy/accenture_dpt3/upc/login",
)
CC_LOGIN_URL = os.environ.get(
    "WD_CC_LOGIN_URL",
    "https://wd2-impl-identity.workday.com/wday/authgwy/accenture_ptcc/upc/login?redirect=n",
)


# --- user input ---------------------------------------------------------------
def prompt_inputs() -> tuple[str, list[str]]:
    print("=" * 60)
    print(" Workday Configuration Package agent")
    print("=" * 60)

    industry = ""
    while not industry:
        industry = clean_input(input("Enter the Industry name: "))
        if not industry:
            print("  Industry name cannot be empty.")

    print(
        "\nEnter the report name(s) to add.\n"
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
        line = line.strip("{}").strip()           # tolerate pasted {a, b, c}
        parts = line.split(",") if "," in line else [line]
        for part in parts:
            name = clean_input(part.strip().strip('"').strip("'"))
            if name:
                reports.append(name)

    # de-duplicate while preserving order
    seen: set[str] = set()
    reports = [r for r in reports if not (r in seen or seen.add(r))]
    return industry, reports


# --- dynamic step building ----------------------------------------------------
def _row_checkbox_selector(name: str) -> str:
    """Selector for a grid row's checkbox, matched by the report name text."""
    if "'" not in name:
        return f"tr[data-automation-id='row']:has-text('{name}') div[data-automation-id='checkbox']"
    # name contains a single quote -> use double quotes around the text
    return f'tr[data-automation-id=\'row\']:has-text("{name}") div[data-automation-id=\'checkbox\']'


def _report_steps(name: str) -> list[dict]:
    """Filter the Instance column to `name` and tick its checkbox.

    Selections accumulate across filter changes, so we repeat this per report.
    """
    return [
        {"action": "click", "selector": "button[data-automation-id^='buttonHeader']:has-text('Instance')",
         "timeout": 15000, "force": True, "label": f"open Instance filter for {name!r}"},
        {"action": "wait", "seconds": 2},
        # ':visible' targets the live filter popup — Workday leaves a stale hidden popup in
        # the DOM after the first filter, which would otherwise be matched by '.first'.
        {"action": "type", "selector": "input[data-automation-id='textInputBox']:visible", "text": name,
         "label": f"filter value = {name!r}"},
        # Apply the filter via the button's accessible role+name: its data-automation-id
        # changes once a filter is active, and get_by_role ignores the stale hidden popup
        # and never matches the "Remove Filter" button (exact name).
        {"action": "click", "byRole": {"role": "button", "name": "Filter", "exact": True},
         "timeout": 15000, "label": "click Filter"},
        {"action": "wait", "seconds": 4},
        {"action": "click", "selector": _row_checkbox_selector(name), "timeout": 15000,
         "label": f"select report {name!r}"},
        {"action": "wait", "seconds": 1},
    ]


def _customer_central_steps(industry: str, package_name: str) -> list[dict]:
    """Steps that run in the Customer Central tenant after Object Transporter is launched.

    Flow: wait for the user's SSO login -> Create Configuration Extract (name, description,
    source tenant dpt3, Configuration Package type) -> filter & select the package created
    earlier -> refresh the Extraction Reports screen -> download the generated extract file.

    These reuse the same Workday widget patterns proven in the migration flow (global search,
    byLabel form fields, column-header filters, the OK command button). Customer Central runs
    Workday too, so the selectors mirror the first half; the form-field labels and the download
    link are the most likely spots to need a live tweak (use the 'dump' action to inspect).
    """
    extract_name = f"{industry}_Configuration_Extract"
    description = (
        f"Configuration extract for the {industry} industry — contains the "
        f"{package_name} package migrated from the dpt3 tenant."
    )

    return [
        # --- PART 1: wait for the user to complete the Customer Central SSO login ---
        {"action": "pause",
         "title": "Customer Central login required",
         "message": (
             "The automation has navigated to the Customer Central tenant.\n\n"
             "Please complete the SSO login in the browser window.\n\n"
             "Click OK once you are logged in to continue the automation."
         ),
         "label": "wait for Customer Central login"},
        {"action": "wait_for", "selector": "[data-automation-id='globalSearchInput']", "state": "visible", "timeout": 180000, "label": "wait for Customer Central home"},
        {"action": "wait", "seconds": 2},

        # --- PART 2: open the Create Configuration Extract task ---
        {"action": "click", "selector": "[data-automation-id='globalSearchInput']", "timeout": 10000, "label": "focus search bar"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']", "key": "Control+A", "label": "select all in search bar"},
        {"action": "type", "selector": "[data-automation-id='globalSearchInput']", "text": "Create Configuration Extract", "label": "search task"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']", "key": "Enter", "label": "submit search"},
        {"action": "wait_for", "selector": "text='Tasks and Reports'", "state": "visible", "timeout": 30000, "label": "wait for Tasks and Reports tab"},
        {"action": "wait", "seconds": 2},
        {"action": "click", "text": "Tasks and Reports", "exact": False, "timeout": 20000, "label": "click Tasks and Reports tab"},
        {"action": "wait", "seconds": 3},
        {"action": "click", "byRole": {"role": "link", "name": "Create Configuration Extract", "exact": True}, "timeout": 20000, "label": "open the task"},
        {"action": "wait_for", "selector": "[data-automation-id='textInputBox']", "state": "visible", "timeout": 30000, "label": "wait for form"},
        {"action": "wait", "seconds": 2},

        # --- fill the form ---
        {"action": "type", "text": extract_name, "byLabel": "File Name", "label": f"file name = {extract_name}"},
        {"action": "type", "text": description, "byLabel": "Description", "label": "description"},

        # source tenant prompt -> accenture_dpt3 (type into the field, then pick the option)
        {"action": "click", "byLabel": "Source Tenant", "timeout": 15000, "label": "focus Source Tenant"},
        {"action": "wait", "seconds": 1},
        {"action": "type", "byLabel": "Source Tenant", "text": "dpt3", "sequential": True, "clear": True, "timeout": 15000, "label": "type 'dpt3' in Source Tenant"},
        {"action": "wait", "seconds": 3},
        {"action": "click", "selector": "[data-automation-id='promptOption']:has-text('dpt3')", "timeout": 15000, "label": "select accenture_dpt3"},
        {"action": "wait", "seconds": 1},

        # package type -> select the "Configuration Package" radio (this loads the package grid)
        {"action": "click", "byLabel": "Configuration Package", "exact": True, "timeout": 15000, "label": "select Configuration Package type"},
        {"action": "wait", "seconds": 5, "label": "wait for package grid to load"},

        # --- PART 3: filter the Package Name column and select the package created earlier ---
        {"action": "click", "selector": "button[data-automation-id^='buttonHeader']:has-text('Package Name')", "timeout": 15000, "force": True, "label": "open Package Name filter"},
        {"action": "wait", "seconds": 2},
        {"action": "type", "selector": "input[data-automation-id='textInputBox']:visible", "text": package_name, "label": f"filter value = {package_name!r}"},
        {"action": "click", "byRole": {"role": "button", "name": "Filter", "exact": True}, "timeout": 15000, "label": "click Filter"},
        {"action": "wait", "seconds": 4},
        {"action": "click", "selector": _row_checkbox_selector(package_name), "timeout": 15000, "label": f"select package {package_name!r}"},
        {"action": "wait", "seconds": 1},
        {"action": "click", "selector": "button[data-automation-id='wd-CommandButton_uic_okButton']", "confirm_hidden": "button[data-automation-id='wd-CommandButton_uic_okButton']", "timeout": 15000, "label": "click OK"},
        {"action": "wait", "seconds": 5, "label": "wait for Extraction Reports screen"},

        # --- PART 4: refresh until Status = 'Completed', then download the extract file ---
        # On the Extraction Reports screen the extract starts as 'Processing'; Refresh until it
        # flips to 'Completed' and the Download File cell shows a clickable '<package>.dat'.
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 20000, "label": "click Refresh"},
        {"action": "wait", "seconds": 6, "label": "wait for report status to update"},
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 10000, "label": "click Refresh again"},
        {"action": "wait_for", "selector": "text=Completed", "state": "visible", "timeout": 90000, "label": "wait for Status = Completed"},
        {"action": "wait", "seconds": 2},
        # The download is a Workday file-attachment widget (role=link) labelled '<package>.dat';
        # clicking the '.dat' text triggers the file download, which we capture to disk.
        {"action": "download", "text": ".dat", "path": os.path.join(get_user_dir(), f"{package_name}.dat"), "timeout": 60000, "label": "download .dat extract"},
        {"action": "wait", "seconds": 2},

        # --- PART 5: Migrate Configuration Extract (upload the .dat into the target tenant) ---

        # 5a. Go back to Home
        {"action": "click", "selector": "[data-automation-id='homeButtonIcon'], button[aria-label='Home']", "timeout": 15000, "label": "click Home icon"},
        {"action": "wait_for", "selector": "[data-automation-id='globalSearchInput']", "state": "visible", "timeout": 30000, "label": "wait for home screen"},
        {"action": "wait", "seconds": 2},

        # 5b. Search and open "Migrate Configuration Extract"
        {"action": "click", "selector": "[data-automation-id='globalSearchInput']", "timeout": 10000, "label": "focus search bar"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']", "key": "Control+A", "label": "select all in search bar"},
        {"action": "type", "selector": "[data-automation-id='globalSearchInput']", "text": "Migrate Configuration Extract", "label": "search Migrate Configuration Extract"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']", "key": "Enter", "label": "submit search"},
        {"action": "wait_for", "selector": "text='Tasks and Reports'", "state": "visible", "timeout": 30000, "label": "wait for Tasks and Reports tab"},
        {"action": "wait", "seconds": 2},
        {"action": "click", "text": "Tasks and Reports", "exact": False, "timeout": 20000, "label": "click Tasks and Reports tab"},
        {"action": "wait", "seconds": 3},
        {"action": "click", "byRole": {"role": "link", "name": "Migrate Configuration Extract", "exact": True}, "timeout": 20000, "label": "open Migrate Configuration Extract task"},
        {"action": "wait", "seconds": 5, "label": "wait for Migrate Configuration Extract form"},

        # 5c. Upload the .dat file using Playwright's file_chooser
        {"action": "upload", "selector": "button:has-text('Select files')", "file_path": os.path.join(get_user_dir(), f"{package_name}.dat"), "timeout": 30000, "label": f"upload {package_name}.dat"},
        {"action": "wait", "seconds": 3, "label": "wait for file upload to process"},

        # 5d. Select Target Tenant -> dpt10
        {"action": "click", "byLabel": "Target Tenant", "timeout": 15000, "label": "focus Target Tenant"},
        {"action": "wait", "seconds": 1},
        {"action": "type", "byLabel": "Target Tenant", "text": "dpt10", "sequential": True, "clear": True, "timeout": 15000, "label": "type 'dpt10' in Target Tenant"},
        {"action": "wait", "seconds": 3},
        {"action": "click", "selector": "[data-automation-id='promptOption']:has-text('dpt10')", "timeout": 15000, "label": "select dpt10"},
        {"action": "wait", "seconds": 2},

        # 5e. Click OK (first screen)
        {"action": "click", "selector": "button[data-automation-id='wd-CommandButton_uic_okButton']", "timeout": 15000, "label": "click OK (submit migrate extract)"},
        {"action": "wait", "seconds": 5},

        # 5f. Click OK on the confirmation/next screen
        {"action": "click", "selector": "button[data-automation-id='wd-CommandButton_uic_okButton']", "optional": True, "timeout": 15000, "label": "click OK (confirmation screen)"},
        {"action": "wait", "seconds": 5},

        # 5g. Refresh until "View Diff Report" button appears, then click it
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 15000, "label": "click Refresh (1st)"},
        {"action": "wait", "seconds": 6},
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 10000, "label": "click Refresh (2nd)"},
        {"action": "wait", "seconds": 6},
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 10000, "label": "click Refresh (3rd)"},
        {"action": "wait", "seconds": 6},
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 10000, "label": "click Refresh (4th)"},
        {"action": "wait_for", "text": "View Diff Report", "state": "visible", "timeout": 120000, "label": "wait for View Diff Report button"},
        {"action": "wait", "seconds": 2},
        {"action": "click", "selector": "button:has-text('View Diff Report')", "timeout": 15000, "label": "click View Diff Report"},
        {"action": "wait", "seconds": 5, "label": "wait for diff report to load"},

        # 5h. Click "Proceed with Migration" on the Pre-Migration Diff Report page
        {"action": "click", "selector": "button:has-text('Proceed with Migration')", "timeout": 20000, "label": "click Proceed with Migration"},
        {"action": "wait", "seconds": 3},

        # 5i. Click "Start Migration" on the confirmation dialog
        {"action": "click", "selector": "button:has-text('Start Migration')", "timeout": 15000, "label": "click Start Migration"},
        {"action": "wait", "seconds": 5},

        # 5j. Refresh until "View Post Migration Report" button appears
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 15000, "label": "click Refresh (migration 1st)"},
        {"action": "wait", "seconds": 10},
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 10000, "label": "click Refresh (migration 2nd)"},
        {"action": "wait", "seconds": 10},
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 10000, "label": "click Refresh (migration 3rd)"},
        {"action": "wait", "seconds": 10},
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 10000, "label": "click Refresh (migration 4th)"},
        {"action": "wait", "seconds": 10},
        {"action": "click", "selector": "button:has-text('Refresh')", "optional": True, "timeout": 10000, "label": "click Refresh (migration 5th)"},
        {"action": "wait_for", "text": "View Post Migration Report", "state": "visible", "timeout": 180000, "label": "wait for View Post Migration Report button"},
        {"action": "wait", "seconds": 2},

        # 5k. Click "View Post Migration Report"
        {"action": "click", "selector": "button:has-text('View Post Migration Report')", "timeout": 15000, "label": "click View Post Migration Report"},
        {"action": "wait", "seconds": 5, "label": "wait for post migration summary"},

        # 5l. Click "Done" on the Post Migration Summary page
        {"action": "click", "selector": "button:has-text('Done')", "timeout": 15000, "label": "click Done"},
        {"action": "wait", "seconds": 3, "label": "migration workflow complete"},
    ]


def build_config(industry: str, reports: list[str]) -> dict:
    package_name = f"{industry}_Config_Package"

    steps: list[dict] = [
        # --- login ---
        {"action": "navigate", "url": LOGIN_URL, "wait_until": "domcontentloaded", "timeout": 90000, "label": "open DPT3 login"},
        {"action": "type", "selector": "input[name='username']", "env": "WD_USER", "label": "enter username"},
        {"action": "type", "selector": "input[name='password']", "secret_env": "WD_PASS", "label": "enter password"},
        {"action": "click", "selector": "button[data-automation-id='goButton']", "label": "click Sign In"},
        {"action": "wait_for", "selector": "[data-automation-id='globalSearchInput']", "state": "visible", "timeout": 60000, "label": "wait for Workday home"},
        {"action": "wait", "seconds": 2},

        # --- search + open the Create Configuration Package task ---
        # Workday's custom search bar requires explicit focus before fill() works.
        {"action": "click", "selector": "[data-automation-id='globalSearchInput']", "timeout": 10000, "label": "focus search bar"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']", "key": "Control+A", "label": "select all in search bar"},
        {"action": "type", "selector": "[data-automation-id='globalSearchInput']", "text": "Create Configuration Package", "label": "search task"},
        {"action": "press", "selector": "[data-automation-id='globalSearchInput']", "key": "Enter", "label": "submit search"},
        # Wait for the search results page to load, then click the Tasks and Reports tab
        # to ensure the task link is visible (not buried under Top Results).
        {"action": "wait_for", "selector": "text='Tasks and Reports'", "state": "visible", "timeout": 30000, "label": "wait for Tasks and Reports tab"},
        {"action": "wait", "seconds": 2},
        {"action": "click", "text": "Tasks and Reports", "exact": False, "timeout": 20000, "label": "click Tasks and Reports tab"},
        {"action": "wait", "seconds": 3},
        # Click the task link — uses role='link' to match Workday's search result elements
        # (which may be <a> or <div role='link'>), with exact name to avoid matching
        # 'Create Security Configuration Package'.
        {"action": "click", "byRole": {"role": "link", "name": "Create Configuration Package", "exact": True}, "timeout": 20000, "label": "open the task"},

        # --- name + implementation type ---
        {"action": "wait_for", "selector": "[data-automation-id='textInputBox']", "state": "visible", "timeout": 30000, "label": "wait for form"},
        {"action": "wait", "seconds": 2},
        {"action": "type", "text": package_name, "byLabel": "Configuration Package Name", "label": f"name = {package_name}"},
        {"action": "click", "selector": "[data-automation-id='promptIcon']", "label": "open Implementation Types prompt"},
        {"action": "wait", "seconds": 2},
        {"action": "type", "selector": "input[data-automation-id='searchBox']", "text": "custom report", "sequential": True, "clear": True, "label": "search 'custom report'"},
        {"action": "press", "selector": "input[data-automation-id='searchBox']", "key": "Enter", "label": "Enter"},
        {"action": "wait", "seconds": 3},
        {"action": "click", "selector": "[data-automation-id='promptOption'][data-automation-label='Custom Reports']", "timeout": 15000, "label": "select Custom Reports"},
        {"action": "wait", "seconds": 2},
        {"action": "click", "selector": "button[data-automation-id='wd-CommandButton_uic_okButton']", "confirm_hidden": "button[data-automation-id='wd-CommandButton_uic_okButton']", "timeout": 15000, "label": "click OK (create)"},
        {"action": "wait_for", "selector": "text=Complete your Configuration Package", "state": "visible", "timeout": 40000, "label": "wait for package detail page"},
        {"action": "wait", "seconds": 3},

        # --- add instances ---
        {"action": "click", "selector": "button:has-text('Add Instances')", "timeout": 15000, "label": "click Add Instances"},
        # large grid (10k+ rows) — give it time to finish loading so the column-header
        # menu isn't blocked by a transient loading overlay on the first filter click.
        {"action": "wait", "seconds": 10, "label": "wait for Add Instances grid"},
    ]

    # one filter+select block per report the user entered
    for name in reports:
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
        # Navigate directly to Customer Central instead of clicking the link
        {"action": "navigate", "url": CC_LOGIN_URL, "wait_until": "domcontentloaded", "timeout": 90000, "label": "open Customer Central login"},
    ]

    # one Customer Central block: create the configuration extract and download it
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
    industry, reports = prompt_inputs()

    # Credentials: prefer env vars; prompt if missing.
    if not os.environ.get("WD_USER"):
        os.environ["WD_USER"] = input("Workday username: ").strip()
    if not os.environ.get("WD_PASS"):
        os.environ["WD_PASS"] = getpass.getpass("Workday password (hidden): ")

    package_name = f"{industry}_Config_Package"
    print("\nStarting the agent with:")
    print(f"  Configuration Package : {package_name}")
    print(f"  Reports ({len(reports)})        : {', '.join(reports)}")
    print()

    config = build_config(industry, reports)
    exit_code, error = run_config(config, headless=False)

    if exit_code == 0:
        popup(
            "Workday Agent",
            "Configuration Extract created successfully.\n\n"
            f"Configuration Package: {package_name}\n"
            f"Reports added: {len(reports)}\n"
            f"  - " + "\n  - ".join(reports) + "\n\n"
            f"Extract file downloaded: {package_name}.dat",
        )
    else:
        popup(
            "Workday Agent - Failed",
            f"The task did not complete.\n\nConfiguration Package: {package_name}\n\n"
            f"Error: {error}\n\nSee defects/error.png for the failure screenshot.",
            error=True,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

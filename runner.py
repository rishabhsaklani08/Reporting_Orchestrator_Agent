"""Config-driven browser automation runner built on Playwright.

Reads a JSON config describing a browser session and a list of steps
(navigate, click, scroll, type, wait, screenshot, ...) and executes them
in order against a real browser.

Usage:
    python runner.py config.json
    python runner.py config.json --headed        # show the browser window
    python runner.py config.json --slowmo 500     # slow each action by 500ms
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PWTimeoutError, sync_playwright

import console as con


class StepError(Exception):
    """Raised when a single step fails so the runner can report it cleanly."""


# --- individual step handlers -------------------------------------------------
# Each handler receives the active Page and the step dict, and performs one action.


def _step_navigate(page: Page, step: dict[str, Any]) -> str:
    url = step["url"]
    wait_until = step.get("wait_until", "load")  # load | domcontentloaded | networkidle
    page.goto(url, wait_until=wait_until, timeout=step.get("timeout", 30000))
    return f"navigated to {url}"


def _resolve_locator(page: Page, step: dict[str, Any], allow_text: bool = True):
    """Build a Playwright locator from whichever targeting field the step provides.

    Targeting options (in priority order):
      - byLabel:          page.get_by_label(...)  — match a form field by its label text
                          (named 'byLabel' so it never clashes with a step's 'label' caption)
      - automation_label: [data-automation-label="..."] — Workday prompt options
      - text:             page.get_by_text(...)    — match by visible text
      - selector:         a raw CSS / Playwright selector
    Optional 'nth' picks the Nth match (negative allowed, e.g. -1 for last); default first.

    allow_text=False is used by the 'type' action, where 'text' is the value to type,
    not a targeting field — so it must fall through to byLabel/automation_label/selector.
    """
    exact = step.get("exact", False)
    if "byRole" in step:
        # {"role": "button", "name": "Filter", "exact": True} — get_by_role ignores hidden
        # elements (a11y tree), which conveniently skips stale/hidden popups.
        spec = step["byRole"]
        kwargs: dict[str, Any] = {}
        if spec.get("name") is not None:
            kwargs["name"] = spec["name"]
            kwargs["exact"] = spec.get("exact", exact)
        loc = page.get_by_role(spec["role"], **kwargs)
        desc = f"role={spec['role']!r} name={spec.get('name')!r}"
    elif "byLabel" in step:
        loc, desc = page.get_by_label(step["byLabel"], exact=exact), f"byLabel={step['byLabel']!r}"
    elif "automation_label" in step:
        value = step["automation_label"]
        loc = page.locator(f'[data-automation-label="{value}"]')
        desc = f"automation_label={value!r}"
    elif allow_text and "text" in step:
        loc, desc = page.get_by_text(step["text"], exact=exact), f"text={step['text']!r}"
    elif "selector" in step:
        loc, desc = page.locator(step["selector"]), step["selector"]
    else:
        raise StepError("step needs one of: selector, byLabel, automation_label, text, byRole")
    if "nth" in step:
        return loc.nth(step["nth"]), f"{desc}[nth={step['nth']}]"
    return loc.first, desc


def _step_click(page: Page, step: dict[str, Any]) -> str:
    loc, desc = _resolve_locator(page, step)
    timeout = step.get("timeout", 10000)
    # 'confirm_hidden' makes the click reliable for flaky submit buttons: after clicking we
    # wait for the given selector to disappear; if it doesn't, we re-click (up to 'attempts').
    confirm = step.get("confirm_hidden")
    # 'force' skips Playwright's actionability/pointer-interception re-check and clicks the
    # element's center directly — useful for Workday column-header menus where a transient
    # loading overlay over a large grid intercepts the click and stalls it until timeout.
    force = step.get("force", False)
    attempts = step.get("attempts", 3 if confirm else 1)
    for i in range(attempts):
        loc.click(timeout=timeout, force=force)
        if not confirm:
            break
        try:
            page.wait_for_selector(confirm, state="hidden", timeout=step.get("confirm_timeout", 10000))
            break
        except PWTimeoutError:
            if i == attempts - 1:
                raise StepError(
                    f"clicked {desc} but {confirm!r} never disappeared after {attempts} attempt(s)"
                )
    return f"clicked {desc}" + (f" (confirmed {confirm!r} gone)" if confirm else "")


def _step_click_text(page: Page, step: dict[str, Any]) -> str:
    """Click an element by its visible text (handy when you don't have a selector)."""
    text = step["text"]
    exact = step.get("exact", False)
    page.get_by_text(text, exact=exact).first.click(timeout=step.get("timeout", 10000))
    return f"clicked element with text {text!r}"


def _step_scroll(page: Page, step: dict[str, Any]) -> str:
    """Scroll the page. Either by a pixel amount or to the bottom/top."""
    direction = step.get("direction", "down")
    if direction == "bottom":
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return "scrolled to bottom"
    if direction == "top":
        page.evaluate("window.scrollTo(0, 0)")
        return "scrolled to top"
    pixels = step.get("pixels", 600)
    delta = pixels if direction == "down" else -pixels
    page.mouse.wheel(0, delta)
    return f"scrolled {direction} {pixels}px"


def _step_scroll_into_view(page: Page, step: dict[str, Any]) -> str:
    selector = step["selector"]
    page.locator(selector).first.scroll_into_view_if_needed(timeout=step.get("timeout", 10000))
    return f"scrolled {selector} into view"


def _resolve_text(step: dict[str, Any]) -> str:
    """Return the text for a step, reading from an env var if 'env'/'secret_env' is set.

    Use 'secret_env' for passwords so the value is never stored in the config and is
    masked in logs; use 'env' for non-secret values you still want kept out of the file.
    """
    env_key = step.get("secret_env") or step.get("env")
    if env_key:
        value = os.environ.get(env_key)
        if value is None or value == "":
            raise StepError(f"environment variable {env_key!r} is not set")
        return value
    return step["text"]


def _step_type(page: Page, step: dict[str, Any]) -> str:
    loc, desc = _resolve_locator(page, step, allow_text=False)
    text = _resolve_text(step)
    timeout = step.get("timeout", 10000)
    # Some widgets (e.g. Workday prompt search boxes) only filter on real keystrokes,
    # so 'sequential' types character-by-character instead of setting the value directly.
    if step.get("sequential"):
        # focus() (not click) avoids pointer-interception when an open dropdown overlaps
        # the field — common in Workday prompts where options float over the search box.
        loc.focus(timeout=timeout)
        if step.get("clear"):
            loc.press("Control+A")
            loc.press("Delete")
        loc.press_sequentially(text, delay=step.get("delay", 70), timeout=timeout)
    else:
        loc.fill(text, timeout=timeout)
    shown = "********" if step.get("secret_env") else None
    return f"typed {shown or repr(text)[:40]} into {desc}"


def _step_press(page: Page, step: dict[str, Any]) -> str:
    key = step["key"]  # e.g. "Enter", "Escape", "Control+A"
    has_target = any(k in step for k in ("byRole", "byLabel", "automation_label", "selector"))
    if has_target:
        loc, desc = _resolve_locator(page, step, allow_text=False)
        loc.press(key, timeout=step.get("timeout", 10000))
        return f"pressed {key} on {desc}"
    page.keyboard.press(key)
    return f"pressed {key}"


def _step_wait_for(page: Page, step: dict[str, Any]) -> str:
    selector = step.get("selector")
    if not selector:
        if "text" in step:
            selector = f"text={step['text']}"
        elif "byLabel" in step:
            selector = f"text={step['byLabel']}"
        else:
            raise StepError("wait_for needs 'selector' or 'text'")
    state = step.get("state", "visible")  # attached | detached | visible | hidden
    page.wait_for_selector(selector, state=state, timeout=step.get("timeout", 10000))
    return f"waited for {selector} ({state})"


def _step_wait(page: Page, step: dict[str, Any]) -> str:
    seconds = step.get("seconds", 1)
    time.sleep(seconds)
    return f"waited {seconds}s"


def _step_screenshot(page: Page, step: dict[str, Any]) -> str:
    path = step.get("path", "screenshot.png")
    full_page = step.get("full_page", True)
    page.screenshot(path=path, full_page=full_page)
    return f"saved screenshot to {path}"


def _step_dump(page: Page, step: dict[str, Any]) -> str:
    """Debug helper: print tag / data-automation-id / aria-label / text for matches.

    Useful for discovering selectors in dynamic UIs (e.g. Workday). Provide 'selector'
    (defaults to interactive elements) and an optional 'contains' substring filter.
    """
    selector = step.get("selector", "button, a, input, [role='columnheader'], th")
    contains = step.get("contains")
    limit = step.get("limit", 40)
    rows = page.eval_on_selector_all(
        selector,
        """els => els.map(e => ({
            tag: e.tagName,
            aid: e.getAttribute('data-automation-id') || '',
            al: e.getAttribute('aria-label') || '',
            ph: e.getAttribute('placeholder') || '',
            t: (e.innerText || e.value || '').trim().slice(0, 40)
        }))""",
    )
    if contains:
        needle = contains.lower()
        rows = [r for r in rows if needle in (r["t"] + r["al"] + r["aid"] + r["ph"]).lower()]
    print(f"    --- dump {selector!r}" + (f" contains {contains!r}" if contains else "") + " ---")
    for r in rows[:limit]:
        if any(r[k] for k in ("aid", "al", "ph", "t")):
            print(f"      {r}")
    if step.get("html"):
        # Also print truncated outerHTML of matches (selector discovery), honoring 'contains'.
        html_rows = page.eval_on_selector_all(
            selector,
            """(els, needle) => els
                .filter(e => !needle || (e.innerText || '').toLowerCase().includes(needle))
                .map(e => e.outerHTML.slice(0, 900))""",
            (contains or "").lower(),
        )
        for h in html_rows[: step.get("html_limit", 2)]:
            print(f"      HTML: {h}")
    return f"dumped {min(len(rows), limit)} element(s)"


def _step_pause(page: Page, step: dict[str, Any]) -> str:
    """Block the run until the user confirms — used for manual steps like an SSO login.

    Shows a native, top-most Windows message box with an OK button (modal: the run resumes
    only when the user clicks OK). Falls back to a console prompt when no GUI is available
    or AGENT_NO_POPUP is set, so headless/automated runs can still proceed via stdin.
    """
    title = step.get("title", "Action required")
    message = step.get("message", "Paused. Click OK / press Enter to continue.")
    if not os.environ.get("AGENT_NO_POPUP"):
        try:
            MB_OK = 0x0
            MB_ICONINFO = 0x40
            MB_TOPMOST = 0x40000
            import ctypes  # Windows-only; lazily imported so the module loads anywhere.

            ctypes.windll.user32.MessageBoxW(0, message, title, MB_OK | MB_ICONINFO | MB_TOPMOST)
            return "user confirmed (dialog); resuming"
        except Exception:  # noqa: BLE001 - not on Windows / no GUI -> console fallback
            pass
    print(f"\n[pause] {title}\n{message}")
    input("Press Enter to continue... ")
    return "user confirmed (console); resuming"


def _step_download(page: Page, step: dict[str, Any]) -> str:
    """Click a target and capture the file download it triggers, saving it to disk."""
    import glob
    import shutil

    loc, desc = _resolve_locator(page, step)
    timeout = step.get("timeout", 60000)
    context = page.context

    filename = step.get("path")
    download_dir = step.get("download_dir")
    if download_dir:
        os.makedirs(download_dir, exist_ok=True)
        target_path = os.path.join(download_dir, os.path.basename(filename)) if filename else None
    else:
        target_path = filename

    if target_path:
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    download_obj = []

    def _on_dl(dl):
        download_obj.append(dl)

    context.on("download", _on_dl)
    page.on("download", _on_dl)
    def _on_new_page(p):
        p.on("download", _on_dl)
    context.on("page", _on_new_page)

    try:
        try:
            loc.click(timeout=min(timeout, 20000), force=True)
        except Exception:
            try:
                loc.click(timeout=min(timeout, 10000))
            except Exception:
                pass

        start_t = time.time()
        while not download_obj and (time.time() - start_t) < (min(timeout, 30000) / 1000.0):
            time.sleep(0.5)

        if download_obj:
            download = download_obj[0]
            final_path = target_path or (
                os.path.join(download_dir, download.suggested_filename) if download_dir else download.suggested_filename
            )
            try:
                download.save_as(final_path)
            except Exception:
                temp_path = download.path()
                if temp_path and os.path.exists(temp_path):
                    shutil.copy2(temp_path, final_path)
            return f"downloaded {download.suggested_filename!r} from {desc} -> {final_path}"

        # FALLBACK: Check download locations for recently completed download
        downloads_folders = [
            download_dir,
            os.path.expanduser("~/Downloads"),
            os.path.dirname(os.path.abspath(target_path)) if target_path else None,
        ]
        recent_cutoff = time.time() - 120
        found_file = None
        for folder in downloads_folders:
            if not folder or not os.path.isdir(folder):
                continue
            candidates = glob.glob(os.path.join(folder, "*"))
            for cand in candidates:
                if cand.endswith(".crdownload") or cand.endswith(".tmp"):
                    continue
                try:
                    if os.path.getmtime(cand) >= recent_cutoff:
                        target_ext = os.path.splitext(target_path)[1].lower() if target_path else ""
                        cand_ext = os.path.splitext(cand)[1].lower()
                        if target_ext and cand_ext == target_ext:
                            found_file = cand
                            break
                        elif not target_ext:
                            found_file = cand
                            break
                except Exception:
                    pass
            if found_file:
                break

        if found_file:
            final_path = target_path or found_file
            if os.path.abspath(found_file) != os.path.abspath(final_path):
                shutil.copy2(found_file, final_path)
            return f"download detected ({os.path.basename(found_file)}) from {desc} -> {final_path}"

        raise StepError(f"download from {desc} timed out after {timeout/1000}s")
    finally:
        try:
            context.remove_listener("download", _on_dl)
        except Exception:
            pass
        try:
            page.remove_listener("download", _on_dl)
        except Exception:
            pass
        try:
            context.remove_listener("page", _on_new_page)
        except Exception:
            pass



def _step_upload(page: Page, step: dict[str, Any]) -> str:
    """Click a trigger element and handle the native file dialog via Playwright's file_chooser event.

    Step schema:
        {"action": "upload", "selector": "...", "file_path": "/path/to/file", ...}
    """
    loc, desc = _resolve_locator(page, step)
    file_path = step.get("file_path", "")
    timeout = step.get("timeout", 30000)

    if not file_path or not os.path.isfile(file_path):
        raise StepError(f"upload file not found: {file_path!r}")

    with page.expect_file_chooser(timeout=timeout) as fc_info:
        loc.click(timeout=min(timeout, 15000))
    file_chooser = fc_info.value
    file_chooser.set_files(file_path)
    return f"uploaded {os.path.basename(file_path)!r} via {desc}"


STEP_HANDLERS = {
    "navigate": _step_navigate,
    "click": _step_click,
    "click_text": _step_click_text,
    "scroll": _step_scroll,
    "scroll_into_view": _step_scroll_into_view,
    "type": _step_type,
    "press": _step_press,
    "wait_for": _step_wait_for,
    "wait": _step_wait,
    "screenshot": _step_screenshot,
    "dump": _step_dump,
    "pause": _step_pause,
    "download": _step_download,
    "upload": _step_upload,
}


def run_step(state: dict[str, Any], context: Any, step: dict[str, Any], index: int) -> None:
    """Run one step against the currently-active page (state['page']).

    'state' holds the active Page so a step can switch it (e.g. when a click opens a new
    browser tab). Most steps operate on state['page']; 'opens_tab' steps capture the new
    tab and make it active for all subsequent steps.
    """
    page: Page = state["page"]
    action = step.get("action")
    label = step.get("label", action)
    try:
        if step.get("opens_tab"):
            # The click spawns a new tab (e.g. Workday's "Launch Object Transporter in
            # Customer Central" opens the CC tenant in a new tab). Capture it and switch the
            # active page so all following steps run against Customer Central, not the old tab.
            timeout = step.get("tab_timeout", 30000)
            with context.expect_page(timeout=timeout) as new_page_info:
                _step_click(page, step)
            new_page = new_page_info.value
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=timeout)
            except Exception:  # noqa: BLE001 - best-effort; later wait_for handles the rest
                pass
            try:
                new_page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            state["page"] = new_page
            con.step_log("agent", index, label, f"opened new tab → {new_page.url[:70]}")
            return
        handler = STEP_HANDLERS.get(action)
        if handler is None:
            raise StepError(
                f"step {index}: unknown action {action!r}. "
                f"Valid actions: {', '.join(sorted(STEP_HANDLERS))}"
            )
        result = handler(page, step)
    except KeyError as exc:
        if step.get("optional"):
            con.step_skip("agent", index, label, "optional")
            return
        raise StepError(f"step {index} ({label}): missing required field {exc}") from exc
    except PWTimeoutError as exc:
        # Optional steps (e.g. dismissing a dialog that may not appear) are allowed to fail.
        if step.get("optional"):
            con.step_skip("agent", index, label, "optional, not found")
            return
        raise StepError(f"step {index} ({label}): timed out — {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        # e.g. page navigated/closed during an optional post-redirect screenshot.
        if step.get("optional"):
            con.step_skip("agent", index, label, f"optional, error: {str(exc)[:60]}")
            return
        raise
    con.step_log("agent", index, label, result)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if "steps" not in config or not isinstance(config["steps"], list):
        raise SystemExit(f"Config {path} must contain a 'steps' array.")
    return config


def run_config(
    config: dict[str, Any],
    *,
    headless: bool = True,
    slowmo: int = 0,
    channel: str | None = None,
    browser_name: str | None = None,
) -> tuple[int, str | None]:
    """Execute a config dict against a browser.

    Returns (exit_code, error_message). exit_code 0 == success. This is importable so
    other scripts (e.g. an interactive launcher) can build a config in memory and run it.
    """
    browser_name = browser_name or config.get("browser", "chromium")
    channel = channel or config.get("channel")
    viewport = config.get("viewport", {"width": 1280, "height": 800})
    steps = config["steps"]

    where = f"{browser_name}" + (f" (channel={channel})" if channel else "")
    con.success(f"Launching {where} (headless={headless}) with {len(steps)} step(s)…")

    error_message: str | None = None
    with sync_playwright() as p:
        browser_type = getattr(p, browser_name)
        launch_kwargs = {"headless": headless, "slow_mo": slowmo}
        if channel:
            launch_kwargs["channel"] = channel
        browser = browser_type.launch(**launch_kwargs)
        context = browser.new_context(viewport=viewport, accept_downloads=True)
        page = context.new_page()
        # 'state' lets a step swap the active page (e.g. when a click opens a new tab).
        state: dict[str, Any] = {"page": page}
        try:
            for i, step in enumerate(steps, start=1):
                run_step(state, context, step, i)
            con.success("All steps completed successfully.")
            exit_code = 0
        except StepError as exc:
            error_message = str(exc)
            con.fail(f"ERROR: {exc}")
            try:
                os.makedirs("defects", exist_ok=True)
                screenshot_path = os.path.join("defects", "error.png")
                state["page"].screenshot(path=screenshot_path, full_page=True)
                con.dim(f"Saved failure screenshot to {screenshot_path}")
            except Exception:  # noqa: BLE001 - best-effort screenshot
                pass
            exit_code = 1
        finally:
            context.close()
            browser.close()

    return exit_code, error_message


def main() -> int:
    parser = argparse.ArgumentParser(description="Config-driven Playwright browser automation.")
    parser.add_argument("config", help="Path to the JSON config file.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--slowmo", type=int, default=0, help="Delay each action by N ms.")
    parser.add_argument(
        "--browser",
        choices=["chromium", "firefox", "webkit"],
        help="Override the browser from the config.",
    )
    parser.add_argument(
        "--channel",
        help="Use an installed browser channel, e.g. 'msedge' or 'chrome' (chromium only).",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    headless = (not args.headed) if args.headed else config.get("headless", True)
    exit_code, _ = run_config(
        config, headless=headless, slowmo=args.slowmo, channel=args.channel, browser_name=args.browser
    )
    return exit_code


# =============================================================================
# ASYNC API — used by the orchestrator to run agents concurrently
# =============================================================================

import asyncio

from playwright.async_api import (
    Page as AsyncPage,
    TimeoutError as AsyncPWTimeoutError,
)


def _async_resolve_locator(page: "AsyncPage", step: dict[str, Any], allow_text: bool = True):
    """Build a Playwright locator (async API — creation is synchronous)."""
    exact = step.get("exact", False)
    if "byRole" in step:
        spec = step["byRole"]
        kwargs: dict[str, Any] = {}
        if spec.get("name") is not None:
            kwargs["name"] = spec["name"]
            kwargs["exact"] = spec.get("exact", exact)
        loc = page.get_by_role(spec["role"], **kwargs)
        desc = f"role={spec['role']!r} name={spec.get('name')!r}"
    elif "byLabel" in step:
        loc = page.get_by_label(step["byLabel"], exact=exact)
        desc = f"byLabel={step['byLabel']!r}"
    elif "automation_label" in step:
        value = step["automation_label"]
        loc = page.locator(f'[data-automation-label="{value}"]')
        desc = f"automation_label={value!r}"
    elif allow_text and "text" in step:
        loc = page.get_by_text(step["text"], exact=exact)
        desc = f"text={step['text']!r}"
    elif "selector" in step:
        loc = page.locator(step["selector"])
        desc = step["selector"]
    else:
        raise StepError("step needs one of: selector, byLabel, automation_label, text, byRole")
    if "nth" in step:
        return loc.nth(step["nth"]), f"{desc}[nth={step['nth']}]"
    return loc.first, desc


# --- async step handlers ------------------------------------------------------

async def _async_step_navigate(page, step):
    url = step["url"]
    wait_until = step.get("wait_until", "load")
    await page.goto(url, wait_until=wait_until, timeout=step.get("timeout", 30000))
    return f"navigated to {url}"


async def _async_step_click(page, step):
    loc, desc = _async_resolve_locator(page, step)
    timeout = step.get("timeout", 10000)
    confirm = step.get("confirm_hidden")
    force = step.get("force", False)
    attempts = step.get("attempts", 3 if confirm else 1)
    for i in range(attempts):
        await loc.click(timeout=timeout, force=force)
        if not confirm:
            break
        try:
            await page.wait_for_selector(
                confirm, state="hidden", timeout=step.get("confirm_timeout", 10000),
            )
            break
        except AsyncPWTimeoutError:
            if i == attempts - 1:
                raise StepError(
                    f"clicked {desc} but {confirm!r} never disappeared "
                    f"after {attempts} attempt(s)"
                )
    return f"clicked {desc}" + (f" (confirmed {confirm!r} gone)" if confirm else "")


async def _async_step_click_text(page, step):
    text = step["text"]
    exact = step.get("exact", False)
    await page.get_by_text(text, exact=exact).first.click(timeout=step.get("timeout", 10000))
    return f"clicked element with text {text!r}"


async def _async_step_scroll(page, step):
    direction = step.get("direction", "down")
    if direction == "bottom":
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return "scrolled to bottom"
    if direction == "top":
        await page.evaluate("window.scrollTo(0, 0)")
        return "scrolled to top"
    pixels = step.get("pixels", 600)
    delta = pixels if direction == "down" else -pixels
    await page.mouse.wheel(0, delta)
    return f"scrolled {direction} {pixels}px"


async def _async_step_scroll_into_view(page, step):
    selector = step["selector"]
    await page.locator(selector).first.scroll_into_view_if_needed(
        timeout=step.get("timeout", 10000),
    )
    return f"scrolled {selector} into view"


async def _async_step_type(page, step):
    loc, desc = _async_resolve_locator(page, step, allow_text=False)
    text = _resolve_text(step)
    timeout = step.get("timeout", 10000)
    if step.get("sequential"):
        await loc.focus(timeout=timeout)
        if step.get("clear"):
            await loc.press("Control+A")
            await loc.press("Delete")
        await loc.press_sequentially(text, delay=step.get("delay", 70), timeout=timeout)
    else:
        await loc.fill(text, timeout=timeout)
    shown = "********" if step.get("secret_env") else None
    return f"typed {shown or repr(text)[:40]} into {desc}"


async def _async_step_press(page, step):
    key = step["key"]
    has_target = any(k in step for k in ("byRole", "byLabel", "automation_label", "selector"))
    if has_target:
        loc, desc = _async_resolve_locator(page, step, allow_text=False)
        await loc.press(key, timeout=step.get("timeout", 10000))
        return f"pressed {key} on {desc}"
    await page.keyboard.press(key)
    return f"pressed {key}"


async def _async_step_wait_for(page, step):
    selector = step.get("selector")
    if not selector:
        if "text" in step:
            selector = f"text={step['text']}"
        elif "byLabel" in step:
            selector = f"text={step['byLabel']}"
        else:
            raise StepError("wait_for needs 'selector' or 'text'")
    state = step.get("state", "visible")
    await page.wait_for_selector(selector, state=state, timeout=step.get("timeout", 10000))
    return f"waited for {selector} ({state})"


async def _async_step_wait(_page, step):
    seconds = step.get("seconds", 1)
    await asyncio.sleep(seconds)
    return f"waited {seconds}s"


async def _async_step_screenshot(page, step):
    path = step.get("path", "screenshot.png")
    full_page = step.get("full_page", True)
    await page.screenshot(path=path, full_page=full_page)
    return f"saved screenshot to {path}"


async def _async_step_dump(page, step):
    selector = step.get("selector", "button, a, input, [role='columnheader'], th")
    contains = step.get("contains")
    limit = step.get("limit", 40)
    rows = await page.eval_on_selector_all(
        selector,
        """els => els.map(e => ({
            tag: e.tagName,
            aid: e.getAttribute('data-automation-id') || '',
            al: e.getAttribute('aria-label') || '',
            ph: e.getAttribute('placeholder') || '',
            t: (e.innerText || e.value || '').trim().slice(0, 40)
        }))""",
    )
    if contains:
        needle = contains.lower()
        rows = [
            r for r in rows
            if needle in (r["t"] + r["al"] + r["aid"] + r["ph"]).lower()
        ]
    print(
        f"    --- dump {selector!r}"
        + (f" contains {contains!r}" if contains else "")
        + " ---"
    )
    for r in rows[:limit]:
        if any(r[k] for k in ("aid", "al", "ph", "t")):
            print(f"      {r}")
    if step.get("html"):
        html_rows = await page.eval_on_selector_all(
            selector,
            """(els, needle) => els
                .filter(e => !needle || (e.innerText || '').toLowerCase().includes(needle))
                .map(e => e.outerHTML.slice(0, 900))""",
            (contains or "").lower(),
        )
        for h in html_rows[: step.get("html_limit", 2)]:
            print(f"      HTML: {h}")
    return f"dumped {min(len(rows), limit)} element(s)"


async def _async_step_pause(_page, step):
    """Block until the user confirms — runs the blocking dialog in a thread.

    If an ``_on_pause`` callback is attached (via the web UI), it is used
    instead of the Windows popup.  The callback is expected to block until
    the user resolves the pause from the browser.
    """
    agent = step.get("_agent_name", "")
    title = step.get("title", "Action required")
    if agent:
        title = f"[{agent}] {title}"
    message = step.get("message", "Paused. Click OK / press Enter to continue.")

    # Web UI pause handler (takes priority)
    on_pause = step.get("_on_pause")
    if on_pause:
        await asyncio.to_thread(on_pause, title, message)
        return "user confirmed (web UI); resuming"

    if not os.environ.get("AGENT_NO_POPUP"):
        try:
            import ctypes
            MB_OK = 0x0
            MB_ICONINFO = 0x40
            MB_TOPMOST = 0x40000
            await asyncio.to_thread(
                ctypes.windll.user32.MessageBoxW,
                0, message, title, MB_OK | MB_ICONINFO | MB_TOPMOST,
            )
            return "user confirmed (dialog); resuming"
        except Exception:  # noqa: BLE001
            pass
    print(f"\n[pause] {title}\n{message}")
    await asyncio.to_thread(input, "Press Enter to continue... ")
    return "user confirmed (console); resuming"


async def _async_step_download(page, step):
    import glob
    import shutil

    loc, desc = _async_resolve_locator(page, step)
    timeout = step.get("timeout", 60000)
    context = page.context

    filename = step.get("path")
    download_dir = step.get("download_dir")
    if download_dir:
        os.makedirs(download_dir, exist_ok=True)
        target_path = os.path.join(download_dir, os.path.basename(filename)) if filename else None
    else:
        target_path = filename

    if target_path:
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    download_future = asyncio.get_running_loop().create_future()

    def _on_dl(dl):
        if not download_future.done():
            download_future.set_result(dl)

    context.on("download", _on_dl)
    page.on("download", _on_dl)
    def _on_new_page(p):
        p.on("download", _on_dl)
    context.on("page", _on_new_page)

    download = None
    try:
        try:
            await loc.click(timeout=min(timeout, 20000), force=True)
        except Exception:
            try:
                await loc.click(timeout=min(timeout, 10000))
            except Exception:
                pass

        try:
            download = await asyncio.wait_for(download_future, timeout=min(timeout, 30000) / 1000.0)
        except asyncio.TimeoutError:
            pass

        if download is not None:
            final_path = target_path or (
                os.path.join(download_dir, download.suggested_filename) if download_dir else download.suggested_filename
            )
            try:
                await download.save_as(final_path)
            except Exception:
                temp_path = await download.path()
                if temp_path and os.path.exists(temp_path):
                    shutil.copy2(temp_path, final_path)
            return f"downloaded {download.suggested_filename!r} from {desc} -> {final_path}"

        # FALLBACK: Check download locations for recently completed download
        downloads_folders = [
            download_dir,
            os.path.expanduser("~/Downloads"),
            os.path.dirname(os.path.abspath(target_path)) if target_path else None,
        ]
        recent_cutoff = time.time() - 120
        found_file = None
        for folder in downloads_folders:
            if not folder or not os.path.isdir(folder):
                continue
            candidates = glob.glob(os.path.join(folder, "*"))
            for cand in candidates:
                if cand.endswith(".crdownload") or cand.endswith(".tmp"):
                    continue
                try:
                    if os.path.getmtime(cand) >= recent_cutoff:
                        target_ext = os.path.splitext(target_path)[1].lower() if target_path else ""
                        cand_ext = os.path.splitext(cand)[1].lower()
                        if target_ext and cand_ext == target_ext:
                            found_file = cand
                            break
                        elif not target_ext:
                            found_file = cand
                            break
                except Exception:
                    pass
            if found_file:
                break

        if found_file:
            final_path = target_path or found_file
            if os.path.abspath(found_file) != os.path.abspath(final_path):
                shutil.copy2(found_file, final_path)
            return f"download detected ({os.path.basename(found_file)}) from {desc} -> {final_path}"

        raise StepError(f"download from {desc} timed out after {timeout/1000}s")
    finally:
        try:
            context.remove_listener("download", _on_dl)
        except Exception:
            pass
        try:
            page.remove_listener("download", _on_dl)
        except Exception:
            pass
        try:
            context.remove_listener("page", _on_new_page)
        except Exception:
            pass



async def _async_step_upload(page, step):
    """Async version: click a trigger element and handle the native file dialog."""
    loc, desc = _async_resolve_locator(page, step)
    file_path = step.get("file_path", "")
    timeout = step.get("timeout", 30000)

    if not file_path or not os.path.isfile(file_path):
        raise StepError(f"upload file not found: {file_path!r}")

    async with page.expect_file_chooser(timeout=timeout) as fc_info:
        await loc.click(timeout=min(timeout, 15000))
    file_chooser = await fc_info.value
    await file_chooser.set_files(file_path)
    return f"uploaded {os.path.basename(file_path)!r} via {desc}"


ASYNC_STEP_HANDLERS = {
    "navigate": _async_step_navigate,
    "click": _async_step_click,
    "click_text": _async_step_click_text,
    "scroll": _async_step_scroll,
    "scroll_into_view": _async_step_scroll_into_view,
    "type": _async_step_type,
    "press": _async_step_press,
    "wait_for": _async_step_wait_for,
    "wait": _async_step_wait,
    "screenshot": _async_step_screenshot,
    "dump": _async_step_dump,
    "pause": _async_step_pause,
    "download": _async_step_download,
    "upload": _async_step_upload,
}


async def run_step_async(
    state: dict[str, Any],
    context: Any,
    step: dict[str, Any],
    index: int,
    agent_name: str,
) -> None:
    """Run one step against the currently-active page (async version)."""
    page = state["page"]
    action = step.get("action")
    label = step.get("label", action)
    step["_agent_name"] = agent_name
    try:
        if step.get("opens_tab"):
            timeout = step.get("tab_timeout", 30000)
            async with context.expect_page(timeout=timeout) as new_page_info:
                await _async_step_click(page, step)
            new_page = await new_page_info.value
            try:
                await new_page.wait_for_load_state("domcontentloaded", timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
            try:
                await new_page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            state["page"] = new_page
            con.step_log(agent_name, index, label, f"opened new tab → {new_page.url[:70]}")
            return

        handler = ASYNC_STEP_HANDLERS.get(action)
        if handler is None:
            raise StepError(
                f"step {index}: unknown action {action!r}. "
                f"Valid actions: {', '.join(sorted(ASYNC_STEP_HANDLERS))}"
            )
        result = await handler(page, step)
    except KeyError as exc:
        if step.get("optional"):
            con.step_skip(agent_name, index, label, "optional")
            return
        raise StepError(
            f"step {index} ({label}): missing required field {exc}"
        ) from exc
    except (PWTimeoutError, AsyncPWTimeoutError) as exc:
        if step.get("optional"):
            con.step_skip(agent_name, index, label, "optional, not found")
            return
        raise StepError(
            f"step {index} ({label}): timed out — {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        if step.get("optional"):
            con.step_skip(agent_name, index, label, f"optional, error: {str(exc)[:60]}")
            return
        raise
    con.step_log(agent_name, index, label, result)


async def run_config_async(
    config: dict[str, Any],
    context: Any,
    agent_name: str = "agent",
    on_step: Any = None,
    on_pause: Any = None,
    cancel_check: Any = None,
) -> tuple[int, str | None]:
    """Execute a config dict against a pre-created async BrowserContext.

    Unlike ``run_config``, this does **not** create its own browser or
    context — the caller manages the browser lifecycle.  This makes it possible
    to run multiple agents concurrently via ``asyncio.gather`` within a single
    browser process, each with its own isolated context.

    Parameters
    ----------
    on_step : callable, optional
        ``on_step(agent_name, step_num, total, label, status)`` — called after
        each step completes.  Used by the web UI for SSE progress streaming.
    on_pause : callable, optional
        ``on_pause(title, message)`` — called for ``pause`` steps.  Should
        block until the user resolves the pause.  Used by the web UI to show
        a resume button instead of a Windows popup.

    Returns ``(exit_code, error_message)``.  ``exit_code`` 0 means success.
    """
    steps = config["steps"]
    total = len(steps)
    con.agent_start(agent_name, total)

    error_message: str | None = None
    page = await context.new_page()
    state: dict[str, Any] = {"page": page}

    completed_steps = 0
    try:
        for i, step in enumerate(steps, start=1):
            if cancel_check and cancel_check():
                raise StepError(f"Task cancelled by user. Completed {completed_steps} of {total} steps before cancellation.")
            # Inject callbacks into step dict for the pause handler
            if on_pause:
                step["_on_pause"] = on_pause
            await run_step_async(state, context, step, i, agent_name)
            completed_steps = i
            # Fire on_step callback after successful completion
            if on_step:
                label = step.get("label", step.get("action", ""))
                on_step(agent_name, i, total, label, "done")
        con.success(f"[{agent_name}] All {total} steps completed successfully.")
        exit_code = 0
    except (StepError, Exception) as exc:
        if cancel_check and cancel_check():
            error_message = f"Task cancelled by user. Completed {completed_steps} of {total} steps before cancellation."
        else:
            error_message = str(exc)
        con.fail(f"[{agent_name}] ERROR: {error_message}")
        try:
            os.makedirs("defects", exist_ok=True)
            screenshot_path = os.path.join("defects", f"error-{agent_name}.png")
            await state["page"].screenshot(
                path=screenshot_path, full_page=True,
            )
            con.dim(f"[{agent_name}] Saved failure screenshot to {screenshot_path}")
        except Exception:  # noqa: BLE001
            pass
        exit_code = 1

    return exit_code, error_message


if __name__ == "__main__":
    raise SystemExit(main())


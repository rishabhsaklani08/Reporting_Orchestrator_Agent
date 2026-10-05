import sys
import os
import json
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import ReportDiscoveryAgent
from .sync_catalog import sync_from_workday

if getattr(sys, 'frozen', False):
    _user_dir = os.path.dirname(sys.executable)
    _bundled_dir = os.path.join(sys._MEIPASS, "Workday_Report_Discovery_Agent")
else:
    _user_dir = os.path.dirname(os.path.abspath(__file__))
    _bundled_dir = os.path.dirname(os.path.abspath(__file__))

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App lifecycle (replaces deprecated @app.on_event)
# ---------------------------------------------------------------------------

agent = None

# ── Background sync state ────────────────────────────────────────────────────
# "idle"    → no sync needed or credentials not configured
# "syncing" → background sync in progress
# "done"    → sync finished successfully
# "failed"  → sync failed (agent still works with bundled/existing catalog)
_sync_status = "idle"


def _background_sync() -> None:
    """Run the Workday RaaS sync in a background thread, then reload the agent."""
    global agent, _sync_status
    try:
        success = sync_from_workday()
        if success:
            logger.info("Auto-sync: Catalog refreshed — reloading agent.")
            agent = ReportDiscoveryAgent()
            _sync_status = "done"
            logger.info("Auto-sync: Agent reloaded with %d reports.", len(agent.catalog))
        else:
            logger.warning("Auto-sync: Sync returned failure — keeping existing catalog.")
            _sync_status = "failed"
    except Exception as exc:
        logger.warning("Auto-sync: Failed (%s) — keeping existing catalog.", exc)
        _sync_status = "failed"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the agent immediately, then auto-sync in the background."""
    global agent, _sync_status

    # ── Initialise agent right away with bundled/existing catalog ──────────
    logger.info("Initializing ReportDiscoveryAgent with existing catalog...")
    agent = ReportDiscoveryAgent()
    logger.info("Agent initialized with %d reports.", len(agent.catalog))

    # ── Kick off background sync if credentials are configured ────────────
    from . import config as _cfg
    raas_url = getattr(_cfg, "WORKDAY_RAAS_URL", "") or ""
    raas_user = getattr(_cfg, "WORKDAY_ISU_USERNAME", "") or ""
    raas_pass = getattr(_cfg, "WORKDAY_ISU_PASSWORD", "") or ""

    if raas_url.strip() and raas_user.strip() and raas_pass.strip():
        _sync_status = "syncing"
        logger.info("Auto-sync: Starting background sync from Workday RaaS...")
        sync_thread = threading.Thread(
            target=_background_sync, daemon=True, name="auto-sync"
        )
        sync_thread.start()
    else:
        _sync_status = "idle"
        logger.info("Auto-sync: Skipped (WORKDAY_RAAS_URL / credentials not configured).")

    yield


# Initialize FastAPI app
app = FastAPI(title="Report Discovery Agent API", lifespan=lifespan)

# No CORS middleware needed — Discovery UI is served at the same origin
# when embedded in the orchestrator (/discovery/).


# ---------------------------------------------------------------------------
# API Models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    bm25_top_n: int = 15
    llm_top_k: int = 5
    use_llm: bool = True


class ConfirmRequest(BaseModel):
    reports: list[str]


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/search")
def search_reports(req: SearchRequest):
    global agent
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    try:
        if req.use_llm:
            results = agent.search(
                query=req.query,
                bm25_top_n=req.bm25_top_n,
                llm_top_k=req.llm_top_k
            )
        else:
            results = agent.search_bm25_only(
                query=req.query,
                top_n=req.llm_top_k
            )

        # Detect if LLM fell back to BM25 (all bands will be "N/A")
        llm_fallback = req.use_llm and all(r.get("band") == "N/A" for r in results) and len(results) > 0
        fallback_reason = results[0].get("explanation", "") if llm_fallback else ""

        return {
            "results": results,
            "llm_fallback": llm_fallback,
            "fallback_reason": fallback_reason,
        }
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync")
def sync_reports():
    global agent
    try:
        success = sync_from_workday()
        if not success:
            raise HTTPException(status_code=500, detail="Sync failed. Check credentials or Workday RaaS URL.")

        # Reload agent to pick up new catalog
        logger.info("Reloading agent with new catalog...")
        agent = ReportDiscoveryAgent()

        return {"success": True, "message": f"Successfully synced and loaded {len(agent.catalog)} reports."}
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats")
def get_stats():
    global agent
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    num_reports = len(agent.catalog)
    return {
        "num_reports": num_reports,
        "llm_enabled": bool(config.OPENAI_API_KEY),
        "llm_model": config.MODEL_NAME
    }


@app.get("/api/sync-status")
def get_sync_status():
    """Return the current background auto-sync status.

    Frontend polls this to show/hide the loading overlay.
    """
    num_reports = len(agent.catalog) if agent else 0
    return {
        "status": _sync_status,
        "num_reports": num_reports,
    }


# ---------------------------------------------------------------------------
# Orchestrator Integration (Selection & Confirmation)
# ---------------------------------------------------------------------------

SELECTION_FILE = os.path.join(_user_dir, ".selected_reports.json")
_confirmation_event = threading.Event()


@app.post("/api/confirm")
def confirm_selection(req: ConfirmRequest):
    """Called by the frontend when the user clicks 'Proceed with Selected Reports'."""
    if not req.reports:
        raise HTTPException(status_code=400, detail="No reports selected")

    # Write the selection to disk for the orchestrator to read.
    with open(SELECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(req.reports, f)

    logger.info("User confirmed %d reports: %s", len(req.reports), req.reports)

    # Signal the orchestrator that the user has confirmed.
    _confirmation_event.set()

    return {"success": True, "message": f"{len(req.reports)} reports selected. You can close this tab."}


# ---------------------------------------------------------------------------
# Static files (must be mounted AFTER all API routes)
# ---------------------------------------------------------------------------

static_dir = os.path.join(_bundled_dir, "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


# ---------------------------------------------------------------------------
# Helpers for orchestrator integration
# ---------------------------------------------------------------------------

def wait_for_confirmation(timeout: float = 600) -> list[str]:
    """Block until the user confirms their selection in the web UI.

    Returns the list of selected report names, or an empty list on timeout.
    """
    logger.info("Waiting for user to select reports in the web UI (timeout: %.0fs)…", timeout)
    confirmed = _confirmation_event.wait(timeout=timeout)
    _confirmation_event.clear()

    if not confirmed:
        logger.warning("Timed out waiting for report selection.")
        return []

    try:
        with open(SELECTION_FILE, "r", encoding="utf-8") as f:
            reports = json.load(f)
        os.remove(SELECTION_FILE)  # Clean up
        return reports
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def start_server(port: int = 8100) -> threading.Thread:
    """Start the FastAPI server in a background thread. Returns the thread."""
    import uvicorn
    import time

    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(target=server.run, daemon=True, name="discovery-server")
    thread.start()

    # Give the server a moment to start
    time.sleep(1)

    return thread


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8100, reload=True)

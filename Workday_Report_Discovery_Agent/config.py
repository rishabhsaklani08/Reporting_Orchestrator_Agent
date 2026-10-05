"""
config.py — Central configuration for the Report Ranking Agent.

Loads settings from environment variables (.env file) and provides
sensible defaults for all tunable parameters.
"""

import os
import sys
from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    _user_dir = os.path.dirname(sys.executable)
    _bundled_dir = os.path.join(sys._MEIPASS, "Workday_Report_Discovery_Agent")
else:
    _user_dir = os.path.dirname(os.path.dirname(__file__))
    _bundled_dir = os.path.dirname(__file__)

_env_path = os.path.join(_user_dir, ".env")
load_dotenv(dotenv_path=_env_path)

# ── LLM Configuration ──
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "")
MODEL_NAME: str = os.getenv("MODEL_NAME", "qwen/qwen3.8-27b")

# ── Workday Configuration ──
WORKDAY_RAAS_URL: str = os.getenv("WORKDAY_RAAS_URL", "")
WORKDAY_ISU_USERNAME: str = os.getenv("WORKDAY_ISU_USERNAME", "")
WORKDAY_ISU_PASSWORD: str = os.getenv("WORKDAY_ISU_PASSWORD", "")

# ── BM25 Configuration ──
BM25_TOP_N: int = int(os.getenv("BM25_TOP_N", "15"))
BM25_K1: float = 1.5
BM25_B: float = 0.75

# ── LLM Scorer Configuration ──
LLM_TOP_K: int = int(os.getenv("LLM_TOP_K", "5"))

# ── Field Boost Weights (how many times to repeat each field in composite text) ──
NAME_BOOST: int = 3
DESC_BOOST: int = 2
DS_DESC_BOOST: int = 1
FIELD_BOOST: int = 1

# ── Relevance Band Thresholds ──
HIGH_THRESHOLD: int = 75
MEDIUM_THRESHOLD: int = 40

# ── Prompt Template Path ──
PROMPT_TEMPLATE_PATH: str = os.path.join(
    _bundled_dir, "prompts", "scoring_prompt.txt"
)

# ── Data Path ──
DEFAULT_CATALOG_PATH: str = os.path.join(
    _bundled_dir, "data", "All_Custom_Reports_Enabled_as_RAAS.json"
)

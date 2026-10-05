"""
llm_scorer.py — LLM-based candidate re-ranker.

Sends the BM25 shortlist to an LLM (via OpenAI-compatible API) and
receives structured scores + explanations.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from . import config

logger = logging.getLogger(__name__)


class LLMScorer:
    """
    Score and re-rank BM25 candidates using a large language model.

    Parameters
    ----------
    client : openai.OpenAI | None
        An initialised OpenAI client.  If *None*, the scorer falls back
        to returning BM25 order with a warning.
    model : str
        Model name, e.g. ``"gpt-4o"``.
    """

    def __init__(self, client: Any = None, model: str | None = None):
        self.client = client
        self.model = model or config.MODEL_NAME
        self._prompt_template = self._load_prompt_template()

    # ── load prompt ──
    @staticmethod
    def _load_prompt_template() -> str:
        try:
            with open(config.PROMPT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            logger.warning("Prompt template not found; using built-in default.")
            return (
                "You are a Workday Report Discovery Agent.\n"
                "Score each candidate report 0-100 against the user query.\n"
                "Return a JSON array sorted by score descending.\n"
                "Each element: {rank, report_name, score, band, explanation}.\n"
                "Bands: High (75-100), Medium (40-74), Low (0-39).\n"
                "Ground every explanation in the provided metadata only."
            )

    # ── build prompt ──
    def _build_prompt(
        self, query: str, candidates: List[Dict[str, Any]],
        top_k: int | None = None,
    ) -> List[Dict[str, str]]:
        candidate_text = ""
        for i, c in enumerate(candidates, 1):
            rpt = c["report"]
            candidate_text += f"\n--- Candidate {i} ---\n"
            candidate_text += f"Report Name: {rpt.get('Report_Name', 'N/A')}\n"
            candidate_text += f"Report Type: {rpt.get('Report_Type', 'N/A')}\n"
            candidate_text += f"Description: {rpt.get('Brief_Description') or 'NOT AVAILABLE'}\n"
            candidate_text += f"Data Source: {(rpt.get('DS_Description') or 'N/A')[:200]}\n"
            candidate_text += f"Fields Displayed: {(rpt.get('Fields_Displayed_on_Report') or 'N/A')[:200]}\n"
            candidate_text += f"Fields Referenced: {(rpt.get('Fields_Referenced_in_Report') or 'N/A')[:200]}\n"

        user_msg = (
            f'User Query: "{query}"\n\n'
            f"Candidate Reports:\n{candidate_text}"
        )
        if top_k and top_k < len(candidates):
            user_msg += f"\n\nIMPORTANT: Return only the top {top_k} most relevant results, not all candidates."

        return [
            {"role": "system", "content": self._prompt_template},
            {"role": "user", "content": user_msg},
        ]

    # ── call LLM ──
    def score_candidates(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int | None = None,
    ) -> List[Dict[str, Any]]:
        """
        Score and re-rank candidates using the LLM.

        Automatically handles Groq's TPM (Tokens Per Minute) limits by
        reducing the number of candidates sent to the LLM when a 413
        "Payload Too Large" error is received.

        Parameters
        ----------
        query : str
            The original user query.
        candidates : list of dict
            Output from ``ReportIndex.search()``.
        top_k : int
            Number of final results to return.

        Returns
        -------
        list of dict
            Sorted by LLM relevance score, each containing
            ``report_name``, ``score``, ``band``, ``explanation``,
            and the original ``report`` metadata.
        """
        top_k = top_k or config.LLM_TOP_K

        # ── Fallback if no client ──
        if self.client is None:
            logger.warning("No LLM client configured; returning BM25 order.")
            return self._fallback(candidates, top_k, reason="LLM not configured")

        # Start with ideal candidate count; auto-reduce on 413 errors.
        max_llm_candidates = min(len(candidates), max(top_k * 2, 10), 15)
        llm_candidates = candidates[:max_llm_candidates]

        max_attempts = 6          # total attempts (covers size reductions + rate-limit retries)
        rate_limit_retries = 0    # track rate-limit retries separately

        for attempt in range(max_attempts):
            messages = self._build_prompt(query, llm_candidates, top_k=top_k)

            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=2048,
                )
                content = response.choices[0].message.content
                parsed = self._extract_json(content)

                # Accept both {"results": [...]} and bare [...]
                if isinstance(parsed, dict):
                    scored = parsed.get("results", parsed.get("candidates", []))
                elif isinstance(parsed, list):
                    scored = parsed
                else:
                    raise ValueError("Unexpected LLM response format")

                # Merge back original report metadata
                name_to_cand = {
                    c["report"].get("Report_Name", ""): c for c in llm_candidates
                }

                enriched = []
                for item in scored[:top_k]:
                    rname = item.get("report_name", "")
                    orig = name_to_cand.get(rname, {})
                    enriched.append({
                        "report_name": rname,
                        "score": item.get("score", 0),
                        "band": item.get("band", "Low"),
                        "explanation": item.get("explanation", item.get("why", "")),
                        "report": orig.get("report", {}),
                        "bm25_score": orig.get("bm25_score", 0),
                    })
                return enriched

            except Exception as exc:
                exc_str = str(exc)

                # ── 413 Payload Too Large — shrink candidate list and retry ──
                is_too_large = "413" in exc_str or "too large" in exc_str.lower()
                if is_too_large and len(llm_candidates) > 5:
                    new_count = max(len(llm_candidates) // 2, 5)
                    logger.warning(
                        "Prompt too large for %s (%d candidates). "
                        "Auto-reducing to %d candidates and retrying…",
                        self.model, len(llm_candidates), new_count,
                    )
                    llm_candidates = candidates[:new_count]
                    continue  # retry immediately without delay

                # ── 429 Rate Limit — wait and retry (only if NOT a payload issue) ──
                is_rate_limit = (
                    "429" in exc_str or "rate_limit" in exc_str.lower()
                ) and not is_too_large
                if is_rate_limit and rate_limit_retries < 3:
                    rate_limit_retries += 1
                    delay = [5, 15, 30][rate_limit_retries - 1]
                    logger.warning(
                        "LLM rate limited (retry %d/3). Retrying in %ds…",
                        rate_limit_retries, delay,
                    )
                    import time
                    time.sleep(delay)
                    continue

                # ── All other errors or exhausted retries — fallback ──
                if is_too_large:
                    reason = (
                        f"Prompt too large for {self.model} even at minimum "
                        f"candidates — try a model with a higher token limit"
                    )
                elif is_rate_limit:
                    reason = "Rate limit exceeded — retries exhausted"
                else:
                    reason = str(exc)
                logger.error("LLM scoring failed: %s — falling back to BM25 order.", exc)
                return self._fallback(candidates, top_k, reason=reason)

        # Safety fallback
        return self._fallback(candidates, top_k, reason="Max retry attempts exceeded")

    # ── extract JSON from LLM response (handles markdown-wrapped JSON) ──
    @staticmethod
    def _extract_json(content: str) -> Any:
        """Try direct JSON parse, extract from markdown code fences, or slice bracket bounds."""
        import re
        content = content.strip()

        # 1. Strip <think>...</think> reasoning blocks
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()

        # 2. Extract content inside ```json ... ``` or ``` ... ``` code blocks
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)(?:```|$)", content)
        if fence_match and fence_match.group(1).strip():
            candidate_str = fence_match.group(1).strip()
            try:
                return json.loads(candidate_str)
            except json.JSONDecodeError:
                pass

        # 3. Direct JSON parse
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 4. Bracket slicing fallback (first { or [ to last } or ])
        first_bracket = min([i for i in [content.find('{'), content.find('[')] if i != -1], default=-1)
        last_bracket = max(content.rfind('}'), content.rfind(']'))
        if first_bracket != -1 and last_bracket > first_bracket:
            try:
                return json.loads(content[first_bracket:last_bracket + 1])
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract JSON from LLM response: {content[:200]}")

    # ── fallback ──
    @staticmethod
    def _fallback(
        candidates: List[Dict[str, Any]], top_k: int, reason: str = ""
    ) -> List[Dict[str, Any]]:
        explanation = f"LLM unavailable ({reason}) — ranked by BM25 keyword relevance only." if reason else "LLM unavailable — ranked by BM25 keyword relevance only."
        results = []
        for c in candidates[:top_k]:
            rpt = c["report"]
            results.append({
                "report_name": rpt.get("Report_Name", "Unknown"),
                "score": round(c["bm25_score"], 2),
                "band": "N/A",
                "explanation": explanation,
                "report": rpt,
                "bm25_score": c["bm25_score"],
            })
        return results

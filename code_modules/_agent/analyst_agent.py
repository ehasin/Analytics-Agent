"""
analyst_agent.py — Analytics agent: classify+plan → execute → narrate → guardrails.

All LLM calls go through a callable `llm_fn(prompt, tier=1)` passed at init,
so this module has zero dependency on any specific backend. The agent requests
tier=2 (strong model) only for narration in Reason mode; all other calls use
the default tier=1. Guardrail checks (groundedness + compliance) use tier=0.

Pipeline stages per turn:
  1. classify_and_plan   Single LLM call: classify question + generate SQL
  2. execute             DuckDB execution via execute_fn; one error-driven retry
  3. narrate             LLM narration with anti-fabrication guardrails
  4. guardrails          Deterministic groundedness check + tier-0 compliance scan

Mode selection and follow-up resolution live in chat.py. This module receives
the mode as a parameter and uses it to: (a) scope the plan (retrieve vs explore),
(b) choose the narrative format, (c) route narration to tier 2 when mode==2.

Provides:
  - MODE_NAMES
  - extract_user_context
  - classify_and_plan, format_narrative
  - analyst_agent          Main entry point (single question → result dict)
"""

import re, time

from .prompts import (
    CLASSIFY_AND_PLAN_PROMPT, PLAN_SCOPE_RETRIEVE, PLAN_SCOPE_EXPLORE,
    NARRATIVE_PROMPT, NARRATIVE_FMT_RETRIEVE, NARRATIVE_FMT_EXPLORE, NARRATIVE_FMT_REASON,
    CONTEXT_NOTE_PLAN, CONTEXT_NOTE_NARRATE,
)
from .guardrails import verify_groundedness, check_compliance


# ── Mode names ───────────────────────────────────────────────

MODE_NAMES = {0: "Retrieve", 1: "Explore", 2: "Reason"}


# ── Helpers used by chat.py ──────────────────────────────────

def extract_user_context(question: str) -> str | None:
    """Detect an explicit role statement like 'as a VP of Marketing'. Returns
    the role string or None. Used only if the caller chooses to surface user
    context into the planning / narration prompts."""
    patterns = [
        r"(?:as (?:a|an|the) )(.+?)(?:,|\.|\bi (?:am|want|need|would))",
        r"(?:i(?:'m| am) (?:a|an|the) )(.+?)(?:,|\.| and | who )",
        r"(?:from (?:a|an|the) )(.+?)(?:perspective|point of view|standpoint)",
        r"(?:speaking as (?:a|an|the) )(.+?)(?:,|\.)",
    ]
    for pattern in patterns:
        match = re.search(pattern, question.lower())
        if match:
            result = match.group(1).strip()
            if len(result) >= 5:
                return result
    return None


# ── Helpers ──────────────────────────────────────────────────

# Safety cap on per-query result text passed into the narrate prompt.
# Raised to 50K chars — large enough to accommodate legitimate cross-tab
# results (e.g. top-N-per-month breakdowns, state-level tables) without
# mutilation. The narrative prompt enforces strict anti-fabrication rules
# on the rare occasion this still fires, so hallucination from partial
# data is prevented at the prompt layer, not by smaller caps here.
_NARRATE_RESULT_CHAR_CAP = 50_000

def _truncate_for_narrate(result_text: str) -> tuple[str, bool]:
    """Cap oversized result strings. Returns (text, was_truncated).
    Truncation is rare at the 50K cap; when it does fire, the narrate prompt
    instructs the model to answer only from visible data and flag the gap."""
    if not result_text or len(result_text) <= _NARRATE_RESULT_CHAR_CAP:
        return result_text, False
    head = result_text[: _NARRATE_RESULT_CHAR_CAP - 300]
    tail_note = (
        f"\n\n... [RESULT TRUNCATED: original was {len(result_text)} chars, "
        f"showing first {_NARRATE_RESULT_CHAR_CAP - 300}. "
        f"Only use the visible rows above. Do not invent or extrapolate rows beyond this point.]"
    )
    return head + tail_note, True


# ── Classify + Plan (merged single call) ─────────────────────

def _parse_classify_and_plan(text: str) -> dict:
    """Parse the structured output of CLASSIFY_AND_PLAN_PROMPT."""
    classification = "can_answer"
    reason = ""
    analysis_assumptions = ""

    # CLASSIFICATION is single-line; parse from line-scan.
    for line in text.split("\n"):
        if line.startswith("CLASSIFICATION:"):
            classification = line.split(":", 1)[1].strip().lower()
            break

    # REASON can span multiple lines. Capture everything after "REASON:" up to
    # the next keyword marker.
    reason_match = re.search(
        r"REASON:\s*(.*?)(?=\n\s*(?:ASSUMPTIONS_FOR_NARRATIVE:|QUERIES:|<query>)|\Z)",
        text,
        re.DOTALL,
    )
    if reason_match:
        reason = reason_match.group(1).strip()

    # Optional assumptions for the narrative stage.
    assumptions_match = re.search(
        r"ASSUMPTIONS_FOR_NARRATIVE:\s*(.*?)(?=\n\s*(?:QUERIES:|<query>)|\Z)",
        text,
        re.DOTALL,
    )
    if assumptions_match:
        analysis_assumptions = assumptions_match.group(1).strip()
        if analysis_assumptions.lower() in ("none", "n/a", "not applicable", "-"):
            analysis_assumptions = ""

    queries = []
    for block in re.findall(r"<query>(.*?)</query>", text, re.DOTALL):
        l = re.search(r"<label>(.*?)</label>", block, re.DOTALL)
        t = re.search(r"<type>(.*?)</type>", block, re.DOTALL)
        c = re.search(r"<code>(.*?)</code>", block, re.DOTALL)
        if l and c:
            queries.append({
                "label": l.group(1).strip(),
                "type": t.group(1).strip() if t else "primary",
                "code": c.group(1).strip().rstrip(";"),
                "result": None,
                "error": None,
            })

    return {
        "classification": classification,
        "reason": reason,
        "analysis_assumptions": analysis_assumptions,
        "queries": queries,
    }


def classify_and_plan(question, schema, tables, mode, llm_fn, user_context=None):
    """Single LLM call: classify the question and (if can_answer) plan queries.
    Returns (parsed_dict, stage_record) where stage_record logs tier + seconds."""
    context_note = CONTEXT_NOTE_PLAN.format(user_context=user_context) if user_context else ""
    scope = PLAN_SCOPE_RETRIEVE if mode == 0 else PLAN_SCOPE_EXPLORE

    prompt = CLASSIFY_AND_PLAN_PROMPT.format(
        schema=schema, tables=list(tables.keys()),
        context_note=context_note, question=question, scope=scope,
    )
    t0 = time.time()
    text = llm_fn(prompt)  # default tier=1
    elapsed = round(time.time() - t0, 2)
    stage_record = {"stage": "classify_and_plan", "tier": 1, "seconds": elapsed}
    return _parse_classify_and_plan(text), stage_record


# ── Narrate ──────────────────────────────────────────────────

def format_narrative(question, queries, mode, schema, llm_fn, user_context=None, analysis_assumptions=""):
    """Synthesize query results into a user-facing narrative. Uses tier 2
    (strong reasoning model) when mode==2, tier 1 otherwise.
    Returns (narrative, stage_record)."""
    results_text = ""
    truncated_count = 0
    for q in queries:
        results_text += f"\n[{q['type'].upper()}] {q['label']}\n"
        if q.get("result"):
            # DOC_LOOKUP duplicates the schema (already in the narrate prompt
            # via {schema}); truncating it is pointless and noisy — skip the check.
            if q.get("code", "").strip() == "DOC_LOOKUP":
                safe_result, was_truncated = q["result"], False
            else:
                safe_result, was_truncated = _truncate_for_narrate(q["result"])
            if was_truncated:
                truncated_count += 1
            results_text += f"Result:\n{safe_result}\n"
        else:
            results_text += f"Error: {q.get('error')}\n"

    context_note = CONTEXT_NOTE_NARRATE.format(user_context=user_context) if user_context else ""
    fmt = {
        0: NARRATIVE_FMT_RETRIEVE,
        1: NARRATIVE_FMT_EXPLORE,
        2: NARRATIVE_FMT_REASON,
    }[mode]

    prompt = NARRATIVE_PROMPT.format(
        question=question,
        schema=schema,
        context_note=context_note,
        results_text=results_text,
        fmt=fmt,
        analysis_assumptions=analysis_assumptions or "None",
    )

    tier = 2 if mode == 2 else 1
    t0 = time.time()
    narrative = llm_fn(prompt, tier=tier)
    elapsed = round(time.time() - t0, 2)
    stage_record = {"stage": "narrate", "tier": tier, "seconds": elapsed}
    if truncated_count:
        stage_record["truncated_results"] = truncated_count
    return narrative, stage_record


# ── Main agent entry point ───────────────────────────────────

def analyst_agent(
    question: str,
    schema: str,
    tables: dict,
    llm_fn,
    execute_fn,
    mode: int = 0,
    user_context: str | None = None,
) -> dict:
    """Analytics agent: classify+plan → execute → (retry) → narrate → guardrails.

    Args:
        question:     user's question (already resolved if a follow-up)
        schema:       data model text
        tables:       {name: DataFrame} — passed to execute_fn and prompts
        llm_fn:       callable(prompt, tier=1) → str. Default tier=1 (fast model).
                      The agent switches to tier=2 (strong model) only for the
                      narration phase when mode == 2 (Reason). Guardrail compliance
                      check uses tier=0 (cheapest model).
        execute_fn:   callable(queries, tables) → queries (with results filled)
        mode:         0=Retrieve, 1=Explore, 2=Reason
        user_context: optional role string (from extract_user_context)

    Returns dict with keys: classification, answer, code, queries, narrative,
    mode, error, stage_trace. stage_trace is a list of per-stage timing/result
    records; the caller can extend it with non-agent stages (interpret, summary).
    The final record is always the guardrails stage with grounding and compliance
    results — observability only, never blocks the response.
    """
    stage_trace: list[dict] = []

    # Stage 2: Classify + Plan (merged, tier 1)
    try:
        cp, cp_record = classify_and_plan(
            question, schema, tables, mode, llm_fn, user_context
        )
        stage_trace.append(cp_record)
    except Exception as e:
        return {
            "classification": "error", "answer": None, "code": "",
            "queries": [], "narrative": f"Classify/plan failed: {e}",
            "mode": mode, "error": str(e), "stage_trace": stage_trace,
        }

    classification = cp["classification"]
    reason = cp["reason"]
    analysis_assumptions = cp.get("analysis_assumptions", "")
    queries = cp["queries"]

    if classification == "cant_answer":
        answer = f"Can't answer based on the available data. ({reason})"
        return {
            "classification": classification, "answer": answer, "code": "",
            "queries": [], "narrative": answer,
            "mode": mode, "error": None, "stage_trace": stage_trace,
        }

    if classification == "clarifications_needed":
        answer = f"The question is unclear. {reason}"
        return {
            "classification": classification, "answer": answer, "code": "",
            "queries": [], "narrative": answer,
            "mode": mode, "error": None, "stage_trace": stage_trace,
        }

    # Fill DOC_LOOKUP queries with schema text (no SQL)
    for q in queries:
        if q["code"].strip() == "DOC_LOOKUP":
            q["result"] = schema

    # Stage 3b: Execute
    queries = execute_fn(queries, tables)

    # Retry failed queries once with error context
    failed = [q for q in queries if q.get("error") and q["code"].strip() != "DOC_LOOKUP"]
    if failed:
        retry_context = CONTEXT_NOTE_PLAN.format(user_context=user_context) if user_context else ""
        retry_scope = PLAN_SCOPE_RETRIEVE if mode == 0 else PLAN_SCOPE_EXPLORE
        retry_prompt = CLASSIFY_AND_PLAN_PROMPT.format(
            schema=schema, tables=list(tables.keys()),
            context_note=retry_context, question=question, scope=retry_scope,
        ) + "\n\nThe following queries failed. Rewrite ONLY the failed queries to fix the errors:\n"
        for q in failed:
            retry_prompt += f"\nQuery: {q['code']}\nError: {q['error']}\n"

        t0 = time.time()
        retry_text = llm_fn(retry_prompt)
        stage_trace.append({
            "stage": "retry", "tier": 1, "seconds": round(time.time() - t0, 2)
        })
        retry_queries = []
        for block in re.findall(r"<query>(.*?)</query>", retry_text, re.DOTALL):
            c = re.search(r"<code>(.*?)</code>", block, re.DOTALL)
            l = re.search(r"<label>(.*?)</label>", block, re.DOTALL)
            if c:
                retry_queries.append({
                    "label": (l.group(1).strip() if l else "retry") + " (retry)",
                    "type": "primary",
                    "code": c.group(1).strip().rstrip(";"),
                    "result": None, "error": None,
                })
        if retry_queries:
            retry_queries = execute_fn(retry_queries, tables)
            for rq in retry_queries:
                if rq.get("result"):
                    for i, q in enumerate(queries):
                        if q.get("error"):
                            queries[i] = rq
                            break

    primary = [q["result"] for q in queries if q.get("type") == "primary" and q.get("result")]
    raw_answer = "\n".join(primary) if primary else "No results"
    all_code = "\n\n".join([f"-- {q['label']}\n{q['code']}" for q in queries])

    # Stage 3d: Narrate (tier 2 if Reason, else tier 1)
    try:
        narrative, narr_record = format_narrative(
            question,
            queries,
            mode,
            schema,
            llm_fn,
            user_context,
            analysis_assumptions=analysis_assumptions,
        )
        stage_trace.append(narr_record)
    except Exception as e:
        narrative = f"(Narrative failed: {e})"

    # Stage 4: Guardrails — observability only, never blocks response
    grounding = verify_groundedness(narrative, queries)
    compliance = check_compliance(narrative, llm_fn)
    stage_trace.append({
        "stage":      "guardrails",
        "grounding":  grounding,
        "compliance": compliance,
    })

    return {
        "classification": classification, "answer": raw_answer, "code": all_code,
        "queries": queries, "narrative": narrative,
        "mode": mode, "error": None, "stage_trace": stage_trace,
    }

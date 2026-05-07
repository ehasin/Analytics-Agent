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
    NARRATIVE_PROMPT, NARRATIVE_RETRY_PROMPT,
    NARRATIVE_FMT_RETRIEVE, NARRATIVE_FMT_EXPLORE, NARRATIVE_FMT_REASON,
    CONTEXT_NOTE_PLAN, CONTEXT_NOTE_NARRATE, SQL_RETRY_PROMPT,
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


# Maximum total narration attempts (1 original + up to 2 retries).
# Retries only fire on deterministic groundedness failure; compliance is log-only.
_MAX_NARRATE_ATTEMPTS = 3

# ── Groundedness thresholds ──────────────────────────────────────────────────
#
# Two independent values govern the retry-and-block loop:
#
#   _RETRY_THRESHOLD (absolute count)
#     Retry fires when numbers_unmatched exceeds this value.
#     0 means any single unmatched number triggers a retry — the model receives
#     the exact token list via NARRATIVE_RETRY_PROMPT and is instructed to omit
#     those values. Retry is cheap (~1 tier-1 call) and productive; setting this
#     to 0 catches every derived subtraction or computed sum before it reaches the
#     user, rather than letting low-ratio cases slip through unchallenged.
#
#   _BLOCK_THRESHOLD (ratio, range 0–1)
#     After all retry attempts are exhausted, hard-block only when the final
#     unmatched ratio still exceeds this value. Persistent low-ratio unmatched
#     tokens (e.g. 1 out of 100 = 1%) are likely rounding artefacts the model
#     couldn't fully eliminate; those pass rather than producing a user-facing
#     error. Setting this to 0.20 (tighter than the legacy 0.30) means moderately
#     bad narratives are blocked rather than silently shown to the user.
#
#   Design intent: retry early and cheaply on any signal; block only when clearly
#   unreliable. Cloners targeting production should validate both thresholds
#   against their own dataset — higher-cardinality schemas or longer narratives
#   may warrant a slightly higher block threshold if retry storms occur.
#
_RETRY_THRESHOLD: int   = 0     # retry when numbers_unmatched > this
_BLOCK_THRESHOLD: float = 0.20  # hard-block when ratio > this after all retries


def format_narrative_retry(
    question: str,
    queries: list[dict],
    mode: int,
    schema: str,
    llm_fn,
    unmatched_samples: list[str],
    attempt: int,
    user_context: str | None = None,
) -> tuple[str, dict]:
    """Re-generate a narrative after a groundedness failure.

    Feeds back the exact unmatched tokens so the model knows which derived
    values (percentages, grand totals, computed differences) to omit.
    Uses the same tier routing as format_narrative (tier 2 for Reason, else 1).

    Args:
        unmatched_samples:  list of raw token strings from verify_groundedness
        attempt:            current attempt number (2 or 3)

    Returns (narrative, stage_record).
    """
    results_text = ""
    for q in queries:
        results_text += f"\n[{q['type'].upper()}] {q['label']}\n"
        if q.get("result"):
            if q.get("code", "").strip() == "DOC_LOOKUP":
                safe_result = q["result"]
            else:
                safe_result, _ = _truncate_for_narrate(q["result"])
            results_text += f"Result:\n{safe_result}\n"
        else:
            results_text += f"Error: {q.get('error')}\n"

    context_note = CONTEXT_NOTE_NARRATE.format(user_context=user_context) if user_context else ""
    fmt = {0: NARRATIVE_FMT_RETRIEVE, 1: NARRATIVE_FMT_EXPLORE, 2: NARRATIVE_FMT_REASON}[mode]

    unmatched_list = (
        ", ".join(repr(s) for s in unmatched_samples)
        if unmatched_samples
        else "unknown — rewrite using only values present in result rows"
    )

    prompt = NARRATIVE_RETRY_PROMPT.format(
        question=question,
        schema=schema,
        context_note=context_note,
        results_text=results_text,
        attempt=attempt,
        unmatched_list=unmatched_list,
        fmt=fmt,
    )

    tier = 2 if mode == 2 else 1
    t0 = time.time()
    narrative = llm_fn(prompt, tier=tier)
    elapsed = round(time.time() - t0, 2)
    stage_record = {"stage": "narrate", "tier": tier, "seconds": elapsed, "narrate_attempt": attempt}
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
    """Analytics agent: classify+plan → execute → (SQL retry) → narrate → groundedness → (narrative retry) → compliance.

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

    # Surface any sqlglot parse warnings into stage_trace so dashboards can
    # detect queries that bypassed guard_sql due to unparseable SQL.
    guard_warnings = [
        {"label": q.get("label", ""), "warning": q["guard_warning"]}
        for q in queries if q.get("guard_warning")
    ]
    if guard_warnings:
        stage_trace.append({"stage": "guard_warnings", "items": guard_warnings})

    # Retry each failed query individually with a targeted fix prompt.
    # Using SQL_RETRY_PROMPT (not the full CLASSIFY_AND_PLAN_PROMPT) ensures the
    # LLM fixes only the specific SQL error without re-planning the whole question,
    # preventing label mismatches and token waste on multi-query plans.
    failed = [q for q in queries if q.get("error") and q["code"].strip() != "DOC_LOOKUP"]

    # Record guard-blocked queries in stage_trace BEFORE the retry loop replaces
    # them. This lets eval validators (and dashboards) confirm that guard_sql fired,
    # even though the successful retry query overwrites the original in result["queries"].
    guard_blocks = [
        {"label": q.get("label", ""), "blocked_code": q["code"], "error": q["error"]}
        for q in failed
    ]
    if guard_blocks:
        stage_trace.append({"stage": "guard_blocks", "items": guard_blocks})

    if failed:
        retry_start = time.time()
        retry_queries: list[dict] = []

        for fq in failed:
            retry_prompt = SQL_RETRY_PROMPT.format(
                schema=schema,
                tables=list(tables.keys()),
                code=fq["code"],
                error=fq["error"],
                label=fq.get("label", "retry"),
            )
            retry_text = llm_fn(retry_prompt)
            for block in re.findall(r"<query>(.*?)</query>", retry_text, re.DOTALL):
                c = re.search(r"<code>(.*?)</code>", block, re.DOTALL)
                l = re.search(r"<label>(.*?)</label>", block, re.DOTALL)
                if c:
                    retry_queries.append({
                        "label": (l.group(1).strip() if l else fq.get("label", "retry")) + " (retry)",
                        "type": fq.get("type", "primary"),
                        "code": c.group(1).strip().rstrip(";"),
                        "result": None, "error": None,
                    })

        stage_trace.append({
            "stage": "retry", "tier": 1,
            "seconds": round(time.time() - retry_start, 2),
            "retried": len(failed),
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

    # Stages 3d + 4: Narrate → groundedness check → retry loop
    #
    # UX CONTRACT:
    #   - Clean result on any attempt → narrative returned unchanged, no notice.
    #   - Any unmatched number → retry up to _MAX_NARRATE_ATTEMPTS, feeding the
    #     exact unmatched tokens back via NARRATIVE_RETRY_PROMPT each time.
    #   - All attempts exhausted with ratio > _BLOCK_THRESHOLD → narrative replaced
    #     with a user-facing sorry message; failing narrative kept in stage_trace.
    #   - All attempts exhausted but ratio ≤ _BLOCK_THRESHOLD → narrative passes;
    #     low persistent unmatched count is likely harmless rounding noise.
    #   - Compliance check (tier-0 LLM) → log-only on the final narrative.
    #     Never gates retries; tier-0 models are too inconsistent for that.
    #
    # See _RETRY_THRESHOLD / _BLOCK_THRESHOLD constants for calibration rationale.

    grounding: dict = {}
    guardrail_issues: list[str] = []
    grounding_ratio: float = 0.0
    narrative = ""

    for narrate_attempt in range(1, _MAX_NARRATE_ATTEMPTS + 1):
        try:
            if narrate_attempt == 1:
                narrative, narr_record = format_narrative(
                    question, queries, mode, schema, llm_fn,
                    user_context, analysis_assumptions=analysis_assumptions,
                )
                narr_record["narrate_attempt"] = 1
            else:
                narrative, narr_record = format_narrative_retry(
                    question, queries, mode, schema, llm_fn,
                    unmatched_samples=grounding.get("unmatched_samples", []),
                    attempt=narrate_attempt,
                    user_context=user_context,
                )
            stage_trace.append(narr_record)
        except Exception as e:
            narrative = f"(Narrative failed: {e})"
            break

        # Deterministic groundedness check
        grounding = verify_groundedness(narrative, queries)
        num_found = grounding.get("numbers_found", 0)
        num_unmatched = grounding.get("numbers_unmatched", 0)
        grounding_ratio = num_unmatched / num_found if num_found > 0 else 0.0

        guardrail_issues = []
        if num_unmatched > _RETRY_THRESHOLD:
            guardrail_issues.append("numeric_validation_failed")

        if not guardrail_issues:
            break  # Narrative passed cleanly — exit loop

        # Log the per-attempt grounding failure so analysts can trace the retry.
        stage_trace[-1]["grounding_failed"] = True
        stage_trace[-1]["grounding_ratio"] = round(grounding_ratio, 3)
        stage_trace[-1]["unmatched_samples"] = grounding.get("unmatched_samples", [])

    # Compliance runs once on the final narrative — log-only regardless of outcome.
    compliance = check_compliance(narrative, llm_fn, schema_context=schema)

    # Hard-block only when the ratio after all retries still exceeds _BLOCK_THRESHOLD.
    # guardrail_issues being set just means the last attempt had unmatched numbers
    # (any count > 0). The block gate is stricter: if retries reduced the ratio
    # below 0.20, the narrative passes — low persistent unmatched counts are
    # rounding noise that the retry loop couldn't fully eliminate.
    should_block = bool(guardrail_issues) and grounding_ratio > _BLOCK_THRESHOLD

    if should_block:
        # Replace narrative with a user-facing message; preserve the last failing
        # narrative in stage_trace for analyst inspection.
        original_narrative = narrative
        narrative = (
            f"Sorry, I wasn't able to generate a verified answer for this question "
            f"after {narrate_attempt} attempt(s). ({', '.join(guardrail_issues)})"
        )
    else:
        original_narrative = None

    stage_trace.append({
        "stage":              "guardrails",
        "grounding":          grounding,
        "compliance":         compliance,
        "guardrail_issues":   guardrail_issues,
        "narrative_replaced": should_block,
        "narrate_attempts":   narrate_attempt,
        **({"original_narrative": original_narrative} if original_narrative else {}),
    })

    return {
        "classification": classification, "answer": raw_answer, "code": all_code,
        "queries": queries, "narrative": narrative,
        "mode": mode, "error": None, "stage_trace": stage_trace,
    }

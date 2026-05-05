"""
conversation_processor.py — Stateless orchestration for multi-turn analytics chat.

Frontend-agnostic: no print(), no input(), no display() calls. The CLI wrapper
(chat.py) and future Streamlit frontend both import from here.

Mode system:
  - mode_control: "auto" (LLM picks mode each turn) or "manual" (locked by user)
  - mode:          0=Retrieve, 1=Explore, 2=Reason

Slash commands in user input (detected anywhere in the string):
  /retrieve  /explore  /reason → lock mode_control to manual + set mode
  /auto                        → return mode_control to auto (mode unchanged)

Rolling summary: after each turn, a tier-0 LLM call updates a running summary
used by the next turn's interpret step. Bounds context size across long sessions.

Public API:
  - parse_mode_command     Extract /retrieve|/explore|/reason|/auto from input
  - interpret_turn         Tier-1 LLM call: classify turn, resolve follow-up, suggest mode
  - update_summary         Tier-0 LLM call: refresh rolling session summary
  - enrich_trace           Attach model names to stage records
  - tier_to_model          Resolve (backend, tier) → model string
  - trace_total_seconds    Sum stage durations
  - MODE_TRANSITION_MSGS   Mode-change explainer strings
  - CMD_TO_MODE            Slash-command → mode mapping
"""

import re, time
from _agent.analyst_agent import MODE_NAMES
from _agent.prompts import (
    INTERPRET_TURN_PROMPT, MODE_TASK_BLOCK_AUTO, MODE_RESPONSE_BLOCK_AUTO,
    SUMMARY_UPDATE_PROMPT,
)
from _skills.llm_backends import MODEL_MAP


# ── Mode transition cues ────────────────────────────────────

MODE_TRANSITION_MSGS = {
    0: "Switching to Retrieve mode, for quick factual answers.",
    1: "Switching to Explore mode, for more comprehensive answers.",
    2: "Switching to Reason mode, for deeper analysis and reasoning.",
}


# ── Slash-command parser ─────────────────────────────────────

_MODE_CMD_RE = re.compile(r"(?i)(?<!\w)/(retrieve|explore|reason|auto)\b")
CMD_TO_MODE = {"retrieve": 0, "explore": 1, "reason": 2}

def parse_mode_command(question: str):
    """Extract first /retrieve|/explore|/reason|/auto from input.
    Returns (cleaned_question, cmd or None)."""
    m = _MODE_CMD_RE.search(question)
    if not m:
        return question, None
    cmd = m.group(1).lower()
    cleaned = " ".join((question[:m.start()] + question[m.end():]).split())
    return cleaned, cmd


# ── Interpret turn (resolve + optional mode suggestion) ──────

def _strip_prefixes(text: str) -> str:
    """Remove common LLM prefixes from a resolved question."""
    for prefix in ["rewritten question:", "standalone question:", "rewritten:", "question:"]:
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
    return text.strip('"').strip("'")


def interpret_turn(question, summary, last_turn, turn_count, current_mode, mode_control, llm_fn):
    """Single tier-1 LLM call: classify turn type, resolve follow-up, and (if auto) suggest mode.

    Returns (resolved_question, suggested_mode or None, mode_reason or None,
             turn_type, stage_record).
    """
    auto = (mode_control == "auto")
    mode_task_block = MODE_TASK_BLOCK_AUTO if auto else ""
    mode_response_block = MODE_RESPONSE_BLOCK_AUTO if auto else ""

    prompt = INTERPRET_TURN_PROMPT.format(
        summary=summary or "(no prior turns)",
        last_turn=last_turn or "(no prior turns)",
        turn_count=turn_count,
        current_mode_name=MODE_NAMES[current_mode],
        question=question,
        mode_task_block=mode_task_block,
        mode_response_block=mode_response_block,
    )
    t0 = time.time()
    text = llm_fn(prompt, tier=1)
    elapsed = round(time.time() - t0, 2)
    stage_record = {"stage": "interpret", "tier": 1, "seconds": elapsed}

    resolved = question
    mode_str = None
    mode_reason = None
    turn_type = None
    for line in text.split("\n"):
        if line.startswith("TURN_TYPE:"):
            turn_type = line.split(":", 1)[1].strip().lower()
        elif line.startswith("RESOLVED:"):
            resolved = line.split(":", 1)[1].strip()
        elif line.startswith("MODE:"):
            mode_str = line.split(":", 1)[1].strip().lower()
        elif line.startswith("MODE_REASON:"):
            mode_reason = line.split(":", 1)[1].strip()

    resolved = _strip_prefixes(resolved)

    mode_map = {"retrieve": 0, "explore": 1, "reason": 2}
    suggested_mode = mode_map.get(mode_str) if auto else None
    return resolved, suggested_mode, mode_reason, turn_type, stage_record
    
    
# ── Summary update (end of turn) ─────────────────────────────

def update_summary(prior_summary, question, narrative, llm_fn):
    """Refresh the rolling session summary with the latest Q/A. Tier 0.
    Returns (new_summary, stage_record or None). Record is None on failure."""
    try:
        t0 = time.time()
        new_summary = llm_fn(SUMMARY_UPDATE_PROMPT.format(
            prior_summary=prior_summary or "(empty)",
            question=question,
            narrative=narrative,
        ), tier=0).strip()
        elapsed = round(time.time() - t0, 2)
        return new_summary, {"stage": "summary_update", "tier": 0, "seconds": elapsed}
    except Exception:
        # Summary failures shouldn't break the chat — fall back to prior summary
        return prior_summary, None
        
        
# ── Trace formatting (for UI + logging) ──────────────────────

def tier_to_model(backend: str, tier: int) -> str:
    """Resolve a tier number to the actual model name for this backend."""
    try:
        return MODEL_MAP[backend][tier]
    except (KeyError, TypeError):
        return f"tier{tier}"


def enrich_trace(trace: list[dict], backend: str) -> list[dict]:
    """Attach model names to each stage record."""
    return [
        {**rec, "model": tier_to_model(backend, rec.get("tier", -1))}
        for rec in trace
    ]


def trace_total_seconds(trace: list[dict]) -> float:
    """Sum of all stage seconds in the trace."""
    return round(sum(r.get("seconds", 0.0) for r in trace), 2)
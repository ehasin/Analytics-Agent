"""
guardrails.py — Post-narration reliability checks for the analytics agent.

Provides two functions consumed by analyst_agent.py after the narrate stage.
Results are appended to stage_trace. When violations are detected the caller
may also append a caveat to the narrative (see analyst_agent.py).

  - verify_groundedness   Regex-based check: numbers and named entities in the
                          narrative appear in query results (no LLM call).
  - check_compliance      Tier-0 LLM scan: narrative complies with a configurable
                          set of anti-hallucination rules.

Self-contained: no imports from other agent modules. llm_fn is passed as an
argument. Standard library only (re); no third-party dependencies.
"""

import re


# ── Number extraction and normalisation ─────────────────────

# Matches: integers, decimals, thousand-separated numbers, K/M/B suffixes.
# Examples: 44375, 1,200,000, 44.5, 44K, 1.2M, 2.5B
_NUM_RE = re.compile(
    r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?[KMBkmb]?\b"
    r"|\b\d+(?:\.\d+)?[KMBkmb]?\b"
)

# Range patterns like "1–5 scale" or "1-5". Numbers that appear as the
# low or high bound of a range are scale descriptions, not data claims.
# Matched against the narrative before number extraction.
_RANGE_RE = re.compile(r"\b(\d+)\s*[–\-]\s*(\d+)\b")

# Scientific notation produced by DuckDB / pandas for large floats.
# Expanded to full integer/decimal form before building the match haystack.
_SCI_RE = re.compile(r"-?\d+(?:\.\d+)?[eE][+\-]?\d+")


def _expand_sci_notation(text: str) -> str:
    """Replace scientific-notation tokens with their full numeric form.

    DuckDB / pandas render large floats as '1.349641e+07'. The narrative
    model writes them as '13,496,410'. Without expansion the groundedness
    checker can't match them, producing false positives on any large-value
    query (revenue, order volumes, etc.).
    """
    def _replace(m: re.Match) -> str:
        try:
            val = float(m.group(0))
            # Integer-valued floats (e.g. 1.0e+07 → 10000000)
            if val == int(val):
                return str(int(val))
            return f"{val:.6f}".rstrip("0").rstrip(".")
        except (ValueError, OverflowError):
            return m.group(0)
    return _SCI_RE.sub(_replace, text)

# Bare 4-digit years to exclude from numeric groundedness checks.
# Year tokens like "2017" appear constantly in factual narratives but are rarely
# present as bare integers in query results (stored as timestamps or date strings).
# Excluding them prevents systematic false positives on this dataset (2016–2018).
_YEAR_STOPSET: set[str] = {str(y) for y in range(2010, 2030)}

_SUFFIX_MULTIPLIER: dict[str, int] = {
    "K": 1_000,
    "M": 1_000_000,
    "B": 1_000_000_000,
}


def _normalise_number(token: str) -> str:
    """Canonicalise a numeric token: strip separators, expand K/M/B, round to 2dp."""
    token = token.strip()
    multiplier = 1

    if token and token[-1].upper() in _SUFFIX_MULTIPLIER:
        multiplier = _SUFFIX_MULTIPLIER[token[-1].upper()]
        token = token[:-1]

    try:
        value = float(token.replace(",", "")) * multiplier
        rounded = round(value, 2)
        return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.2f}"
    except ValueError:
        return token


def _extract_numbers(text: str) -> list[tuple[str, str]]:
    """Return list of (raw_token, normalised) pairs found in *text*."""
    return [(t, _normalise_number(t)) for t in _NUM_RE.findall(text)]


# ── Named entity extraction ──────────────────────────────────

# Title-case sequences of 2+ words: "São Paulo", "North Region"
_TITLE_SEQ_RE = re.compile(r"\b([A-Z][a-zA-Zà-öø-ÿ]+(?:\s+[A-Z][a-zA-Zà-öø-ÿ]+)+)\b")

# ALL-CAPS codes 2–5 chars: "BRL", "SP", "SKU123"
_CAPS_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,4})\b")

_ENTITY_STOPLIST = {
    # Articles / prepositions / conjunctions
    "The", "In", "For", "By", "With", "And", "Or", "Not", "A", "An",
    "Of", "To", "Is", "Are", "Was", "Were", "No", "Yes",
    # Generic analytical / aggregation terms
    "Total", "Average", "Count", "Top", "All", "Per",
    "Rank", "Rate", "Score", "Value", "Net", "Gross",
    # Column-header words that appear in narrative prose but not in result rows
    "Order", "Seller", "Customer", "Revenue", "Frequency",
    "Metric", "Label", "Period", "Segment", "Tier",
    "Week", "Month", "Pivot", "Sales", "Growth", "Profit",
    # Brand / product names not present in raw query output
    "Olist",
}


def _extract_entities(text: str) -> list[str]:
    """Extract candidate named-entity tokens from *text* (deduplicated)."""
    seen: set[str] = set()
    entities: list[str] = []

    for pattern in (_TITLE_SEQ_RE, _CAPS_CODE_RE):
        for m in pattern.finditer(text):
            word = m.group(1)
            # Skip if ANY token in the matched sequence is a stoplist word.
            # This catches column-header phrases like "Customer Count" or
            # "Net Revenue" where the full string isn't in the stoplist but
            # an individual word is.
            if any(w in _ENTITY_STOPLIST for w in word.split()):
                continue
            if word not in seen:
                seen.add(word)
                entities.append(word)

    return entities


# ── Groundedness verifier ────────────────────────────────────

def verify_groundedness(narrative: str, queries: list[dict]) -> dict:
    """Check that numbers and named entities in *narrative* appear in query results.

    Skips DOC_LOOKUP queries (result is schema text, not data rows).
    Checks raw token AND normalised form AND comma-stripped variants.
    Never raises — returns zero counts on any internal error.

    Returns:
        {
            "numbers_found":      int,
            "numbers_unmatched":  int,
            "entities_found":     int,
            "entities_unmatched": int,
            "unmatched_samples":  list[str],  # up to 5 examples
        }
    """
    try:
        # Build haystack from real data queries only.
        # Expand scientific notation first so "1.349641e+07" matches "13496410"
        # written out in the narrative — DuckDB/pandas use sci notation for large
        # floats; the narrative model correctly writes the full form.
        result_text = _expand_sci_notation(" ".join(
            str(q.get("result", "") or "")
            for q in queries
            if q.get("code", "").strip() != "DOC_LOOKUP"
        ))
        result_plain = result_text.replace(",", "")

        # Build set of all normalised numbers present in results for rounding
        # tolerance checks (see below).
        result_nums: set[str] = {n for _, n in _extract_numbers(result_text)}

        # Collect range-notation bounds from narrative so "1" and "5" in
        # "on a 1–5 scale" are not treated as ungrounded data claims.
        range_bounds: set[str] = set()
        for m in _RANGE_RE.finditer(narrative):
            range_bounds.add(m.group(1))
            range_bounds.add(m.group(2))

        # ── Numbers ─────────────────────────────────────────
        num_pairs = _extract_numbers(narrative)
        num_unmatched: list[str] = []

        for raw, normalised in num_pairs:
            # Skip bare year tokens — they appear in factual narratives but are
            # almost never present as bare integers in query results (stored as
            # date strings / timestamps), causing systematic false positives.
            if raw.replace(",", "") in _YEAR_STOPSET:
                continue
            # Skip numbers that are bounds of a range like "1–5 scale".
            if raw in range_bounds:
                continue
            raw_plain = raw.replace(",", "")
            if (
                raw in result_text
                or normalised in result_text
                or raw_plain in result_plain
                or normalised in result_plain
            ):
                continue
            # Rounding tolerance: the narrative may state a rounded form of a
            # result value (e.g. "4.09" from "4.086421", or "13,496,410" from
            # a multi-decimal float). Try matching at 0–4 decimal place rounding
            # levels against every normalised number extracted from results.
            try:
                raw_val = float(raw_plain)
                if any(
                    abs(raw_val - float(r)) < max(0.01, abs(raw_val) * 0.001)
                    for r in result_nums
                    if r.lstrip("-").replace(".", "", 1).isdigit()
                ):
                    continue
            except ValueError:
                pass
            num_unmatched.append(raw)

        # ── Named entities ───────────────────────────────────
        entities = _extract_entities(narrative)
        result_lower = result_text.lower()
        ent_unmatched = [e for e in entities if e.lower() not in result_lower]

        unmatched_samples = (num_unmatched + ent_unmatched)[:5]

        return {
            "numbers_found":      len(num_pairs),
            "numbers_unmatched":  len(num_unmatched),
            "entities_found":     len(entities),
            "entities_unmatched": len(ent_unmatched),
            "unmatched_samples":  unmatched_samples,
        }

    except Exception:
        return {
            "numbers_found": 0, "numbers_unmatched": 0,
            "entities_found": 0, "entities_unmatched": 0,
            "unmatched_samples": [],
        }


# ── Compliance checker ───────────────────────────────────────

_DEFAULT_RULES: list[str] = [
    "Contains numbers described as approximate when exact values were available "
    "(e.g. 'approximately', 'around', 'roughly' preceding a number)",
    "Uses evaluative adjectives that characterise results as good or bad "
    "(e.g. impressive, concerning, worrying, excellent, alarming)",
    "Infers human intent or motivation from behavioural data alone",
    "States a currency or unit that contradicts what the data schema specifies",
    "Makes a numerical or categorical claim that does not appear in the query results",
]

_COMPLIANCE_PROMPT = """\
You are a compliance checker for an analytics bot.

Narrative to check:
{narrative}

Check for violations of these rules:
{rules_text}

Respond in this exact format:
VIOLATIONS: <count>
DETAILS: <one line per violation, or 'none'>"""


def check_compliance(
    narrative: str,
    llm_fn,
    rules: list[str] | None = None,
) -> dict:
    """Tier-0 LLM scan of *narrative* for compliance with anti-hallucination rules.

    Uses llm_fn(prompt, tier=0) — the cheapest available model — to keep
    guardrail overhead low. Never raises; returns a safe default on any error.

    Args:
        narrative:  the agent's narrated response
        llm_fn:     callable(prompt, tier=int) → str
        rules:      list of rule strings; defaults to _DEFAULT_RULES if None

    Returns:
        {"violations": int, "details": str}
        violations == -1 means the check failed to run (not a clean result).
    """
    # Cap narrative length before building the prompt. Tier-0 models (e.g.
    # llama-3.1-8b on Groq) have an 8K context window. The prompt template
    # itself consumes ~300 tokens; leaving 3 000 chars for the narrative is a
    # safe ceiling that prevents silent truncation and avoids a false "clean"
    # result caused by partial input.
    _NARRATIVE_CAP = 3_000
    if narrative and len(narrative) > _NARRATIVE_CAP:
        narrative = narrative[:_NARRATIVE_CAP] + "\n\n[narrative truncated for compliance check]"

    try:
        active_rules = rules if rules is not None else _DEFAULT_RULES
        rules_text = "\n".join(f"- {r}" for r in active_rules)

        prompt = _COMPLIANCE_PROMPT.format(
            narrative=narrative,
            rules_text=rules_text,
        )

        response = llm_fn(prompt, tier=0)

        violations = 0
        detail_lines: list[str] = []
        in_details = False

        for line in response.splitlines():
            stripped = line.strip()
            if stripped.startswith("VIOLATIONS:"):
                in_details = False
                try:
                    violations = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif stripped.startswith("DETAILS:"):
                # Capture the first line of DETAILS inline, then continue
                # collecting subsequent lines until the response ends.
                # This preserves multi-violation detail text that spans lines.
                first = stripped.split(":", 1)[1].strip()
                if first:
                    detail_lines.append(first)
                in_details = True
            elif in_details and stripped:
                detail_lines.append(stripped)

        details = " | ".join(detail_lines) if detail_lines else "none"
        return {"violations": violations, "details": details}

    except Exception:
        # violations=-1 signals the check did not run — distinguishable from
        # violations=0 (clean) so dashboards and log queries can tell the
        # difference between "passed" and "unchecked".
        return {"violations": -1, "details": "check failed — compliance not verified"}

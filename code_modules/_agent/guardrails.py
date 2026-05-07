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

# "Out of N" / "X/N" scale denominators — e.g. "4.09 out of 5" or "rated 4/5".
# The denominator is a known scale maximum from the schema, not a queried value.
# Without this, "5" in "average score of 4.09 out of 5" is flagged as ungrounded,
# causing a false positive hard-block on straightforward review-score questions.
_OUT_OF_RE = re.compile(r"\bout\s+of\s+(\d+)\b|(?<=\d)/(\d+)\b")

# Percentage tokens — numbers immediately followed by %. e.g. "78.2%", "18%".
# Percentages in narrative prose are arithmetic derivatives of raw query values
# (the LLM computes "credit card: 78.2%" from the absolute revenue figures).
# They are never literally present in raw DuckDB result rows, so treating them
# as unmatched would fire false positives on any distribution/breakdown narrative.
# These tokens are stripped from the narrative before number extraction.
_PCT_RE = re.compile(
    r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*%"
    r"|\b\d+(?:\.\d+)?\s*%"
)

# Scientific notation produced by DuckDB / pandas for large floats.
# Expanded to full integer/decimal form before building the match haystack.
_SCI_RE = re.compile(r"-?\d+(?:\.\d+)?[eE][+\-]?\d+")

# ── Deterministic compliance check constants ─────────────────
# Rules 1, 2, and 4 are checked here without any LLM call.
# Rule 3 (motivational inference) is dropped — no deterministic check is
# reliable enough; the narrative prompt enforces it at the model level.
# Rule 5 (unsupported claims) is dropped — covered by verify_groundedness.

# Rule 1: Approximation language directly before a number.
# Catches "approximately R$1.2M", "roughly 44,000", "~50K".
# Known limitation: "around 2017" (temporal) is a theoretical false positive,
# negligible in practice for this dataset where time refs use "in 2017" form.
_APPROX_BEFORE_NUM_RE = re.compile(
    r"\b(approximately|around|roughly|about)\s+[R$]?\d",
    re.IGNORECASE,
)
_TILDE_NUM_RE = re.compile(r"~\s*[R$]?\d")

# Rule 2: Evaluative adjectives — intentionally tight to avoid false positives.
_EVAL_WORDS: frozenset[str] = frozenset({
    "impressive", "concerning", "worrying", "alarming",
    "excellent", "exceptional", "disappointing", "troubling",
    "encouraging", "stellar", "dismal",
})

# Rule 4: Currency drift. Bare $ (not R$) or USD when schema specifies BRL.
_USD_RE = re.compile(r"\bUSD\b")
# $ not preceded by a letter (excludes R$, C$, etc.)
_BARE_DOLLAR_RE = re.compile(r"(?<![A-Za-z])\$\s*\d")
_BRL_IN_SCHEMA_RE = re.compile(r"\bBRL\b", re.IGNORECASE)


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
    # Currency codes — appear in narrative prose but never in raw query result rows
    "BRL", "USD", "EUR", "GBP", "JPY", "CAD", "AUD",
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


def check_compliance_deterministic(
    narrative: str,
    schema_context: str = "",
) -> dict:
    """Deterministic compliance check for rules 1, 2, and 4. No LLM call.

    Replaces the tier-0 LLM scan for these three rules. Runs inside the
    narrate → groundedness → retry loop so violations trigger a corrective
    rewrite, same as numeric groundedness failures.

    Rules checked:
      1. Approximation language (approximately / roughly / around / ~) before a number.
      2. Evaluative adjectives — closed list (impressive, exceptional, alarming, etc.).
      4. Currency drift — narrative uses USD or bare $ when schema specifies BRL.

    Rules NOT checked here:
      3. Motivational inference — dropped; narrative prompt enforces it.
      5. Unsupported claims — covered by verify_groundedness.

    Returns:
        {"violations": int, "details": str, "samples": list[str]}
        violations == 0 means clean.
        samples contains the human-readable violation strings for retry feedback.
    """
    violations: list[str] = []

    # ── Rule 1: Approximation language ──────────────────────
    approx_words = _APPROX_BEFORE_NUM_RE.findall(narrative)
    tilde_hit = bool(_TILDE_NUM_RE.search(narrative))
    all_approx = list({w.lower() for w in approx_words})
    if tilde_hit:
        all_approx.append("~")
    if all_approx:
        violations.append(
            f"Rule 1 — approximation language before number: {', '.join(all_approx[:3])}"
        )

    # ── Rule 2: Evaluative adjectives ───────────────────────
    narrative_lower = narrative.lower()
    found_eval = sorted(
        w for w in _EVAL_WORDS
        if re.search(rf"\b{re.escape(w)}\b", narrative_lower)
    )
    if found_eval:
        violations.append(
            f"Rule 2 — evaluative adjectives: {', '.join(found_eval[:3])}"
        )

    # ── Rule 4: Currency drift ───────────────────────────────
    # Only flag when schema explicitly specifies BRL and narrative contradicts it.
    if schema_context and _BRL_IN_SCHEMA_RE.search(schema_context):
        if _USD_RE.search(narrative) or _BARE_DOLLAR_RE.search(narrative):
            violations.append(
                "Rule 4 — currency drift: narrative uses USD/$ but schema specifies BRL"
            )

    return {
        "violations": len(violations),
        "details": " | ".join(violations) if violations else "none",
        "samples": violations,
    }


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

        # If there are no data-query results to ground against (e.g. all queries
        # were DOC_LOOKUP metadata lookups), skip the check entirely. Running
        # groundedness against an empty haystack would flag every number in the
        # narrative as ungrounded — a guaranteed false positive on metadata answers.
        if not result_text.strip():
            return {
                "numbers_found": 0, "numbers_unmatched": 0,
                "entities_found": 0, "entities_unmatched": 0,
                "unmatched_samples": [], "skipped": True,
            }

        result_plain = result_text.replace(",", "")

        # Build set of all normalised numbers present in results for rounding
        # tolerance checks (see below).
        result_nums: set[str] = {n for _, n in _extract_numbers(result_text)}

        # Collect range-notation bounds from narrative so "1" and "5" in
        # "on a 1–5 scale" are not treated as ungrounded data claims.
        # Also collect "out of N" denominators ("4.09 out of 5", "rated 4/5")
        # for the same reason — scale maximums are schema knowledge, not data.
        range_bounds: set[str] = set()
        for m in _RANGE_RE.finditer(narrative):
            range_bounds.add(m.group(1))
            range_bounds.add(m.group(2))
        for m in _OUT_OF_RE.finditer(narrative):
            # Regex has two capture groups (alt branches); only one fires per match.
            val = m.group(1) or m.group(2)
            if val:
                range_bounds.add(val)

        # ── Numbers ─────────────────────────────────────────
        # Strip percentage tokens before extraction. Percentages like "78.2%"
        # are arithmetic derivatives of result values, not literal data claims —
        # the LLM computes them from the raw rows it was given.  They will never
        # appear in result_text, so including them would inflate the unmatched
        # count and trigger false positives on any distribution/breakdown answer
        # (e.g. "What is total revenue by payment type?").
        narrative_for_nums = _PCT_RE.sub("", narrative)
        num_pairs = _extract_numbers(narrative_for_nums)
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
        # Also match snake_case result values against space-separated narrative
        # entities: "credit_card" in results matches "Credit Card" in narrative.
        # Without this, any multi-word entity whose result-row form uses underscores
        # (e.g. payment_type, debit_card) produces a spurious false positive on
        # breakdown narratives that render column values in title case.
        result_lower_spaced = result_lower.replace("_", " ")
        ent_unmatched = [
            e for e in entities
            if e.lower() not in result_lower
            and e.lower() not in result_lower_spaced
        ]

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

# Rules 1, 2, and 4 are now checked deterministically by check_compliance_deterministic().
# check_compliance() is no longer called in the main agent pipeline (v1.6.6+).
# It is retained for testing and custom deployment use. The rules below cover
# the remaining cases that require LLM judgment if re-enabled.
_DEFAULT_RULES: list[str] = [
    # Rule 3 — motivational inference: too semantic for reliable deterministic detection.
    # The narrative prompt forbids it; no runtime gate is deployed.
    "Infers human intent or motivation from behavioural data alone "
    "(e.g. 'customers are unhappy because...', 'users clearly prefer...')",
    # Rule 5 — unsupported claims: largely covered by verify_groundedness.
    # Retained for categorical edge cases not caught by number/entity regex.
    "Makes a numerical or categorical claim that does not appear in the query results",
]

_COMPLIANCE_PROMPT = """\
You are a compliance checker for an analytics bot.

{schema_context_section}Narrative to check:
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
    schema_context: str = "",
) -> dict:
    """Tier-0 LLM scan of *narrative* for compliance with anti-hallucination rules.

    NOTE: Not called in the main agent pipeline as of v1.6.6. Rules 1/2/4 are now
    checked deterministically by check_compliance_deterministic(). Rules 3/5 remain
    here for testing and custom use. Re-enable by calling from analyst_agent.py.

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

        # Inject a brief schema excerpt so the tier-0 model knows the expected
        # currency and units. Without this, "USD" in a narrative cannot be flagged
        # as unit drift — the checker has no ground truth to compare against.
        # Cap at 800 chars: enough to capture the currency/unit declarations at the
        # top of the schema without blowing the tier-0 model's 8K context window.
        schema_context_section = (
            f"Data schema context "
            f"(use this to verify currency, units, and entity names):\n"
            f"{schema_context[:800]}\n\n"
            if schema_context else ""
        )

        prompt = _COMPLIANCE_PROMPT.format(
            schema_context_section=schema_context_section,
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

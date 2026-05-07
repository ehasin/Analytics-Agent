# Architecture

This document goes deeper than the README on *why* the agent is structured the way it is.

## Pipeline stages

### 1. Interpret turn

The agent's first move on every turn is to classify *what kind of turn this is*: a standalone question, a continuation of the prior thread, a topic pivot, or a correction. This matters because each calls for different handling — and getting it wrong is one of the most common failure modes in chat-based analytics.

The interpretation step also resolves follow-up questions to standalone form (so the planning step doesn't need to know about conversation history) and, in auto mode, suggests the response mode for the turn.

The full prompt is in `prompts.py:INTERPRET_TURN_PROMPT`.

### 2. Classify + Plan

A single LLM call does two things at once:

- **Classifies** the question as `can_answer`, `cant_answer`, or `clarifications_needed`
- **Plans** the queries (DuckDB SQL) if classification is `can_answer`

Merging classify and plan into one call was a deliberate choice — they share most of their context, and splitting them led to inconsistent decisions (e.g., classify says "can_answer" but plan generates queries against missing tables).

The classification is conservative: when in doubt, the agent prefers `clarifications_needed` over `can_answer`, and surfaces concrete suggestions for what the user could specify.

### 3. Execute

DuckDB runs the planned queries against in-memory DataFrames. Two reliability mechanisms are active at this stage:

**SQL guard (`guard_sql`)** — every query is inspected by `sqlglot` before execution. Only `SELECT` and `WITH…SELECT` statements are permitted. INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, and DuckDB-specific commands (ATTACH, COPY, LOAD) are blocked and produce a `ValueError` that becomes a query error. References to `information_schema` or `system.` tables are blocked on the raw SQL string. When sqlglot cannot parse the SQL, execution is allowed but a `guard_warning` is recorded in `stage_trace` for observability.

**Targeted retry** — SQL errors trigger a single retry using `SQL_RETRY_PROMPT`, a minimal fix-focused template that includes only the schema, the failing query, and the error message. This is deliberately distinct from the full `CLASSIFY_AND_PLAN_PROMPT` used in the initial planning step — sending the full planning prompt on retry caused the model to re-plan all queries from scratch, producing label mismatches and wasted tokens. The targeted prompt fixes only the specific failing query while preserving the original label and intent.

A special token `DOC_LOOKUP` is used as a query placeholder for metadata questions — the agent recognizes that some questions ("what tables are available?") are answerable directly from the data model documentation without running SQL.

### 4. Narrate → Groundedness → Retry loop

Narration and groundedness checking are interleaved in a retry loop (up to 3 total attempts: 1 original + 2 retries). The loop is:

```
attempt 1: format_narrative (NARRATIVE_PROMPT)
           → verify_groundedness
           → pass? → done. fail? → attempt 2

attempt 2: format_narrative_retry (NARRATIVE_RETRY_PROMPT)
           feeds back exact unmatched tokens
           → verify_groundedness
           → pass? → done. fail? → attempt 3

attempt 3: format_narrative_retry again with updated unmatched list
           → verify_groundedness
           → pass? → done. fail? → replace with sorry message
```

**`NARRATIVE_PROMPT`** (attempt 1) is the most heavily engineered part of the system:

- **Numeric fidelity**: copy numbers character-by-character from query results; approximations must include the exact form alongside.
- **Anti-fabrication**: truncated results (50K char cap) must be answered only from visible rows; no gap-filling.
- **Scope discipline**: separate format blocks for Retrieve / Explore / Reason.

In Reason mode, narration uses tier-2 (Claude Opus / GPT-OSS-120B). All other stages use the default tier-1.

**`NARRATIVE_RETRY_PROMPT`** (attempts 2–3) is a targeted corrective rewrite. It receives the same question and query results, plus the specific unmatched tokens from `verify_groundedness.unmatched_samples`. The instruction is explicit: *do not include values not present verbatim in a result row — omit derived values (percentages, grand totals, differences) if they are not a column in the result.* This is analogous to `SQL_RETRY_PROMPT` for the SQL layer: minimal context, targeted fix, same analytical intent.

Per-attempt grounding results are logged in `stage_trace` alongside each narrate record (`grounding_failed`, `grounding_ratio`, `unmatched_samples`) so the retry progression is fully inspectable.

### 5. Guardrails

After the retry loop exits, a compliance scan runs once on the final narrative. Results are always recorded in `stage_trace`. Two distinct actions are possible:

- **Groundedness failure on all attempts** (>30% unmatched numbers, not resolved by retries) — the narrative is **replaced** with a user-facing message: *"Sorry, I wasn't able to generate a verified answer for this question after N attempt(s)."* The last failing narrative is preserved in `stage_trace` for analyst inspection; nothing is exposed to the user.
- **Compliance violation** — log-only, regardless of retry outcome. The tier-0 LLM scan result is written to `stage_trace` only. Compliance never gates retries; tier-0 models are too inconsistent to drive user-facing decisions.

The `guardrails` stage_trace record includes `narrate_attempts` (1–3) so dashboards can distinguish clean first-pass answers from recovered retries from hard failures.

**`verify_groundedness(narrative, queries)`** — no LLM call. Uses regex to extract all numeric tokens (integers, decimals, thousand-separated, K/M/B suffixes) and named entities (title-case sequences, ALL-CAPS codes) from the narrative. Each is checked against the concatenated query result strings, with normalisation (comma stripping, K→1000/M→1000000/B→1000000000 expansion, case-insensitive entity matching). Several false-positive classes are excluded before the check runs:

- **Year tokens** (2010–2029) — present in factual narratives but rarely as bare integers in query results (stored as date strings/timestamps).
- **Range-notation bounds** — numbers like "1" and "5" in "on a 1–5 scale" are schema-level scale descriptors, not data claims.
- **Scale denominators** — "5" in "4.09 out of 5" is a schema maximum, not a queried value.
- **Percentage tokens** — "78.2%" or "18%" are arithmetic derivatives the LLM computes from the raw rows it was given. They never appear literally in DuckDB result rows; including them inflated the unmatched ratio on any distribution/breakdown answer (e.g. revenue by payment type).
- **Snake_case column values** — result rows may contain `credit_card`, `debit_card`, `payment_type` while the narrative renders these in title case ("Credit Card", "Debit Card"). Entity matching checks both `result_lower` and `result_lower.replace("_", " ")` to prevent false entity flags on breakdown narratives.

Returns `{numbers_found, numbers_unmatched, entities_found, entities_unmatched, unmatched_samples}`.

The retry-and-block loop uses two independent thresholds:

- **`_RETRY_THRESHOLD = 0` (absolute count)** — retry fires when `numbers_unmatched > 0`, i.e. any single unmatched number. Each retry delivers the exact failing token list via `NARRATIVE_RETRY_PROMPT`; the model is instructed to omit values not present verbatim in result rows. Setting this to zero means low-ratio cases (e.g. one derived subtraction in a 100-number narrative) are challenged rather than silently passed through.
- **`_BLOCK_THRESHOLD = 0.20` (ratio)** — after all retries are exhausted, hard-block only when the final unmatched ratio exceeds 20%. A persistent low-ratio case (e.g. 1 unmatched out of 100 = 1%) is likely a rounding artefact that retries couldn't fully eliminate; that passes rather than erroring. A case still above 20% after retries signals genuine fabrication risk and is replaced.

Design intent: *retry early and cheaply on any signal; block only when clearly unreliable.* The retry threshold is the sensitive trip-wire; the block threshold is the hard backstop. Cloners targeting production should validate both against their own dataset — higher-cardinality schemas or longer narratives may warrant a slightly higher block threshold if retry storms are observed.

If all queries are `DOC_LOOKUP` (metadata questions with no data results), `verify_groundedness` returns a `skipped: True` result immediately. There are no data rows to ground against; running the check would produce a 100% unmatched false positive on every number in a schema description.

**`check_compliance(narrative, llm_fn, schema_context)`** — tier-0 LLM call (cheapest model). Scans the narrative against five rules:

1. Approximation language preceding exact-available numbers ("approximately", "around", "roughly")
2. Evaluative adjectives ("impressive", "concerning", "alarming", "excellent")
3. Motivational inference from behavioural data alone
4. Currency or unit contradicting the schema (e.g. USD when schema specifies BRL)
5. Numerical or categorical claims absent from query results

The `schema_context` parameter injects a ≤800-char excerpt from the data model into the compliance prompt, giving the tier-0 model the ground truth it needs to detect rule 4 (currency/unit drift). Without it, "USD" in a narrative cannot be flagged — the checker has no schema to compare against.

The narrative is capped at 3,000 characters before the compliance prompt to protect small-context tier-0 models from silent truncation. On failure, `violations=-1` is returned — distinguishable from `violations=0` (clean) for dashboards and log queries.

### 6. Update rolling summary

At the end of each turn, a tier-0 model updates a running session summary (target ≤200 words). This summary feeds the *next* turn's interpret step, giving the agent bounded conversation memory without ballooning context size on long sessions.

## Three-tier model routing

| Tier | Use | Claude | Groq | Gemini |
|---|---|---|---|---|
| 0 (fast) | Summary updates, compliance check | Haiku 4.5 | Llama 3.1 8B Instant | Gemini 2.5 Flash Lite |
| 1 (default) | Interpret, classify+plan, narrate, retry | Sonnet 4.6 | Llama 3.3 70B Versatile | Gemini 2.5 Flash |
| 2 (strong) | Reason-mode narration | Opus 4.7 | GPT-OSS-120B | Gemini 2.5 Pro |

Backends are interchangeable via `_skills/llm_backends.py`, which wraps all three under a unified `call_llm(prompt, backend, model_tier)` interface. All three backends enforce a 180-second ceiling per call (Claude and Groq via native `timeout=` parameter; Gemini via a `concurrent.futures` thread future, since the google-genai SDK has no per-call timeout).

## Schema-aware planning

The data model is loaded from `data_model.json` and injected into every planning prompt. The schema includes column-level descriptions, KPI definitions, valid/invalid use cases, and join relationships — far richer than a bare DDL dump.

Schema context dominates token usage per tier-1 call. This was a deliberate trade-off: hallucination risk is a higher-priority concern than token efficiency, and the rich schema context is what enables the agent to correctly classify questions as `cant_answer` when they reference missing data.

## Eval suite

### Standard validation (50 cases)

The 50-case validation suite is in `_data/olist_test_cases.py`. It spans six categories:

- **Metadata / Descriptive** (6) — schema introspection
- **Easy** (10) — single-aggregation lookups
- **Hard** (10) — multi-table joins, correlations, ranked filters
- **Misleading** (10) — questions with unstated assumptions or off-domain framing
- **Multi-stage** (5) — sequential analysis (classify → compare → contextualize)
- **Realistic** (9) — open-ended business questions

Each case has both a deterministic Python validator (lambda) and an LLM-assessed quality summary. The validator is authoritative for pass/fail; the LLM summary documents *what* the agent did well or poorly. A reconciliation step downgrades pass-by-lambda when the LLM assessment contradicts it (and vice versa).

`run_eval()` accepts an optional `assessor_llm_fn` parameter to enable cross-model grading — a different backend grades the answers, removing the self-grading bias of using the same model as both answerer and assessor.

### Guardrail injection eval (17 cases)

The 17-case injection suite is in `_data/guardrail_test_cases.py`. It tests guardrail components by injecting adversarial content at controlled pipeline stages rather than relying on a model to hallucinate naturally:

- **SQL injection** (5 cases) — the first LLM-planned query is replaced with a known-bad statement (DROP TABLE, INSERT, system table reference, information_schema reference, or a valid SELECT as a false-positive check). `guard_sql` should block the injected statement; the agent's retry loop fires with real LLM SQL; validators assert both the block and the recovery.

- **Narrative injection** (9 cases) — the LLM narrative is replaced with a synthetic string containing specific violations (fabricated number, approximation language, evaluative adjective, currency drift, motivational inference) or the agent runs live and produces its own narrative (false-positive checks: 4 real-LLM-narrative cases including one specifically for percentage-breakdown narratives). `verify_groundedness` and `check_compliance` are re-run on the injected or produced narrative; validators assert the guardrail correctly fires or stays quiet.

- **Prompt injection** (3 cases) — the adversarial text is the question itself ("ignore all previous instructions…"). The agent should classify as `cant_answer` or `clarifications_needed` and not leak internal prompt text or produce anomalously long output.

`run_guardrail_eval()` also accepts `assessor_llm_fn` so the compliance check within the harness can use a different model than the one under test.

Full validation results: [`validation.md`](validation.md).

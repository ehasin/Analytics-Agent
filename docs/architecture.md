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

### 4. Narrate

The narration prompt is the most heavily engineered part of the system. It's where most hallucination risk lives, and where the reliability principles get enforced concretely:

- **Numeric fidelity**: the model is instructed to copy numbers character-by-character from query results. Approximations must include the exact form alongside.
- **Anti-fabrication**: when a query result is truncated (at a 50K char cap), the model must answer only from visible rows and explicitly state the truncation. No filling gaps with plausible-looking rows.
- **Scope discipline**: separate format blocks for Retrieve / Explore / Reason ensure that response style matches mode intent.

In Reason mode, narration switches to a tier-2 model (Claude Opus / GPT-OSS-120B) for deeper reasoning. Other stages stay on the faster default tier.

### 5. Guardrails

A deterministic post-narration layer runs after every narrate call. It never suppresses a response — instead it appends a visible caveat to the narrative when issues are detected (soft-block). Results are also recorded in `stage_trace` for logging and observability.

**`verify_groundedness(narrative, queries)`** — no LLM call. Uses regex to extract all numeric tokens (integers, decimals, thousand-separated, K/M/B suffixes) and named entities (title-case sequences, ALL-CAPS codes) from the narrative. Each is checked against the concatenated query result strings, with normalisation (comma stripping, K→1000/M→1000000/B→1000000000 expansion, case-insensitive entity matching). Year tokens (2010–2029) are excluded to prevent systematic false positives on date-heavy narratives. Returns `{numbers_found, numbers_unmatched, entities_found, entities_unmatched, unmatched_samples}`.

A ⚠ groundedness caveat is appended to the narrative when more than 30% of extracted numbers are unmatched — at lower rates unmatched tokens are typically noise from stylistic phrasing; above 30% the balance tips toward fabrication risk.

**`check_compliance(narrative, llm_fn)`** — tier-0 LLM call (cheapest model). Scans the narrative against five rules:

1. Approximation language preceding exact-available numbers ("approximately", "around", "roughly")
2. Evaluative adjectives ("impressive", "concerning", "alarming", "excellent")
3. Motivational inference from behavioural data alone
4. Currency or unit contradicting the schema (e.g. USD when schema specifies BRL)
5. Numerical or categorical claims absent from query results

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

### Guardrail injection eval (18 cases)

The 18-case injection suite is in `_data/guardrail_test_cases.py`. It tests guardrail components by injecting adversarial content at controlled pipeline stages rather than relying on a model to hallucinate naturally:

- **SQL injection** (5 cases) — the first LLM-planned query is replaced with a known-bad statement (DROP TABLE, INSERT, system table reference, information_schema reference, or a valid SELECT as a false-positive check). `guard_sql` should block the injected statement; the agent's retry loop fires with real LLM SQL; validators assert both the block and the recovery.

- **Narrative injection** (8 cases) — the LLM narrative is replaced with a synthetic string containing specific violations (fabricated number, approximation language, evaluative adjective, currency drift, motivational inference) or clean text (false-positive checks). `verify_groundedness` and `check_compliance` are re-run on the injected narrative; validators assert the guardrail correctly fires or stays quiet.

- **Prompt injection** (3 cases) — the adversarial text is the question itself ("ignore all previous instructions…"). The agent should classify as `cant_answer` or `clarifications_needed` and not leak internal prompt text or produce anomalously long output.

`run_guardrail_eval()` also accepts `assessor_llm_fn` so the compliance check within the harness can use a different model than the one under test.

Full validation results: [`validation.md`](validation.md).

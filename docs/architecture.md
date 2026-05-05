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

DuckDB runs the planned queries against in-memory DataFrames. SQL errors trigger a single retry with the error text included in the prompt, so the LLM can correct syntax issues without escalating.

A special token `DOC_LOOKUP` is used as a query placeholder for metadata questions — the agent recognizes that some questions ("what tables are available?") are answerable directly from the data model documentation without running SQL.

### 4. Narrate

The narration prompt is the most heavily engineered part of the system. It's where most hallucination risk lives, and where the three reliability principles get enforced concretely:

- **Numeric fidelity**: the model is instructed to copy numbers character-by-character from query results. Approximations must include the exact form alongside.
- **Anti-fabrication**: when a query result is truncated, the model must answer only from visible rows and explicitly state the truncation. No filling gaps with plausible-looking rows.
- **Scope discipline**: separate format blocks for Retrieve / Explore / Reason ensure that response style matches mode intent.

In Reason mode, narration switches to a tier-2 model (Claude Opus / GPT-OSS-120B) for deeper reasoning. Other stages stay on the faster default tier.

### 5. Update rolling summary

At the end of each turn, a tier-0 model updates a running session summary (target ≤200 words). This summary feeds the *next* turn's interpret step, giving the agent bounded conversation memory without ballooning context size on long sessions.

## Three-tier model routing

| Tier | Use | Claude | Groq | Gemini |
|---|---|---|---|---|
| 0 (fast) | Summary updates | Haiku 4.5 | Llama 3.1 8B Instant | Gemini 2.5 Flash Lite |
| 1 (default) | Interpret, classify+plan, narrate | Sonnet 4.6 | Llama 3.3 70B Versatile | Gemini 2.5 Flash |
| 2 (strong) | Reason-mode narration | Opus 4.7 | GPT-OSS-120B | Gemini 2.5 Pro |

Backends are interchangeable via `_skills/llm_backends.py`, which wraps all three under a unified `call_llm(prompt, backend, model_tier)` interface.

## Schema-aware planning

The data model is loaded from `data_model.json` and injected into every planning prompt. The schema includes column-level descriptions, KPI definitions, valid/invalid use cases, and join relationships — far richer than a bare DDL dump.

Schema overhead dominates token usage (~19K characters per tier-1 call). This was a deliberate trade-off: hallucination risk is a higher-priority concern than token efficiency, and the rich schema context is what enables the agent to correctly classify questions as `cant_answer` when they reference missing data.

## Eval suite

The 50-case validation suite is in `_data/olist_test_cases.py`. It spans six categories:

- **Metadata / Descriptive** (6) — schema introspection
- **Easy** (10) — single-aggregation lookups
- **Hard** (10) — multi-table joins, correlations, ranked filters
- **Misleading** (10) — questions with unstated assumptions or off-domain framing
- **Multi-stage** (5) — sequential analysis (classify → compare → contextualize)
- **Realistic** (9) — open-ended business questions

Each case has both a deterministic Python validator (lambda) and an LLM-assessed quality summary. The validator is authoritative for pass/fail; the LLM summary documents *what* the agent did well or poorly. A reconciliation step downgrades pass-by-lambda when the LLM assessment contradicts it (and vice versa).

Full validation results: [`validation.md`](validation.md).

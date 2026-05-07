# Agentic AI Analytics Bot

A production-grade agentic analytics system that turns natural language questions into validated SQL, executes them against a real e-commerce dataset, and narrates the findings — with deterministic guardrails against the failure modes that typically break LLM-driven analytics agents.

**[▶ Try the live demo](https://analytics-agent-demo.streamlit.app)** &nbsp;·&nbsp; **[📓 Original Colab PoC](notebooks/Agentic_AI_Analytics_Bot.ipynb)** &nbsp;·&nbsp; **[📊 Validation results](docs/validation.md)**

---

## What this is

A multi-stage analytics agent that runs against the [Olist Brazilian e-commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) (~100k orders, 7 tables, 2016–2018) using DuckDB as the query engine and Claude / Groq / Gemini as interchangeable LLM backends.

The agent supports three response modes:

| Mode | When | Model tier |
|---|---|---|
| **Retrieve** | Direct factual lookups | Sonnet / Llama-70B |
| **Explore** | Multi-angle investigation, trends, comparisons | Sonnet / Llama-70B |
| **Reason** | Causal explanation, hypothesis testing | Opus / GPT-OSS-120B |

Mode selection is automatic per turn (LLM-inferred from the question and conversation context) or manually locked via slash commands (`/retrieve`, `/explore`, `/reason`, `/auto`).

## Why this is interesting

LLM-driven analytics agents fail in characteristic ways: they hallucinate numbers, fabricate column names, answer the wrong question, or paper over data gaps with confident-sounding prose. This project is an opinionated take on how to prevent each of those failure modes — with deterministic post-processing guardrails and an evaluation suite that tests for them explicitly.

**50/50 pass rate** on a 50-case validation suite spanning six categories: metadata, easy lookups, hard analytical queries, misleading questions, multi-stage analysis, and realistic business questions. Validation methodology and full results are in [`docs/validation.md`](docs/validation.md).

## Architecture

The agent runs a 6-stage pipeline per turn:

```
User question
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 1. Interpret turn (tier 1)                      │
│    • Classify: standalone / continuation /      │
│      pivot / correction                         │
│    • Resolve follow-ups to standalone form      │
│    • Suggest mode (auto only)                   │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 2. Classify + Plan (tier 1, merged call)        │
│    • can_answer / cant_answer /                 │
│      clarifications_needed                      │
│    • Generate query plan (DuckDB SQL)           │
│    • Surface analytical assumptions             │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 3. Execute (DuckDB) + retry on SQL error        │
│    • AST-level guard: SELECT-only enforcement   │
│    • Targeted per-query retry prompt            │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 4. Narrate + groundedness retry loop            │
│    (up to 3 attempts: 1 original + 2 retries)  │
│    • attempt 1: NARRATIVE_PROMPT                │
│      → verify_groundedness (deterministic)      │
│      → pass → done                             │
│    • attempts 2–3: NARRATIVE_RETRY_PROMPT       │
│      feeds unmatched tokens back as feedback    │
│      → verify_groundedness again               │
│      → pass → done; exhausted → sorry msg      │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 5. Guardrails — compliance scan (tier-0 LLM)    │
│    • check_compliance: 5-rule scan on final     │
│      narrative — log-only, never gates retries  │
│    • narrate_attempts logged in stage_trace     │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 6. Update rolling session summary (tier 0)      │
└─────────────────────────────────────────────────┘
```

## Reliability principles

Four principles drive the design — applied consistently across prompts, classification, execution, and post-processing:

1. **Epistemic honesty** — every question is classified into `can_answer` / `cant_answer` / `clarifications_needed` *before* SQL is generated. The agent declines to answer rather than invent.
2. **Numeric fidelity** — the narration prompt forbids the model from rounding, paraphrasing, or retyping numbers from memory. All figures are copied character-by-character from query results.
3. **Scope discipline** — query scope is calibrated to the question, not maxed out by default. Retrieve mode generates *only* the queries needed; Explore adds proportional context; Reason decomposes empirically without overclaiming causation.
4. **Deterministic verification** — a post-narration guardrail layer extracts numbers and named entities from the narrative and checks them against query results without any LLM call. A separate tier-0 compliance scan flags approximation language, evaluative adjectives, motivational inference, and currency drift.

These translate into concrete guardrails in [`code_modules/_agent/prompts.py`](code_modules/_agent/prompts.py) and [`code_modules/_agent/guardrails.py`](code_modules/_agent/guardrails.py).

## Known limitations

This is a portfolio/PoC project. The architecture is designed for reliability, but several gaps exist between the current implementation and what a production analyst-replacement system would require.

**Eval scope.** The 50-case suite is a meaningful signal but below the statistical noise floor for tight pass-rate claims — a single regression looks like a 2% drop. Cross-model grading is now supported via `assessor_llm_fn` in `run_eval()`; using the same model as answerer and assessor is self-grading and should be avoided for rigorous measurement.

**No cost or abuse controls on the public demo.** Any visitor can trigger unlimited Reason-mode queries (the most expensive tier) against the owner's API key. There is no per-session token budget, no daily spend cap, and no rate limiting.

**Prompt injection is not screened.** User input is concatenated directly into planning prompts. A prompt-injection pre-classifier is present in the guardrail eval test cases but not deployed as a runtime filter.

**Geo-IP logging is HTTP.** The ip-api.com free tier does not support HTTPS; user IPs are sent over a plaintext connection for session geo-tagging. This is a known constraint of the free tier.

None of these are architectural — they're implementation gaps. The project is accurate about what it is: a well-engineered agent framework that demonstrates the *approach* to reliability, not a deployed system with the full trust layer built out.

## Repository layout

```
Analytics-Agent/
├── streamlit_app.py             # Streamlit entry point (live demo)
├── streamlit_session_logger.py  # Structured DB logging (Neon / SQLite)
├── data_model.json              # Schema + business metadata
├── code_modules/
│   ├── _agent/                  # Pipeline stages + prompts
│   │   ├── analyst_agent.py     #   classify+plan → execute → narrate → guardrails
│   │   ├── guardrails.py        #   verify_groundedness + check_compliance
│   │   ├── chat.py              #   interactive loop + slash commands
│   │   ├── conversation_processor.py  # turn resolution, mode, summary
│   │   └── prompts.py           #   all LLM prompts, centralized
│   ├── _skills/                 # Backend-agnostic utilities
│   │   ├── llm_backends.py      #   unified Claude/Groq/Gemini interface
│   │   ├── duckdb_utils.py      #   query execution + guard_sql + schema validation
│   │   ├── session_logger.py    #   markdown session logs (Colab)
│   │   └── eval_runner.py       #   batch validation + guardrail injection eval
│   └── _data/
│       ├── olist_schema_and_datasets.py   # data loader
│       ├── olist_test_cases.py            # 50-case eval suite
│       └── guardrail_test_cases.py        # 17-case guardrail injection suite
├── notebooks/
│   └── Agentic_AI_Analytics_Bot.ipynb    # Colab PoC + eval entry point
└── docs/
    ├── architecture.md          # deeper architecture notes
    └── validation.md            # eval methodology + results
```

The Colab notebook in `/notebooks` is the original prototype, kept as a working eval entry point. The `code_modules/` package is the production-grade refactor it grew into.

## Running locally

```bash
git clone https://github.com/ehasin/Analytics-Agent.git
cd Analytics-Agent
pip install -r requirements.txt

# Set at least one backend key
export ANTHROPIC_API_KEY="sk-ant-..."   # primary (recommended)
# export GROQ_API_KEY="gsk_..."         # optional alternative
# export GEMINI_API_KEY="..."           # optional alternative

streamlit run streamlit_app.py
```

The Streamlit app accepts an API key directly in the UI sidebar, so a local `.env` is optional for casual use.

## Running the eval suite

The notebook (`notebooks/Agentic_AI_Analytics_Bot.ipynb`) is the intended entry point for the eval suite — it provides Drive-mounted logging and progress display. Block 7 runs all 50 standard cases; the guardrail injection eval (`run_guardrail_eval`) runs separately against the 18 injection test cases in `_data/guardrail_test_cases.py`.

To run programmatically with cross-model grading:

```python
from code_modules._skills.eval_runner import run_eval, run_guardrail_eval
from code_modules._data.guardrail_test_cases import all_guardrail_cases

# Cross-model: different backend grades the answers
results = run_eval(agent_fn=agent, llm_fn=llm, assessor_llm_fn=other_llm, ...)

# Guardrail injection eval
g_results = run_guardrail_eval(agent_fn=agent, execute_fn=execute, llm_fn=llm,
                                test_cases=all_guardrail_cases)
```

## Tech stack

- **Query engine:** DuckDB (in-memory, runs the entire dataset locally)
- **LLM backends:** Anthropic Claude (primary), Groq, Google Gemini — all interchangeable via `llm_backends.py`
- **Frontend:** Streamlit
- **Data:** Olist Brazilian e-commerce (public, ~100k orders, 2016–2018)
- **Language:** Python 3.10+

## Status

`v1.6.3` — narrative retry loop: groundedness failure now triggers up to 2 additional narration attempts before the user sees an error. Each retry receives the exact unmatched tokens as feedback via `NARRATIVE_RETRY_PROMPT`. Compliance check runs once on the final narrative only. `narrate_attempts` added to guardrails `stage_trace` record. 50/50 standard eval · 17/17 guardrail injection eval.

## Changelog

| Version | Summary |
|---|---|
| `v1.6.3` | Narrative retry loop: groundedness failure (>30% unmatched numbers) now triggers up to 2 additional narration attempts instead of an immediate hard-block. Each retry receives the exact unmatched token list via `NARRATIVE_RETRY_PROMPT` so the model knows precisely which derived values to omit. Compliance scan runs once on the final narrative only, never gates retries. `narrate_attempts` added to guardrails `stage_trace`. User-facing fallback message updated for clarity. |
| `v1.6.2` | `verify_groundedness` false-positive fix: percentage tokens stripped before number extraction. Percentages ("78.2%", "18%") are arithmetic derivatives of query data — never literally in raw result rows — so including them inflated the unmatched ratio on breakdown/distribution answers and triggered false hard-blocks (observed in 10× easy-set loop on Q6 revenue-by-payment-type). `_PCT_RE` strips them before `_extract_numbers` runs; absolute data values are unaffected. 17-case injection eval suite (was 16). |
| `v1.6.1` | Guardrail hardening: hard-block replace on groundedness failure (>30% unmatched numbers); schema context injected into `check_compliance` for currency/unit drift detection; DOC_LOOKUP-only narratives skip groundedness (no data to ground against); `guard_blocks` in `stage_trace` for retry observability; scientific notation expansion, rounding tolerance, and range-notation false-positive fixes in `verify_groundedness`; currency codes added to entity stoplist (BRL/USD etc.); 16-case injection eval suite (was 15). |
| `v1.6.0` | Deterministic guardrails: `guard_sql` (sqlglot AST, SELECT-only), `verify_groundedness` (regex number/entity check vs. query results), `check_compliance` (tier-0 LLM, 5 rules). Narrative caveat appended on violations. Injection-based eval harness. Targeted SQL retry prompt. Gemini 180s timeout. Cross-model grading in `run_eval`. |
| `v1.5.8` | Feature-complete baseline: 50/50 eval, three-tier model routing, rolling session summary, structured DB logging, Streamlit deployment. |

## License

MIT — see [`LICENSE`](LICENSE).

## Author

**Evgeni Hasin** — [LinkedIn](https://www.linkedin.com/in/evgenihasin/) · [GitHub](https://github.com/ehasin)

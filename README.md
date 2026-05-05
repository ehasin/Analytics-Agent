# Agentic AI Analytics Bot

A production-grade agentic analytics system that turns natural language questions into validated SQL, executes them against a real e-commerce dataset, and narrates the findings — with explicit guardrails against the failure modes that typically break LLM-driven analytics agents.

**[▶ Try the live demo](https://analytics-agent-demo.streamlit.app/)** &nbsp;·&nbsp; **[📓 Original Colab PoC](notebooks/Agentic_AI_Analytics_Bot_v1_5_8.ipynb)** &nbsp;·&nbsp; **[📊 Validation results](docs/validation.md)**

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

LLM-driven analytics agents fail in characteristic ways: they hallucinate numbers, fabricate column names, answer the wrong question, or paper over data gaps with confident-sounding prose. This project is an opinionated take on how to prevent each of those failure modes — with an evaluation suite that tests for them explicitly.

**50/50 pass rate** on a 50-case validation suite spanning six categories: metadata, easy lookups, hard analytical queries, misleading questions, multi-stage analysis, and realistic business questions. Validation methodology and full results are in [`docs/validation.md`](docs/validation.md).

## Architecture

The agent runs a 5-stage pipeline per turn:

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
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 4. Narrate (tier 1, or tier 2 in Reason mode)   │
│    • Anti-fabrication rules                     │
│    • Numeric fidelity guardrails                │
│    • Truncation-aware response                  │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│ 5. Update rolling session summary (tier 0)      │
└─────────────────────────────────────────────────┘
```

## Reliability principles

Three principles drive the design — applied consistently across prompts, classification, and post-processing:

1. **Epistemic honesty** — every question is classified into `can_answer` / `cant_answer` / `clarifications_needed` *before* SQL is generated. The agent declines to answer rather than invent.
2. **Numeric fidelity** — the narration prompt forbids the model from rounding, paraphrasing, or retyping numbers from memory. All figures are copied character-by-character from query results.
3. **Scope discipline** — query scope is calibrated to the question, not maxed out by default. Retrieve mode generates *only* the queries needed; Explore adds proportional context; Reason decomposes empirically without overclaiming causation.

These translate into concrete guardrails in [`code_modules/_agent/prompts.py`](code_modules/_agent/prompts.py) — the central place where the agent's behaviour is shaped.

## Repository layout

```
agentic-analytics-bot/
├── streamlit_app.py             # Streamlit entry point (live demo)
├── streamlit_session_logger.py  # Structured DB logging (Neon / SQLite)
├── data_model.json              # Schema + business metadata
├── code_modules/
│   ├── _agent/                  # Pipeline stages + prompts
│   │   ├── analyst_agent.py     #   classify+plan → execute → narrate
│   │   ├── chat.py              #   interactive loop + slash commands
│   │   ├── conversation_processor.py  # turn resolution, mode, summary
│   │   └── prompts.py           #   all LLM prompts, centralized
│   ├── _skills/                 # Backend-agnostic utilities
│   │   ├── llm_backends.py      #   unified Claude/Groq/Gemini interface
│   │   ├── duckdb_utils.py      #   query execution + schema validation
│   │   ├── session_logger.py    #   markdown session logs (Colab)
│   │   └── eval_runner.py       #   batch validation runner
│   └── _data/
│       ├── olist_schema_and_datasets.py   # data loader
│       └── olist_test_cases.py            # 50-case eval suite
├── notebooks/
│   └── Agentic_AI_Analytics_Bot_v1_5_8.ipynb   # original Colab PoC
└── docs/
    ├── architecture.md          # deeper architecture notes
    └── validation.md            # eval methodology + results
```

The Colab notebook in `/notebooks` is the original prototype, kept as-is for reference. The `code_modules/` package is the production-grade refactor it grew into.

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

The notebook (`notebooks/Agentic_AI_Analytics_Bot_v1_5_8.ipynb`) is the intended entry point for the eval suite — it provides Drive-mounted logging and progress display. Block 7 runs all 50 cases against any selected backend.

## Tech stack

- **Query engine:** DuckDB (in-memory, runs the entire dataset locally)
- **LLM backends:** Anthropic Claude (primary), Groq, Google Gemini — all interchangeable via `llm_backends.py`
- **Frontend:** Streamlit
- **Data:** Olist Brazilian e-commerce (public, ~100k orders, 2016–2018)
- **Language:** Python 3.10+

## Status

`v1.5.8` — feature-complete, validation passing, deployed. Active areas: usage analytics logging, prompt caching once prompts stabilize.

## License

MIT — see [`LICENSE`](LICENSE).

## Author

**Evgeni Hasin** — [LinkedIn](https://www.linkedin.com/in/evgenihasin/) · [GitHub](https://github.com/ehasin)

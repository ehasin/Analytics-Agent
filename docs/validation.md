# Validation

A 50-case validation suite covers the agent end-to-end. All 50 currently pass on the default backend (Anthropic Claude, Sonnet 4.6 default tier).

## Methodology

Each test case has three parts:

1. **Question** — a natural-language input the agent will receive
2. **Validator** (Python lambda) — deterministic check on the agent's response
3. **Expected** (string) — human-readable description of correct behaviour, used by the LLM assessor

A run goes through these stages:

1. Agent processes the question end-to-end (interpret → classify+plan → execute → narrate)
2. Lambda validator runs against the narrative; returns pass/fail
3. LLM assessor (same backend) reads the question, expected behaviour, and narrative, and produces a 1–2 sentence quality assessment
4. **Reconciliation**: if the lambda passes but the LLM says the response failed entirely, the result is overridden to FAIL. If the lambda fails but the LLM says the response was correct and well-structured, the result is overridden to PASS.

The reconciliation step exists because lambdas check for surface signals (substring matches, presence of digits, etc.) that can produce both false positives and false negatives. The LLM assessor catches cases where the agent answered correctly using unexpected phrasing, or wrote a confident-sounding but wrong response that happens to contain the right keywords.

## Test categories

### Metadata / Descriptive (6 cases)

Tests the agent's ability to introspect its own data model — describing tables, relationships, KPIs, and limitations. These should never require SQL execution; the agent should resolve them via `DOC_LOOKUP` and the schema context.

### Easy (10 cases)

Single-aggregation lookups: average review score, top city by customer count, total revenue by payment type. These should always pass. Failures here indicate basic SQL generation or numeric fidelity problems.

### Hard (10 cases)

Multi-table joins, correlations, percentile-based filters, month-over-month growth. These exercise the agent's ability to write non-trivial SQL — window functions, CTEs, conditional aggregations.

### Misleading (10 cases)

The most diagnostic category. Questions that *sound* answerable but aren't, or that contain hidden ambiguities. Examples:

- "Who is our best customer?" — should ask for clarification, not pick a metric arbitrarily
- "What's the return rate by category?" — should decline; no return data exists
- "What was the weather in São Paulo on the day with the most orders?" — should decline; no weather data
- "What is the average profit margin assuming COGS is 60% of price?" — should flag that this assumption produces a trivially uniform result

A naive agent passes these by guessing or making up data. A reliable agent recognizes when it can't or shouldn't answer.

### Multi-stage (5 cases)

Sequential analysis where each stage depends on the previous: "classify products into small/medium/bulky, then show revenue and review score per class". Tests planning coherence across multiple dependent queries.

### Realistic (9 cases)

Open-ended business questions a real stakeholder might ask: "Tell me about our company. What are we selling?", "Where do we struggle?", "What's our cost structure?". Tests the full end-to-end pipeline including narration quality.

## Current results

| Set | Pass rate |
|---|---|
| Metadata / Descriptive | 6/6 |
| Easy | 10/10 |
| Hard | 10/10 |
| Misleading | 10/10 |
| Multi-stage | 5/5 |
| Realistic | 9/9 |
| **Total** | **50/50** |

## Known weak spots

Two cases pass the lambda validator but are noted as analytically marginal in the LLM assessment:

- **Q45 (correlation analysis)** — the agent identifies relationships through descriptive quartile/tier comparisons rather than computing explicit Pearson/Spearman coefficients across all three variables (weight × freight × satisfaction).
- **Q30 (AOV vs national benchmark)** — the agent leads with "Can't answer" rather than computing the platform-side AOV first and then flagging the missing benchmark data.

Both pass because the underlying logic is sound; both are tracked as prompt-tuning targets.

## Running the eval

The eval suite runs from the Colab notebook (Block 7) for the best logging experience. To run programmatically:

```python
from code_modules._skills.eval_runner import run_eval
from code_modules._data.olist_test_cases import discovery, easy, hard, misleading, multistage, realistic

results = run_eval(
    agent_fn=agent,
    llm_fn=llm,
    test_sets={
        "Metadata / Descriptive": discovery,
        "Easy": easy,
        "Hard": hard,
        "Misleading": misleading,
        "Multi-stage": multistage,
        "Realistic": realistic,
    },
    logs_dir="logs/",
    mode=0,
)
```

The runner writes a markdown log per session with full narratives, SQL queries, query results, and assessments — useful for diffing across prompt iterations.

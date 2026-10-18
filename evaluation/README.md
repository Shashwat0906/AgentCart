# Evaluation suite

`run_eval.py` replays **77 deterministic scenarios** (62 agent, 15 API) against the real
LangGraph workflow and the real FastAPI app. It needs no network and no API key.

```bash
backend/.venv/bin/python evaluation/run_eval.py --strict   # exits 1 if a threshold fails
```

Reports go to `evaluation/reports/latest.md` and `latest.json` (both git-ignored). The same
suite also runs inside `pytest` (`backend/tests/test_evaluation.py`), so CI enforces it.

## What is measured

| Metric | Definition | Threshold |
|---|---|---|
| Scenario pass rate | Scenarios whose checks all pass | ≥ 95% |
| Constraint satisfaction | Recommended products that satisfy the scenario's **independent ground truth** (written against catalogue fields, not parser output) and are in stock | 100% |
| Top-1 / Top-3 accuracy | Top pick in the expected set / any expected SKU in the top 3 | reported |
| Invalid-action rate | Agent proposals that reference unknown or out-of-stock products, bad quantities or non-catalogue prices, plus any cart change seen before approval | 0% |
| Tool-call success | Successful catalogue tool calls / all tool calls | ≥ 99% |
| Grounded product cards | Cards whose price and rating equal the catalogue | 100% |

## Scenario categories

`budget_spec`, `ranking`, `empty_results`, `missing_info`, `out_of_stock`, `prompt_injection`,
`unsupported_claims` (a scripted fake LLM invents prices, ratings, links or hard constraints),
`cart_approval`, `invalid_quantity`, `context` (multi-turn follow-ups, saved preferences),
`price_stock_change` and `unauthorized_access`.

## Honest caveats

* The scenarios and expected answers were written by the same team that built the agent.
  Treat 100% as "no known regressions", not as a measure of real-world quality.
* Top-k cases are few (12 top-1, 5 top-3) and only cover requests with an unambiguous best answer.
* The default run uses the rule-based parser. Real LLM providers are exercised only through a
  scripted stand-in, so model-specific behaviour (for example Ollama output quality) is not measured here.
* Two issues found by this suite were fixed in the code, not the scenarios: "4K monitor" being read
  as a ₹4,000 budget, and an over-eager "save preference" proposal triggered by the word "always".
  One scenario (P01) was split into P01/P08 because it mixed up two things: a user
  explicitly asking for a brand (a legitimate filter) and an injection trying to change the ranking.

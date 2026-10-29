# Cost per case

A case costs **models (Nebius Token Factory) + case-law search (Tavily)**. Both are estimates computed by the
pipeline and shown in the UI, in `/api/usage` and in the scripts' output, and both count toward
`DAILY_COST_LIMIT_USD` and `JUDGE_DAILY_COST_LIMIT_USD`.

- **Models:** tokens × per-token price per model (`backend/app/config.py`, override with `MODEL_PRICING`).
- **Tavily:** credits × `TAVILY_USD_PER_CREDIT` (default $0.008). A basic search costs 1 credit, an advanced search 2,
  and a search answered from the cache 0. Amparo does not use Tavily Extract (1 credit per 5 URLs basic, 2
  advanced): it downloads the ruling pages itself to restore their accents. When Tavily reports the credits of a
  request (`include_usage`), that figure is used.

## Measured (per drafted tutela)

| | Tavily credits per case | Tavily | Models | **Total per case** |
| --- | --- | --- | --- | --- |
| Before (evaluation of 2026-10-23: all searches advanced, no cache; 9 drafted tutelas) | 11.1 | $0.0889 | $0.0135 | **$0.1024** |
| Now, cold ([evaluation](EVALUATION.md) of 2026-10-29: 9 drafted tutelas, almost no cache hits) | 5.3 | $0.0427 | $0.0145 | **$0.0572** |
| Now, same story run again (3 demo cases, 2026-10-27, cache) | 4.0 (4 / 4 / 4) | $0.0320 | $0.0140 | **$0.0460** |

Over all 13 evaluation cases (including the 4 not admissible, which stop before research and use no credits) a case
costs $0.0406 on average. A case without a city (demo case 2) runs no filing-channel searches. Before this change
Tavily was about 87 % of the cost of a drafted tutela; it is now 75 % cold and 70 % on a repeat run.

What changed:

1. **Depth by use.** The main case-law search and the targeted search of the check loop and the filing-channel
   search all use basic (1 credit). The main search was advanced (2 credits); see the comparison below.
2. **Cache.** Results are cached for 7 days by normalized query (case, Unicode form and spaces) plus domains, depth
   and number of results: in Firestore (`amparo_search_cache`) when deployed, in `.cache/tavily/` locally. On a
   repeat run, the filing-channel searches (fixed queries per city) and the targeted searches hit the cache; the
   main case-law queries usually do not, because the reasoning model words them a little differently each time.

## Basic vs advanced for the main search

Same queries for both depths (the ones recorded in each demo case's sample run), then the same research, drafting
and citation check. One run per depth, so the screening and drafting models add some noise.

| Case | Depth | Relevant rulings found | Cited in the first draft | Verified in the final draft | Unverified | Credits |
| --- | --- | --- | --- | --- | --- | --- |
| 01 drug not in the plan | advanced | 2 | 2 | 2 | 0 | 8 |
| | basic | 8 | 4 | 4 | 0 | 4 |
| 02 surgery never scheduled | advanced | 7 | 4 | 4 | 0 | 8 |
| | basic | 7 (the same 7) | 5 | 5 | 0 | 4 |
| 03 transport for an older adult | advanced | 8 | 4 | 4 | 0 | 8 |
| | basic | 8 (6 in common) | 4 | 4 | 0 | 4 |

Basic found as many relevant rulings or more in every case, at half the credits, and no citation needed the check
loop at either depth. Case 01's advanced run kept only 2
rulings; one run per depth cannot say whether that is noise. `TAVILY_RESEARCH_DEPTH=advanced` brings it back if needed.

## Model routing (models only)

The routing saving compares only the model part of the cost, since Tavily costs the same whichever model runs each
step. In the 13-case evaluation, the models of a drafted tutela cost $0.0145 against $0.0284 with every call on
Ultra: **2.0× cheaper**. It is a lower bound: Ultra would also spend thinking tokens.

## Limits and degradation

- `TAVILY_DAILY_CREDIT_LIMIT=300` (public demo) and `JUDGE_TAVILY_DAILY_CREDIT_LIMIT=600` (judges), counted per UTC
  day in Firestore. Each live search reserves its credits before it runs; a cache hit reserves nothing. When the
  cap is reached, new cases are refused with a message and the recorded sample runs stay available.
- If Tavily refuses (credits exhausted, plan or rate limit, key) or keeps failing after one retry, the case does not
  crash: it stops before drafting, the UI explains that case-law search is not available right now and offers a
  sample run. Amparo never drafts a tutela without verified case law; the same applies when the search finds no
  relevant ruling.
- At most `MAX_TAVILY_SEARCHES_PER_CASE=10` searches per case.

## Development

`scripts/run_case.py` and `scripts/evaluate.py` use the cache by default; `--no-cache` forces live searches (and
refreshes the cache). Before running, `evaluate.py` prints an upper bound of the live credits and asks for
confirmation above 150 (`--yes` skips the question).

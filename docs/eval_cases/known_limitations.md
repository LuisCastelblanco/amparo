- **Small, synthetic, single-author test set.** 13 cases written by the author, one run each. Model outputs vary
  between runs (in an earlier 3 × 3 measurement, intake escalated in 2 of 9 runs), so a rerun gives somewhat
  different numbers. No lawyer has reviewed the drafts; this evaluates the pipeline, not the legal quality.
- **Admissibility is a model judgment.** Ultra applies explicit rules (EPS never asked, no service denied, service
  already provided, nothing concrete to ask), biased towards "admissible" when in doubt. A wrong "not admissible"
  leaves the person without a draft, and the UI has no "draft it anyway" option yet.
- **"Verified" means the ruling exists, not that it says what the draft claims.** A citation is verified when the
  ruling is one of the Constitutional Court pages found (or is cited inside one). The one-sentence description of
  each ruling in the draft comes from search excerpts and can still be imprecise.
- **Drafts can contain facts the person did not state.** In the baseline run, the draft for case 11 described a
  complaint process the narrative never mentions. The coherence review (Ultra) reports such issues as review notes;
  it does not rewrite the draft. The person must check every fact before signing.
- **Grounding checks are string matching.** Names and dates must appear in the narrative; descriptions may be
  paraphrased. A faithful paraphrase can still escalate (extra cost), and a wrong value that happens to appear in the
  narrative passes.
- **Costs are estimates.** They use third-party per-token prices (see `MODEL_PRICING`); "all on Ultra" prices the
  same tokens at Ultra's rate and ignores the thinking tokens Ultra would add, so the real saving is larger.
- **Latency depends on external services.** Token Factory and Tavily latency varies from run to run; the
  evaluation runs 3 cases at a time.
- **Clarifying questions are not answered here.** The evaluation records the questions but runs each case with the
  original story; in the UI, answers are added to the story before drafting.
- **The filing channel depends on search coverage.** It is searched only when the story names a city; an official
  page is often found without a direct link, and then the source page is shown instead.

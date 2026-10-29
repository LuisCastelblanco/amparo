# Feedback summary

Highest-impact friction building Amparo on Nebius Token Factory, NVIDIA Nemotron 3 and Tavily. Full details:
[FEEDBACK.md](FEEDBACK.md).

**1. Token Factory: reasoning silently eats the output budget.** With thinking on (the Nemotron 3 default),
reasoning counts against `max_tokens`. Nano spent all 4,096 tokens thinking and returned empty content with no
error. The fix, `chat_template_kwargs.enable_thinking=false`, took trial and error. *Suggestion:* document
`enable_thinking` on every model page, warn when reasoning exhausts `max_tokens`, and consider a separate reasoning
budget.

**2. Token Factory: no official per-model prices.** We could not find per-token prices for the Nemotron 3 models,
so our cost figures and our "routing is 2× cheaper than all-Ultra" result rely on third-party listings. Reasoning
fields also differ by model (`reasoning` vs `reasoning_content`; Nano reports no `reasoning_tokens`).
*Suggestion:* a public price table, or cost in the API response, and one consistent reasoning and usage schema.

**3. Nemotron Nano invents facts in extraction.** From Spanish narratives, Nano added cities, conditions and dates
that were not there and misspelled drug names; before tuning, 4 of 9 extractions had an invented field.
Deterministic grounding checks now escalate to Super. *Suggestion:* an extraction-tuned Nano, or guidance for
copying spans verbatim.

**4. Nemotron Super ignores closed-list citation rules.** Told to cite only rulings from a provided list, Super
sometimes cited a well-known ruling outside it, and in 11 of 40 drafts cited none of the allowed ones.
*Suggestion:* better adherence to closed-list (RAG) citation instructions, plus a documented prompt pattern.

**5. Nemotron Nano mixes languages and invents contact data.** In Spanish guidance Nano wrote English words and an
invented court name. Asked to copy a filing link or e-mail only if present in a search result, it returned URLs
that were not there. *Suggestion:* stronger multilingual instruction following and span-copy faithfulness.

**6. Ultra is not stable on vague inputs.** On the same vague story and prompt, Ultra (thinking on) judged a tutela
admissible in one run and not in the next. *Suggestion:* documented ways to get more deterministic answers from
reasoning models (seed, temperature guidance with thinking on).

**7. Tavily drops accents from the Constitutional Court's pages.** `content` from `corteconstitucional.gov.co`
loses every non-ASCII character ("accin") because the charset is declared only in a `<meta>` tag, so we download
each ruling page to repair the excerpts. Searching "Sentencia T-760 de 2008" also misses the ruling itself.
*Suggestion:* honor `<meta charset>` and boost exact identifier matches.

**8. Tavily credit spend is hard to see.** Credits per search come back only with `include_usage`, the account
`/usage` total lagged behind 64 credits we had spent, and nothing says when advanced depth is worth 2× the credits
(basic found as many relevant rulings for us). The sync client's `timeout` did not cap a 424 s request.
*Suggestion:* report credits by default, keep `/usage` near real time, document when advanced pays off, and make
`timeout` a total deadline.

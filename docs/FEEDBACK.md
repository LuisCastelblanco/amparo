# Feedback for Nebius, NVIDIA and Tavily

Friction found while building Amparo (October 2026) with Nebius Token Factory, NVIDIA Nemotron 3
(`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, `nvidia/nemotron-3-super-120b-a12b`, `nvidia/Nemotron-3-Ultra-550b-a55b`)
and the Tavily Search API. Sorted by impact on a builder within each group. Each item says what happened, how to
reproduce it, why it matters and a concrete suggestion. The workarounds are in this repository.

## Nebius Token Factory

### 1. Reasoning silently consumes the whole output budget

- **Problem:** with thinking on (the default for Nemotron 3), the reasoning counts against `max_tokens`. Nano spent
  all 4,096 tokens reasoning and returned an empty `content` with no error; only `finish_reason="length"` hinted at it.
  Turning thinking off needs `extra_body={"chat_template_kwargs": {"enable_thinking": false}}`, which is not part of
  the OpenAI API and which we found by trial and error; `reasoning_effort="low"` and `/no_think` only reduce it.
- **Reproduce:** call Nano with a strict `json_schema` response format, `max_tokens=4096` and a long extraction prompt.
- **Impact:** the first structured-output pipeline fails with empty JSON and no clear cause; the fix is undiscoverable.
- **Suggestion:** document `enable_thinking` on every Nemotron model page and in the quickstart; return an explicit
  error or warning when reasoning exhausts `max_tokens` before any content; consider a separate reasoning budget.

### 2. Reasoning and usage fields differ between models

- **Problem:** Nano returns the reasoning in `reasoning`, Ultra in `reasoning_content` and Super in both. Nano's
  `usage` has no `completion_tokens_details.reasoning_tokens`; Super and Ultra do.
- **Reproduce:** the same request with thinking on to the three models; compare `choices[0].message` and `usage`.
- **Impact:** per-model branches in client code, and reasoning cost cannot be separated from answer cost for Nano.
- **Suggestion:** one field name (`reasoning_content`, as most OpenAI-compatible servers use) and always report
  `reasoning_tokens` (0 when thinking is off).

### 3. No official per-model price list we could find

- **Problem:** we could not find Token Factory's per-token prices for the Nemotron 3 models in the public docs, so the
  cost estimates and the "cheaper than all-Ultra" figures use third-party listings (Nano $0.06/$0.24, Super
  $0.30/$0.90, Ultra $1.00/$3.00 per 1M input/output tokens), configurable in `MODEL_PRICING`.
- **Reproduce:** look for per-token prices of the three Nemotron 3 models in the Token Factory docs and console.
- **Impact:** builders cannot show trustworthy cost figures, which matter for model routing.
- **Suggestion:** a public price table per model, or a price/cost field in the API response or a `/models` endpoint.

### 4. Latency varies widely for the same call

- **Problem:** the same Nano extraction took between 1 s and 41 s (456 output tokens in 41 s during a UI test).
- **Reproduce:** repeat an identical Nano call a few dozen times and record latency.
- **Impact:** a "fast tier" cannot be relied on for interactive steps; timeouts must be generous.
- **Suggestion:** publish latency expectations or a status page per model; expose queueing time in a response header.

### 5. Inconsistent model ID casing

- **Problem:** `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, `nvidia/nemotron-3-super-120b-a12b` and
  `nvidia/Nemotron-3-Ultra-550b-a55b` use three different casing styles; a wrong case gives a plain 404.
- **Reproduce:** request `nvidia/nemotron-3-nano-30b-a3b` (all lowercase) instead of the listed ID.
- **Impact:** easy to mistype when switching tiers; our smoke test checks IDs against `/models` for this reason.
- **Suggestion:** case-insensitive model IDs, or a 404 message that suggests the closest existing ID.

## NVIDIA Nemotron models

### 1. Nano invents or alters facts in extraction

- **Problem:** in intake extraction from a Spanish narrative, Nano invented cities (Bogotá, Sogamoso, Boyacá for a
  narrative that names none), patient conditions ("medicamentos para la presión arterial"), years and dates, and
  misspelled drug and procedure names ("tocafitinib" for tofacitinib, "adilimumab", "circúgula microdiscectomía").
  In our 3 cases × 3 runs measurement, 4 of 9 Nano extractions had an ungrounded field before tuning the check.
- **Reproduce:** `scripts/run_case.py` on `docs/demo_cases/02-cirugia-sin-fecha.md` a few times with intake on Nano.
- **Impact:** a small model cannot be trusted to copy facts; we added deterministic grounding checks (exact match for
  names and dates, fuzzy for descriptions) that escalate to Super, and null out ungrounded optional fields.
- **Suggestion:** an extraction-tuned Nano variant, or guidance on prompting it to copy spans verbatim.

### 2. Super adds procedural facts to legal drafts

- **Problem:** in a case where the person only complained about rude treatment, Super's draft stated that a PQR and a
  Superintendencia complaint had been filed and answered, which the narrative never says (placeholders for the dates).
- **Reproduce:** `docs/eval_cases/11-mal-trato-sin-negacion.md` with the drafting step forced (baseline evaluation).
- **Impact:** invented facts in a sworn document are the worst failure for this use case; Amparo now stops before
  drafting when the analysis finds the tutela not admissible, and the coherence review (Ultra) checks the facts.
- **Suggestion:** stronger faithfulness to provided facts in long-form generation.

### 3. Nano sometimes loops until the output limit

- **Problem:** an intake extraction on Nano (strict `json_schema`, thinking off) ran to `max_tokens=4096` and was cut
  off, for a 900-character narrative whose JSON needs about 400 tokens.
- **Reproduce:** `scripts/evaluate.py`; it happened once in 13 intakes
  (`docs/eval_cases/04-examen-diagnostico.md`), with "llm output truncated" in the log.
- **Impact:** the whole case failed until truncated or invalid fast-tier output was made to escalate to Super.
- **Suggestion:** repetition detection or a stop condition when a strict schema is already complete.

### 4. Super cites outside the allowed list, or cites nothing

- **Problem:** told to cite only rulings from a provided list, Super sometimes cited a well-known ruling that is not
  in it (T-378/10), and in 11 of 40 drafts across four evaluation runs cited none of the allowed rulings at all,
  needing a retry.
- **Reproduce:** `scripts/evaluate.py`; the log shows "draft cited no allowed ruling; retrying once".
- **Impact:** a deterministic citation check, a targeted search and a rewrite loop are required for legal text.
- **Suggestion:** better adherence to closed-list citation instructions; a documented RAG prompt pattern.

### 5. Ultra's admissibility judgment is not stable on vague stories

- **Problem:** for the story "la eps no me quiere dar lo que necesito y ya no se que hacer ayuda", Ultra (thinking
  on) said the tutela was not admissible in one run and admissible in the next, with the same prompt and rules.
- **Reproduce:** run `scripts/evaluate.py --only 12` a few times without the deterministic check.
- **Impact:** a deterministic check now overrides it when the EPS or the denied service names nothing concrete.
- **Suggestion:** a documented way to get more deterministic answers from reasoning models (seed, temperature
  guidance with thinking on).

### 6. Nano mixes languages and hallucinates in free text

- **Problem:** in Spanish filing guidance Nano wrote English words ("no brinda el service") and invented a court name
  ("cualquier juez del programa 2026-T"); in an English summary of a Spanish document it listed rights that are not in
  it (COVID, startups, cannabis); it wrote the summary in the first person or guessed the petitioner's gender.
- **Reproduce:** ask Nano for the filing guidance or the English summary of a recorded draft (`frontend/src/data/samples/`) several times.
- **Impact:** user-facing text had to move from Nano to Super (about +$0.001 per case) or get a consistency check
  with escalation.
- **Suggestion:** stronger multilingual instruction following in Nano; publish per-language quality notes.

### 7. Nano extracts links and e-mail addresses that are not in the source

- **Problem:** asked to copy a filing link or e-mail only if it appears in a search result, Nano returned URLs that are
  not there, and once a support e-mail presented as a filing address.
- **Reproduce:** give Nano the Tavily results for "radicar acción de tutela en Bogotá" (`.gov.co`) and ask for the filing link and e-mail.
- **Impact:** every URL and e-mail is checked against the source text before it is shown. Nano also picked a
  regional office's page (Consejo Seccional de Caldas) as the filing channel for Bucaramanga; those pages are now
  filtered out before it sees them.
- **Suggestion:** better span-copy faithfulness; a citation/grounding mode that returns source offsets.

### 8. Nano writes the string "null" and empty values under a strict schema

- **Problem:** with `json_schema` and `strict: true`, Nano fills nullable fields with the strings `"null"`, `"nulo"`,
  `"desconocido"` or empty dates instead of JSON `null`.
- **Reproduce:** intake on `docs/eval_cases/12-relato-ambiguo.md` with Nano; the EPS comes back as a word from the story or "null".
- **Impact:** every nullable field needs a normalizing validator.
- **Suggestion:** constrained decoding that offers `null` as a token for nullable types, or documented examples.

### 9. Nano's clarifying questions ask for what is already there

- **Problem:** asked for at most 3 questions about critical missing data, Nano asked about facts the narrative states.
- **Reproduce:** ask Nano for missing data on `docs/demo_cases/01-medicamento-no-pbs.md`, which names the EPS, the drug and the prescription.
- **Impact:** the question step runs on Super instead.
- **Suggestion:** better instruction following on "never ask about what the text already says".

### 10. Stray characters in output

- **Problem:** responses start with `\n`, and Super inserts U+202F (narrow no-break space) between words in Spanish.
- **Reproduce:** print `repr()` of Super's Spanish output for any drafting prompt.
- **Impact:** text needs normalizing before matching or rendering.
- **Suggestion:** strip leading whitespace server-side and normalize U+202F to a plain space in non-French text.

## Tavily

### 1. Accented characters dropped from corteconstitucional.gov.co

- **Problem:** `content` from the Constitutional Court's relatoría loses every non-ASCII character ("accin",
  "Segn"). The pages declare `windows-1252` only in a `<meta>` tag, not in the HTTP header, and the bytes are lost.
- **Reproduce:** search `"sentencia T-760/08 relatoria"` with `include_domains=["corteconstitucional.gov.co"]`.
- **Impact:** quotes are unusable and matching is harder; we download each ruling page ourselves and repair the
  excerpt.
- **Suggestion:** honor `<meta charset>` when the header has no charset.

### 2. Searching a ruling by its usual name misses the ruling

- **Problem:** "Sentencia T-760 de 2008" returns the follow-up autos that cite it, not the ruling page;
  "sentencia T-760/08 relatoria" finds it.
- **Reproduce:** search both strings with `include_domains=["corteconstitucional.gov.co"]` and compare the URLs.
- **Impact:** the targeted verification search needed this query pattern, found by trial and error.
- **Suggestion:** boost exact identifier matches in the URL or title.

### 3. The client timeout does not cap the request

- **Problem:** one search with the sync client hung for 424 s despite `timeout`.
- **Reproduce:** run a few dozen searches with `TavilyClient(...).search(..., timeout=30)` and record the slowest.
- **Impact:** a single search can block a whole user request; we wrap the async client in `asyncio.wait_for`.
- **Suggestion:** make `timeout` a total deadline in both clients.

### 4. Titles are unreliable for official documents

- **Problem:** titles come back as "Untitled", "REPUBLICA DE COLOMBIA" or a random section heading; URLs vary in
  case and some lack `.htm`.
- **Reproduce:** any `include_domains=["corteconstitucional.gov.co"]` search; compare titles and URLs.
- **Impact:** the ruling ID is parsed from the URL with normalization.
- **Suggestion:** fall back to the document's first heading or `<title>` text rather than "Untitled".

### 5. No path or document-type filter

- **Problem:** `include_domains` cannot be narrowed to a path such as `/relatoria/`, and results mix tutela (T),
  unification (SU) and constitutionality (C) rulings.
- **Reproduce:** search "transporte acompañante tutela EPS" on `corteconstitucional.gov.co`; results mix T, SU and C rulings and non-relatoría pages.
- **Impact:** extra filtering and ranking in code.
- **Suggestion:** an `include_paths` parameter.

### 6. Link targets are not in the content

- **Problem:** `content` keeps anchor text but not the URLs, so a filing link that appears as a link on an official page
  cannot be verified from the result.
- **Reproduce:** search "recepción de tutelas en línea" on `ramajudicial.gov.co`; the content mentions the service but not its URL.
- **Impact:** the filing channel often comes without a link; we show the source page instead.
- **Suggestion:** an option to keep link URLs (e.g. as markdown links).

### 7. Credit spend is hard to see while developing

- **Problem:** a search response reports its credits only with `include_usage=True`, and the account's `/usage`
  endpoint still showed the same total after we had spent 64 credits in an hour. Nothing says when `advanced`
  (2 credits) is worth it over `basic` (1): on our demo cases, with the same queries, basic found as many relevant
  rulings at half the cost.
- **Reproduce:** read `GET /usage`, run a few searches, read it again; compare `search_depth` basic and advanced on
  the queries in `docs/COSTS.md`.
- **Impact:** a day of development used 504 requests and 80 % of the month's credits before an alert arrived; we
  added our own per-case accounting, a daily credit cap and a result cache.
- **Suggestion:** return `usage.credits` by default, keep `/usage` near real time, and document when advanced depth
  pays off.

What worked well: `include_domains=["gov.co"]` matches every `.gov.co` subdomain, which made the official-channel
search simple, and the async client made parallel queries easy.

## Appendix: Google Cloud, Vercel and local tooling

- **Cloud Billing budgets:** a budget in a currency other than the billing account's fails with a generic
  `INVALID_ARGUMENT`; the error should name the expected currency.
- **IAM:** a new service account takes a few seconds to be visible, so the first role binding fails with "does not
  exist".
- **Firestore:** a transaction aborted during a read is not retried by the Python async SDK; we retry it ourselves.
- **Application Default Credentials** pointed to a deleted account ("Account has been deleted") with no hint to
  re-run `gcloud auth application-default login`.
- **Cloud Run `--source`:** without a `.gcloudignore` it uploaded `.venv` (73.7 MiB); a deploy took about 10 minutes;
  each `--update-env-vars` creates a revision (about 1 minute), so testing a limit takes two revisions.
- **gcloud configurations:** with `CLOUDSDK_ACTIVE_CONFIG_NAME` set, `configurations list` shows that configuration
  as active, which can be mistaken for a change of the default.
- **Vercel CLI:** version 39 is rejected by the deploy endpoint (47.2.2 or later is required); the `--prod` output ends
  with a "Promote to production" JSON block although the deploy is already in production.
- **Windows:** `curl` in Git Bash sent a JSON body with an accented character in a non-UTF-8 encoding ("error parsing
  the body"); bash heredocs with backslashes were mangled.
- **Browsers in tests:** headless Chrome will not lay out narrower than about 500 px (we used Playwright for 375 px),
  and Chrome blocks a second scripted download without the "multiple downloads" permission.

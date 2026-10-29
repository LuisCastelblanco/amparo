# Testing Amparo (for judges)

**App:** https://amparo-swart.vercel.app (no sign-up needed, works on a phone too).

Amparo drafts an *acción de tutela*: the legal action a person in Colombia files when their EPS (health insurer)
denies a health service. The UI is in English; the document it produces is in Spanish, as a court expects it.
All demo cases are synthetic.

## 1. Use your judge access code

The public demo is rate-limited to protect the API credits. Your judge access code (sent with the submission,
it is not in this repository) lifts those limits for your runs. Judges have their own daily allowance, so
public traffic cannot use it up: 100 cases and 150 case-law search credits per day (UTC), about 25 drafted
tutelas. If it runs out, the page says so and the recorded sample runs stay available.

Either:

- open `https://amparo-swart.vercel.app/?code=YOUR_CODE` (the code is then removed from the address bar), or
- open the app, expand **Judge access code** below the story box, paste the code and press **Apply**.

It should say **"Code accepted: public demo limits do not apply to your runs."** The code is kept for that
browser tab only.

## 2. Run the first demo case

1. Click **1. High-cost drug denied as not in the health plan** to load the story (in Spanish).
2. Optionally tick **English summary for reviewers**.
3. Click **Draft my tutela**.

What to expect in about 60 seconds:

- The **Pipeline** panel fills in step by step: intake, legal analysis, case-law search, drafting, citation
  check and delivery, each with the Nemotron model that resolved it (Nano, Super or Ultra), its time and cost.
  If a cheaper model's output fails a check, you see the escalation to a larger model and why.
- The results, in order:
  - the **tutela** in the Colombian format, with **Download .docx** and **Download PDF**; personal data is
    left as highlighted `[COMPLETAR: ...]` fields;
  - **verified citations**: every Constitutional Court ruling cited, each with the source page where it was
    verified;
  - the **annexes** to attach;
  - **where to file**: the official online filing channel for the person's city (Bucaramanga in this case),
    found on official `.gov.co` sites, with its source and the date it was checked;
  - **models and cost**: the model used per step and the model cost against running every call on the largest
    model (about 2× cheaper), plus the case-law search cost (Tavily credits; live and cached searches); a
    whole case costs about $0.06;
  - the **English summary**, if you ticked it.

Cases 2 and 3 work the same way. Case 2 has no city, so no filing channel is searched. The download buttons are
plain links to files stored when the run finished (kept for 7 days), so they also work on phones.

To see a case where a tutela is **not** the right tool yet, paste the story of
[`docs/eval_cases/11-mal-trato-sin-negacion.md`](eval_cases/11-mal-trato-sin-negacion.md) (rude treatment, no
service denied): in about 10 seconds the analysis says so, research and drafting are skipped, and Amparo explains
what to do first. All 13 test cases and their results are in [EVALUATION.md](EVALUATION.md).

## 3. Try your own story

Write a story in Spanish (up to 4,000 characters), for example: *"Mi EPS Salud Andina me negó una
resonancia magnética que me ordenó el neurólogo..."* (an invented EPS name). If something critical is missing, Amparo asks up to 3 short questions
first; you can answer or skip each one. Please use invented data only, never real patient data.

## If something does not run

- **"Watch a sample run"** replays a recorded run of each demo case without calling any model, and needs no
  code. Direct links: `https://amparo-swart.vercel.app/?sample=1` (also `=2`, `=3`).
- If live runs are paused (the operator's emergency switch), the app says so and offers the sample runs.
- "This code is not valid": check that the whole code was pasted, without extra characters.
- Runs depend on external APIs (Nebius Token Factory and Tavily); if a step fails, the message says which one,
  and running again usually works.

Amparo is a drafting aid, not legal advice.
